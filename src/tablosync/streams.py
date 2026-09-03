"""FFmpeg-backed conversion of Tablo HLS streams into Plex-friendly MPEG-TS."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

from .bridge import BridgeError, TabloBridge
from .config import Settings

logger = logging.getLogger(__name__)


class NoTunerAvailable(BridgeError):
    """Raised when all emulated tuner slots are occupied."""


@dataclass(slots=True)
class ActiveStream:
    identifier: str
    started_at: datetime
    process: subprocess.Popen[bytes]


class StreamManager:
    def __init__(self, bridge: TabloBridge, settings: Settings) -> None:
        self.bridge = bridge
        self.settings = settings
        self._active: dict[str, ActiveStream] = {}
        self._starting = 0
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return len(self._active) + self._starting

    async def open(self, identifier: str) -> AsyncIterator[bytes]:
        """Reserve a tuner and start FFmpeg before the HTTP response is committed."""
        async with self._lock:
            if self.active_count >= self.bridge.tuner_count:
                raise NoTunerAvailable("All tuner slots are currently in use")
            self._starting += 1

        try:
            source = await self.bridge.watch(identifier)
            process = self._start_ffmpeg(source.playlist_url)
            stream_id = uuid.uuid4().hex
            active = ActiveStream(identifier, datetime.now(UTC), process)
            async with self._lock:
                self._starting -= 1
                self._active[stream_id] = active
        except Exception:
            async with self._lock:
                self._starting -= 1
            raise

        logger.info(
            "Started channel %s (%d/%d tuner slots active)",
            identifier,
            self.active_count,
            self.bridge.tuner_count,
        )

        return self._read(stream_id, active)

    async def _read(self, stream_id: str, active: ActiveStream) -> AsyncIterator[bytes]:
        process = active.process
        try:
            assert process.stdout is not None
            while True:
                chunk = await asyncio.to_thread(process.stdout.read, 64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            await self._stop(stream_id)

    def _start_ffmpeg(self, playlist_url: str) -> subprocess.Popen[bytes]:
        command = [
            self.settings.ffmpeg_path,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            playlist_url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-f",
            "mpegts",
            "pipe:1",
        ]
        return subprocess.Popen(  # noqa: S603 - fixed executable and argument vector, no shell.
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

    async def _stop(self, stream_id: str) -> None:
        async with self._lock:
            active = self._active.pop(stream_id, None)
        if active is None:
            return

        process = active.process
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=3)
            except TimeoutError:
                process.kill()
                await asyncio.to_thread(process.wait)
        logger.info(
            "Stopped channel %s (%d/%d tuner slots active)",
            active.identifier,
            self.active_count,
            self.bridge.tuner_count,
        )

    async def close(self) -> None:
        async with self._lock:
            stream_ids = list(self._active)
        await asyncio.gather(*(self._stop(stream_id) for stream_id in stream_ids))
