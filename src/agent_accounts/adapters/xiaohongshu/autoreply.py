"""自动回复（xiaohongshu run / decide / reply）。

一轮 tick：sync（只点开已关注、有新消息的会话）→ 找出对方的新消息 → 关系类护栏先过滤（不花钱）
→ 分析新分享的笔记 → 回复决策 → 完整护栏 → dry_run 记录或发送 → 推进 handled_store_id。

- 只处理已关注的会话；陌生人的消息和关注请求一概不管（用户决定，2026-09-27）
- 首次见到的会话只记基线（handled_store_id = 已入库的最后一条），不回复旧消息
- 同一会话连续多条新消息合并成一次决策；上下文保证包含全部新消息
- 新消息里的笔记本轮没轮到分析（超出 digest_per_tick）时整个会话留到下一轮，
  不在没看笔记的情况下回复；分析失败则照常决策，并告诉模型这篇没看成
- 平台自动发的「已相互关注」开场语和系统提示按 system 处理，只有它们时不回复
- 真正发送需要 [xiaohongshu] auto_reply = "on" 且调用方显式允许（--allow-send）；否则一律 dry_run
- [reply_tools] enabled 时回复模型可以先调用只读工具（翻更早的消息、搜索、看笔记详情、看对方关系）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_accounts.adapters.xiaohongshu import PLATFORM
from agent_accounts.adapters.xiaohongshu import digest as xdigest
from agent_accounts.adapters.xiaohongshu import store as xstore
from agent_accounts.adapters.xiaohongshu.reply_tools import XhsImageViewer
from agent_accounts.adapters.xiaohongshu.sender import SendError, open_conversation, send_messages
from agent_accounts.adapters.xiaohongshu.sync import sync_in_session
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core import digests, guard, persona
from agent_accounts.core.config import Config
from agent_accounts.core.reply import ChatLine, ReplyError, decide, split_messages
from agent_accounts.core.reply_tools import MediaBudget, RefBook, ReplyToolbox
from agent_accounts.core.run import RunContext

_GUARD_TYPES = {"system": "system", "other": "unsupported"}


@dataclass
class ConvOutcome:
    peer_id: str
    name: str | None
    # baseline / no_new / blocked / skipped / dry_run / sent / failed / partial / deferred / error
    action: str
    reply: xstore.XhsReply | None = None
    detail: str = ""


@dataclass
class TickResult:
    mode: str
    outcomes: list[ConvOutcome] = field(default_factory=list)
    digested: int = 0
    opened: int = 0
    image_attempts: int = 0


def effective_mode(cfg: Config, *, dry_run: bool, allow_send: bool) -> str:
    """两道确认：auto_reply = "on" 且 allow_send；dry_run=True 可强制不发送。"""
    mode = cfg.xiaohongshu.auto_reply
    if mode == "on" and (dry_run or not allow_send):
        return "dry_run"
    return mode


def eligible(conv: xstore.XhsConversation) -> bool:
    return conv.followed and not conv.is_official and not conv.is_ai_assistant


# ---- 聊天记录渲染 ----


def _note_line(m: xstore.XhsMessage, failed: set[str], refs: RefBook | None = None) -> str:
    kind = "视频笔记" if m.note_type == "video" else "图文笔记"
    title = " ".join((m.note_title or "").split())[:80]
    ref = f" {refs.share(m.note_id)}" if refs is not None and m.note_id else ""
    line = f"[分享{kind}{ref}] {title}（作者 {m.note_author or '未知'}）"
    d = digests.get(PLATFORM, m.note_id) if m.note_id else None
    if d is None:
        why = "内容分析失败，没看成" if m.note_id in failed else "内容还没有分析"
        return f"{line} —— {why}"
    hooks = "；".join(d.reply_hooks)
    unavailable = "（笔记当前不可看，只根据卡片理解）" if not d.available else ""
    return f"{line}{unavailable} —— 笔记摘要：{d.summary} 氛围：{d.vibe} 可以聊的点：{hooks}"


def _content(m: xstore.XhsMessage, failed: set[str], refs: RefBook | None = None) -> str:
    if m.revoked:
        return "[已撤回]"
    if m.type == "note":
        return _note_line(m, failed, refs)
    if m.type == "image":
        if refs is not None and refs.images_enabled and not m.from_me:
            return f"[图片 {refs.image(m.msg_id)}]（可用 view_image 查看）"
        return "[图片]（图片内容没有分析）"
    return m.preview(max_len=200)


def _line(
    m: xstore.XhsMessage, new_ids: set[str], failed: set[str], refs: RefBook | None
) -> ChatLine:
    return ChatLine(m.from_me, m.sent_at, _content(m, failed, refs), is_new=m.msg_id in new_ids)


def chat_lines(
    peer_id: str,
    new_ids: set[str],
    limit: int,
    failed: set[str] = frozenset(),
    refs: RefBook | None = None,
) -> list[ChatLine]:
    return [_line(m, new_ids, failed, refs) for m in xstore.list_messages(peer_id, limit=limit)]


def _fmt(dt: datetime | None) -> str:
    if dt is None:
        return "未知"
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone().strftime("%Y-%m-%d %H:%M")


class XhsChatSource:
    """回复模型工具用的本会话查询（只读本地库）。"""

    platform = PLATFORM

    def __init__(self, conv: xstore.XhsConversation, refs: RefBook, oldest_store_id: int | None):
        self.conv = conv
        self.refs = refs
        self._cursor = oldest_store_id  # 已经给模型看过的最早一条

    def older(self, limit: int) -> list[ChatLine]:
        if self._cursor is None:
            return []
        msgs = xstore.list_messages(self.conv.peer_id, limit=limit, before_store_id=self._cursor)
        if msgs:
            self._cursor = msgs[0].store_id
        return [_line(m, set(), set(), self.refs) for m in msgs]

    def search(self, query: str, limit: int) -> list[ChatLine]:
        msgs = xstore.search_messages(self.conv.peer_id, query, limit=limit)
        return [_line(m, set(), set(), self.refs) for m in msgs]

    def peer_info(self) -> dict[str, Any]:
        n, first = xstore.message_stats(self.conv.peer_id)
        mine = [m for m in xstore.list_messages(self.conv.peer_id, limit=200) if m.from_me]
        return {
            "昵称": self.conv.name or "未知",
            "关系": "互相关注" if self.conv.followed else "未确认互相关注",
            "本地记录里的消息数": n,
            "本地记录最早一条的时间": _fmt(first),
            "你最近几次发消息的时间": "、".join(_fmt(m.sent_at) for m in mine[-3:]) or "没有",
        }


def new_peer_messages(conv: xstore.XhsConversation) -> list[xstore.XhsMessage]:
    msgs = xstore.list_messages(conv.peer_id, limit=500)
    return [m for m in msgs if not m.from_me and m.store_id > (conv.handled_store_id or 0)]


# ---- 护栏 ----


def guard_input(
    cfg: Config,
    conv: xstore.XhsConversation,
    *,
    text: str,
    should_reply: bool,
    confidence: float | None,
    trigger_types: list[str],
    manual: bool,
    now: datetime,
) -> guard.GuardInput:
    day = xstore.sent_replies_since(now - timedelta(days=1))
    hour = [r for r in day if r.sent_at and r.sent_at >= now - timedelta(hours=1)]
    in_conv = [r.sent_at for r in day if r.peer_id == conv.peer_id and r.sent_at]
    return guard.GuardInput(
        mode=cfg.xiaohongshu.auto_reply,
        conv_id=conv.peer_id,
        conv_name=conv.name,
        conv_kind="private",
        is_mutual=conv.followed,
        trigger_types=[_GUARD_TYPES.get(t, t) for t in trigger_types],
        should_reply=should_reply,
        text=text,
        confidence=confidence,
        last_sent_in_conv=max(in_conv, default=None),
        sent_last_hour=len(hour),
        sent_last_day=len(day),
        now=now,
        manual=manual,
    )


# ---- 发送 ----


async def send_in_session(
    s: BrowserSession, run: RunContext, conv: xstore.XhsConversation, reply: xstore.XhsReply
) -> xstore.XhsReply:
    """依次发送 reply.text 的各条消息（一行一条），并更新记录。只发出一部分记为 partial，不补发。"""
    messages = split_messages(reply.text or "")
    try:
        await open_conversation(s, conv.peer_id, conv.name)
        result = await send_messages(s, messages)
    except SendError as e:
        reply.status, reply.error = "failed", str(e)
        run.alert("warning", f"小红书发送失败（{conv.name or conv.peer_id}）：{e}")
        await s.snapshot(run.dir, f"send-failed-{reply.id or 'new'}")
    else:
        reply.status = "sent" if result.ok else ("partial" if result.sent else "failed")
        reply.error = None if result.ok else result.detail
        reply.sent_at = datetime.now(UTC) if result.sent else None
        run.audit(
            "xiaohongshu.send",
            peer=conv.peer_id,
            chars=[len(m) for m in messages],
            ok=result.ok,
            sent=result.sent,
            total=result.total,
            detail=result.detail,
            retried=any(r.retried for r in result.results),
        )
        if not result.ok:
            run.alert(
                "warning", f"小红书发送未确认（{conv.name or conv.peer_id}）：{result.detail}"
            )
            await s.snapshot(run.dir, "send-unconfirmed")
    return xstore.save_reply(reply)


async def manual_reply(
    cfg: Config, run: RunContext, conv: xstore.XhsConversation, text: str
) -> xstore.XhsReply:
    now = datetime.now(UTC)
    g = guard_input(
        cfg, conv, text=text, should_reply=True, confidence=None, trigger_types=[], manual=True,
        now=now,
    )  # fmt: skip
    check = guard.check(cfg.guard, g)
    reply = xstore.XhsReply(
        peer_id=conv.peer_id,
        source="manual",
        should_reply=True,
        text=text,
        reason="人工发送",
        guard_reasons=json.dumps(check.reasons, ensure_ascii=False),
        status="blocked" if not check.ok else "pending",
        run_id=run.id,
    )
    if not check.ok:
        run.audit("xiaohongshu.reply.blocked", peer=conv.peer_id, reasons=check.reasons)
        return xstore.save_reply(reply)
    async with BrowserSession(PLATFORM, cfg.browser, headless=False) as s:
        return await send_in_session(s, run, conv, reply)


# ---- 决策 ----


async def _decide_and_act(
    s: BrowserSession | None,
    cfg: Config,
    run: RunContext,
    conv: xstore.XhsConversation,
    new_msgs: list[xstore.XhsMessage],
    mode: str,
    source: str,
    failed: set[str] = frozenset(),
    use_tools: bool = False,
    media_budget: MediaBudget | None = None,
) -> ConvOutcome:
    now = datetime.now(UTC)
    trigger_types = [m.type for m in new_msgs]
    base = dict(
        peer_id=conv.peer_id,
        trigger_msg_ids=json.dumps([m.msg_id for m in new_msgs]),
        trigger_last_store_id=max(m.store_id for m in new_msgs),
        source=source,
        run_id=run.id,
    )

    def gi(text: str, should_reply: bool, confidence: float | None) -> guard.GuardInput:
        return guard_input(
            cfg, conv, text=text, should_reply=should_reply, confidence=confidence,
            trigger_types=trigger_types, manual=False, now=now,
        )  # fmt: skip

    # 1) 不需要模型就能判断的护栏先过（不花钱）
    pre = guard.check(cfg.guard, gi("", False, None))
    if not pre.ok:
        reasons = json.dumps(pre.reasons, ensure_ascii=False)
        reply = xstore.save_reply(xstore.XhsReply(**base, status="blocked", guard_reasons=reasons))
        return ConvOutcome(conv.peer_id, conv.name, "blocked", reply, "；".join(pre.reasons))

    # 2) 回复决策：上下文至少包含全部新消息（可选：先调用只读工具）
    new_ids = {m.msg_id for m in new_msgs}
    limit = max(cfg.xiaohongshu.context_messages, len(new_msgs) + 10)
    context = xstore.list_messages(conv.peer_id, limit=limit)
    images_enabled = "view_image" in cfg.reply_tools.paid
    refs = RefBook(images_enabled=images_enabled) if use_tools else None
    lines = [_line(m, new_ids, failed, refs) for m in context]
    toolbox = None
    if refs is not None:
        oldest = context[0].store_id if context else None
        toolbox = ReplyToolbox(
            XhsChatSource(conv, refs, oldest),
            refs,
            conv.peer_id,
            max_rounds=cfg.reply_tools.max_rounds,
            audit=run.audit,
            image_viewer=XhsImageViewer(cfg, conv.peer_id, s) if images_enabled else None,
            max_paid_calls=cfg.reply_tools.max_paid_calls,
            media_budget=media_budget,
        )
    try:
        d = await decide(
            cfg.llm.reply,
            persona.load(),
            lines,
            recent=persona.load_recent(),
            platform="xiaohongshu",
            tools=toolbox,
        )
    except ReplyError as e:
        run.audit("xiaohongshu.reply.error", peer=conv.peer_id, error=str(e))
        return ConvOutcome(conv.peer_id, conv.name, "error", None, str(e))

    common = dict(
        **base,
        should_reply=d.should_reply,
        text=d.text or None,
        reason=d.reason,
        confidence=d.confidence,
        model=cfg.llm.reply.model,
        tool_calls_json=toolbox.calls_json() if toolbox else "[]",
    )
    if not d.should_reply:
        reply = xstore.save_reply(xstore.XhsReply(**common, status="skipped"))
        return ConvOutcome(conv.peer_id, conv.name, "skipped", reply, d.reason)

    # 3) 完整护栏（内容、把握、频率）
    full = guard.check(cfg.guard, gi(d.text, True, d.confidence))
    reasons = json.dumps(full.reasons, ensure_ascii=False)
    if not full.ok:
        reply = xstore.save_reply(
            xstore.XhsReply(**common, status="blocked", guard_reasons=reasons)
        )
        return ConvOutcome(conv.peer_id, conv.name, "blocked", reply, "；".join(full.reasons))
    if mode != "on" or s is None:
        reply = xstore.save_reply(
            xstore.XhsReply(**common, status="dry_run", guard_reasons=reasons)
        )
        return ConvOutcome(conv.peer_id, conv.name, "dry_run", reply, d.text)

    # 4) 发送
    reply = await send_in_session(s, run, conv, xstore.XhsReply(**common, status="pending"))
    return ConvOutcome(conv.peer_id, conv.name, reply.status, reply, reply.error or d.text)


def _missing_notes(msgs: list[xstore.XhsMessage]) -> list[str]:
    ids: list[str] = []
    for m in msgs:
        if m.note_id and m.note_id not in ids and digests.get(PLATFORM, m.note_id) is None:
            ids.append(m.note_id)
    return ids


async def run_once(
    cfg: Config, run: RunContext, *, dry_run: bool = False, allow_send: bool = False
) -> TickResult:
    mode = effective_mode(cfg, dry_run=dry_run, allow_send=allow_send)
    result = TickResult(mode=mode)
    async with BrowserSession(PLATFORM, cfg.browser, headless=False) as s:
        synced = await sync_in_session(s, run, max_open=cfg.xiaohongshu.max_open)
        result.opened = len(synced.opened)
        budget = MediaBudget(cfg.xiaohongshu.digest_per_tick)
        for conv in xstore.list_conversations():
            if not eligible(conv):
                continue
            if conv.handled_store_id is None:  # 首次见到：只记基线
                if conv.synced_store_id is not None:
                    xstore.set_handled(conv.peer_id, conv.synced_store_id)
                    result.outcomes.append(ConvOutcome(conv.peer_id, conv.name, "baseline"))
                continue
            new_msgs = new_peer_messages(conv)
            if not new_msgs:
                continue

            missing = _missing_notes(new_msgs)
            todo, left = missing[: max(budget.remaining, 0)], missing[max(budget.remaining, 0) :]
            failed: set[str] = set()
            if todo:
                outcomes = await xdigest.digest_in_session(s, cfg, run, todo)
                failed = {o.note_id for o in outcomes if o.error}
                budget.remaining -= len(todo)
                result.digested += len(todo)
            if left:  # 这一轮分析不完：整个会话留到下一轮，不在没看笔记的情况下回复
                result.outcomes.append(
                    ConvOutcome(
                        conv.peer_id,
                        conv.name,
                        "deferred",
                        None,
                        f"还有 {len(left)} 篇笔记没分析，下一轮再决定",
                    )  # fmt: skip
                )
                continue

            before_images = budget.remaining
            outcome = await _decide_and_act(
                s,
                cfg,
                run,
                conv,
                new_msgs,
                mode,
                "auto",
                failed,
                cfg.reply_tools.enabled,
                media_budget=budget,
            )
            result.image_attempts += before_images - budget.remaining
            if outcome.action != "error":  # 模型出错时不推进，下一轮重试
                xstore.set_handled(conv.peer_id, max(m.store_id for m in new_msgs))
            result.outcomes.append(outcome)
    run.audit(
        "xiaohongshu.run",
        mode=mode,
        opened=result.opened,
        outcomes=[{"peer": o.peer_id, "action": o.action} for o in result.outcomes],
        digested=result.digested,
        image_attempts=result.image_attempts,
    )
    return result


async def decide_for(
    cfg: Config,
    run: RunContext,
    conv: xstore.XhsConversation,
    last: int,
    *,
    tools: bool | None = None,
) -> ConvOutcome:
    """试运行：把对方最近 last 条消息当作新消息做一次决策，只记录不发送，不改 handled_store_id。

    tools 为 None 时跟随 [reply_tools] enabled。
    """
    use_tools = cfg.reply_tools.enabled if tools is None else tools
    msgs = xstore.list_messages(conv.peer_id, limit=500)
    peer = [m for m in msgs if not m.from_me][-last:]
    if not peer:
        return ConvOutcome(conv.peer_id, conv.name, "no_new", None, "没有对方的消息")
    return await _decide_and_act(
        None, cfg, run, conv, peer, "dry_run", "decide", use_tools=use_tools
    )
