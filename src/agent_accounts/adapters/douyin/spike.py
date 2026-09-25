"""M0 Spike：可重复运行的探查脚本，产物放在 ``runs/<id>/``。

- ``net``（Spike-2）：录制打开私信面板、进入会话、向上翻历史时的全部接口和 WebSocket 帧
"""

from __future__ import annotations

from typing import Any

from playwright.async_api import Page

from agent_accounts.adapters.douyin import PLATFORM
from agent_accounts.adapters.douyin import selectors as sel
from agent_accounts.adapters.douyin.page import ensure_not_blocked, login_state, open_home
from agent_accounts.browser.locate import locate
from agent_accounts.browser.netlog import NetRecorder
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.run import RunContext


async def spike_net(
    cfg: Config, run: RunContext, *, conv_index: int = 0, scrolls: int = 3
) -> dict[str, Any]:
    """Spike-2。会点进第 ``conv_index`` 个会话（会标记已读），不输入、不发送。"""
    recorder = NetRecorder(run.dir / "net")
    async with BrowserSession(PLATFORM, cfg.browser) as s:
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
