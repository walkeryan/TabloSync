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


def test_reader_cancellation_does_not_cancel_process_cleanup(monkeypatch) -> None:
    async def run() -> None:
        manager = StreamManager(FakeBridge(), settings())  # type: ignore[arg-type]
        process = FakeProcess()
        monkeypatch.setattr(manager, "_start_ffmpeg", lambda _: process)
        entered, release = asyncio.Event(), asyncio.Event()
        original_stop = manager._stop

        async def delayed_stop(stream_id):
            entered.set()
            await release.wait()
            await original_stop(stream_id)

        monkeypatch.setattr(manager, "_stop", delayed_stop)
        body = await manager.open("S123")

        async def consume():
            return b"".join([chunk async for chunk in body])

        reader = asyncio.create_task(consume())
        await entered.wait()
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        release.set()
        await manager.close()
        assert process.stopped
        assert not manager._cleanup_tasks
        assert manager.active_count == 0

    asyncio.run(run())


def test_cancelled_source_lookup_releases_reserved_tuner(monkeypatch) -> None:
    async def run() -> None:
        bridge = FakeBridge()
        entered = asyncio.Event()

        async def watch(_):
            entered.set()
            await asyncio.Future()

        monkeypatch.setattr(bridge, "watch", watch)
        manager = StreamManager(bridge, settings())  # type: ignore[arg-type]
        opening = asyncio.create_task(manager.open("S123"))
        await entered.wait()
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert manager.active_count == 0

    asyncio.run(run())
