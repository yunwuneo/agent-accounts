"""把视频「看完整」的通用步骤（与平台无关）：下载完整文件、整段分段转写。

下载函数由平台适配器传入（各平台的 Referer、cookie 不同）；不完整时换候选地址重下，
都不完整就用最长的那份，并在 notes 里注明后面没看到。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from agent_accounts.core import media
from agent_accounts.core.config import Config, ConfigError
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.transcribe import TranscribeError

Fetch = Callable[[str, Path], Awaitable[Path]]
Transcribe = Callable[..., Awaitable[str]]


def short_by(got: float, expected: float | None) -> bool:
    """下载到的时长明显短于作品时长（允许 2 秒或 3% 的误差）。"""
    return bool(expected) and got < expected - max(2.0, expected * 0.03)  # type: ignore[operator]


async def download_full_video(
    fetch: Fetch, urls: list[str], expected_s: float | None, tmp: Path, notes: list[str]
) -> Path | None:
    """依次尝试候选地址，直到拿到完整的视频；都不完整时用最长的那个，并在 notes 里说明。"""
    best: tuple[float, Path] | None = None
    last_error = ""
    for i, url in enumerate(urls):
        dest = tmp / f"video_{i}.mp4"
        try:
            await fetch(url, dest)
            got = (await media.probe(dest)).duration_s or 0.0
        except HumanRequired:
            raise
        except Exception as e:  # 这个地址下载或解析失败，换下一个
            last_error = type(e).__name__
            dest.unlink(missing_ok=True)
            continue
        if best is None or got > best[0]:
            if best:
                best[1].unlink(missing_ok=True)
            best = (got, dest)
        else:
            dest.unlink(missing_ok=True)
        if not short_by(got, expected_s):
            break
    if best is None:
        if urls:
            raise RuntimeError(f"视频下载失败（{last_error}）")
        return None
    if short_by(best[0], expected_s):
        notes.append(
            f"下载到的视频只有 {media.clock(best[0])}，作品时长 {media.clock(expected_s or 0)}，"
            "后面的内容没有看到"
        )
    return best[1]


async def transcribe_full(
    cfg: Config,
    video: Path,
    duration_s: float | None,
    tmp: Path,
    notes: list[str],
    *,
    transcribe: Transcribe,
) -> str | None:
    """整段音轨分段转写，按段落拼接并标出时间范围。"""
    limit = cfg.media.max_video_seconds
    audio = await media.extract_audio(video, tmp / "audio.mp3", max_seconds=limit)
    if audio is None:
        notes.append("视频没有音轨")
        return None
    if duration_s and duration_s > limit:
        notes.append(f"语音只转写了前 {media.clock(limit)}，之后的没有听到")
    seg_s = cfg.media.transcribe_segment_s
    segments = await media.split_audio(audio, tmp, segment_s=seg_s)
    parts: list[str] = []
    for i, seg in enumerate(segments):
        try:
            text = await transcribe(cfg.transcribe, seg)
        except ConfigError as e:
            notes.append(f"语音转写失败：{e}")
            return None
        except TranscribeError as e:
            if len(segments) == 1:
                notes.append(f"语音转写失败：{e}")
                return None
            span = f"{media.clock(i * seg_s)}–{media.clock((i + 1) * seg_s)}"
            notes.append(f"{span} 这段语音转写失败：{e}")
            continue
        if text:
            if len(segments) == 1:
                parts.append(text)
            else:
                end = min((i + 1) * seg_s, duration_s or (i + 1) * seg_s)
                parts.append(f"[{media.clock(i * seg_s)}–{media.clock(end)}] {text}")
    return "\n".join(parts) or None


async def frames_and_transcript(
    cfg: Config, video: Path, tmp: Path, notes: list[str], *, transcribe: Transcribe
) -> tuple[media.Frames, str | None]:
    """整段均匀抽帧 + 整段转写。"""
    frames = await media.extract_frames(
        video,
        tmp,
        min_frames=cfg.media.min_frames,
        max_frames=cfg.media.max_frames,
        max_side=cfg.media.frame_width,
        interval_s=cfg.media.frame_interval_s,
    )
    transcript = await transcribe_full(
        cfg, video, frames.duration_s, tmp, notes, transcribe=transcribe
    )
    return frames, transcript
