"""M0 Spike：可重复运行的探查脚本，产物放在 ``runs/<id>/``。

- ``net``（Spike-2）：录制打开私信面板、进入会话、向上翻历史时的全部接口和 WebSocket 帧
- ``media``（Spike-3）：打开作品页拦截详情接口，下载视频/图片并用 ffmpeg 验证，验证完即删除
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any

from playwright.async_api import Page, Response

from agent_accounts.adapters.douyin import PLATFORM
from agent_accounts.adapters.douyin import selectors as sel
from agent_accounts.adapters.douyin.media import find_aweme, find_filter
from agent_accounts.adapters.douyin.page import ensure_not_blocked, login_state, open_home
from agent_accounts.browser.locate import locate
from agent_accounts.browser.netlog import NetRecorder, endpoint
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.run import RunContext


async def spike_net(
    cfg: Config,
    run: RunContext,
    *,
    conv_index: int = 0,
    scrolls: int = 3,
    headless: bool | None = None,
) -> dict[str, Any]:
    """Spike-2。会点进第 ``conv_index`` 个会话（会标记已读），不输入、不发送。"""
    recorder = NetRecorder(run.dir / "net")
    async with BrowserSession(PLATFORM, cfg.browser, headless=headless) as s:
        recorder.attach(s.page)
        try:
            await open_home(s, cfg.douyin.base_url)
            await ensure_not_blocked(s.page)
            if not (await login_state(s)).logged_in:
                raise HumanRequired("未登录", "请先运行 douyin login")
            recorder.mark("home_loaded")

            entry = await locate(s.page, sel.MESSAGES_ENTRY)
            if entry is None:
                raise RuntimeError("找不到消息入口，先跑 douyin doctor")
            await s.op(lambda _p: entry.first.click())
            await s.pause(3.0, 4.0)
            await ensure_not_blocked(s.page)
            recorder.mark("panel_opened")

            rows = await locate(s.page, sel.CONVERSATION_ROW)
            if rows is None or rows.count <= conv_index:
                raise RuntimeError("会话数量不足")
            await s.op(lambda _p: rows.locator.nth(conv_index).click())
            await s.pause(4.0, 5.0)
            recorder.mark("thread_opened")

            async def scroll_up(page: Page) -> None:
                dialog = page.locator('[data-e2e="im-dialog"]')
                box = await dialog.bounding_box()
                if box:
                    await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                await page.mouse.wheel(0, -1500)

            for _ in range(scrolls):
                await s.op(scroll_up)
                await s.pause(1.5, 2.5)
            recorder.mark("history_scrolled")
            await s.snapshot(run.dir, "thread")
        finally:
            recorder.save_index()
    summary = recorder.summary()
    run.audit("spike.net", endpoints=len(summary["http"]), websockets=len(summary["ws"]))
    return summary


DOUYIN_REFERER = "https://www.douyin.com/"


def _ffprobe(path) -> dict[str, Any]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return json.loads(out.stdout or "{}") if out.returncode == 0 else {"error": out.stderr[-300:]}


def _summarize_detail(d: dict[str, Any]) -> dict[str, Any]:
    video = d.get("video") or {}
    return {
        "aweme_type": d.get("aweme_type"),
        "desc_len": len(d.get("desc") or ""),
        "has_author": bool((d.get("author") or {}).get("nickname")),
        "hashtags": sum(1 for t in d.get("text_extra") or [] if t.get("hashtag_name")),
        "duration_ms": video.get("duration") or d.get("duration"),
        "play_urls": len((video.get("play_addr") or {}).get("url_list") or []),
        "bit_rates": len(video.get("bit_rate") or []),
        "images": len(d.get("images") or []),
        "has_statistics": bool(d.get("statistics")),
        "top_level_keys": len(d),
    }


async def spike_media(cfg: Config, run: RunContext, aweme_id: str, kind: str) -> dict[str, Any]:
    """Spike-3。只打开公开作品页并下载媒体做一次验证，不点赞、不评论。"""
    # (接口, 作品对象) 或 (接口, filter_detail)；不同作品类型走的接口不一样，所以不按路径过滤
    captured: list[tuple[str, dict[str, Any]]] = []
    filtered: list[tuple[str, dict[str, Any]]] = []
    endpoints: list[str] = []

    async def on_response(resp: Response) -> None:
        if resp.request.resource_type not in ("xhr", "fetch") or "/aweme/" not in resp.url:
            return
        ep = endpoint(resp.url)
        endpoints.append(ep)
        try:
            data = await resp.json()
        except Exception:
            return
        if fd := find_filter(data, aweme_id):
            filtered.append((ep, fd))
        if aweme := find_aweme(data, aweme_id):
            captured.append((ep, aweme))

    result: dict[str, Any] = {"aweme_id": aweme_id, "kind": kind}
    async with BrowserSession(PLATFORM, cfg.browser) as s:
        s.page.on("response", on_response)

        async def go(page: Page) -> None:
            await page.goto(
                f"https://www.douyin.com/{kind}/{aweme_id}", wait_until="domcontentloaded"
            )

        await s.op(go)
        for _ in range(60):  # 最多等 15 秒
            if captured or filtered:
                break
            await asyncio.sleep(0.25)
        await ensure_not_blocked(s.page)
        result["detail_endpoint"] = captured[0][0] if captured else None
        if not captured:
            # 作品不可见（私密、删除、审核中等）时平台返回 filter_detail
            result["filter_detail"] = filtered[0][1] if filtered else None
            result["aweme_endpoints_seen"] = sorted(set(endpoints))
            await s.snapshot(run.dir, "media")
            return result
        detail = captured[0][1]
        (run.dir / "detail.json").write_text(
            json.dumps(detail, ensure_ascii=False), encoding="utf-8"
        )
        result["detail"] = _summarize_detail(detail)

        if kind == "video":
            urls = (detail.get("video") or {}).get("play_addr", {}).get("url_list") or []
            target, suffix = (urls[0] if urls else None), ".mp4"
        else:
            images = detail.get("images") or []
            target = (images[0].get("url_list") or [None])[0] if images else None
            suffix = ".img"
        if not target:
            result["download"] = "no url"
            return result

        resp = await s.context.request.get(
            target, headers={"Referer": DOUYIN_REFERER}, timeout=60_000
        )
        body = await resp.body()
        result["download"] = {
            "status": resp.status,
            "content_type": resp.headers.get("content-type"),
            "bytes": len(body),
        }

    media = run.dir / f"media{suffix}"
    media.write_bytes(body)
    try:
        result["ffprobe"] = _ffprobe(media)
        if kind == "video":
            frame = run.dir / "frame.jpg"
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    "1",
                    "-i",
                    str(media),
                    "-frames:v",
                    "1",
                    str(frame),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            result["frame"] = str(frame) if proc.returncode == 0 else proc.stderr[-300:]
    finally:
        media.unlink()  # 视频/图片只用于临时分析，处理完就删除
    run.audit("spike.media", aweme_id=aweme_id, kind=kind, ok=bool(result.get("ffprobe")))
    return result
