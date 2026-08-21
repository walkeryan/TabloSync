from collections.abc import AsyncIterator

from fastapi.testclient import TestClient
from tablo_api.models import TabloChannel

from tablosync.app import _device_id, create_app
from tablosync.config import Settings


class FakeBridge:
    ready = False
    tuner_count = 2

    def __init__(self) -> None:
        self.force_refreshes = 0
        self._channels = [
            TabloChannel(
                identifier="S123_004_01",
                call_sign="WTEST",
                major=4,
                minor=1,
                network="Test Network",
                kind="ota",
            )
        ]

    async def initialize(self) -> None:
        self.ready = True

    async def channels(self, *, force: bool = False) -> list[TabloChannel]:
        self.force_refreshes += int(force)
        return self._channels


class FakeStreams:
    active_count = 0
    closed = False

    async def open(self, identifier: str) -> AsyncIterator[bytes]:
        assert identifier == "S123_004_01"

        async def body() -> AsyncIterator[bytes]:
            yield b"mpeg-ts"

        return body()

    async def close(self) -> None:
        self.closed = True


def settings() -> Settings:
    return Settings(
        email="viewer@example.com",
        password="secret",
        advertise_url="http://192.168.1.20:5004",
    )


def test_generated_device_id_has_hdhomerun_checksum() -> None:
    lookup = (0xA, 0x5, 0xF, 0x6, 0x7, 0xC, 0x1, 0xB, 0x9, 0x2, 0x8, 0xD, 0x4, 0x3, 0xE, 0x0)
    device_id = int(_device_id("viewer@example.com"), 16)
    checksum = 0
    for shift in (28, 20, 12, 4):
        checksum ^= lookup[(device_id >> shift) & 0x0F]
        checksum ^= (device_id >> (shift - 4)) & 0x0F

    assert checksum == 0


def test_hdhr_discovery_and_lineup() -> None:
    bridge = FakeBridge()
    streams = FakeStreams()

    with TestClient(create_app(settings(), bridge, streams)) as client:
        discover = client.get("/discover.json")
        lineup = client.get("/lineup.json")
        health = client.get("/healthz")

    assert discover.status_code == 200
    assert discover.json()["TunerCount"] == 2
    assert discover.json()["LineupURL"] == "http://192.168.1.20:5004/lineup.json"
    assert lineup.json() == [
        {
            "GuideNumber": "4.1",
            "GuideName": "Test Network",
            "URL": "http://192.168.1.20:5004/stream/S123_004_01",
        }
    ]
    assert health.json()["channels"] == 1
    assert streams.closed


def test_scan_refreshes_lineup() -> None:
    bridge = FakeBridge()
    with TestClient(create_app(settings(), bridge, FakeStreams())) as client:
        response = client.post("/lineup.json")

    assert response.status_code == 200
    assert bridge.force_refreshes == 1


def test_stream_is_mpeg_ts() -> None:
    with TestClient(create_app(settings(), FakeBridge(), FakeStreams())) as client:
        response = client.get("/stream/S123_004_01")

    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp2t"
    assert response.content == b"mpeg-ts"


def test_stream_head_does_not_open_stream() -> None:
    with TestClient(create_app(settings(), FakeBridge(), FakeStreams())) as client:
        response = client.head("/stream/S123_004_01")

    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp2t"


def test_stream_head_rejects_unknown_channel() -> None:
    with TestClient(create_app(settings(), FakeBridge(), FakeStreams())) as client:
        response = client.head("/stream/not-real")

    assert response.status_code == 404
