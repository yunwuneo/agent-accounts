"""发送器：打开会话 → 真实键入 → 点发送 → 校验。

防重复发送是第一原则：
- 键入前输入框必须为空，键入后内容必须和要发的一致，否则不点发送
- 校验成功 = 输入框清空 且（这段文字在聊天区多出现一次 或 发送接口 /v1/message/send 返回成功）。
  聊天区是倒序的虚拟列表，不能按位置找新气泡（2026-09-25 首次真实发送时踩到）
- 只有文字仍留在输入框里（确定没发出去）时才重试，且只重新点一次发送，不重新键入
- 页面上已经出现这段文字就当作已发送

点进会话会把对方消息标为已读，这是发送的必要代价。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Protocol

from playwright.async_api import Locator, Page, Request, Response

from agent_accounts.adapters.douyin import selectors as sel
from agent_accounts.adapters.douyin.page import ensure_not_blocked
from agent_accounts.browser.locate import locate

IMAPI_HOST = "imapi.douyin.com"
SEND_API = "/v1/message/send"
BUBBLE = '[data-e2e="msg-item-content"]'
_ZERO_WIDTH = str.maketrans("", "", "​‌‍﻿")


class PageSession(Protocol):
    page: Page

    async def pause(self, lo: float | None = None, hi: float | None = None) -> None: ...


class SendError(RuntimeError):
    pass


@dataclass
class SendResult:
    ok: bool
    detail: str
    retried: bool = False
    imapi_requests: list[str] = field(
        default_factory=list
    )  # 发送期间的私信接口，便于以后改用接口校验


def normalize(text: str) -> str:
    return " ".join(text.translate(_ZERO_WIDTH).split())


async def _input_text(loc: Locator) -> str:
    return normalize(await loc.inner_text())


async def _bubble_texts(page: Page) -> list[str]:
    return [normalize(t) for t in await page.locator(BUBBLE).all_inner_texts()]


async def open_conversation(s: PageSession, name: str) -> None:
    """打开私信面板，点进昵称完全匹配的会话。"""
    entry = await locate(s.page, sel.MESSAGES_ENTRY)
    if entry is None:
        raise SendError("找不到消息入口，先跑 douyin doctor")
    await entry.first.click()
    await s.pause(2.0, 3.0)
    await ensure_not_blocked(s.page)
    rows = await locate(s.page, sel.CONVERSATION_ROW)
    if rows is None:
        raise SendError("找不到会话列表")
    for i in range(rows.count):
        row = rows.locator.nth(i)
        title = row.locator('[class*="ConversationItemtitle"]')
        text = await (title.first if await title.count() else row).inner_text()
        if normalize(text).startswith(normalize(name)):
            await row.click()
            await s.pause(2.0, 3.0)
            await ensure_not_blocked(s.page)
            return
    raise SendError(f"会话列表里找不到「{name}」")


async def _occurrences(page: Page, text: str) -> int:
    # 聊天区是倒序的（最新在最前），而且是虚拟列表，不能按位置找新气泡；只比较出现次数
    return sum(text in b for b in await _bubble_texts(page))


async def _wait_sent(
    page: Page, box: Locator, text: str, before: int, api_ok: asyncio.Event, timeout_s: float
) -> bool:
    """输入框清空，并且（页面上这段文字多了一次 或 发送接口返回成功）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not await _input_text(box) and (
            api_ok.is_set() or await _occurrences(page, text) > before
        ):
            return True
        await asyncio.sleep(0.3)
    return False


async def send_text(s: PageSession, text: str, *, verify_timeout_s: float = 8.0) -> SendResult:
    """在当前打开的会话里发送一条文本。调用前必须已经 open_conversation。"""
    text = normalize(text)  # 换行会被当成回车直接发送，统一成单行
    if not text:
        raise SendError("内容为空")
    page = s.page
    requests: list[str] = []
    api_ok = asyncio.Event()

    def on_request(req: Request) -> None:
        if IMAPI_HOST in req.url:
            requests.append(req.url.split("?")[0].split(IMAPI_HOST, 1)[1])

    def on_response(resp: Response) -> None:
        if IMAPI_HOST in resp.url and SEND_API in resp.url and resp.ok:
            api_ok.set()

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        box_hit = await locate(page, sel.THREAD_INPUT)
        send_hit = await locate(page, sel.SEND_BUTTON)
        if box_hit is None or send_hit is None:
            raise SendError("找不到输入框或发送按钮")
        box = box_hit.first
        if await _input_text(box):
            raise SendError("输入框里已有内容，为避免误发不继续")

        before = await _occurrences(page, text)
        await box.click()
        await s.pause(0.3, 0.8)
        for ch in text:  # 拟人键入速度
            await page.keyboard.type(ch)
            await asyncio.sleep(random.uniform(0.05, 0.16))
        await s.pause(0.4, 1.0)
        if await _input_text(box) != text:
            raise SendError("键入后输入框内容和要发送的不一致，未发送（请人工检查输入框）")

        await send_hit.first.click()
        if await _wait_sent(page, box, text, before, api_ok, verify_timeout_s):
            return SendResult(True, "已发送", imapi_requests=requests)

        # 没确认成功：已有发送证据就算发送了；文字还在输入框里才重试一次
        if api_ok.is_set() or await _occurrences(page, text) > before:
            return SendResult(True, "已发送（输入框未及时清空）", imapi_requests=requests)
        if await _input_text(box) == text:
            await s.pause(1.0, 2.0)
            await send_hit.first.click()
            if await _wait_sent(page, box, text, before, api_ok, verify_timeout_s):
                return SendResult(True, "重试后已发送", retried=True, imapi_requests=requests)
            return SendResult(False, "重试一次仍未发送成功", retried=True, imapi_requests=requests)
        return SendResult(
            False, "发送状态无法确认（输入框已变化但没看到新消息）", imapi_requests=requests
        )
    finally:
        page.remove_listener("request", on_request)
        page.remove_listener("response", on_response)
