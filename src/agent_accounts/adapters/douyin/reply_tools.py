"""抖音分享补分析：使用本会话卡片，复用已有浏览器与摘要缓存。"""

from collections.abc import Callable

from playwright.async_api import Error as BrowserError
from sqlmodel import col, select

from agent_accounts.adapters.douyin import digest as ddigest
from agent_accounts.adapters.douyin import store as dstore
from agent_accounts.adapters.douyin.page import ensure_not_blocked, login_state
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core import digests, store
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.reply_tools import ToolError, share_detail
from agent_accounts.core.run import RunContext


class DouyinShareAnalyzer:
    def __init__(self, cfg: Config, conv_id: str, session: BrowserSession | None, run: RunContext):
        self.cfg, self.conv_id, self.session, self.run = cfg, conv_id, session, run

    @property
    def available(self) -> bool:
        return self.session is not None

    def _message(self, item_id: str) -> dstore.DouyinMessage:
        with store.session() as db:
            msg = db.exec(
                select(dstore.DouyinMessage)
                .where(
                    dstore.DouyinMessage.conv_id == self.conv_id,
                    dstore.DouyinMessage.aweme_id == item_id,
                    col(dstore.DouyinMessage.type).in_(["video_share", "note_share"]),
                )
                .order_by(col(dstore.DouyinMessage.msg_index).desc())
            ).first()
        if msg is None:
            raise ToolError("当前会话没有这条有效分享")
        return msg

    def cached(self, item_id: str) -> str | None:
        self._message(item_id)
        return share_detail("douyin", item_id) if digests.get("douyin", item_id) else None

    async def _check(self) -> None:
        assert self.session is not None
        await ensure_not_blocked(self.session.page)
        if not (await login_state(self.session)).logged_in:
            raise HumanRequired("分享补分析期间抖音登录失效", freeze=True)

    async def analyze(self, item_id: str, on_model: Callable[[], None]) -> str:
        msg = self._message(item_id)
        if self.session is None:
            raise ToolError("当前没有浏览器，分享补分析暂不可用")
        try:
            await self._check()
            out = await ddigest._digest_one(
                self.session,
                self.cfg,
                self.run,
                item_id,
                "note" if msg.type == "note_share" else "video",
                message=msg,
                on_model=on_model,
            )
            await self._check()
        except BrowserError as exc:
            raise ToolError(f"分享补分析失败（{type(exc).__name__}）") from None
        if out.error or out.digest is None:
            raise ToolError("分享补分析未成功，请根据已有信息决定")
        return share_detail("douyin", item_id)
