"""回复流程：频率统计、护栏检查、发送、记录。douyin reply 和 douyin run 共用。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from agent_accounts.adapters.douyin import PLATFORM
from agent_accounts.adapters.douyin import store as dstore
from agent_accounts.adapters.douyin.page import login_state, open_home
from agent_accounts.adapters.douyin.sender import SendError, open_conversation, send_text
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core import guard
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.run import RunContext


@dataclass
class RateStats:
    last_sent_in_conv: datetime | None
    sent_last_hour: int
    sent_last_day: int


def rate_stats(conv_id: str, now: datetime) -> RateStats:
    day = dstore.sent_replies_since(now - timedelta(days=1))
    hour = [r for r in day if r.sent_at and r.sent_at >= now - timedelta(hours=1)]
    in_conv = [r.sent_at for r in day if r.conv_id == conv_id and r.sent_at]
    return RateStats(max(in_conv, default=None), len(hour), len(day))


def guard_input(
    cfg: Config,
    conv: dstore.DouyinConversation,
    *,
    text: str,
    should_reply: bool,
    confidence: float | None,
    trigger_types: list[str],
    manual: bool,
    now: datetime,
) -> guard.GuardInput:
    stats = rate_stats(conv.conv_id, now)
    return guard.GuardInput(
        mode=cfg.douyin.auto_reply,
        conv_id=conv.conv_id,
        conv_name=conv.name,
        conv_kind=conv.kind,
        is_mutual=conv.is_mutual,
        trigger_types=trigger_types,
        should_reply=should_reply,
        text=text,
        confidence=confidence,
        last_sent_in_conv=stats.last_sent_in_conv,
        sent_last_hour=stats.sent_last_hour,
        sent_last_day=stats.sent_last_day,
        now=now,
        manual=manual,
    )


async def send_in_session(
    s: BrowserSession, run: RunContext, conv: dstore.DouyinConversation, reply: dstore.DouyinReply
) -> dstore.DouyinReply:
    """在已登录的会话里发送 reply.text，并更新记录。"""
    if not conv.name:
        raise SendError("会话没有昵称，无法在列表里定位")
    try:
        await open_conversation(s, conv.name)
        result = await send_text(s, reply.text or "")
    except SendError as e:
        reply.status, reply.error = "failed", str(e)
        run.alert("warning", f"发送失败（{conv.name}）：{e}")
        await s.snapshot(run.dir, f"send-failed-{reply.id or 'new'}")
    else:
        reply.status = "sent" if result.ok else "failed"
        reply.error = None if result.ok else result.detail
        reply.sent_at = datetime.now(UTC) if result.ok else None
        run.audit(
            "douyin.send",
            conv_id=conv.conv_id,
            chars=len(reply.text or ""),
            ok=result.ok,
            detail=result.detail,
            retried=result.retried,
            imapi_requests=sorted(set(result.imapi_requests)),
        )
        if not result.ok:
            run.alert("warning", f"发送未确认（{conv.name}）：{result.detail}")
            await s.snapshot(run.dir, "send-unconfirmed")
    return dstore.save_reply(reply)


async def manual_reply(
    cfg: Config, run: RunContext, conv: dstore.DouyinConversation, text: str
) -> dstore.DouyinReply:
    now = datetime.now(UTC)
    g = guard_input(
        cfg, conv, text=text, should_reply=True, confidence=None, trigger_types=[], manual=True,
        now=now,
    )  # fmt: skip
    check = guard.check(cfg.guard, g)
    reply = dstore.DouyinReply(
        conv_id=conv.conv_id,
        source="manual",
        should_reply=True,
        text=text,
        reason="人工发送",
        guard_reasons=json.dumps(check.reasons, ensure_ascii=False),
        status="blocked" if not check.ok else "pending",
        run_id=run.id,
    )
    if not check.ok:
        run.audit("douyin.reply.blocked", conv_id=conv.conv_id, reasons=check.reasons)
        return dstore.save_reply(reply)

    async with BrowserSession(PLATFORM, cfg.browser) as s:
        await open_home(s, cfg.douyin.base_url)
        if not (await login_state(s)).logged_in:
            raise HumanRequired("未登录", "请先运行 douyin login")
        return await send_in_session(s, run, conv, reply)
