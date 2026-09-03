"""FOX Sports TV Everywhere authentication and BTN playback access."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .bridge import BridgeError
from .tve_config import TVESettings

logger = logging.getLogger(__name__)

# These identify FOX's public web client and are present in its browser application.
FOX_API_KEY = "cf289e299efdfa39fb6316f259d1de93"
FOX_CLIENT_ID = "b8704183-b1b6-4a43-a243-8a187d6c62a1"
FOX_REQUESTOR = "fbc-fox"
FOX_XFINITY_ID = "Comcast_SSO"
FOX_PREVIEW_ID = "TempPass_fbcfox_5min"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class FoxError(BridgeError):
    """Raised when FOX cannot satisfy an authentication or playback request."""


class FoxAuthenticationRequired(FoxError):
    """Raised when Xfinity authorization is missing or expired."""


class FoxProtectedStream(FoxError):
    """Raised when FOX returns a stream this bridge cannot consume."""


@dataclass(slots=True)
class FoxAuthState:
    device_id: str
    access_token: str
    refresh_token: str
    token_expiration: int
    mvpd: str = ""
    authn_expire: int = 0
    pending_code: str = ""
    activation_url: str = ""
    pending_expires: int = 0

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> FoxAuthState:
        if not isinstance(value, dict):
            raise ValueError("Invalid FOX state")
        allowed = {field.name for field in fields(cls)}
        state = cls(**{key: item for key, item in value.items() if key in allowed})
        for field in fields(cls):
            item = getattr(state, field.name)
            expected = int if field.name.endswith(("expiration", "expire", "expires")) else str
            if type(item) is not expected:
                raise ValueError("Invalid FOX state field")
        if not all((state.device_id, state.access_token, state.refresh_token)):
            raise ValueError("Incomplete FOX state")
        return state


@dataclass(frozen=True, slots=True)
class ActivationStatus:
    authorized: bool
    authorization: str
    provider: str
    code: str
    activation_url: str
    expires: int


@dataclass(frozen=True, slots=True)
class TunerChannel:
    identifier: str
    call_sign: str
    major: int
    minor: int
    network: str
    kind: str
    guide_number: str


@dataclass(frozen=True, slots=True)
class SourceStream:
    playlist_url: str


class FoxClient:
    """Minimal client for FOX's public TV Everywhere device flow and player API."""

    def __init__(
        self,
        state_file: Path,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.state_file = state_file
        self._state: FoxAuthState | None = None
        self._lock = asyncio.Lock()
        self._last_poll = 0.0
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
            transport=transport,
            headers={"User-Agent": USER_AGENT},
        )

    @property
    def ready(self) -> bool:
        return self._state is not None

    @property
    def authorized(self) -> bool:
        state = self._state
        if state is None:
            return False
        provider_is_valid = bool(state.mvpd) and not state.mvpd.lower().startswith("temppass")
        return provider_is_valid and state.authn_expire > self._now_ms() + 30_000

    async def initialize(self) -> None:
        async with self._lock:
            self._state = self._load_state()
            if self._state is None:
                self._state = await self._anonymous_login()
                self._save_state()
            elif self._state.token_expiration <= self._now_ms() + 120_000:
                await self._refresh_locked()

    async def close(self) -> None:
        await self._http.aclose()

    async def activation_status(self, *, poll: bool = False) -> ActivationStatus:
        async with self._lock:
            self._require_state()
            if poll and not self.authorized and self._pending_is_current():
                await self._poll_activation_locked()
            return self._activation_status_locked()

    async def start_activation(self) -> ActivationStatus:
        async with self._lock:
            state = self._require_state()
            if self.authorized or self._pending_is_current():
                return self._activation_status_locked()
            await self._refresh_if_needed_locked()

            params = {
                "device_id": state.device_id,
                "device_type": "web",
                "app_version": "0.2.0",
                "mvpd_id": FOX_XFINITY_ID,
                "first_screen": "true",
                "redirect_url": ("https://www.foxsports.com/provider/register?mvpd_id=Comcast_SSO"),
                "requestor": FOX_REQUESTOR,
                "model": "TabloSync",
                "osName": "Linux",
            }
            response = await self._request(
                "GET",
                "https://api3.fox.com/v2.0/adoberegcode",
                params=params,
                headers=self._identity_headers(),
            )
            payload = self._json_response(response, "FOX could not start device authorization")
            # The legacy second-screen URL now redirects to FOX One, whose codes are
            # not these Adobe provider codes. Use the current web client's direct flow.
            state.pending_code = ""
            state.activation_url = str(payload.get("authenticateURL", ""))
            if not self._is_provider_login_url(state.activation_url):
                state.activation_url = ""
                raise FoxError("FOX returned an unexpected provider sign-in site")
            state.pending_expires = int(payload.get("expires", 0))
            if state.pending_expires <= self._now_ms():
                raise FoxError("FOX returned an expired provider sign-in link")
            self._save_state()
            return self._activation_status_locked()

    async def playback_url(self) -> str:
        async with self._lock:
            if not self.authorized:
                raise FoxAuthenticationRequired(
                    "Xfinity authorization is required; open the FOX activation page"
                )
            await self._refresh_if_needed_locked()
            if not self.authorized:
                raise FoxAuthenticationRequired("FOX provider authorization has expired")
            state = self._require_state()

            location_response = await self._request(
                "GET",
                "https://api-sps.foxsports.com/locator/v1/location",
                headers={"x-api-key": FOX_API_KEY, "Accept": "application/json"},
            )
            self._json_response(location_response, "FOX could not determine playback region")
            platform_location = location_response.headers.get("x-platform-location")
            if not platform_location:
                raise FoxError("FOX did not return a playback region")

            listing = await self._current_btn_listing_locked()
            request = {
                "asset": {"id": listing["entity_id"]},
                "stream": {"type": "live"},
                "device": {
                    "capabilities": ["drm/widevine"],
                    "model": "TabloSync TVE",
                    "width": 1920,
                    "height": 1080,
                    "os": "Linux",
                },
                "ad": {
                    "did": state.device_id,
                    "customParams": {"xid": str(uuid.uuid4())},
                    "capabilities": ["ssai"],
                },
                "debug": {"traceId": None},
                "privacy": {"us": "1YYN", "lat": True},
            }
            response = await self._request(
                "POST",
                "https://prod.api.digitalvideoplatform.com/sports/v3.0/watchlive",
                json=request,
                headers={
                    "x-api-key": FOX_API_KEY,
                    "x-access-token": f"Bearer {state.access_token}",
                    "x-platform-location": platform_location,
                    "x-device-capabilities": "drm/widevine",
                    "Referer": "https://www.foxsports.com/",
                },
            )
            payload = self._json_response(response, "FOX rejected BTN playback")
            playback_url = payload.get("stream", {}).get("playbackUrl")
            if not isinstance(playback_url, str) or not playback_url.startswith("https://"):
                raise FoxError("FOX did not return a BTN playback URL")
            if ".m3u8" not in playback_url:
                raise FoxProtectedStream("FOX returned a non-HLS protected BTN stream")

            await self._check_manifest(playback_url, seen=set())
            return playback_url

    async def _check_manifest(self, url: str, *, seen: set[str], depth: int = 0) -> None:
        if url in seen:
            return
        if depth > 3 or len(seen) >= 32 or urlsplit(url).scheme != "https":
            raise FoxError("FOX returned an unsupported BTN manifest structure")
        seen.add(url)
        manifest = await self._request(
            "GET", url, headers={"Referer": "https://www.foxsports.com/"}
        )
        if manifest.status_code >= 400 or not manifest.text.lstrip().startswith("#EXTM3U"):
            raise FoxError("FOX returned an unreadable BTN HLS manifest")
        children: list[str] = []
        next_is_variant = False
        for line in manifest.text.splitlines():
            line = line.strip()
            if line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
                if not re.search(r"METHOD=(?:NONE|AES-128)(?:,|$)", line):
                    raise FoxProtectedStream("FOX returned DRM-protected BTN video")
                key_format = re.search(r'KEYFORMAT="([^"]+)"', line)
                if key_format and key_format[1] != "identity":
                    raise FoxProtectedStream("FOX returned DRM-protected BTN video")
            if line.startswith("#EXT-X-MEDIA:"):
                uri = re.search(r'URI="([^"]+)"', line)
                if uri:
                    children.append(urljoin(str(manifest.url), uri[1]))
            elif line.startswith("#EXT-X-STREAM-INF:"):
                next_is_variant = True
            elif line and not line.startswith("#") and next_is_variant:
                children.append(urljoin(str(manifest.url), line))
                next_is_variant = False
        for child in children:
            await self._check_manifest(child, seen=seen, depth=depth + 1)

    async def _current_btn_listing_locked(self) -> dict[str, Any]:
        now = int(time.time())
        response = await self._request(
            "GET",
            "https://api.fox.com/fs/product/curated/v1/sporting/keystone/detail/by_filters",
            params={
                "callsign": "BTN,BTN-DIGITAL",
                "start_date": now - 60,
                "end_date": now + 300,
                "size": 10,
                "video_type": "listing",
            },
            headers={"x-fox-apikey": FOX_API_KEY},
        )
        payload = self._json_response(response, "FOX could not load the BTN schedule")
        listings = payload.get("data", {}).get("listings", {}).get("items") or []
        candidates = [item for item in listings if item.get("call_sign") == "BTN"]
        if not candidates:
            raise FoxError("FOX returned no current BTN listing")

        current_time = datetime.now(UTC)
        for item in candidates:
            try:
                start = datetime.fromisoformat(str(item["start_time"]).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(item["end_time"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if start <= current_time < end:
                return item
        raise FoxError("FOX returned no currently airing BTN program")

    async def _anonymous_login(self) -> FoxAuthState:
        device_id = str(uuid.uuid4())
        response = await self._request(
            "POST",
            "https://id.fox.com/account/login/v3",
            json={"deviceId": device_id, "clientId": FOX_CLIENT_ID},
            headers={"x-api-key": FOX_API_KEY},
        )
        payload = self._json_response(response, "FOX anonymous device registration failed")
        return self._state_from_payload(payload, device_id=device_id)

    async def _poll_activation_locked(self) -> None:
        if time.monotonic() - self._last_poll < 5:
            return
        self._last_poll = time.monotonic()
        state = self._require_state()
        await self._refresh_if_needed_locked()
        response = await self._request(
            "GET",
            "https://id.fox.com/adobeauthn/v3/checkauthn/" + quote(state.refresh_token, safe=""),
            params={
                "device_id": state.device_id,
                "client_id": FOX_CLIENT_ID,
                "adobeauth": "true",
            },
            headers={
                **self._identity_headers(),
                "X-Delegated-Auth-Flow": "true",
            },
        )
        if response.status_code in {404, 409}:
            return
        payload = self._json_response(response, "FOX could not check device authorization")
        self._update_state_from_payload(payload)
        if self.authorized:
            state.pending_code = ""
            state.activation_url = ""
            state.pending_expires = 0
            logger.info("FOX TV provider authorization completed for %s", state.mvpd)
        self._save_state()

    async def _refresh_if_needed_locked(self) -> None:
        state = self._require_state()
        if state.token_expiration <= self._now_ms() + 120_000:
            await self._refresh_locked()

    async def _refresh_locked(self) -> None:
        state = self._require_state()
        response = await self._request(
            "POST",
            "https://id.fox.com/identityhydra/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": state.refresh_token,
                "scope": "openid offline",
                "client_id": FOX_CLIENT_ID,
            },
            headers={
                "authorization": f"Bearer {state.access_token}",
                "x-api-key": FOX_API_KEY,
                "content-type": "application/x-www-form-urlencoded",
            },
        )
        payload = self._json_response(response, "FOX authentication refresh failed")
        self._update_state_from_payload(payload)
        self._save_state()

    def _identity_headers(self) -> dict[str, str]:
        state = self._require_state()
        return {
            "authorization": f"Bearer {state.access_token}",
            "x-refresh-token": state.refresh_token,
            "x-api-key": FOX_API_KEY,
            "content-type": "application/json",
        }

    def _update_state_from_payload(self, payload: dict[str, Any]) -> None:
        state = self._require_state()
        updated = self._state_from_payload(payload, device_id=state.device_id)
        state.access_token = updated.access_token
        state.refresh_token = updated.refresh_token
        state.token_expiration = updated.token_expiration
        state.mvpd = updated.mvpd
        state.authn_expire = updated.authn_expire

    @staticmethod
    def _state_from_payload(payload: dict[str, Any], *, device_id: str) -> FoxAuthState:
        access_token = str(payload.get("accessToken") or payload.get("access_token") or "")
        refresh_token = str(payload.get("refreshToken") or payload.get("refresh_token") or "")
        token_expiration = int(payload.get("tokenExpiration", 0))
        if not token_expiration and payload.get("expires_at"):
            token_expiration = int(payload["expires_at"]) * 1000
        if not token_expiration and payload.get("expires_in"):
            token_expiration = int((time.time() + int(payload["expires_in"])) * 1000)
        if not access_token or not refresh_token or token_expiration <= 0:
            raise FoxError("FOX returned incomplete authentication data")
        claims: dict[str, Any] = {}
        try:
            encoded = access_token.split(".")[1]
            decoded = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if isinstance(decoded, dict):
                claims = decoded
        except (IndexError, ValueError, TypeError):
            pass
        authn_expire = int(payload.get("authn_expire") or claims.get("authn_expire") or 0)
        if authn_expire and authn_expire < 1_000_000_000_000:
            authn_expire *= 1000
        return FoxAuthState(
            device_id=str(payload.get("deviceId") or device_id),
            access_token=access_token,
            refresh_token=refresh_token,
            token_expiration=token_expiration,
            mvpd=str(payload.get("mvpd") or claims.get("mvpdid") or ""),
            authn_expire=authn_expire,
        )

    def _activation_status_locked(self) -> ActivationStatus:
        state = self._require_state()
        if self.authorized:
            authorization = "authorized"
        elif self._pending_is_current():
            authorization = "pending"
        else:
            authorization = "required"
        return ActivationStatus(
            authorized=self.authorized,
            authorization=authorization,
            provider=state.mvpd,
            code=state.pending_code if self._pending_is_current() else "",
            activation_url=state.activation_url if self._pending_is_current() else "",
            expires=state.pending_expires if self._pending_is_current() else 0,
        )

    def _pending_is_current(self) -> bool:
        state = self._require_state()
        return (
            self._is_provider_login_url(state.activation_url)
            and state.pending_expires > self._now_ms()
        )

    @staticmethod
    def _is_provider_login_url(url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (
                parsed.scheme == "https"
                and parsed.netloc == "api.auth.adobe.com"
                and parsed.path.startswith(f"/api/v2/authenticate/{FOX_REQUESTOR}/")
            )
        except ValueError:
            return False

    def _load_state(self) -> FoxAuthState | None:
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            return FoxAuthState.from_json(payload)
        except FileNotFoundError:
            return None
        except (OSError, TypeError, ValueError):
            raise FoxError("Could not read the FOX authentication state") from None

    def _save_state(self) -> None:
        state = self._require_state()
        temporary: Path | None = None
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            descriptor, filename = tempfile.mkstemp(dir=self.state_file.parent, prefix=".fox-auth-")
            temporary = Path(filename)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(asdict(state), output)
            temporary.replace(self.state_file)
        except OSError:
            raise FoxError("Could not save the FOX authentication state") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http.request(method, url, **kwargs)
        except httpx.HTTPError:
            # FOX embeds credentials in some URL paths. Never propagate those exceptions.
            raise FoxError("FOX network request failed; please retry later") from None

    def _require_state(self) -> FoxAuthState:
        if self._state is None:
            raise FoxError("FOX client is not initialized")
        return self._state

    @staticmethod
    def _json_response(response: httpx.Response, message: str) -> dict[str, Any]:
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise FoxError(f"{message} (HTTP {response.status_code})") from None
        if not isinstance(payload, dict):
            raise FoxError(message)
        return payload

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)


class FoxBridge:
    """Expose Big Ten Network as one virtual cable channel."""

    def __init__(self, settings: TVESettings, client: FoxClient | None = None) -> None:
        self.settings = settings
        self.client = client or FoxClient(settings.state_file)
        self._channel = TunerChannel(
            identifier="fox-btn",
            call_sign="BTN",
            major=0,
            minor=0,
            network="Big Ten Network",
            kind="cable",
            guide_number=settings.btn_guide_number,
        )

    @property
    def ready(self) -> bool:
        return self.client.ready

    @property
    def authorized(self) -> bool:
        return self.client.authorized

    @property
    def tuner_count(self) -> int:
        return self.settings.tuner_count

    @property
    def device_id(self) -> str:
        return self.client._require_state().device_id

    async def initialize(self) -> None:
        await self.client.initialize()

    async def close(self) -> None:
        await self.client.close()

    async def channels(self, *, force: bool = False) -> list[TunerChannel]:
        del force
        return [self._channel]

    async def watch(self, identifier: str) -> SourceStream:
        if identifier != self._channel.identifier:
            raise FoxError("Unknown channel")
        return SourceStream(await self.client.playback_url())

    async def start_activation(self) -> ActivationStatus:
        return await self.client.start_activation()

    async def activation_status(self, *, poll: bool = False) -> ActivationStatus:
        return await self.client.activation_status(poll=poll)
