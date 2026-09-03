import io
from fractions import Fraction

import av
import httpx
import pytest

from tablosync.tve_remux import RemuxError, TrackClock, parse_segments, select_variant


def test_highest_quality_variant_is_selected() -> None:
    master = (
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nlow.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=200\nhigh.m3u8\n"
    )
    assert select_variant(master, "https://video.example/live/master.m3u8") == (
        "https://video.example/live/high.m3u8"
    )


def test_segment_manifest_preserves_encryption_and_sequence() -> None:
    media = (
        "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:123\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="key?private=value"\n'
        "#EXTINF:4.004,\nfirst.ts\n#EXT-X-DISCONTINUITY\n"
        "#EXT-X-KEY:METHOD=NONE\n#EXTINF:4.004,\nsecond.ts\n"
    )
    segments = parse_segments(media, "https://video.example/live/index.m3u8")
    assert [s.sequence for s in segments] == [123, 124]
    assert "#EXT-X-MEDIA-SEQUENCE:123" in segments[0].manifest().decode()
    assert 'URI="https://video.example/live/key?private=value"' in segments[0].key
    assert segments[1].key == "#EXT-X-KEY:METHOD=NONE"
    assert segments[1].url == "https://video.example/live/second.ts"
    assert segments[0].manifest().endswith(b"#EXT-X-ENDLIST\n")


@pytest.mark.parametrize(
    "key",
    [
        'METHOD=SAMPLE-AES,URI="https://private.example/license"',
        'METHOD=AES-128,URI="key",KEYFORMAT="com.apple.streamingkeydelivery"',
    ],
)
def test_each_new_media_playlist_rejects_drm(key) -> None:
    with pytest.raises(RemuxError):
        parse_segments(f"#EXTM3U\n#EXT-X-KEY:{key}\n", "https://video.example/master.m3u8")


@pytest.mark.parametrize(
    "body",
    [
        "#EXTM3U\n#EXTINF:4,\nfile:///private.ts\n",
        '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n',
        "#EXTM3U\n#EXTINF:nan,\na.ts\n",
        "#EXTM3U\n#EXTINF:0,\na.ts\n",
    ],
)
def test_unsupported_segments_fail_closed(body) -> None:
    with pytest.raises(RemuxError):
        parse_segments(body, "https://video.example/master.m3u8")


def test_track_changes_keep_a_continuous_clock() -> None:
    tb = Fraction(1, 90000)
    clock = TrackClock(Fraction(-100), Fraction(1, 60))
    first = 100 * 90000
    assert clock.shift(first, tb, 1500) == -first
    assert clock.shift(first + 1500, tb, 1500) == -first
    # An inserted segment has a different timestamp origin and track ID.
    shift = clock.shift(126000, tb, 1500)
    assert (126000 + shift) * tb == Fraction(2, 60)
    # Returning to the programme is continuous too.
    shift = clock.shift(first + 4 * 90000, tb, 1500)
    assert (first + 4 * 90000 + shift) * tb == Fraction(3, 60)


def test_duplicate_boundary_packets_are_not_emitted_twice() -> None:
    clock = TrackClock(Fraction(0), Fraction(1024, 48000))
    tb = Fraction(1, 90000)
    assert clock.shift(1920, tb, 1920) == 0
    assert clock.shift(1920, tb, 1920) is None
    assert clock.shift(1919, tb, 1920) is None
    assert clock.shift(3840, tb, 1920) == 0


def test_remux_decodes_across_segment_program_and_pid_changes(monkeypatch) -> None:
    from tablosync import tve_remux

    def encoded_segment(pid: int, program: int, offset: int) -> bytes:
        buffer = io.BytesIO()
        with av.open(
            buffer,
            "w",
            format="mpegts",
            options={
                "mpegts_start_pid": str(pid),
                "mpegts_service_id": str(program),
            },
        ) as container:
            video = container.add_stream("libx264", rate=10)
            video.width, video.height, video.pix_fmt = 64, 64, "yuv420p"
            video.options = {"bf": "0", "g": "10", "preset": "ultrafast"}
            audio = container.add_stream("aac", rate=48000)
            audio.layout = "stereo"
            audio_pts = offset * 48000
            for index in range(10):
                frame = av.VideoFrame(64, 64, "yuv420p")
                for plane in frame.planes:
                    plane.update(bytes([128]) * plane.buffer_size)
                frame.pts, frame.time_base = offset * 10 + index, Fraction(1, 10)
                container.mux(video.encode(frame))
                for _ in range(5):
                    sound = av.AudioFrame(format="fltp", layout="stereo", samples=1024)
                    sound.sample_rate = 48000
                    sound.pts, sound.time_base = audio_pts, Fraction(1, 48000)
                    for plane in sound.planes:
                        plane.update(bytes(plane.buffer_size))
                    container.mux(audio.encode(sound))
                    audio_pts += 1024
            container.mux(video.encode(None))
            container.mux(audio.encode(None))
        return buffer.getvalue()

    one, two = encoded_segment(256, 1, 0), encoded_segment(512, 2, 100)
    real_open = av.open

    def open_media(target, *args, **kwargs):
        if kwargs.get("format") == "hls":
            body = target.getvalue()
            data = one if b"/one.ts" in body else two
            return real_open(io.BytesIO(data), format="mpegts")
        return real_open(target, *args, **kwargs)

    class FakeHTTP:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                text=(
                    "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:100\n#EXTINF:1.1,\none.ts\n"
                    "#EXT-X-DISCONTINUITY\n#EXTINF:1.1,\ntwo.ts\n#EXT-X-ENDLIST\n"
                ),
            )

    monkeypatch.setattr(tve_remux.httpx, "Client", FakeHTTP)
    monkeypatch.setattr(tve_remux.av, "open", open_media)
    result = io.BytesIO()
    tve_remux.remux("https://video.example/playlist.m3u8", result)
    with real_open(io.BytesIO(result.getvalue())) as decoded:
        assert len(decoded.streams.video) == len(decoded.streams.audio) == 1
        frames = list(decoded.decode(video=0))
        assert len(frames) == 20
        assert frames[-1].time - frames[0].time < 3
