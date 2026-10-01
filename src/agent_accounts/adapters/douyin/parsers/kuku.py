"""KuKuTool 可见网页自动化；使用页面公开媒体元素，不调用逆向接口。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from contextlib import suppress
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from agent_accounts.adapters.douyin.android.media import validate_files
from agent_accounts.adapters.douyin.parsers.download import (
    ParseError,
    download,
    media_url,
    share_url,
)
from agent_accounts.core.errors import HumanRequired

SITE = "https://dy.kukutool.com/"


def check_notice(text: str) -> None:
    # FAQ 的“为什么需要验证码”“免费无需登录”不代表当前出现拦截。
    if re.search(r"请.{0,8}(完成|通过).{0,8}(验证|验证码)|拖动滑块|验证您是人类|人机验证", text):
        raise HumanRequired("第三方解析需要人工验证；未重试或绕过")
    if re.search(
        r"请先登录|登录后.{0,8}(解析|下载)|请.{0,5}(扫码|支付|付款)|充值后|余额不足", text
    ):
        raise HumanRequired("第三方解析要求登录或付费，请人工处理")


async def check_page(page) -> None:
    if urlsplit(page.url).hostname != "dy.kukutool.com":
        raise ParseError("解析页面离开已指定网站，已停止")
    check_notice(await page.locator("body").inner_text(timeout=5000))
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        u = urlsplit(frame.url)
        if any(k in (u.hostname or "") for k in ("recaptcha", "hcaptcha", "challenges.cloudflare")):
            element = await frame.frame_element()
            if await element.is_visible():
                raise HumanRequired("第三方显示验证控件，请人工处理")
        # Google 的可交互 reCAPTCHA；不把不可见的后台脚本当成需要解决的题目。
        if "/recaptcha/" in u.path:
            element = await frame.frame_element()
            if await element.is_visible():
                raise HumanRequired("第三方显示验证码，请人工处理")


async def close_ads(page) -> None:
    # 只关闭已观察到的普通插屏广告，不点广告链接，不关闭验证/付费页面。
    promotion = page.get_by_text("一次提交多个链接，批量解析更省时间", exact=True)
    if await promotion.count() == 1 and await promotion.is_visible():
        # 本次独立临时上下文内的推广提示，不进入批量解析，也不影响用户浏览器设置。
        await page.get_by_role("button", name="7天不再提示", exact=True).click(timeout=3000)
    for frame in page.frames:
        if urlsplit(frame.url).hostname == "googleads.g.doubleclick.net":
            dismiss = frame.locator("#dismiss-button")
            if await dismiss.count() == 1 and await dismiss.is_visible():
                await dismiss.click(timeout=3000)


def select_media(elements: list[dict], kind: str, count: int) -> list[str]:
    videos = [e for e in elements if e["tag"] == "VIDEO"]
    if kind == "video":
        if count != 1 or len(videos) != 1:
            raise ParseError("解析结果不是单个视频，未下载")
        return [media_url(videos[0]["url"], resolve=False)]
    if kind != "gallery" or videos:
        raise ParseError("解析类型不符；视频封面不能作为图集下载")
    images = {}
    for element in elements:
        match = re.fullmatch(r"Image (\d+)", element.get("alt", ""))
        if element["tag"] == "IMG" and match:
            index = int(match[1])
            if index in images:
                raise ParseError("图片序号重复，未下载")
            images[index] = media_url(element["url"], resolve=False)
    if set(images) != set(range(1, count + 1)):
        raise ParseError("解析图集数量与指定页数不一致，未下载")
    return [images[i] for i in range(1, count + 1)]


async def submit_and_resolve(
    page, link: str, kind: str, count: int, *, expected_title: str, timeout_s: float
) -> list[str]:
    # 该站 SSR 表单先出现，客户端加载后才绑定事件。不能在仅有 HTML 时抢填、抢点。
    await page.goto(SITE, wait_until="load", timeout=int(timeout_s * 1000))
    await check_page(page)
    await close_ads(page)
    field = page.get_by_placeholder("粘贴带链接的文本", exact=True)
    await field.fill(link, timeout=10000)
    await field.press("Tab")
    await asyncio.sleep(0.5)
    await check_page(page)
    await close_ads(page)
    if await field.input_value() != link:
        raise ParseError("网页加载后输入内容变化，未提交；请检查页面")
    # 提交一次。超时也不自动刷新、再提交或切换其他平台。
    await page.get_by_role("button", name="开始解析", exact=True).click(timeout=10000)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        await check_page(page)
        await close_ads(page)
        body = await page.locator("body").inner_text()
        if re.search(r"解析失败|无法解析|不支持该链接|请求过于频繁|次数已用完", body):
            raise ParseError("第三方解析返回失败或限流；未自动重试")
        ready = page.get_by_role(
            "heading", name="视频列表" if kind == "video" else "封面或图片列表", exact=True
        )
        if await ready.count() and await ready.is_visible():
            if await field.input_value() != link:
                raise ParseError("解析结果的提交链接变化，未下载")
            if expected_title and "".join(expected_title.split()) not in "".join(body.split()):
                raise ParseError("解析结果未匹配预期标题，未下载")
            elements = await page.locator("video, img[alt^='Image ']").evaluate_all(
                "els => els.map(e => ({tag:e.tagName,alt:e.getAttribute('alt')||'',"
                "url:e.currentSrc||e.src||''}))"
            )
            return select_media(elements, kind, count)
        await asyncio.sleep(0.5)
    raise ParseError("等待解析结果超时；未自动重试")


async def parse_media(
    cfg,
    run,
    link: str,
    kind: str,
    count: int,
    *,
    execute: bool = False,
    expected_title: str = "",
    max_mb: int = 200,
    timeout_s: float = 90,
) -> dict:
    link = share_url(link)
    if (
        kind not in {"video", "gallery"}
        or not 1 <= count <= 200
        or (kind == "video" and count != 1)
    ):
        raise ParseError("须指定单个 video（count=1）或 gallery（count=总页数）")
    if not 1 <= max_mb <= 2048 or not 5 <= timeout_s <= 300:
        raise ParseError("max-mb 须为 1–2048，timeout 须为 5–300 秒")
    summary = {
        "provider": "kukutool",
        "kind": kind,
        "count": count,
        "link_hash": hashlib.sha256(link.encode()).hexdigest(),
    }
    if not execute:
        run.audit("media.parse.dry_run", **summary)
        return {
            "status": "dry_run",
            "provider": "kukutool",
            "submitted": False,
            "kind": kind,
            "expected_count": count,
        }
    run.audit("media.parse.start", **summary)
    raw = run.dir / "parser-downloads"
    raw.mkdir()
    files = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel=cfg.browser.channel or None, headless=False)
            # 全新临时上下文，和抖音登录 profile 隔离；不授予剪贴板或账号权限。
            context = await browser.new_context(locale="zh-CN", accept_downloads=False)
            page = await context.new_page()
            try:
                urls = await submit_and_resolve(
                    page, link, kind, count, expected_title=expected_title, timeout_s=timeout_s
                )
                await page.screenshot(path=run.dir / "parser-result.png", full_page=True)
                run.audit("media.parse.resolved", **summary, title_match=bool(expected_title))
                # 下载仅使用网页已经展示的 URL；绝不传 Cookie、Authorization 或分享文案。
                for i, url in enumerate(urls):
                    await check_page(page)
                    path = raw / f"item-{i + 1:03}.bin"
                    await asyncio.to_thread(
                        download,
                        url,
                        path,
                        kind,
                        max_bytes=max_mb * 1024 * 1024,
                        timeout_s=timeout_s,
                    )
                    files.append(path)
            except BaseException:
                # 本地诊断，不把网页正文、分享链接或 URL 写入审计。
                with suppress(PlaywrightError):
                    await page.screenshot(path=run.dir / "parser-stopped.png", timeout=5000)
                raise
            finally:
                await context.close()
                await browser.close()
        result = await validate_files(run, files, kind, "third_party_kukutool", count)
        manifest_path = run.dir / "media.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["parser"] = {
            **summary,
            "title_match": bool(expected_title),
            "submission_link_verified": True,
            "quality": "page_default",
            "order_source": "parser_image_number",
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
        return {**result, "provider": "kukutool", "status": "downloaded_and_validated"}
    except PlaywrightError:
        raise ParseError("解析网页加载或控件操作失败；未自动重试，原始页面错误未写入日志") from None
