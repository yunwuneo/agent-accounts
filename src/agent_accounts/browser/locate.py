"""多策略元素定位。

平台页面的 class name 大多是混淆过的，不能作为稳定依赖。每个目标（Target）配置多种定位策略，
按顺序尝试，任何一个命中可见元素即可；命中的策略名会返回给调用方，便于 doctor 报告健康度。

几何类策略用一段 JS 在页面里找到元素后打上 ``data-aa-target`` 标记，再转成 Locator，
这样后续操作仍然走 Playwright 的自动等待和真实输入。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from playwright.async_api import Locator, Page

MARK_ATTR = "data-aa-target"

Finder = Callable[[Page], Awaitable[Locator | None]]


@dataclass(frozen=True)
class Strategy:
    name: str
    find: Finder


@dataclass(frozen=True)
class Target:
    name: str
    description: str
    strategies: tuple[Strategy, ...]


@dataclass(frozen=True)
class Hit:
    target: str
    strategy: str
    locator: Locator  # 所有命中的元素（可能多个）
    count: int

    @property
    def first(self) -> Locator:
        return self.locator.first


def css(selector: str, name: str | None = None) -> Strategy:
    async def find(page: Page) -> Locator:
        return page.locator(selector)

    return Strategy(name or f"css:{selector}", find)


def text(
    pattern: str | re.Pattern[str], *, exact: bool = True, name: str | None = None
) -> Strategy:
    async def find(page: Page) -> Locator:
        return page.get_by_text(pattern, exact=exact)

    label = pattern.pattern if isinstance(pattern, re.Pattern) else pattern
    return Strategy(name or f"text:{label}", find)


def js_mark(target: str, script: str, name: str) -> Strategy:
    """``script`` 是一个 JS 函数体，返回 Element 或 Element[]；命中的元素会被打上标记。"""
    mark = f"{target}:{name}"

    async def find(page: Page) -> Locator | None:
        found = await page.evaluate(
            """([attr, mark, body]) => {
                document.querySelectorAll(`[${attr}="${mark}"]`)
                    .forEach(el => el.removeAttribute(attr));
                let res = new Function(body)();
                if (!res) return 0;
                if (!Array.isArray(res)) res = [res];
                res.forEach(el => el.setAttribute(attr, mark));
                return res.length;
            }""",
            [MARK_ATTR, mark, script],
        )
        return page.locator(f'[{MARK_ATTR}="{mark}"]') if found else None

    return Strategy(name, find)


async def _visible_count(loc: Locator) -> int:
    n = await loc.count()
    return n if n and await loc.first.is_visible() else 0


async def locate(page: Page, target: Target, timeout_ms: int = 5000) -> Hit | None:
    """在超时内轮询所有策略，返回第一个命中可见元素的策略。"""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for strategy in target.strategies:
            try:
                loc = await strategy.find(page)
                if loc is not None and (n := await _visible_count(loc)):
                    return Hit(target.name, strategy.name, loc, n)
            except Exception:  # 单个策略出错不影响其他策略
                continue
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.25)
