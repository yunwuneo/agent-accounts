"""抖音页面的通用动作：打开首页、判断登录态、检测风控。"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.async_api import Page

from agent_accounts.adapters.douyin import selectors as sel
from agent_accounts.browser.locate import locate
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core.errors import HumanRequired

COOKIE_DOMAIN = "douyin.com"
# 登录后才会下发的会话 cookie（只看名字，不读值）
LOGIN_COOKIES = frozenset({"sessionid", "sessionid_ss", "sid_tt"})


@dataclass(frozen=True)
class LoginState:
    has_session_cookie: bool
    login_button_visible: bool

    @property
    def logged_in(self) -> bool:
        return self.has_session_cookie and not self.login_button_visible


async def open_home(session: BrowserSession, base_url: str) -> None:
    async def go(page: Page) -> None:
        await page.goto(base_url, wait_until="domcontentloaded")

    await session.op(go)
    await session.pause(2.5, 4.0)  # 等顶栏和异步组件渲染


async def login_state(session: BrowserSession) -> LoginState:
    names = await session.cookie_names(COOKIE_DOMAIN)
    button = await locate(session.page, sel.LOGIN_BUTTON, timeout_ms=1500)
    return LoginState(
        has_session_cookie=bool(names & LOGIN_COOKIES),
        login_button_visible=button is not None,
    )


async def detect_block(page: Page) -> str | None:
    """检测验证码 / 风控页面，命中时返回原因。"""
    if sel.BLOCK_URL_PATTERN.search(page.url):
        return f"页面跳转到了验证页：{page.url.split('?')[0]}"
    for frame in page.frames:
        if frame is not page.main_frame and sel.BLOCK_URL_PATTERN.search(frame.url):
            return "页面中出现了验证码弹窗"
    loc = page.get_by_text(sel.BLOCK_TEXT_PATTERN)
    if await loc.count() and await loc.first.is_visible():
        return f"页面出现风控提示：{(await loc.first.inner_text()).strip()[:40]}"
    return None


async def ensure_not_blocked(page: Page) -> None:
    if reason := await detect_block(page):
        raise HumanRequired("触发平台验证或风控", reason)
