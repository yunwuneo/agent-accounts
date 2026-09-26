"""通用媒体处理（与平台无关）：探测、均匀抽帧、图片转 JPEG、提取音轨。全部用 ffmpeg。

输出都写在调用方给的临时目录里，调用方负责清理。
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, field
from pathlib import Path


class MediaError(RuntimeError):
    pass


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise MediaError(f"{args[0]} 失败：{err.decode(errors='replace')[-300:]}")
    return out.decode(errors="replace")


@dataclass(frozen=True)
class ProbeInfo:
    duration_s: float | None
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None


async def probe(path: Path) -> ProbeInfo:
    out = await _run(
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,width,height", "-of", "json", str(path),
    )  # fmt: skip
    data = json.loads(out or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    duration = (data.get("format") or {}).get("duration")
    return ProbeInfo(
        duration_s=float(duration) if duration not in (None, "N/A") else None,
        has_video=video is not None,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        width=video.get("width") if video else None,
        height=video.get("height") if video else None,
    )


def frame_times(
    duration_s: float, min_frames: int, max_frames: int, interval_s: float = 5.0
) -> list[float]:
    """把整段视频均匀切成 n 段，每段取中点（避开片头片尾黑帧），覆盖从头到尾。

    n 约为「时长 / interval_s」，限制在 [min_frames, max_frames]；超过上限时帧间隔变大，
    但仍然覆盖整段视频，不会只看开头。
    """
    n = max(min_frames, min(max_frames, math.ceil(duration_s / interval_s)))
    return [round(duration_s * (i + 0.5) / n, 2) for i in range(n)]


@dataclass
class Frames:
    paths: list[Path] = field(default_factory=list)
    times: list[float] = field(default_factory=list)  # 每张对应的秒数，与 paths 一一对应
    duration_s: float | None = None  # 实际文件的时长（ffprobe）


def _scale_filter(max_side: int) -> str:
    # 长边缩到 max_side 以内，不放大；-2 保证偶数尺寸
    return f"scale='if(gt(iw,ih),min({max_side},iw),-2)':'if(gt(iw,ih),-2,min({max_side},ih))'"


async def extract_frames(
    video: Path,
    out_dir: Path,
    *,
    min_frames: int,
    max_frames: int,
    max_side: int,
    interval_s: float = 5.0,
) -> Frames:
    info = await probe(video)
    if not info.has_video:
        raise MediaError("文件里没有视频流")
    result = Frames(duration_s=info.duration_s)
    for i, t in enumerate(frame_times(info.duration_s or 1.0, min_frames, max_frames, interval_s)):
        out = out_dir / f"frame_{i:03d}.jpg"
        await _run(
            "ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(video),
            "-frames:v", "1", "-vf", _scale_filter(max_side), "-q:v", "4", str(out),
        )  # fmt: skip
        if out.exists():
            result.paths.append(out)
            result.times.append(t)
    return result


async def to_jpeg(image: Path, out: Path, *, max_side: int) -> Path:
    await _run(
        "ffmpeg", "-v", "error", "-y", "-i", str(image),
        "-frames:v", "1", "-vf", _scale_filter(max_side), "-q:v", "4", str(out),
    )  # fmt: skip
    return out


async def extract_audio(video: Path, out: Path, *, max_seconds: int) -> Path | None:
    """单声道 16kHz mp3，适合语音转写。没有音轨时返回 None。"""
    if not (await probe(video)).has_audio:
        return None
    await _run(
        "ffmpeg", "-v", "error", "-y", "-i", str(video), "-t", str(max_seconds),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k", str(out),
    )  # fmt: skip
    return out


async def split_audio(audio: Path, out_dir: Path, *, segment_s: int) -> list[Path]:
    """把长音轨按 segment_s 秒切段（不重新编码），用于分段转写；短音轨原样返回。"""
    duration = (await probe(audio)).duration_s or 0
    if duration <= segment_s:
        return [audio]
    pattern = out_dir / f"{audio.stem}_%03d{audio.suffix}"
    await _run(
        "ffmpeg", "-v", "error", "-y", "-i", str(audio), "-f", "segment",
        "-segment_time", str(segment_s), "-c", "copy", str(pattern),
    )  # fmt: skip
    return sorted(out_dir.glob(f"{audio.stem}_*{audio.suffix}"))


def clock(seconds: float) -> str:
    """秒数 → m:ss（超过一小时为 h:mm:ss）。"""
    s = int(seconds)
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
