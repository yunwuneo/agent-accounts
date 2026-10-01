"""发送器：在私信页点开会话 → 真实键入 → 回车 → 校验。

小红书的消息经 WebSocket 编码帧发出（M0 实测），没有可监听的发送接口，所以校验只看页面：
- 键入前输入框必须为空，键入后内容必须和要发的一致，否则不回车
- 校验成功 = 输入框清空 且 这段文字在聊天区多出现一次
- 只有文字仍留在输入框里（确定没发出去）时才重试，且只重新回车一次，不重新键入
- 多条消息逐条发送，条间停顿；任何一条没确认发出就停下，不再发后面的

发送后下一轮 sync 会把自己发的消息也拉进库（messages/history），作为事后核对。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Protocol

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from agent_accounts.adapters.xiaohongshu.doctor import CHAT_URL, detect_captcha
from agent_accounts.adapters.xiaohongshu.sync import click_conversation
from agent_accounts.core.errors import HumanRequired

EDITOR = ".xhs-im-input-bar-editor[contenteditable]"
BUBBLE = "p.xhs-im-bubble__text"
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


@dataclass
class MultiSendResult:
    ok: bool
    sent: int
    total: int
    detail: str
    results: list[SendResult] = field(default_factory=list)


def normalize(text: str) -> str:
    return " ".join(text.translate(_ZERO_WIDTH).split())


async def _ensure_ok(page: Page) -> None:
    if await detect_captcha(page):
        raise HumanRequired("触发小红书验证或风控", freeze=True)


async def open_conversation(s: PageSession, peer_id: str, name: str | None) -> None:
    """确保在私信页，并点开这个会话。"""
    try:
        if not s.page.url.split("?", 1)[0].rstrip("/").endswith("/chat"):
            await s.page.goto(CHAT_URL, wait_until="domcontentloaded")
            await s.pause(2.0, 3.0)
        await _ensure_ok(s.page)
        if not await click_conversation(s.page, peer_id, name):
            raise SendError(f"会话列表里找不到「{name or peer_id}」")
        await s.pause(2.0, 3.0)
        await _ensure_ok(s.page)
        await s.page.locator(EDITOR).first.wait_for(state="visible", timeout=10_000)
    except PlaywrightError as e:  # 还没键入任何内容，按发送失败处理
        raise SendError(f"打不开会话：{str(e).splitlines()[0]}") from None


async def _editor_text(box: Locator) -> str:
    return normalize(await box.inner_text())


async def _occurrences(page: Page, text: str) -> int:
    return sum(normalize(t) == text for t in await page.locator(BUBBLE).all_inner_texts())


async def _wait_sent(page: Page, box: Locator, text: str, before: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not await _editor_text(box) and await _occurrences(page, text) > before:
            return True
        await asyncio.sleep(0.3)
    return False


async def send_text(s: PageSession, text: str, *, verify_timeout_s: float = 8.0) -> SendResult:
    """在当前打开的会话里发送一条文本。调用前必须已经 open_conversation。"""
    text = normalize(text)  # 换行会被当成回车直接发送，统一成单行
    if not text:
        raise SendError("内容为空")
    page = s.page
    box = page.locator(EDITOR).first
    if not await box.count():
        raise SendError("找不到输入框")
    if await _editor_text(box):
        raise SendError("输入框里已有内容，为避免误发不继续")

    before = await _occurrences(page, text)
    await box.click()
    await s.pause(0.3, 0.8)
    for ch in text:  # 拟人键入速度
        await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(0.05, 0.16))
    await s.pause(0.4, 1.0)
    if await _editor_text(box) != text:
        raise SendError("键入后输入框内容和要发送的不一致，未发送（请人工检查输入框）")

    await page.keyboard.press("Enter")
    if await _wait_sent(page, box, text, before, verify_timeout_s):
        return SendResult(True, "已发送")
    await _ensure_ok(page)
    if await _occurrences(page, text) > before:
        return SendResult(True, "已发送（输入框未及时清空）")
    if await _editor_text(box) == text:
        await s.pause(1.0, 2.0)
        await box.click()
        await page.keyboard.press("End")
        await page.keyboard.press("Enter")
        if await _wait_sent(page, box, text, before, verify_timeout_s):
            return SendResult(True, "重试后已发送", retried=True)
        return SendResult(False, "重试一次仍未发送成功", retried=True)
    return SendResult(False, "发送状态无法确认（输入框已变化但没看到新消息）")


async def send_messages(
    s: PageSession, texts: list[str], *, verify_timeout_s: float = 8.0
) -> MultiSendResult:
    """在当前打开的会话里依次发送多条消息。前一条确认发出后才发下一条。"""
    results: list[SendResult] = []
    for i, text in enumerate(texts):
        if i:
            await s.pause(1.0, 3.0)
        try:
            r = await send_text(s, text, verify_timeout_s=verify_timeout_s)
        except SendError as e:
            r = SendResult(False, str(e))
        results.append(r)
        if not r.ok:
            break
    sent, total = sum(r.ok for r in results), len(texts)
    last = results[-1].detail if results else "没有要发送的消息"
    if total and sent == total:
        return MultiSendResult(
            True, sent, total, last if total == 1 else f"已发送 {total} 条", results
        )
    detail = last if total <= 1 else f"第 {sent + 1}/{total} 条：{last}"
    return MultiSendResult(False, sent, total, detail, results)
