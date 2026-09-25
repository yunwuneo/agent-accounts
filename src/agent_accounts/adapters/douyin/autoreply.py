"""自动回复（douyin run / douyin decide）。

一轮 tick：sync → 找出对方的新消息 → 关系类护栏先过滤（不花钱）→ 必要时分析分享的作品 →
回复决策 → 完整护栏 → dry_run 记录或发送 → 更新 handled_index。

- 首次见到的会话只记基线（handled_index = 最后一条），不回复旧消息
- 同一会话连续多条新消息合并成一次决策
- 真正发送需要 auto_reply = "on" 且调用方显式允许（M3 的 --allow-send）；否则一律 dry_run
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agent_accounts.adapters.douyin import digest as ddigest
from agent_accounts.adapters.douyin import store as dstore
from agent_accounts.adapters.douyin.page import open_home
from agent_accounts.adapters.douyin.replying import guard_input, send_in_session
from agent_accounts.adapters.douyin.sync import sync_in_session
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core import digests, guard, persona
from agent_accounts.core.config import Config
from agent_accounts.core.reply import ChatLine, ReplyError, decide
from agent_accounts.core.run import RunContext

PLATFORM = "douyin"


@dataclass
class ConvOutcome:
    conv_id: str
    name: str | None
    action: str  # baseline / no_new / blocked / skipped / dry_run / sent / failed / error
    reply: dstore.DouyinReply | None = None
    detail: str = ""


@dataclass
class TickResult:
    mode: str
    outcomes: list[ConvOutcome] = field(default_factory=list)
    digested: int = 0


def _share_line(m: dstore.DouyinMessage) -> str:
    kind = "视频" if m.type == "video_share" else "图集"
    title = " ".join((m.share_title or "").split())[:80]
    line = f"[分享{kind}] {title}（作者 {m.share_author or '未知'}）"
    d = digests.get(PLATFORM, m.aweme_id) if m.aweme_id else None
    if d is None:
        return line + " —— 作品内容还没有分析"
    hooks = "；".join(d.reply_hooks)
    return f"{line} —— 作品摘要：{d.summary} 氛围：{d.vibe} 可以聊的点：{hooks}"


def chat_lines(conv_id: str, new_ids: set[str], limit: int) -> list[ChatLine]:
    lines = []
    for m in dstore.list_messages(conv_id, limit=limit):
        content = _share_line(m) if m.aweme_id else m.preview(max_len=200)
        lines.append(ChatLine(m.from_me, m.sent_at, content, is_new=m.msg_id in new_ids))
    return lines


def new_peer_messages(conv: dstore.DouyinConversation) -> list[dstore.DouyinMessage]:
    msgs = dstore.list_messages(conv.conv_id, limit=200)
    return [m for m in msgs if not m.from_me and m.msg_index > (conv.handled_index or 0)]


def effective_mode(cfg: Config, *, dry_run: bool, allow_send: bool) -> str:
    mode = cfg.douyin.auto_reply
    if mode == "on" and (dry_run or not allow_send):
        return "dry_run"
    return mode


async def _decide_and_act(
    s: BrowserSession | None,
    cfg: Config,
    run: RunContext,
    conv: dstore.DouyinConversation,
    new_msgs: list[dstore.DouyinMessage],
    mode: str,
    source: str,
) -> ConvOutcome:
    now = datetime.now(UTC)
    trigger_types = [m.type for m in new_msgs]
    base = dict(
        conv_id=conv.conv_id,
        trigger_msg_ids=json.dumps([m.msg_id for m in new_msgs]),
        trigger_last_index=max(m.msg_index for m in new_msgs),
        source=source,
        run_id=run.id,
    )

    # 1) 不需要模型就能判断的护栏先过（不花钱）
    pre = guard.check(
        cfg.guard,
        guard_input(
            cfg,
            conv,
            text="",
            should_reply=False,
            confidence=None,
            trigger_types=trigger_types,
            manual=False,
            now=now,
        ),  # fmt: skip
    )
    if not pre.ok:
        reply = dstore.save_reply(
            dstore.DouyinReply(
                **base, status="blocked", guard_reasons=json.dumps(pre.reasons, ensure_ascii=False)
            )
        )
        return ConvOutcome(conv.conv_id, conv.name, "blocked", reply, "；".join(pre.reasons))

    # 2) 回复决策
    new_ids = {m.msg_id for m in new_msgs}
    try:
        d = await decide(
            cfg.llm.reply,
            persona.load(),
            chat_lines(conv.conv_id, new_ids, cfg.douyin.context_messages),
        )
    except ReplyError as e:
        run.audit("douyin.reply.error", conv_id=conv.conv_id, error=str(e))
        return ConvOutcome(conv.conv_id, conv.name, "error", None, str(e))

    common = dict(
        **base,
        should_reply=d.should_reply,
        text=d.text or None,
        reason=d.reason,
        confidence=d.confidence,
        model=cfg.llm.reply.model,
    )
    if not d.should_reply:
        reply = dstore.save_reply(dstore.DouyinReply(**common, status="skipped"))
        return ConvOutcome(conv.conv_id, conv.name, "skipped", reply, d.reason)

    # 3) 完整护栏（内容、把握、频率）
    full = guard.check(
        cfg.guard,
        guard_input(
            cfg,
            conv,
            text=d.text,
            should_reply=True,
            confidence=d.confidence,
            trigger_types=trigger_types,
            manual=False,
            now=now,
        ),  # fmt: skip
    )
    reasons = json.dumps(full.reasons, ensure_ascii=False)
    if not full.ok:
        reply = dstore.save_reply(
            dstore.DouyinReply(**common, status="blocked", guard_reasons=reasons)
        )
        return ConvOutcome(conv.conv_id, conv.name, "blocked", reply, "；".join(full.reasons))
    if mode != "on" or s is None:
        reply = dstore.save_reply(
            dstore.DouyinReply(**common, status="dry_run", guard_reasons=reasons)
        )
        return ConvOutcome(conv.conv_id, conv.name, "dry_run", reply, d.text)

    # 4) 发送
    await open_home(s, cfg.douyin.base_url)
    reply = await send_in_session(s, run, conv, dstore.DouyinReply(**common, status="pending"))
    return ConvOutcome(conv.conv_id, conv.name, reply.status, reply, reply.error or d.text)


async def _ensure_digests(
    s: BrowserSession, cfg: Config, run: RunContext, msgs: list[dstore.DouyinMessage], budget: int
) -> int:
    ids = []
    for m in msgs:
        if m.aweme_id and m.aweme_id not in ids and digests.get(PLATFORM, m.aweme_id) is None:
            ids.append(m.aweme_id)
    ids = ids[:budget]
    if ids:
        await ddigest.digest_in_session(s, cfg, run, ids)
    return len(ids)


async def run_once(
    cfg: Config, run: RunContext, *, dry_run: bool = True, allow_send: bool = False
) -> TickResult:
    mode = effective_mode(cfg, dry_run=dry_run, allow_send=allow_send)
    result = TickResult(mode=mode)
    async with BrowserSession(PLATFORM, cfg.browser) as s:
        await sync_in_session(s, cfg, run)
        budget = cfg.douyin.digest_per_tick
        for conv in dstore.list_conversations():
            if conv.handled_index is None:  # 首次见到：只记基线
                if conv.last_index is not None:
                    dstore.set_handled(conv.conv_id, conv.last_index)
                result.outcomes.append(ConvOutcome(conv.conv_id, conv.name, "baseline"))
                continue
            new_msgs = new_peer_messages(conv)
            if not new_msgs:
                continue
            n = await _ensure_digests(s, cfg, run, new_msgs, budget)
            budget -= n
            result.digested += n
            outcome = await _decide_and_act(s, cfg, run, conv, new_msgs, mode, "auto")
            if outcome.action != "error":  # 模型出错时不推进，下一轮重试
                dstore.set_handled(conv.conv_id, max(m.msg_index for m in new_msgs))
            result.outcomes.append(outcome)
    run.audit(
        "douyin.run",
        mode=mode,
        outcomes=[{"conv_id": o.conv_id, "action": o.action} for o in result.outcomes],
        digested=result.digested,
    )
    return result


async def decide_for(
    cfg: Config, run: RunContext, conv: dstore.DouyinConversation, last: int
) -> ConvOutcome:
    """试运行：把对方最近 last 条消息当作新消息做一次决策，只记录不发送，不改 handled_index。"""
    peer = [m for m in dstore.list_messages(conv.conv_id, limit=200) if not m.from_me][-last:]
    if not peer:
        return ConvOutcome(conv.conv_id, conv.name, "no_new", None, "没有对方的消息")
    return await _decide_and_act(None, cfg, run, conv, peer, "dry_run", "decide")
