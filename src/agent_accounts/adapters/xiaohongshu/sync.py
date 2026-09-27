"""``xiaohongshu sync``：打开私信首页拿会话列表，只点开有新消息的会话拉取消息入库。

与抖音不同，小红书首页只有每个会话的预览；完整消息必须点进会话，页面会随即上报已读
（``messages/read``）。所以：

- 只点开 ``max_store_id`` 大于本地进度的会话，不重复打开没有新消息的会话；
- 只打开已关注的会话（陌生人的消息和关注请求一概不管、不点开）；官方账号、平台 AI 助手不打开；
  每轮最多打开 ``max_open`` 个；
- ``open_chats=False`` 时只更新会话列表和未读数，完全不标已读；
- 每次打开都记录在结果和审计里，并统计已读上报次数。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Page, Request, Response

from agent_accounts.adapters.xiaohongshu import PLATFORM, im
from agent_accounts.adapters.xiaohongshu import store as xstore
from agent_accounts.adapters.xiaohongshu.doctor import CHAT_URL, detect_block, logged_in
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.run import RunContext

_CHATS = re.compile(r"/api/im/web/[^/]+/chats$")
_UNREAD = "/api/im/web/chat/get_unread"
_HISTORY = "/api/im/web/messages/history"
_READ = "/api/im/web/v2/messages/read"
_ME = "/api/sns/web/v2/user/me"

CONV_ITEM = ".xhs-im-conv-item[data-conv-id]"
CONV_NAME = ".xhs-im-conv-item__name"
MSG_LIST = ".xhs-im-msg-list"


class Collector:
    def __init__(self) -> None:
        self.my_id: str | None = None
        self.chats: dict[str, im.XhsChat] = {}
        self.unread: dict[str, int] = {}
        self.history: list[list[im.XhsMessage]] = []
        self.read_requests = 0
        self.errors: list[str] = []
        self.chats_seen = asyncio.Event()

    def attach(self, page: Page) -> None:
        page.on("request", self._on_request)
        page.on("response", self._on_response)

    def detach(self, page: Page) -> None:
        page.remove_listener("request", self._on_request)
        page.remove_listener("response", self._on_response)

    def _on_request(self, request: Request) -> None:
        if request.method == "POST" and request.url.split("?", 1)[0].endswith(_READ):
            self.read_requests += 1

    async def _on_response(self, response: Response) -> None:
        path = response.url.split("?", 1)[0].split(".com", 1)[-1]
        if (
            not (_CHATS.search(path) or path in (_UNREAD, _HISTORY, _ME))
            or response.request.method != "GET"
        ):
            return
        try:
            body: Any = await response.json()
        except Exception:  # noqa: BLE001 — 页面关闭后响应体可能不可读
            return
        try:
            if path == _ME:
                self.my_id = im.parse_me(body) or self.my_id
            elif path == _UNREAD:
                self.unread = im.parse_unread(body)
            elif path == _HISTORY:
                self.history.append(im.parse_history(body))
            else:
                for chat in im.parse_chats(body):
                    self.chats[chat.peer_id] = chat
                self.chats_seen.set()
        except Exception as exc:  # noqa: BLE001 — 解析失败记下来，不打断同步
            self.errors.append(f"{path}：{type(exc).__name__}")


@dataclass
class OpenedChat:
    peer_id: str
    name: str | None
    fetched: int = 0  # 这次拉到的消息条数（含已入库的）
    new: int = 0
    pages: int = 0
    gap: bool = False  # 仍有没拉到的中间消息
    error: str | None = None


@dataclass
class SyncResult:
    conversations: list[xstore.XhsConversation] = field(default_factory=list)
    opened: list[OpenedChat] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # 有新消息但本轮没打开的会话
    new_messages: list[xstore.XhsMessage] = field(default_factory=list)
    revoked: int = 0
    read_requests: int = 0
    errors: list[str] = field(default_factory=list)


def pick_to_open(
    convs: list[xstore.XhsConversation], *, open_chats: bool, max_open: int
) -> tuple[list[xstore.XhsConversation], list[str]]:
    """要点开的会话，以及有新消息但本轮没打开的会话 ID。

    只看已关注的会话；陌生人、官方号、平台 AI 助手有新消息也不点开，不计入 skipped。
    """
    candidates = [
        c for c in convs
        if c.has_new and c.followed and not c.is_official and not c.is_ai_assistant
    ]  # fmt: skip
    to_open = candidates[:max_open] if open_chats else []
    opening = {c.peer_id for c in to_open}
    return to_open, [c.peer_id for c in candidates if c.peer_id not in opening]


async def _ensure_ok(page: Page) -> None:
    if await detect_block(page):
        raise HumanRequired("触发小红书验证或风控", freeze=True)


async def click_conversation(page: Page, peer_id: str, name: str | None) -> bool:
    items = page.locator(CONV_ITEM)
    count = await items.count()
    for i in range(count):
        conv_id = await items.nth(i).get_attribute("data-conv-id") or ""
        if peer_id in conv_id:
            await items.nth(i).click()
            return True
    if name:  # 退回按昵称匹配，只接受唯一命中
        matches = [
            i
            for i in range(count)
            if (await items.nth(i).locator(CONV_NAME).inner_text()).strip() == name
        ]
        if len(matches) == 1:
            await items.nth(matches[0]).click()
            return True
    return False


async def _wait_history(collector: Collector, since: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if len(collector.history) > since:
            return True
        await asyncio.sleep(0.2)
    return False


async def _open_and_fetch(
    s: BrowserSession,
    collector: Collector,
    conv: xstore.XhsConversation,
    *,
    max_pages: int,
    run: RunContext,
) -> tuple[OpenedChat, list[im.XhsMessage]]:
    opened = OpenedChat(peer_id=conv.peer_id, name=conv.name)
    before = len(collector.history)
    await s.pause()
    if not await click_conversation(s.page, conv.peer_id, conv.name):
        opened.error = "会话列表里找不到这个会话"
        return opened, []
    run.audit("xiaohongshu.open_chat", peer=conv.peer_id)  # 点进会话 = 平台标已读
    if not await _wait_history(collector, before, 12):
        opened.error = "没有等到消息接口"
        return opened, []
    await _ensure_ok(s.page)

    messages: list[im.XhsMessage] = []
    while True:
        batch = [
            m
            for page in collector.history[before:]
            for m in page
            if m.peer_of(collector.my_id) == conv.peer_id
        ]
        messages = batch
        opened.pages = len(collector.history) - before
        oldest = min((m.store_id for m in batch), default=None)
        synced = conv.synced_store_id
        opened.gap = synced is not None and oldest is not None and oldest > synced + 1
        if not opened.gap or opened.pages >= max_pages:
            break
        # 本地进度和这页最早一条之间还有消息：把消息列表滚到顶，加载更早一页
        count = len(collector.history)
        await s.page.locator(MSG_LIST).evaluate("el => { el.scrollTop = 0; }")
        await s.pause(1.0, 2.0)
        if not await _wait_history(collector, count, 6):
            break
        await _ensure_ok(s.page)
    opened.fetched = len({m.msg_id for m in messages})
    return opened, messages


async def sync(
    cfg: Config,
    run: RunContext,
    *,
    open_chats: bool = True,
    max_open: int = 5,
    max_pages: int = 3,
    wait_s: float = 12,
) -> SyncResult:
    async with BrowserSession(PLATFORM, cfg.browser, headless=False) as s:
        return await sync_in_session(
            s, run, open_chats=open_chats, max_open=max_open, max_pages=max_pages, wait_s=wait_s
        )


async def sync_in_session(
    s: BrowserSession,
    run: RunContext,
    *,
    open_chats: bool = True,
    max_open: int = 5,
    max_pages: int = 3,
    wait_s: float = 12,
) -> SyncResult:
    collector = Collector()
    collector.attach(s.page)
    result = SyncResult()
    try:
        await s.page.goto(CHAT_URL, wait_until="domcontentloaded")
        await s.page.wait_for_timeout(2000)
        await _ensure_ok(s.page)
        if not await logged_in(s.page):
            raise HumanRequired("小红书未登录", "请先运行 xiaohongshu login")
        try:
            await asyncio.wait_for(collector.chats_seen.wait(), timeout=wait_s)
        except TimeoutError:
            raise HumanRequired("没有拦截到会话列表接口", "页面或接口可能改版") from None
        await s.pause(1.5, 2.5)  # 等未读数、自己的 ID 等附带接口
        await _ensure_ok(s.page)
        if collector.my_id is None:
            raise HumanRequired("没有拿到自己的用户 ID", "user/me 接口可能改版")

        result.conversations = xstore.apply_chats(collector.chats.values(), collector.unread)
        to_open, result.skipped = pick_to_open(
            result.conversations, open_chats=open_chats, max_open=max_open
        )

        for conv in to_open:
            opened, messages = await _open_and_fetch(
                s, collector, conv, max_pages=max_pages, run=run
            )
            applied = xstore.apply_messages(messages, collector.my_id, run_id=run.id)
            if opened.error is None:
                xstore.mark_opened(conv.peer_id)
            opened.new = len(applied.new_messages)
            result.new_messages += applied.new_messages
            result.revoked += applied.revoked
            result.opened.append(opened)
        await s.pause(1.0, 2.0)
        await _ensure_ok(s.page)
    finally:
        collector.detach(s.page)

    result.read_requests = collector.read_requests
    result.errors = collector.errors + [
        f"{o.name or o.peer_id}：{o.error}" for o in result.opened if o.error
    ]
    result.conversations = [c for c in xstore.list_conversations() if c.peer_id in collector.chats]
    run.audit(
        "xiaohongshu.sync",
        conversations=len(result.conversations),
        opened=len(result.opened),
        skipped=len(result.skipped),
        new_messages=len(result.new_messages),
        revoked=result.revoked,
        read_requests=result.read_requests,
        gaps=sum(o.gap for o in result.opened),
        errors=len(result.errors),
    )
    return result
