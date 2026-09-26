"""视频完整观看：下载不完整时换地址重下、整段抽帧、分段转写、关键帧带时间点。

视频用 ffmpeg lavfi 现场生成；下载和转写都替换成本地假实现，不开浏览器、不访问网络。
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from agent_accounts.adapters.douyin import digest as ddigest
from agent_accounts.adapters.douyin import media as dmedia
from agent_accounts.core import config
from agent_accounts.core.transcribe import TranscribeError
from agent_accounts.core.understand import UnderstandInput, build_content


def _video(path, seconds: int):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=5",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )  # fmt: skip
    return path


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("clips")
    return {"full": _video(d / "full.mp4", 40), "cut": _video(d / "cut.mp4", 12)}


@pytest.fixture
def fake_download(monkeypatch, clips):
    """地址名就是要返回的片段：cut = 只有前 12 秒（CDN 返回不完整），full = 完整 40 秒。"""
    calls = []

    async def download(s, url, dest, timeout_s=120):
        calls.append(url)
        if url == "broken":
            raise RuntimeError("下载失败：HTTP 403")
        shutil.copy(clips[url], dest)
        return dest

    monkeypatch.setattr(dmedia, "download", download)
    return calls


def _media(urls, duration=40.0):
    return dmedia.DouyinMedia(
        aweme_id="1", kind="video", duration_s=duration, video_url=urls[0], video_urls=urls
    )


async def test_truncated_download_retries_next_url(fake_download, tmp_path):
    notes: list[str] = []
    path = await ddigest._download_full_video(None, _media(["cut", "full"]), tmp_path, notes)
    assert fake_download == ["cut", "full"] and notes == []
    assert len(list(tmp_path.glob("video_*.mp4"))) == 1  # 不完整的那份已删除
    assert path.exists()


async def test_all_truncated_keeps_longest_and_notes(fake_download, tmp_path):
    notes: list[str] = []
    await ddigest._download_full_video(None, _media(["cut", "broken"]), tmp_path, notes)
    assert fake_download == ["cut", "broken"]
    assert len(notes) == 1 and "0:12" in notes[0] and "0:40" in notes[0] and "没有看到" in notes[0]


async def test_complete_first_download_does_not_retry(fake_download, tmp_path):
    notes: list[str] = []
    await ddigest._download_full_video(None, _media(["full", "cut"]), tmp_path, notes)
    assert fake_download == ["full"] and notes == []


async def test_all_downloads_fail_raises(fake_download, tmp_path):
    with pytest.raises(RuntimeError, match="视频下载失败"):
        await ddigest._download_full_video(None, _media(["broken"]), tmp_path, [])


async def test_build_input_covers_whole_video_and_transcribes_all_segments(
    fake_download, monkeypatch, tmp_path
):
    seen = []

    async def fake_transcribe(cfg, audio):
        seen.append(audio.name)
        if len(seen) == 2:
            raise TranscribeError("HTTP 500（server_error）")
        return f"第{len(seen)}段"

    monkeypatch.setattr(ddigest, "transcribe", fake_transcribe)
    cfg = config.Config(media=config.MediaConfig(transcribe_segment_s=15))
    inp, notes = await ddigest._build_input(None, cfg, _media(["full"]), tmp_path)

    assert len(inp.images) == 8 == len(inp.frame_times)  # 40 秒约每 5 秒一帧
    assert inp.frame_times[0] < 5 and inp.frame_times[-1] > 35  # 从头覆盖到尾
    assert len(seen) == 3  # 40 秒按 15 秒切成 3 段，全部送去转写
    assert inp.transcript == "[0:00–0:15] 第1段\n[0:30–0:40] 第3段"
    assert notes == ["0:15–0:30 这段语音转写失败：HTTP 500（server_error）"]


async def test_audio_beyond_limit_is_noted(fake_download, monkeypatch, tmp_path):
    async def fake_transcribe(cfg, audio):
        return "内容"

    monkeypatch.setattr(ddigest, "transcribe", fake_transcribe)
    cfg = config.Config(media=config.MediaConfig(max_video_seconds=20))
    inp, notes = await ddigest._build_input(None, cfg, _media(["full"]), tmp_path)
    assert any("只转写了前 0:20" in n for n in notes)
    assert any("没有听到" in n for n in inp.notes)  # 模型也知道后面没听到


def test_video_urls_ordered_with_mirrors():
    good = {
        "format": "mp4",
        "play_addr": {"width": 720, "height": 1280, "data_size": 5, "url_list": ["a1", "a2"]},
    }
    big = {
        "format": "mp4",
        "play_addr": {"width": 1080, "height": 1920, "data_size": 9, "url_list": ["b1"]},
    }
    low = {"format": "mp4", "play_addr": {"width": 360, "height": 640, "url_list": ["c1"]}}
    video = {"bit_rate": [big, low, good], "play_addr": {"url_list": ["main", "a1"]}}
    assert dmedia.select_video_urls(video) == ["a1", "a2", "b1", "c1", "main"]


def test_frames_are_labelled_with_time(tmp_path):
    frames = []
    for i in range(2):
        p = tmp_path / f"f{i}.jpg"
        p.write_bytes(b"\xff\xd8\xff")
        frames.append(p)
    inp = UnderstandInput(kind="video", duration_s=118, images=frames, frame_times=[2.5, 65.0])
    content = build_content(inp)
    assert [c["type"] for c in content] == ["text", "image", "text", "image", "text"]
    assert content[0]["text"] == "[0:02]" and content[2]["text"] == "[1:05]"
    assert "均匀覆盖整段视频（0:00–1:58）" in content[-1]["text"]
