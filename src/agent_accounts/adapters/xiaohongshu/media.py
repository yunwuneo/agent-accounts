"""小红书笔记解析与下载（M2 resolver）。

笔记页地址用私信卡片里的 xsec_token 拼出（M0 实测：在私信里点卡片打开的就是
``/explore/<id>?xsec_token=<卡片里的 token>&xsec_source=app_share``）。笔记数据在页面服务端渲染的
``__INITIAL_STATE__.note.noteDetailMap`` 里：标题、正文、图片列表、视频多档编码流。

下载用浏览器上下文发请求（带同样的 cookie），只用于临时分析，调用方负责删除。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from agent_accounts.adapters.xiaohongshu.doctor import detect_captcha, detect_unavailable
from agent_accounts.adapters.xiaohongshu.spike import note_state
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core.errors import HumanRequired

BASE = "https://www.xiaohongshu.com"
MIN_FRAME_SIDE = 720  # 抽帧用 720p 足够

Kind = Literal["video", "note"]
_TOPIC = re.compile(r"#([^#\[\]\n]+?)\[话题\]#")


@dataclass
class XhsNote:
    note_id: str
    kind: Kind
    available: bool = True
    unavailable_reason: str | None = None
    title: str = ""
    body: str = ""
    author: str = ""
    tags: list[str] = field(default_factory=list)
    duration_s: float | None = None
    video_urls: list[str] = field(default_factory=list)  # 候选地址，下载不完整时换下一个
    image_urls: list[str] = field(default_factory=list)


def note_url(note_id: str, xsec_token: str) -> str:
    query = urlencode({"xsec_token": xsec_token, "xsec_source": "app_share"})
    return f"{BASE}/explore/{note_id}?{query}"


def clean_body(text: str) -> str:
    """正文里的话题写成 ``#话题[话题]#``，还原成 ``#话题``。"""
    return _TOPIC.sub(lambda m: f"#{m.group(1)}", text or "").strip()


def _image_url(img: dict[str, Any]) -> str | None:
    for info in img.get("infoList") or []:
        if isinstance(info, dict) and info.get("imageScene") == "WB_DFT" and info.get("url"):
            return info["url"]
    return img.get("urlDefault") or img.get("url") or None


def _streams(video: dict[str, Any]) -> list[dict[str, Any]]:
    stream = ((video.get("media") or {}).get("stream")) or {}
    out = []
    for items in stream.values() if isinstance(stream, dict) else []:
        out += [s for s in items or [] if isinstance(s, dict) and s.get("masterUrl")]
    return out


def select_video_urls(video: dict[str, Any]) -> list[str]:
    """候选下载地址（去重）：短边 ≥ 720 里体积最小的优先；都不到 720 时按清晰度从高到低。

    每一档先用 masterUrl，再用它的 backupUrls。
    """
    streams = _streams(video)

    def side(s: dict[str, Any]) -> int:
        return min(s.get("width") or 0, s.get("height") or 0)

    def size(s: dict[str, Any]) -> int:
        return s.get("size") or 0

    good = sorted((s for s in streams if side(s) >= MIN_FRAME_SIDE), key=size)
    rest = sorted((s for s in streams if side(s) < MIN_FRAME_SIDE), key=side, reverse=True)
    urls: list[str] = []
    for s in good + rest:
        for url in [s.get("masterUrl"), *(s.get("backupUrls") or [])]:
            if url and url not in urls:
                urls.append(url)
    return urls


def _duration(video: dict[str, Any]) -> float | None:
    capa = (video.get("capa") or {}).get("duration")
    if isinstance(capa, int | float) and capa > 0:
        return float(capa)  # 秒
    for s in _streams(video):
        ms = s.get("duration")
        if isinstance(ms, int | float) and ms > 0:
            return ms / 1000
    return None


def find_note(state: Any, note_id: str) -> dict[str, Any] | None:
    """从 noteDetailMap 里取这篇笔记；页面上可能还有推荐笔记，只认 id 对得上的。"""
    if not isinstance(state, dict):
        return None
    for key, detail in state.items():
        note = (detail or {}).get("note") if isinstance(detail, dict) else None
        if isinstance(note, dict) and note_id in (key, note.get("noteId")) and note.get("type"):
            return note
    return None


def parse_note(note: dict[str, Any], note_id: str) -> XhsNote:
    video = note.get("video") if isinstance(note.get("video"), dict) else {}
    is_video = note.get("type") == "video" or bool(_streams(video))
    return XhsNote(
        note_id=note_id,
        kind="video" if is_video else "note",
        title=note.get("title") or "",
        body=clean_body(note.get("desc") or ""),
        author=((note.get("user") or {}).get("nickname")) or "",
        tags=[
            t["name"] for t in note.get("tagList") or [] if isinstance(t, dict) and t.get("name")
        ],
        duration_s=_duration(video) if is_video else None,
        video_urls=select_video_urls(video) if is_video else [],
        image_urls=[u for img in note.get("imageList") or [] if (u := _image_url(img))],
    )


async def resolve(
    s: BrowserSession, note_id: str, xsec_token: str, kind_hint: Kind = "note"
) -> XhsNote | None:
    """打开笔记页取笔记数据。笔记不可看时返回 available=False；拿不到数据返回 None。"""
    await s.pause()
    await s.page.goto(note_url(note_id, xsec_token), wait_until="domcontentloaded")
    await s.page.wait_for_timeout(4000)
    if await detect_captcha(s.page):
        raise HumanRequired("打开笔记触发小红书验证或风控", freeze=True)
    note = find_note(await note_state(s.page), note_id)
    if note is not None:
        return parse_note(note, note_id)
    if await detect_unavailable(s.page):
        return XhsNote(note_id, kind_hint, available=False, unavailable_reason="暂时无法浏览")
    return None


async def download(s: BrowserSession, url: str, dest: Path, timeout_s: float = 120) -> Path:
    """用浏览器上下文下载（带 cookie 和 Referer）。"""
    resp = await s.context.request.get(
        url, headers={"Referer": f"{BASE}/"}, timeout=timeout_s * 1000
    )
    if not resp.ok:
        raise RuntimeError(f"下载失败：HTTP {resp.status}")
    dest.write_bytes(await resp.body())
    return dest
