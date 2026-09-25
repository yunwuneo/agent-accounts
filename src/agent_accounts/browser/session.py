"""浏览器会话：Playwright 持久化 profile + 串行操作 + 拟人节奏。

- 每个平台一个独立 profile（``~/.agent-accounts/profiles/<platform>/``），登录由人完成。
- 所有页面操作通过 :meth:`BrowserSession.op` 串行执行，防止并发点乱。
- 不做任何反检测或绕过风控的处理；遇到验证码、风控由上层停机并通知人。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Self

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from agent_accounts.core import paths
from agent_accounts.core.config import BrowserConfig


class BrowserSession:
    def __init__(self, platform: str, cfg: BrowserConfig, *, headless: bool | None = None):
        self.platform = platform
        self.cfg = cfg
        self.headless = cfg.headless if headless is None else headless
        self._lock = asyncio.Lock()
        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def page(self) -> Page:
        assert self._page is not None, "BrowserSession 尚未启动"
        return self._page

    @property
    def context(self) -> BrowserContext:
        assert self._context is not None, "BrowserSession 尚未启动"
        return self._context

    async def __aenter__(self) -> Self:
        self._pw = await async_playwright().start()
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=paths.profile_dir(self.platform),
            channel=self.cfg.channel or None,
            headless=self.headless,
            locale=self.cfg.locale,
            timezone_id=self.cfg.timezone_id,
            viewport={"width": self.cfg.viewport_width, "height": self.cfg.viewport_height},
        )
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._context is not None:
            await self._context.close()
        if self._pw is not None:
            await self._pw.stop()

    async def pause(self, lo: float | None = None, hi: float | None = None) -> None:
        lo = self.cfg.pause_min if lo is None else lo
        hi = self.cfg.pause_max if hi is None else hi
        await asyncio.sleep(random.uniform(lo, hi))

    async def op[T](self, fn: Callable[[Page], Awaitable[T]]) -> T:
        """串行执行一个页面操作，结束后随机停顿。"""
        async with self._lock:
            result = await fn(self.page)
            await self.pause()
            return result

    async def cookie_names(self, domain_suffix: str) -> set[str]:
        """只返回 cookie 名称，值不外露（凭据不进日志）。"""
        cookies = await self.context.cookies()
        return {c["name"] for c in cookies if c.get("domain", "").endswith(domain_suffix)}

    async def snapshot(self, run_dir: Path, label: str) -> Path:
        """保存截图和 DOM 快照到 ``runs/<id>/``，用于排查选择器问题。"""
        shot = run_dir / f"{label}.png"
        await self.page.screenshot(path=shot)
        (run_dir / f"{label}.html").write_text(await self.page.content(), encoding="utf-8")
        return shot
