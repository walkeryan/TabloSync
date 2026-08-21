"""FastAPI application exposing the HDHomeRun-compatible surface Plex expects."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from tablo_api.models import TabloChannel

from . import __version__
from .bridge import BridgeError, TabloBridge, UnknownChannel
from .config import Settings
from .streams import NoTunerAvailable, StreamManager

logger = logging.getLogger(__name__)


class BridgeContract(Protocol):
    ready: bool
    tuner_count: int

    async def initialize(self) -> None: ...

    async def channels(self, *, force: bool = False) -> list[TabloChannel]: ...


class StreamContract(Protocol):
    active_count: int

    async def open(self, identifier: str) -> AsyncIterator[bytes]: ...

    async def close(self) -> None: ...


def _device_id(seed: str) -> str:
    """Return a stable ID satisfying the HDHomeRun device-ID checksum."""
    lookup = (0xA, 0x5, 0xF, 0x6, 0x7, 0xC, 0x1, 0xB, 0x9, 0x2, 0x8, 0xD, 0x4, 0x3, 0xE, 0x0)
    base = (int(hashlib.sha256(seed.encode()).hexdigest()[:7], 16) << 4) & 0xFFFFFFF0
    for final_nibble in range(16):
        candidate = base | final_nibble
        checksum = 0
        for shift in (28, 20, 12, 4):
            checksum ^= lookup[(candidate >> shift) & 0x0F]
            checksum ^= (candidate >> (shift - 4)) & 0x0F
        if checksum == 0:
            return f"{candidate:08X}"
    raise AssertionError("HDHomeRun checksum has no valid final nibble")


def _device_auth(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:8].upper()


def _base_url(request: Request, settings: Settings) -> str:
    return settings.advertise_url or str(request.base_url).rstrip("/")


def create_app(
    settings: Settings,
    bridge: BridgeContract | None = None,
    streams: StreamContract | None = None,
) -> FastAPI:
    real_bridge = bridge or TabloBridge(settings)
    real_streams = streams or StreamManager(real_bridge, settings)  # type: ignore[arg-type]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await real_bridge.initialize()
        try:
            yield
        finally:
            await real_streams.close()

    app = FastAPI(
        title="TabloSync",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(BridgeError)
    async def bridge_error(_: Request, exc: BridgeError) -> JSONResponse:
        if isinstance(exc, UnknownChannel):
            status = 404
        elif isinstance(exc, NoTunerAvailable):
            status = 503
        else:
            status = 502
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    @app.get("/healthz")
    async def health() -> JSONResponse:
        status = 200 if real_bridge.ready else 503
        return JSONResponse(
            status_code=status,
            content={
                "status": "ok" if real_bridge.ready else "starting",
                "channels": len(await real_bridge.channels()) if real_bridge.ready else 0,
                "active_streams": real_streams.active_count,
                "tuner_count": real_bridge.tuner_count,
                "version": __version__,
            },
        )

    async def discover(request: Request) -> dict[str, str | int]:
        base = _base_url(request, settings)
        return {
            "FriendlyName": settings.friendly_name,
            "Manufacturer": "TabloSync",
            "ModelNumber": "HDHR3-US",
            "FirmwareName": "hdhomerun3_atsc",
            "FirmwareVersion": __version__.replace(".", ""),
            "DeviceID": _device_id(settings.device_sid or settings.email),
            "DeviceAuth": _device_auth(f"auth:{settings.device_sid or settings.email}"),
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

    @app.get("/lineup_status.json")
    async def lineup_status() -> dict[str, str | int | list[str]]:
        return {
            "ScanInProgress": 0,
            "ScanPossible": 0,
            "Source": "Antenna",
            "SourceList": ["Antenna"],
        }

    @app.post("/lineup.json")
    async def scan_lineup() -> dict[str, int]:
        await real_bridge.channels(force=True)
        return {"ScanInProgress": 0}

    @app.get("/lineup.json")
    async def lineup(request: Request) -> list[dict[str, str]]:
        base = _base_url(request, settings)
        channels = await real_bridge.channels()
        return [
            {
                "GuideNumber": (
                    f"{channel.major}.{channel.minor}" if channel.major > 0 else channel.call_sign
                ),
                "GuideName": channel.network or channel.call_sign,
                "URL": f"{base}/stream/{quote(channel.identifier, safe='')}",
            }
            for channel in channels
        ]

    @app.head("/stream/{identifier}")
    async def stream_head(identifier: str) -> Response:
        channels = await real_bridge.channels()
        if not any(channel.identifier == identifier for channel in channels):
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

    return app


def load_app() -> FastAPI:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return create_app(Settings.from_env())
