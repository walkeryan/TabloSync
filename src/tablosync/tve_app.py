"""HDHomeRun-compatible FastAPI application for TV Everywhere sources."""

from __future__ import annotations

import html
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from . import __version__
from .app import _device_auth, _device_id
from .bridge import BridgeError
from .fox import (
    ActivationStatus,
    FoxAuthenticationRequired,
    FoxBridge,
    FoxProtectedStream,
    TunerChannel,
)
from .streams import NoTunerAvailable, StreamManager
from .tve_config import TVESettings

logger = logging.getLogger(__name__)


class TVEBridgeContract(Protocol):
    ready: bool
    authorized: bool
    tuner_count: int
    device_id: str

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def channels(self, *, force: bool = False) -> list[TunerChannel]: ...

    async def start_activation(self) -> ActivationStatus: ...

    async def activation_status(self, *, poll: bool = False) -> ActivationStatus: ...


class StreamContract(Protocol):
    active_count: int

    async def open(self, identifier: str) -> AsyncIterator[bytes]: ...

    async def close(self) -> None: ...


def _base_url(request: Request, settings: TVESettings) -> str:
    return settings.advertise_url or str(request.base_url).rstrip("/")


def _status_payload(status: ActivationStatus) -> dict[str, str | int | bool]:
    return {
        "authorized": status.authorized,
        "authorization": status.authorization,
        "provider": status.provider,
        "code": status.code,
        "activation_url": status.activation_url,
        "expires": status.expires,
    }


def _auth_page(status: ActivationStatus) -> str:
    if status.authorized:
        detail = "Xfinity authorization is active. BTN can now be tested from Plex."
        action = ""
    elif status.authorization == "pending":
        url = html.escape(status.activation_url, quote=True)
        code = html.escape(status.code)
        detail = (
            f'Open <a href="{url}" target="_blank" rel="noopener">{url}</a> and enter '
            f'<strong class="code">{code}</strong>. This page checks automatically.'
        )
        action = ""
    else:
        detail = "Authorize FOX Sports with the Xfinity subscription that includes BTN."
        action = (
            '<form method="post" action="/auth/fox/start"><button>Start activation</button></form>'
        )

    refresh = '<meta http-equiv="refresh" content="5">' if not status.authorized else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  {refresh}
  <title>TabloSync FOX authorization</title>
  <style>
    body {{ font: 18px/1.5 system-ui, sans-serif; max-width: 680px; margin: 10vh auto;
      padding: 0 24px; color: #172033; background: #f5f7fb; }}
    main {{ background: white; padding: 32px; border-radius: 16px;
      box-shadow: 0 12px 36px #17203318; }}
    h1 {{ margin-top: 0; }} .code {{ display: inline-block; letter-spacing: .14em;
      font-size: 1.35em; padding: 4px 9px; background: #eef2ff; border-radius: 6px; }}
    button {{ font: inherit; padding: 10px 16px; border: 0; border-radius: 8px;
      color: white; background: #1d4ed8; cursor: pointer; }}
    small {{ color: #667085; }}
  </style>
</head>
<body><main><h1>Big Ten Network</h1><p>{detail}</p>{action}
<small>Your Xfinity password is entered only on Xfinity's site and is never sent to
TabloSync.</small>
</main></body></html>"""


def create_tve_app(
    settings: TVESettings,
    bridge: TVEBridgeContract | None = None,
    streams: StreamContract | None = None,
) -> FastAPI:
    real_bridge = bridge or FoxBridge(settings)
    real_streams = streams or StreamManager(real_bridge, settings)  # type: ignore[arg-type]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            await real_bridge.initialize()
            yield
        finally:
            await real_streams.close()
            await real_bridge.close()

    app = FastAPI(
        title="TabloSync TV Everywhere",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(BridgeError)
    async def bridge_error(_: Request, exc: BridgeError) -> JSONResponse:
        if isinstance(exc, (FoxAuthenticationRequired, NoTunerAvailable)):
            status = 503
        elif isinstance(exc, FoxProtectedStream):
            status = 422
        else:
            status = 502
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    async def discover(request: Request) -> dict[str, str | int]:
        base = _base_url(request, settings)
        seed = f"fox-btn:{real_bridge.device_id}"
        return {
            "FriendlyName": settings.friendly_name,
            "Manufacturer": "TabloSync",
            "ModelNumber": "HDHR3-CC",
            "FirmwareName": "hdhomerun3_cablecard",
            "FirmwareVersion": __version__.replace(".", ""),
            "DeviceID": _device_id(seed),
            "DeviceAuth": _device_auth(f"auth:{seed}"),
            "BaseURL": base,
            "LineupURL": f"{base}/lineup.json",
            "TunerCount": real_bridge.tuner_count,
        }

    @app.get("/")
    async def root(request: Request) -> dict[str, str | int]:
        return await discover(request)

    @app.get("/discover.json")
    async def discover_json(request: Request) -> dict[str, str | int]:
        return await discover(request)

    @app.get("/healthz")
    async def health() -> JSONResponse:
        status = await real_bridge.activation_status()
        return JSONResponse(
            status_code=200 if real_bridge.ready else 503,
            content={
                "status": "ok" if real_bridge.ready else "starting",
                "channels": 1 if real_bridge.ready else 0,
                "active_streams": real_streams.active_count,
                "tuner_count": real_bridge.tuner_count,
                "authorized": status.authorized,
                "authorization": status.authorization,
                "version": __version__,
            },
        )

    @app.get("/lineup_status.json")
    async def lineup_status() -> dict[str, str | int | list[str]]:
        return {
            "ScanInProgress": 0,
            "ScanPossible": 0,
            "Source": "Cable",
            "SourceList": ["Cable"],
        }

    @app.post("/lineup.json")
    async def scan_lineup() -> dict[str, int]:
        return {"ScanInProgress": 0}

    @app.get("/lineup.json")
    async def lineup(request: Request) -> list[dict[str, str]]:
        base = _base_url(request, settings)
        return [
            {
                "GuideNumber": channel.guide_number,
                "GuideName": channel.network,
                "URL": f"{base}/stream/{quote(channel.identifier, safe='')}",
            }
            for channel in await real_bridge.channels()
        ]

    @app.head("/stream/{identifier}")
    async def stream_head(identifier: str) -> Response:
        if not any(channel.identifier == identifier for channel in await real_bridge.channels()):
            raise HTTPException(status_code=404, detail="Unknown channel")
        return Response(media_type="video/mp2t", headers={"Cache-Control": "no-store"})

    @app.get("/stream/{identifier}")
    async def stream(identifier: str) -> StreamingResponse:
        body = await real_streams.open(identifier)
        return StreamingResponse(
            body,
            media_type="video/mp2t",
            headers={
                "Cache-Control": "no-store",
                "Connection": "close",
                "X-Content-Type-Options": "nosniff",
            },
        )

    if settings.auth_ui:

        @app.get("/auth/fox", response_class=HTMLResponse)
        async def fox_auth() -> HTMLResponse:
            status = await real_bridge.activation_status(poll=True)
            return HTMLResponse(_auth_page(status), headers={"Cache-Control": "no-store"})

        @app.post("/auth/fox/start")
        async def fox_auth_start(request: Request) -> RedirectResponse:
            origin = request.headers.get("origin")
            allowed = {str(request.base_url).rstrip("/"), _base_url(request, settings)}
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin is not None and origin not in allowed
            ):
                raise HTTPException(
                    status_code=403, detail="Cross-origin activation is not allowed"
                )
            await real_bridge.start_activation()
            return RedirectResponse("/auth/fox", status_code=303)

        @app.get("/auth/fox/status")
        async def fox_auth_status() -> JSONResponse:
            return JSONResponse(
                _status_payload(await real_bridge.activation_status(poll=True)),
                headers={"Cache-Control": "no-store"},
            )

    return app


def load_tve_app() -> FastAPI:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # FOX's token-refresh API includes the refresh token in its URL path.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return create_tve_app(TVESettings.from_env())
