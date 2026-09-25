"""``douyin doctor``：逐项检查登录态和关键元素能否定位。

只读：不输入、不发送。默认不点进会话（点进会话会把它标为已读），``open_thread`` 为真时才检查
输入框和发送按钮。任何一项失败都会在 ``runs/<id>/`` 保存截图和 DOM 快照。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from playwright.async_api import Page

from agent_accounts.adapters.douyin import PLATFORM
from agent_accounts.adapters.douyin import selectors as sel
from agent_accounts.adapters.douyin.page import detect_block, login_state, open_home
from agent_accounts.browser.locate import Hit, Target, locate
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.run import RunContext


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None  # None 表示跳过
    detail: str = ""


def _hit_check(target: Target, hit: Hit | None) -> Check:
    if hit is None:
        return Check(target.description, False, "所有策略都未命中")
    extra = f"，{hit.count} 个" if hit.count > 1 else ""
    return Check(target.description, True, f"{hit.strategy}{extra}")


async def doctor(
    cfg: Config,
    run: RunContext,
    *,
    headless: bool | None = None,
    open_thread: bool = False,
    dump: bool = False,
) -> list[Check]:
    checks: list[Check] = []
    async with BrowserSession(PLATFORM, cfg.browser, headless=headless) as s:
        try:
            await _run_checks(s, cfg, checks, open_thread)
        finally:
            if dump or any(c.ok is False for c in checks):
                try:
                    await s.snapshot(run.dir, "doctor")
                    checks.append(Check("快照", None, str(run.dir)))
                except Exception as e:  # 快照失败不能掩盖原本的错误
                    checks.append(Check("快照", False, f"保存失败：{e}"))
            run.audit("doctor", headless=s.headless, checks=[asdict(c) for c in checks])
    return checks


async def _run_checks(s: BrowserSession, cfg: Config, checks: list[Check], open_thread: bool):
    await open_home(s, cfg.douyin.base_url)

    if reason := await detect_block(s.page):
        checks.append(Check("风控/验证", False, reason))
        raise HumanRequired("触发平台验证或风控", reason)
    checks.append(Check("风控/验证", True, "未发现"))

    state = await login_state(s)
    checks.append(
        Check(
            "登录态",
            state.logged_in,
            f"会话 cookie {'有' if state.has_session_cookie else '无'}，"
            f"登录按钮{'可见' if state.login_button_visible else '不可见'}",
        )
    )
    if not state.logged_in:
        raise HumanRequired("未登录", "请先运行 douyin login")

    entry = await locate(s.page, sel.MESSAGES_ENTRY)
    checks.append(_hit_check(sel.MESSAGES_ENTRY, entry))
    if entry is None:
        return

    async def open_panel(page: Page) -> None:
        await entry.first.click()

    await s.op(open_panel)
    if reason := await detect_block(s.page):
        checks.append(Check("风控/验证", False, reason))
        raise HumanRequired("触发平台验证或风控", reason)

    panel = await locate(s.page, sel.MESSAGES_PANEL)
    checks.append(_hit_check(sel.MESSAGES_PANEL, panel))
    rows = await locate(s.page, sel.CONVERSATION_ROW)
    checks.append(_hit_check(sel.CONVERSATION_ROW, rows))

    if not open_thread:
        for target in (sel.THREAD_INPUT, sel.SEND_BUTTON):
            checks.append(
                Check(target.description, None, "跳过（加 --open-thread 检查，会标记已读）")
            )
        return
    if rows is None:
        return

    async def open_first(page: Page) -> None:
        await rows.first.click()

    await s.op(open_first)
    for target in (sel.THREAD_INPUT, sel.SEND_BUTTON):
        checks.append(_hit_check(target, await locate(s.page, target)))
