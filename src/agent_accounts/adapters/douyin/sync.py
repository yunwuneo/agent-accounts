"""``douyin sync``：打开首页，拦截私信接口，解析后入库。

不点进任何会话，因此不会触发 mark_read（把对方消息标为已读）。同时监听 mark_read 请求，
一旦出现就在结果里报告，作为「读取无副作用」的持续校验。

接口数据没拦截到时退回 DOM：打开私信面板读会话列表（只有会话，没有消息，也不入库）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Page, Request, Response

from agent_accounts.adapters.douyin import PLATFORM, im
from agent_accounts.adapters.douyin import selectors as sel
from agent_accounts.adapters.douyin import store as dstore
from agent_accounts.adapters.douyin.page import ensure_not_blocked, login_state, open_home
from agent_accounts.browser.locate import locate
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core import paths
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.pb import PbError
from agent_accounts.core.run import RunContext

IMAPI_HOST = "imapi.douyin.com"
USER_INFO_PATH = "/aweme/v1/web/im/user/info/"
MARK_READ_PATH = "/conversation/mark_read"


class ImCollector:
    def __init__(self) -> None:
        self.bodies: list[tuple[str, bytes]] = []
        self.user_info: list[dict[str, Any]] = []
        self.mark_read_requests = 0
        self.init_seen = asyncio.Event()

    def attach(self, page: Page) -> None:
        page.on("request", self._on_request)
        page.on("response", self._on_response)

    def _on_request(self, request: Request) -> None:
        if MARK_READ_PATH in request.url:
            self.mark_read_requests += 1

    async def _on_response(self, response: Response) -> None:
        url = response.url
        try:
            if IMAPI_HOST in url and "protobuf" in response.headers.get("content-type", ""):
                body = await response.body()
                self.bodies.append((url.split("?")[0], body))
                if "get_message_by_init" in url:
                    self.init_seen.set()
            elif USER_INFO_PATH in url:
                self.user_info.append(await response.json())
        except Exception:  # 页面关闭后 body 可能不可读
            pass


@dataclass
class DomConversation:
    name: str
    preview: str
    time_text: str
    unread: int


@dataclass
class SyncResult:
    source: str  # "api" 或 "dom"
    new_messages: list[dstore.DouyinMessage] = field(default_factory=list)
    conversations: list[dstore.DouyinConversation] = field(default_factory=list)
    dom_conversations: list[DomConversation] = field(default_factory=list)
    responses: int = 0
    skipped_commands: int = 0
    errors: list[str] = field(default_factory=list)
    mark_read_requests: int = 0


_DOM_ROWS_JS = """
els => els.map(el => {
    const q = s => el.querySelector(s);
    const txt = s => (q(s)?.innerText || '').trim();
    return {
        name: txt('[class*="ConversationItemtitle"]'),
        time_text: txt('[class*="timeStr"]'),
        preview: txt('pre'),
        unread: parseInt(q('[x-semi-prop="count"]')?.innerText || '0', 10) || 0,
    };
})
"""


async def _read_dom_inbox(s: BrowserSession) -> list[DomConversation]:
    entry = await locate(s.page, sel.MESSAGES_ENTRY)
    if entry is None:
        return []
    await s.op(lambda _p: entry.first.click())
    await s.pause(2.0, 3.0)
    await ensure_not_blocked(s.page)
    rows = await locate(s.page, sel.CONVERSATION_ROW)
    if rows is None:
        return []
    data = await rows.locator.evaluate_all(_DOM_ROWS_JS)
    return [DomConversation(**d) for d in data]


async def sync(
    cfg: Config, run: RunContext, *, wait_s: float = 15, save_raw: bool = False
) -> SyncResult:
    collector = ImCollector()
    async with BrowserSession(PLATFORM, cfg.browser) as s:
        collector.attach(s.page)
        await open_home(s, cfg.douyin.base_url)
        await ensure_not_blocked(s.page)
        if not (await login_state(s)).logged_in:
            raise HumanRequired("未登录", "请先运行 douyin login")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(collector.init_seen.wait(), timeout=wait_s)
        await s.pause(1.5, 2.5)  # 等昵称等附带接口
        await ensure_not_blocked(s.page)

        if not collector.bodies:
            run.alert("warning", "没有拦截到私信接口数据，退回 DOM，仅读取会话列表")
            result = SyncResult(source="dom", dom_conversations=await _read_dom_inbox(s))
            result.mark_read_requests = collector.mark_read_requests
            run.audit("douyin.sync", source="dom", conversations=len(result.dom_conversations))
            return result

    if save_raw:
        raw_dir = paths.ensure_dir(run.dir / "raw")
        for i, (url, body) in enumerate(collector.bodies):
            (raw_dir / f"{i:03d}-{url.rsplit('/', 1)[-1]}.pb").write_bytes(body)
        (raw_dir / "user_info.json").write_text(json.dumps(collector.user_info, ensure_ascii=False))

    result = SyncResult(source="api", responses=len(collector.bodies))
    batches = []
    for url, body in collector.bodies:
        try:
            batch = im.parse_response(body)
        except PbError as e:
            result.errors.append(f"{url.rsplit('/', 1)[-1]}：{e}")
            continue
        batches.append(batch)
        result.skipped_commands += batch.skipped_commands
        result.errors.extend(batch.errors)
    users = [u for data in collector.user_info for u in im.parse_user_info(data)]
    applied = dstore.apply(batches, users, run_id=run.id)
    result.new_messages = applied.new_messages
    result.conversations = applied.conversations
    result.mark_read_requests = collector.mark_read_requests

    if result.mark_read_requests:
        run.alert("warning", f"sync 期间出现了 {result.mark_read_requests} 次 mark_read 请求")
    run.audit(
        "douyin.sync",
        source="api",
        responses=result.responses,
        conversations=len(result.conversations),
        new_messages=len(result.new_messages),
        errors=len(result.errors),
        mark_read_requests=result.mark_read_requests,
    )
    return result
