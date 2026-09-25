"""媒体管线测试：用 ffmpeg lavfi 现场生成测试视频，转写用 MockTransport，不访问网络。"""

from __future__ import annotations

import subprocess

import httpx2
import pytest

from agent_accounts.core import media
from agent_accounts.core.config import TranscribeConfig
from agent_accounts.core.transcribe import TranscribeError, transcribe


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory):
    path = tmp_path_factory.mktemp("media") / "sample.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=duration=24:size=1280x720:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=24",
        "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(path),
    )  # fmt: skip
    return path


def test_frame_times_are_bounded_and_centered():
    assert media.frame_times(24, 3, 10) == [4.0, 12.0, 20.0]
    assert len(media.frame_times(600, 3, 10)) == 10
    assert len(media.frame_times(2, 3, 10)) == 3
    times = media.frame_times(80, 3, 10)
    assert times[0] > 0 and times[-1] < 80


async def test_probe_and_extract(sample_video, tmp_path):
    info = await media.probe(sample_video)
    assert info.has_video and info.has_audio and 23 < info.duration_s < 25

    frames = await media.extract_frames(
        sample_video, tmp_path, min_frames=3, max_frames=10, max_side=640
    )
    assert len(frames) == 3
    frame_info = await media.probe(frames[0])
    assert (frame_info.width, frame_info.height) == (640, 360)  # 长边缩到 640，保持比例

    audio = await media.extract_audio(sample_video, tmp_path / "a.mp3", max_seconds=10)
    assert audio is not None
    audio_info = await media.probe(audio)
    assert audio_info.has_audio and not audio_info.has_video
    assert audio_info.duration_s <= 10.2


async def test_extract_audio_returns_none_without_audio(tmp_path):
    silent = tmp_path / "silent.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=5", "-c:v", "mpeg4", str(silent)
    )
    assert await media.extract_audio(silent, tmp_path / "a.mp3", max_seconds=10) is None


async def test_portrait_image_to_jpeg(tmp_path):
    src = tmp_path / "portrait.png"
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=1080x1920", "-frames:v", "1", str(src))
    out = await media.to_jpeg(src, tmp_path / "p.jpg", max_side=1024)
    info = await media.probe(out)
    assert info.height == 1024 and info.width == 576


def _cfg(**kw) -> TranscribeConfig:
    return TranscribeConfig(
        base_url="https://asr.test/v1", api_key="sk-test-key", model="asr-1", **kw
    )


async def test_transcribe_sends_multipart_and_returns_text(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"fake-mp3")
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.read()
        return httpx2.Response(200, json={"text": " 你好，世界 "})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        text = await transcribe(_cfg(), audio, client=client)
    assert text == "你好，世界"
    assert seen["url"] == "https://asr.test/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer sk-test-key"
    assert b'name="model"' in seen["body"] and b"asr-1" in seen["body"]
    assert b'name="language"' in seen["body"] and b"fake-mp3" in seen["body"]


async def test_transcribe_error_does_not_leak_key(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"error": "invalid key"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(TranscribeError) as exc:
            await transcribe(_cfg(), audio, client=client)
    assert "401" in str(exc.value) and "sk-test-key" not in str(exc.value)
