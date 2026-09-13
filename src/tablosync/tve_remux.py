"""Keep FOX HLS audio/video stable across ad and programme track changes.

Each segment is demuxed independently, then copied into two persistent output
tracks. No video is re-encoded and no advertisements are removed.
"""

from __future__ import annotations

import io
import math
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from fractions import Fraction
from urllib.parse import urljoin, urlsplit

import av
import httpx

from .streams import StreamManager


class RemuxError(RuntimeError):
    """A safe-to-log stream compatibility error, without private URLs."""


def _https(base: str, value: str) -> str:
    url = urljoin(base, value)
    if urlsplit(url).scheme != "https":
        raise RemuxError("The TVE playlist contains an unsupported URL scheme")
    return url


@dataclass(frozen=True)
class Segment:
    sequence: int
    duration: float
    key: str
    url: str

    def manifest(self) -> bytes:
        return (
            f"#EXTM3U\n#EXT-X-TARGETDURATION:{math.ceil(self.duration)}\n"
            f"#EXT-X-MEDIA-SEQUENCE:{self.sequence}\n{self.key}\n"
            f"#EXTINF:{self.duration},\n{self.url}\n#EXT-X-ENDLIST\n"
        ).encode()


def parse_segments(text: str, base: str) -> list[Segment]:
    if not text.lstrip().startswith("#EXTM3U"):
        raise RemuxError("The TVE media playlist is unreadable")
    sequence, duration, key = 0, None, ""
    segments = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("#EXT-X-MAP:", "#EXT-X-BYTERANGE:")):
            raise RemuxError("The TVE playlist uses an unsupported segment format")
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-KEY:"):
            if not re.search(r"METHOD=(?:NONE|AES-128)(?:,|$)", line):
                raise RemuxError("The TVE segment is DRM-protected")
            key_format = re.search(r'KEYFORMAT="([^"]+)"', line)
            if key_format and key_format[1] != "identity":
                raise RemuxError("The TVE segment uses an unsupported key format")
            key = re.sub(
                r'URI="([^"]+)"',
                lambda match: f'URI="{_https(base, match[1])}"',
                line,
            )
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",")[0])
            if not math.isfinite(duration) or not 0 < duration <= 60:
                raise RemuxError("The TVE segment duration is invalid")
        elif line and not line.startswith("#"):
            if duration is None:
                raise RemuxError("Expected a TVE media playlist, not a master playlist")
            segments.append(Segment(sequence, duration, key, _https(base, line)))
            sequence += 1
            duration = None
    return segments


def select_variant(text: str, base: str) -> str:
    variants = []
    bandwidth = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            match = re.search(r"(?:[:,])BANDWIDTH=(\d+)", line)
            bandwidth = int(match[1]) if match else 0
        elif line and not line.startswith("#") and bandwidth is not None:
            variants.append((bandwidth, _https(base, line)))
            bandwidth = None
    return max(variants)[1] if variants else base


@dataclass
class TrackClock:
    offset: Fraction
    duration: Fraction
    last: Fraction | None = None

    def shift(self, dts: int, time_base: Fraction, duration: int) -> int | None:
        adjusted = dts * time_base + self.offset
        if self.last is not None:
            delta = adjusted - self.last
            # Duplicate boundary packets occur in the source. Do not emit them
            # twice, or introduce non-monotonic DTS into Plex's recorder.
            if -1 <= delta <= 0:
                return None
            if delta < -1 or delta > 2:
                self.offset += self.last + self.duration - adjusted
                adjusted = dts * time_base + self.offset
        self.last = adjusted
        if duration:
            self.duration = duration * time_base
        return round(self.offset / time_base)


def remux(url: str, destination: object) -> None:
    av.logging.set_level(av.logging.PANIC)  # Upstream errors can contain signed URLs.
    with httpx.Client(timeout=15, follow_redirects=True) as http:
        master = http.get(url)
        master.raise_for_status()
        media_url = select_variant(master.text, str(master.url))
        with av.open(
            destination,
            "w",
            format="mpegts",
            options={
                "mpegts_flags": "+resend_headers",
                "flush_packets": "1",
            },
        ) as output:
            outputs, clocks, pending = {}, {}, []
            first_time = None
            last_sequence = None
            last_progress = time.monotonic()
            while True:
                response = http.get(media_url)
                response.raise_for_status()
                segments = parse_segments(response.text, str(response.url))
                if last_sequence is None and segments:
                    last_sequence = max(segments[0].sequence - 1, segments[-1].sequence - 3)
                for segment in segments:
                    if last_sequence is not None and segment.sequence <= last_sequence:
                        continue
                    with av.open(
                        io.BytesIO(segment.manifest()),
                        format="hls",
                        options={
                            "protocol_whitelist": "file,crypto,http,https,tcp,tls",
                        },
                        timeout=(15, 15),
                    ) as source:
                        for packet in source.demux():
                            kind = packet.stream.type
                            if kind not in ("video", "audio") or not packet.size:
                                continue
                            if packet.dts is None or packet.time_base is None:
                                continue
                            if kind not in outputs:
                                outputs[kind] = output.add_stream_from_template(packet.stream)
                            tb = packet.time_base
                            if first_time is None:
                                first_time = packet.dts * tb
                            clock = clocks.setdefault(
                                kind,
                                TrackClock(
                                    -first_time,
                                    Fraction(1001, 60000)
                                    if kind == "video"
                                    else Fraction(1024, 48000),
                                ),
                            )
                            shift = clock.shift(packet.dts, tb, packet.duration)
                            if shift is None:
                                continue
                            packet.dts += shift
                            if packet.pts is not None:
                                packet.pts += shift
                            packet.stream = outputs[kind]
                            pending.append(packet)
                            if len(outputs) == 2:
                                for ready in pending:
                                    output.mux(ready)
                                pending.clear()
                            elif len(pending) > 1000:
                                raise RemuxError("The TVE stream is missing an audio/video track")
                    last_sequence = segment.sequence
                    last_progress = time.monotonic()
                if "#EXT-X-ENDLIST" in response.text:
                    return
                if time.monotonic() - last_progress > 30:
                    raise RemuxError("The TVE live playlist stopped advancing")
                time.sleep(1)


class TVEStreamManager(StreamManager):
    def _start_ffmpeg(self, playlist_url: str) -> subprocess.Popen[bytes]:
        # The URL travels over a pipe, not process arguments or a temporary file.
        process = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "tablosync.tve_remux"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        assert process.stdin is not None
        try:
            process.stdin.write(playlist_url.encode() + b"\n")
            process.stdin.close()
        except BaseException:
            process.kill()
            process.wait()
            raise
        return process


def main() -> None:
    try:
        url = sys.stdin.buffer.readline(65536).decode().strip()
        if urlsplit(url).scheme != "https":
            raise RemuxError("Expected an HTTPS TVE source")
        remux(url, sys.stdout.buffer)
    except BrokenPipeError:
        pass
    except Exception:
        # Deliberately omit third-party exception text, which may expose URLs.
        print("TVE stream ended because the upstream media could not be read", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
