"""在独立的可见浏览器会话中交给人完成小红书登录。"""

from __future__ import annotations

import asyncio
import time

from agent_accounts.adapters.xiaohongshu import PLATFORM
from agent_accounts.adapters.xiaohongshu.doctor import CHAT_URL, detect_block, logged_in
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.run import RunContext

POLL_SECONDS = 2.0


async def login(cfg: Config, run: RunContext, timeout_s: int = 300) -> None:
    """等人在独立窗口里登录；检测到进入私信页且没有登录入口即结束。"""
    async with BrowserSession(PLATFORM, cfg.browser, headless=False) as session:
        await session.page.goto(CHAT_URL, wait_until="domcontentloaded")
        await session.page.wait_for_timeout(2000)
        if await logged_in(session.page):
            run.audit("login.already")
            print("已是登录状态。")
            return
        print("请在浏览器窗口中用 agent 专用小红书账号扫码登录。")
        print(f"等待登录完成（最长 {timeout_s} 秒）……")
        run.audit("login.waiting")

        warned_block = False
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if session.page.is_closed():
                raise HumanRequired("登录窗口已关闭")
            # 登录时人就在窗口前，验证交给人完成；程序只提示，不处理
            if not warned_block and await detect_block(session.page):
                print("检测到平台验证，请在窗口中手动完成。")
                run.audit("login.block_seen")
                warned_block = True
            if await logged_in(session.page):
                await asyncio.sleep(3)  # 给 profile 留出落盘时间
                run.audit("login.ok", block_seen=warned_block)
                print("登录完成。")
                return
            await asyncio.sleep(POLL_SECONDS)
        raise HumanRequired("人工登录等待超时")
