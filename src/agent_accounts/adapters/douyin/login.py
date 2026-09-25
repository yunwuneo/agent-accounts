"""``douyin login``：headed 模式打开专用 profile，由人扫码登录。"""

from __future__ import annotations

import asyncio
import time

from agent_accounts.adapters.douyin import PLATFORM
from agent_accounts.adapters.douyin.page import LoginState, detect_block, login_state, open_home
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.run import RunContext

POLL_SECONDS = 2.0


async def login(cfg: Config, run: RunContext, timeout_s: int = 300) -> LoginState:
    # 登录必须由人在可见窗口里完成，无视 headless 配置
    async with BrowserSession(PLATFORM, cfg.browser, headless=False) as s:
        await open_home(s, cfg.douyin.base_url)
        state = await login_state(s)
        if state.logged_in:
            run.audit("login.already")
            return state

        print("请在弹出的浏览器窗口中点击「登录」，用 agent 专用抖音账号扫码。")
        print(f"等待登录完成（最长 {timeout_s} 秒）……")
        run.audit("login.waiting")

        warned_block = False
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if s.page.is_closed():
                raise HumanRequired("登录窗口被关闭")
            if not warned_block and (reason := await detect_block(s.page)):
                # 登录时人就在窗口前，验证交给人完成，程序只提示、不处理
                print(f"检测到平台验证（{reason}），请在窗口中手动完成。")
                warned_block = True
            state = await login_state(s)
            if state.logged_in:
                await s.pause(3.0, 4.0)  # 给 profile 留出落盘时间
                run.audit("login.ok")
                return state
            await asyncio.sleep(POLL_SECONDS)

        raise HumanRequired("扫码登录超时", f"{timeout_s} 秒内未检测到登录")
