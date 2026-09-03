import asyncio
import base64
import json
import stat
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tablosync.fox import (
    FOX_PREVIEW_ID,
    FoxAuthenticationRequired,
    FoxClient,
    FoxError,
    FoxProtectedStream,
)


def auth_payload(*, provider: str = "", device_id: str = "device-1") -> dict[str, object]:
    expires = int((time.time() + 3600) * 1000)
    return {
        "deviceId": device_id,
        "accessToken": f"access-{provider or 'anonymous'}",
        "refreshToken": f"refresh-{provider or 'anonymous'}",
        "tokenExpiration": expires,
        "mvpd": provider,
        "authn_expire": expires,
    }


def saved_state(*, provider: str) -> dict[str, object]:
    payload = auth_payload(provider=provider)
    return {
        "device_id": payload["deviceId"],
        "access_token": payload["accessToken"],
        "refresh_token": payload["refreshToken"],
        "token_expiration": payload["tokenExpiration"],
        "mvpd": payload["mvpd"],
        "authn_expire": payload["authn_expire"],
    }


def test_xfinity_device_activation_is_persisted(tmp_path) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/account/login/v3":
            return httpx.Response(200, json=auth_payload(), request=request)
        if request.url.path == "/v2.0/adoberegcode":
            assert request.url.params["first_screen"] == "true"
            assert request.url.params["mvpd_id"] == "Comcast_SSO"
            return httpx.Response(
                200,
                json={
                    "authenticateURL": "https://api.auth.adobe.com/api/v2/authenticate/fbc-fox/ABC123",
                    "expires": int((time.time() + 600) * 1000),
                },
                request=request,
            )
        if request.url.path.startswith("/adobeauthn/v3/checkauthn/"):
            assert request.url.params["adobeauth"] == "true"
            return httpx.Response(200, json=auth_payload(provider="Comcast_SSO"), request=request)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def exercise() -> None:
        state_file = tmp_path / "fox-auth.json"
        client = FoxClient(state_file, transport=httpx.MockTransport(handler))
        await client.initialize()
        pending = await client.start_activation()
        assert pending.authorization == "pending"
        assert pending.code == ""
        assert pending.activation_url.startswith("https://api.auth.adobe.com/")
        assert (await client.start_activation()).activation_url == pending.activation_url
        complete = await client.activation_status(poll=True)
        assert complete.authorized
        assert complete.authorization == "authorized"
        assert complete.provider == "Comcast_SSO"
        saved = json.loads(state_file.read_text())
        assert saved["mvpd"] == "Comcast_SSO"
        assert saved["pending_code"] == ""
        assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
        await client.close()

    asyncio.run(exercise())
    assert requests == [
        "/account/login/v3",
        "/v2.0/adoberegcode",
        "/adobeauthn/v3/checkauthn/refresh-anonymous",
    ]


def playback_handler(manifest: str, observed: dict[str, object]):
    now = datetime.now(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api-sps.foxsports.com":
            return httpx.Response(
                200,
                json={"data": {"status": {"success": True}}},
                headers={"x-platform-location": "encoded-location"},
                request=request,
            )
        if "curated/v1/sporting" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "listings": {
                            "items": [
                                {
                                    "entity_id": "BTN-program-123",
                                    "call_sign": "BTN",
                                    "start_time": (now - timedelta(minutes=5)).isoformat(),
                                    "end_time": (now + timedelta(minutes=55)).isoformat(),
                                }
                            ]
                        }
                    }
                },
                request=request,
            )
        if request.url.path == "/sports/v3.0/watchlive":
            observed.update(json.loads(request.content))
            assert request.headers["x-platform-location"] == "encoded-location"
            return httpx.Response(
                200,
                json={"stream": {"playbackUrl": "https://video.example/btn/index.m3u8"}},
                request=request,
            )
        if request.url.host == "video.example":
            return httpx.Response(200, text=manifest, request=request)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    return handler


def test_btn_playback_uses_current_listing_and_clear_hls(tmp_path) -> None:
    state_file = tmp_path / "fox-auth.json"
    state_file.write_text(json.dumps(saved_state(provider="Comcast_SSO")))
    observed: dict[str, object] = {}
    handler = playback_handler('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n', observed)

    async def exercise() -> None:
        client = FoxClient(state_file, transport=httpx.MockTransport(handler))
        await client.initialize()
        assert await client.playback_url() == "https://video.example/btn/index.m3u8"
        await client.close()

    asyncio.run(exercise())
    assert observed["asset"] == {"id": "BTN-program-123"}


def test_drm_manifest_is_refused(tmp_path) -> None:
    state_file = tmp_path / "fox-auth.json"
    state_file.write_text(json.dumps(saved_state(provider="Comcast_SSO")))
    handler = playback_handler('#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://license"\n', {})

    async def exercise() -> None:
        client = FoxClient(state_file, transport=httpx.MockTransport(handler))
        await client.initialize()
        with pytest.raises(FoxProtectedStream, match="DRM-protected"):
            await client.playback_url()
        await client.close()

    asyncio.run(exercise())


def test_preview_pass_is_not_treated_as_xfinity_authorization(tmp_path) -> None:
    state_file = tmp_path / "fox-auth.json"
    state_file.write_text(json.dumps(saved_state(provider=FOX_PREVIEW_ID)))

    async def exercise() -> None:
        client = FoxClient(state_file, transport=httpx.MockTransport(lambda _: None))
        await client.initialize()
        assert not client.authorized
        with pytest.raises(FoxAuthenticationRequired):
            await client.playback_url()
        await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("provider", ["Comcast_SSO", ""])
def test_oauth_refresh_normalizes_tokens_and_provider_claims(tmp_path, provider) -> None:
    state_file = tmp_path / "fox-auth.json"
    state = saved_state(provider="Comcast_SSO")
    state["token_expiration"] = 1
    state_file.write_text(json.dumps(state))
    expires = int(time.time()) + 86400
    claims = {"mvpdid": provider, "authn_expire": expires}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/identityhydra/oauth2/token"
        assert b"grant_type=refresh_token" in request.content
        return httpx.Response(
            200,
            json={
                "access_token": f"header.{encoded}.signature",
                "refresh_token": "renewed-refresh",
                "expires_at": expires,
            },
        )

    async def exercise() -> None:
        client = FoxClient(state_file, transport=httpx.MockTransport(handler))
        await client.initialize()
        assert client.authorized is bool(provider)
        await client.close()

    asyncio.run(exercise())
    saved = json.loads(state_file.read_text())
    assert saved["token_expiration"] == expires * 1000
    assert saved["refresh_token"] == "renewed-refresh"
    assert saved["mvpd"] == provider


def test_pending_activation_404_and_network_errors_do_not_leak_tokens(tmp_path) -> None:
    state_file = tmp_path / "fox-auth.json"
    state = saved_state(provider="")
    state.update(
        activation_url="https://api.auth.adobe.com/api/v2/authenticate/fbc-fox/CODE123",
        pending_expires=int((time.time() + 600) * 1000),
    )
    state_file.write_text(json.dumps(state))
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert "checkauthn" in request.url.path
        if calls == 1:
            return httpx.Response(404)
        raise httpx.ConnectError(f"Failure on {request.url}", request=request)

    async def exercise() -> None:
        client = FoxClient(state_file, transport=httpx.MockTransport(handler))
        await client.initialize()
        status = await client.activation_status(poll=True)
        assert status.authorization == "pending"
        # Repeated UI/status requests do not flood FOX.
        await client.activation_status(poll=True)
        assert calls == 1
        client._last_poll = 0
        with pytest.raises(FoxError, match="network request failed") as caught:
            await client.activation_status(poll=True)
        assert "refresh-anonymous" not in str(caught.value)
        assert caught.value.__suppress_context__
        await client.close()

    asyncio.run(exercise())


def test_drm_in_child_manifest_is_refused(tmp_path) -> None:
    state_file = tmp_path / "fox-auth.json"
    state_file.write_text(json.dumps(saved_state(provider="Comcast_SSO")))
    default = playback_handler("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nvideo.m3u8\n", {})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/btn/video.m3u8":
            return httpx.Response(200, text='#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"\n')
        return default(request)

    async def exercise() -> None:
        client = FoxClient(state_file, transport=httpx.MockTransport(handler))
        await client.initialize()
        with pytest.raises(FoxProtectedStream):
            await client.playback_url()
        await client.close()

    asyncio.run(exercise())


def test_invalid_saved_auth_state_is_rejected(tmp_path) -> None:
    state_file = tmp_path / "fox-auth.json"
    state = saved_state(provider="Comcast_SSO")
    state["token_expiration"] = "bad-value"
    state_file.write_text(json.dumps(state))

    async def exercise() -> None:
        client = FoxClient(state_file, transport=httpx.MockTransport(lambda _: None))
        with pytest.raises(FoxError, match="Could not read"):
            await client.initialize()
        await client.close()

    asyncio.run(exercise())


def test_legacy_second_screen_code_is_not_reused(tmp_path) -> None:
    state_file = tmp_path / "fox-auth.json"
    state = saved_state(provider="")
    state.update(
        pending_code="OLDCODE",
        activation_url="https://activate.fox.com/activate",
        pending_expires=int((time.time() + 600) * 1000),
    )
    state_file.write_text(json.dumps(state))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2.0/adoberegcode"
        return httpx.Response(
            200,
            json={
                "authenticateURL": "https://api.auth.adobe.com/api/v2/authenticate/fbc-fox/NEWLINK",
                "expires": int((time.time() + 600) * 1000),
            },
        )

    async def exercise() -> None:
        client = FoxClient(state_file, transport=httpx.MockTransport(handler))
        await client.initialize()
        assert (await client.activation_status()).authorization == "required"
        status = await client.start_activation()
        assert status.code == ""
        assert status.activation_url.endswith("/NEWLINK")
        await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "url",
    [
        "https://activate.fox.com/activate",
        "http://api.auth.adobe.com/api/v2/authenticate/fbc-fox/CODE",
        "https://api.auth.adobe.com.evil.example/api/v2/authenticate/fbc-fox/CODE",
        "https://evil.example/api/v2/authenticate/fbc-fox/CODE",
        "javascript:alert(1)",
    ],
)
def test_unexpected_provider_login_urls_are_rejected(url) -> None:
    assert not FoxClient._is_provider_login_url(url)
