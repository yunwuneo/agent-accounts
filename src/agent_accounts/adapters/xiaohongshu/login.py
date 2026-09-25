"""在独立的可见浏览器会话中交给人完成小红书登录。"""

from __future__ import annotations

import asyncio
import select
import sys
import time

from agent_accounts.adapters.xiaohongshu import PLATFORM
from agent_accounts.adapters.xiaohongshu.doctor import CHAT_URL, detect_block
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.run import RunContext


async def login(cfg: Config, run: RunContext, timeout_s: int = 300) -> None:
    """等待人工在独立窗口登录；按回车只结束交接，登录态仍由 doctor 验收。"""
    if not sys.stdin.isatty():
        raise HumanRequired("人工登录需要交互终端")

    async with BrowserSession(PLATFORM, cfg.browser, headless=False) as session:
        await session.page.goto(CHAT_URL, wait_until="domcontentloaded")
        print("请在浏览器窗口中使用 agent 专用小红书账号人工登录。完成后回到终端按回车。")
        run.audit("login.waiting")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if session.page.is_closed():
                raise HumanRequired("登录窗口已关闭")
            if await detect_block(session.page):
                raise HumanRequired("触发小红书验证或风控", freeze=True)
            if select.select([sys.stdin], [], [], 0)[0]:
                if not sys.stdin.readline():
                    raise HumanRequired("交互终端已关闭")
                run.audit("login.handoff_complete")
                return
            await asyncio.sleep(1)
        raise HumanRequired("人工登录等待超时")
