from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from tablosync.fox import ActivationStatus, TunerChannel
from tablosync.tve_app import create_tve_app
from tablosync.tve_config import TVESettings


class FakeTVEBridge:
    ready = False
    authorized = True
    tuner_count = 1
    device_id = "test-fox-device"
    closed = False

    def __init__(self) -> None:
        self._channel = TunerChannel(
            identifier="fox-btn",
            call_sign="BTN",
            major=0,
            minor=0,
            network="Big Ten Network",
            kind="cable",
            guide_number="6100",
        )

    async def initialize(self) -> None:
        self.ready = True

    async def close(self) -> None:
        self.closed = True

    async def channels(self, *, force: bool = False) -> list[TunerChannel]:
        del force
        return [self._channel]

    async def start_activation(self) -> ActivationStatus:
        return await self.activation_status()

    async def activation_status(self, *, poll: bool = False) -> ActivationStatus:
        del poll
        return ActivationStatus(True, "authorized", "Comcast_SSO", "", "", 0)


class FakeTVEStreams:
    active_count = 0
    closed = False

    async def open(self, identifier: str) -> AsyncIterator[bytes]:
        assert identifier == "fox-btn"

        async def body() -> AsyncIterator[bytes]:
            yield b"btn-mpeg-ts"

        return body()

    async def close(self) -> None:
        self.closed = True


def settings() -> TVESettings:
    return TVESettings(advertise_url="http://192.168.1.20:5005")


def test_tve_tuner_discovery_lineup_and_health() -> None:
    bridge = FakeTVEBridge()
    streams = FakeTVEStreams()
    with TestClient(create_tve_app(settings(), bridge, streams)) as client:
        discover = client.get("/discover.json")
        lineup = client.get("/lineup.json")
        lineup_status = client.get("/lineup_status.json")
        health = client.get("/healthz")
    assert discover.status_code == 200
    assert discover.json()["FriendlyName"] == "TabloSync TV Everywhere"
    assert discover.json()["TunerCount"] == 1
    assert discover.json()["LineupURL"] == "http://192.168.1.20:5005/lineup.json"
    assert lineup.json() == [
        {
            "GuideNumber": "6100",
            "GuideName": "Big Ten Network",
            "URL": "http://192.168.1.20:5005/stream/fox-btn",
        }
    ]
    assert lineup_status.json()["Source"] == "Cable"
    assert health.json()["authorized"] is True
    assert bridge.closed
    assert streams.closed


def test_tve_stream_is_mpeg_ts() -> None:
    with TestClient(create_tve_app(settings(), FakeTVEBridge(), FakeTVEStreams())) as client:
        response = client.get("/stream/fox-btn")
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp2t"
    assert response.content == b"btn-mpeg-ts"


def test_auth_page_does_not_request_xfinity_password() -> None:
    with TestClient(create_tve_app(settings(), FakeTVEBridge(), FakeTVEStreams())) as client:
        response = client.get("/auth/fox")
    assert response.status_code == 200
    assert "Xfinity authorization is active" in response.text
    assert 'type="password"' not in response.text


def test_cross_origin_activation_is_rejected() -> None:
    with TestClient(create_tve_app(settings(), FakeTVEBridge(), FakeTVEStreams())) as client:
        response = client.post("/auth/fox/start", headers={"Origin": "https://untrusted.example"})
        assert response.status_code == 403
        response = client.post("/auth/fox/start", follow_redirects=False)
        assert response.status_code == 303
        assert client.get("/auth/fox/status").headers["cache-control"] == "no-store"


def test_activation_ui_can_be_disabled() -> None:
    config = TVESettings(auth_ui=False)
    with TestClient(create_tve_app(config, FakeTVEBridge(), FakeTVEStreams())) as client:
        assert client.get("/auth/fox").status_code == 404
        assert client.get("/auth/fox/status").status_code == 404
        assert client.post("/auth/fox/start").status_code == 404
        assert client.get("/lineup.json").status_code == 200
