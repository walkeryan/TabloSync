import asyncio
import io

import pytest
from tablo_api.models import TabloStream

from tablosync.config import Settings
from tablosync.streams import NoTunerAvailable, StreamManager


class FakeBridge:
    tuner_count = 1

    async def watch(self, identifier: str) -> TabloStream:
        return TabloStream(channel_identifier=identifier, playlist_url="http://tablo/live.m3u8")


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = io.BytesIO(b"one" + b"two")
        self.stopped = False

    def poll(self) -> int | None:
        return 0 if self.stopped else None

    def terminate(self) -> None:
        self.stopped = True

    def kill(self) -> None:
        self.stopped = True

    def wait(self) -> int:
        self.stopped = True
        return 0


def settings() -> Settings:
    return Settings(email="viewer@example.com", password="secret")


def test_stream_is_released_after_reader_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        manager = StreamManager(FakeBridge(), settings())  # type: ignore[arg-type]
        process = FakeProcess()
        monkeypatch.setattr(manager, "_start_ffmpeg", lambda _: process)

        body = await manager.open("S123")
        content = b"".join([chunk async for chunk in body])

        assert content == b"onetwo"
        assert manager.active_count == 0
        assert process.stopped

    asyncio.run(run())


def test_tuner_limit_is_reserved_before_response(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        manager = StreamManager(FakeBridge(), settings())  # type: ignore[arg-type]
        monkeypatch.setattr(manager, "_start_ffmpeg", lambda _: FakeProcess())

        await manager.open("S123")
        with pytest.raises(NoTunerAvailable):
            await manager.open("S456")
        await manager.close()

        assert manager.active_count == 0

    asyncio.run(run())
