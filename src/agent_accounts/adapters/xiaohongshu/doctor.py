"""只读 M0 探针：打开私信首页，观察阻断信号和网络元数据。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.async_api import Page

from agent_accounts.adapters.xiaohongshu import PLATFORM
from agent_accounts.adapters.xiaohongshu.netmeta import MetadataRecorder
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.run import RunContext

CHAT_URL = "https://www.xiaohongshu.com/chat"
_BLOCK_URL = re.compile(r"captcha|verify|risk|security", re.I)
_CAPTCHA_TEXT = re.compile(r"滑块|拖动.*验证|安全验证|请完成验证|访问异常|操作频繁|账号异常")
# 笔记本身不可看（删除、仅自己可见、审核中等）；单独出现时不算风控
UNAVAILABLE_TEXT = re.compile(r"当前笔记暂时无法浏览|暂时无法浏览|笔记不见了")


@dataclass(frozen=True)
class DoctorResult:
    chat_page: bool
    login_visible: bool
    network: dict[str, object]


async def _visible_text(page: Page, pattern: re.Pattern[str]) -> bool:
    text = page.get_by_text(pattern)
    for index in range(min(await text.count(), 20)):
        if await text.nth(index).is_visible():
            return True
    return False


async def detect_captcha(page: Page) -> bool:
    """验证码 / 风控信号；不含「笔记不可看」这类内容本身的状态。"""
    if _BLOCK_URL.search(page.url):
        return True
    for frame in page.frames:
        if frame is page.main_frame or not _BLOCK_URL.search(frame.url):
            continue
        if await (await frame.frame_element()).is_visible():
            return True
    return await _visible_text(page, _CAPTCHA_TEXT)


async def detect_unavailable(page: Page) -> bool:
    return await _visible_text(page, UNAVAILABLE_TEXT)


async def detect_block(page: Page) -> bool:
    """私信页上的阻断信号：验证码、风控，以及（私信页不该出现的）不可浏览提示。"""
    return await detect_captcha(page) or await detect_unavailable(page)


async def login_visible(page: Page) -> bool:
    login = page.get_by_role("button", name=re.compile(r"^登录$"))
    return bool(await login.count() and await login.first.is_visible())


async def logged_in(page: Page) -> bool:
    """登录后侧栏有指向自己主页的「我」；未登录时有「登录」按钮。"""
    if await login_visible(page):
        return False
    me = page.locator('a[href*="/user/profile/"]')
    return bool(await me.count() and await me.first.is_visible())


async def doctor(cfg: Config, run: RunContext) -> DoctorResult:
    recorder = MetadataRecorder()
    # 真实平台强制 headed；不允许配置文件把本探针切到 headless。
    async with BrowserSession(PLATFORM, cfg.browser, headless=False) as session:
        recorder.attach(session.page)
        await session.page.goto(CHAT_URL, wait_until="domcontentloaded")
        await session.page.wait_for_timeout(2000)
        if await detect_block(session.page):
            raise HumanRequired("触发小红书验证或风控", freeze=True)
        result = DoctorResult(
            chat_page=session.page.url.split("?", 1)[0].rstrip("/") == CHAT_URL,
            login_visible=await login_visible(session.page),
            network=recorder.summary(),
        )
        if result.login_visible:
            raise HumanRequired("小红书尚未人工登录")
        if not result.chat_page:
            raise HumanRequired("未进入小红书私信页，请人工检查登录或权限状态")
        recorder.save(run.dir / "xiaohongshu-netmeta.jsonl")
        run.audit("doctor", chat_page=result.chat_page, network=result.network)
        return result
