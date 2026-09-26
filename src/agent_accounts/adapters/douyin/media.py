"""抖音作品解析与下载（M2 resolver）。

在已登录的浏览器里打开 ``/video/<id>`` 或 ``/note/<id>``，在所有 ``/aweme/`` 接口 JSON 里按
aweme_id 找作品对象（Spike-3：视频来自 aweme/detail，图集来自作者作品列表 aweme/post）。
作品不可见时平台返回 filter_detail，这里记为 ``available=False``，不当作错误。

下载通过浏览器上下文发请求（带同样的 cookie 和 Referer），只用于临时分析，调用方负责删除。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from playwright.async_api import Page, Response

from agent_accounts.adapters.douyin.page import ensure_not_blocked
from agent_accounts.browser.session import BrowserSession

REFERER = "https://www.douyin.com/"
NOTE_AWEME_TYPE = 68
MIN_FRAME_SIDE = 720  # 抽帧用 720p 足够，没必要下载 1080p

Kind = Literal["video", "note"]


@dataclass
class DouyinMedia:
    aweme_id: str
    kind: Kind
    available: bool = True
    filter_reason: str | None = None
    title: str = ""
    author: str = ""
    hashtags: list[str] = field(default_factory=list)
    duration_s: float | None = None
    video_url: str | None = None
    video_urls: list[str] = field(default_factory=list)  # 候选地址，下载不完整时换下一个
    image_urls: list[str] = field(default_factory=list)
    cover_url: str | None = None
    music_title: str | None = None


def _walk(o: Any) -> Iterator[dict[str, Any]]:
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def find_aweme(data: Any, aweme_id: str) -> dict[str, Any] | None:
    for d in _walk(data):
        if str(d.get("aweme_id")) == aweme_id and ("video" in d or "images" in d):
            return d
    return None


def find_filter(data: Any, aweme_id: str) -> dict[str, Any] | None:
    for d in _walk(data):
        if str(d.get("aweme_id")) == aweme_id and "filter_reason" in d:
            return d
    return None


def _first_url(obj: dict[str, Any] | None) -> str | None:
    urls = (obj or {}).get("url_list") or []
    return urls[0] if urls else None


def select_video_urls(video: dict[str, Any]) -> list[str]:
    """候选下载地址，按优先级排序（去重）。

    首选短边 ≥ 720 的 mp4 里体积最小的一档；都不到 720p 时按清晰度从高到低。dash 可能只有
    画面没有声音，不选。每一档的 url_list 里通常有几个 CDN 镜像，都作为候选，最后是 play_addr。
    """
    candidates = []
    for br in video.get("bit_rate") or []:
        addr = br.get("play_addr") or {}
        if br.get("format") != "mp4" or not addr.get("url_list"):
            continue
        side = min(addr.get("width") or 0, addr.get("height") or 0)
        candidates.append((side >= MIN_FRAME_SIDE, addr.get("data_size") or 0, side, addr))
    good = sorted((c for c in candidates if c[0]), key=lambda c: c[1])
    rest = sorted((c for c in candidates if not c[0]), key=lambda c: -c[2])
    urls: list[str] = []
    for addr in [c[3] for c in good + rest] + [video.get("play_addr") or {}]:
        for u in addr.get("url_list") or []:
            if u not in urls:
                urls.append(u)
    return urls


def select_video_url(video: dict[str, Any]) -> str | None:
    urls = select_video_urls(video)
    return urls[0] if urls else None


def parse_aweme(d: dict[str, Any], kind_hint: Kind | None = None) -> DouyinMedia:
    video = d.get("video") or {}
    images = d.get("images") or []
    is_note = d.get("aweme_type") == NOTE_AWEME_TYPE or bool(images) or kind_hint == "note"
    duration_ms = video.get("duration") or d.get("duration") or 0
    return DouyinMedia(
        aweme_id=str(d.get("aweme_id")),
        kind="note" if is_note else "video",
        title=d.get("desc") or "",
        author=(d.get("author") or {}).get("nickname") or "",
        hashtags=[t["hashtag_name"] for t in d.get("text_extra") or [] if t.get("hashtag_name")],
        duration_s=duration_ms / 1000 if duration_ms else None,
        video_url=None if is_note else select_video_url(video),
        video_urls=[] if is_note else select_video_urls(video),
        image_urls=[u for img in images if (u := _first_url(img))],
        cover_url=_first_url(video.get("cover") or video.get("origin_cover")),
        music_title=(d.get("music") or {}).get("title") or None,
    )


async def resolve(
    s: BrowserSession, aweme_id: str, kind_hint: Kind = "video", wait_s: float = 15
) -> DouyinMedia | None:
    """打开作品页并拦截作品 JSON。拿不到时返回 None。"""
    found: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    got = asyncio.Event()

    async def on_response(resp: Response) -> None:
        if resp.request.resource_type not in ("xhr", "fetch") or "/aweme/" not in resp.url:
            return
        try:
            data = await resp.json()
        except Exception:
            return
        if aweme := find_aweme(data, aweme_id):
            found.append(aweme)
            got.set()
        elif fd := find_filter(data, aweme_id):
            filtered.append(fd)
            got.set()

    s.page.on("response", on_response)
    try:

        async def go(page: Page) -> None:
            await page.goto(f"{REFERER}{kind_hint}/{aweme_id}", wait_until="domcontentloaded")

        await s.op(go)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(got.wait(), timeout=wait_s)
        await ensure_not_blocked(s.page)
    finally:
        s.page.remove_listener("response", on_response)

    if found:
        return parse_aweme(found[0], kind_hint)
    if filtered:
        return DouyinMedia(
            aweme_id=aweme_id,
            kind=kind_hint,
            available=False,
            filter_reason=filtered[0].get("filter_reason") or "unknown",
        )
    return None


async def download(s: BrowserSession, url: str, dest: Path, timeout_s: float = 120) -> Path:
    """用浏览器上下文下载（带 cookie 和 Referer）。"""
    resp = await s.context.request.get(url, headers={"Referer": REFERER}, timeout=timeout_s * 1000)
    if not resp.ok:
        raise RuntimeError(f"下载失败：HTTP {resp.status}")
    dest.write_bytes(await resp.body())
    return dest
