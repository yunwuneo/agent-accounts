"""有界、当前私聊文本自动回复。UI 序列仅在本次运行有效，不是平台消息 ID。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from agent_accounts.adapters.douyin import store as dstore
from agent_accounts.adapters.douyin.android import messaging
from agent_accounts.adapters.douyin.android.session import AndroidError, bounds, label, nodes, one
from agent_accounts.adapters.douyin.replying import rate_stats
from agent_accounts.core import config, guard, persona, reply, store
from agent_accounts.core.errors import HumanRequired


async def decide_once(endpoint, *args, **kwargs):
    # 一次预算对应一次网络请求；禁用 SDK 默认重试，不附带工具循环。
    async with reply.make_client(endpoint) as client:
        return await reply.decide(
            endpoint, *args, **kwargs, client=client.with_options(max_retries=0)
        )


def single_bubble(decision, max_messages):
    """保留模型文字，将允许数量内的短句合成一条；总长度仍交护栏检查。"""
    if decision.should_reply and 1 <= len(decision.messages) <= max_messages:
        decision = decision.model_copy(update={"messages": [" ".join(decision.messages)]})
    return decision


@dataclass(frozen=True)
class Message:
    from_me: bool
    kind: str
    text: str


def read_messages(root, expected: str, self_name: str) -> list[Message]:
    """同时核对头像标签、气泡方向与纵向位置；无法归属时拒绝推断。"""
    messaging.thread(root, expected)
    if not self_name or self_name == expected:
        raise AndroidError("必须提供不同的己方昵称和对方精确会话标题")
    header_bottom = bounds(one(root, "vw_"))[3]
    editor_top = bounds(one(root, "msg_et"))[1]
    avatars = nodes(root, "dwh")
    bubbles = nodes(root, "sws") + nodes(root, "sww")
    bubbles.sort(key=lambda n: bounds(n)[1])
    used = set()
    out = []
    parents = {child: parent for parent in root.iter() for child in parent}
    columns = {}

    def column(bubble):
        ancestor = bubble
        while ancestor in parents:
            ancestor = parents[ancestor]
            if ancestor.get("resource-id", "").endswith(":id/m60"):
                x1, _, x2, _ = bounds(ancestor)
                return x1, x2
        return None

    for bubble in bubbles:
        x1, y1, x2, y2 = bounds(bubble)
        if not (header_bottom < y1 < y2 <= editor_top):
            raise AndroidError("气泡被遮挡或越出聊天区域；请停留在消息底部")
        candidates = []
        for i, avatar in enumerate(avatars):
            ax1, ay1, ax2, ay2 = bounds(avatar)
            if max(y1, ay1) < min(y2, ay2) and (ax2 <= x1 or ax1 >= x2):
                candidates.append((i, avatar, ax1 >= x2))
        if not candidates and not out and bubble is bubbles[0]:
            ancestor = bubble
            while ancestor in parents:
                ancestor = parents[ancestor]
                if ancestor.get("scrollable") == "true":
                    break
            # 新消息让首条旧气泡的头像滚出屏幕；只排除贴着容器顶边的残片。
            if ancestor.get("scrollable") == "true" and y1 == bounds(ancestor)[1]:
                continue
        group_column = column(bubble)
        if not candidates and out and group_column is not None:
            # 连续同一发送方的后续行可隐藏头像；必须匹配已有头像行的容器列。
            owners = columns.get(group_column, set())
            if owners == {out[-1].from_me}:
                text = label(bubble)
                kind = "text" if bubble.get("resource-id", "").endswith(":id/sws") else "share"
                if not text.strip():
                    raise AndroidError("出现无法读取的连续消息")
                out.append(Message(out[-1].from_me, kind, text))
                continue
        if len(candidates) != 1 or candidates[0][0] in used:
            raise AndroidError("无法唯一确定气泡发件人")
        i, avatar, from_me = candidates[0]
        if label(avatar) != (self_name if from_me else expected) + "的头像":
            raise AndroidError("头像身份与确认的私聊不一致")
        used.add(i)
        if group_column is not None:
            columns.setdefault(group_column, set()).add(from_me)
        text = label(bubble)
        kind = "text" if bubble.get("resource-id", "").endswith(":id/sws") else "share"
        if not text.strip():
            raise AndroidError("出现无法读取的消息")
        out.append(Message(from_me, kind, text))
    if len(used) != len(avatars) or not out:
        raise AndroidError("存在未识别消息或当前没有可核对的聊天记录")
    return out


def appended(previous: list[Message], current: list[Message]) -> list[Message]:
    if previous == current:
        return []
    overlaps = [
        n for n in range(1, min(len(previous), len(current)) + 1) if previous[-n:] == current[:n]
    ]
    if len(overlaps) != 1 or overlaps[0] == len(current):
        raise HumanRequired("消息序列衔接不唯一或已滚动，停止并等待人工重新确认")
    return current[overlaps[0] :]


def same_tail(previous: list[Message], current: list[Message]) -> bool:
    """键盘可能收窄可见历史；只允许唯一的、至少两条的原序列尾部。"""
    if previous == current:
        return True
    if len(current) < 2 or previous[-len(current) :] != current:
        return False
    return (
        sum(
            previous[i : i + len(current)] == current
            for i in range(len(previous) - len(current) + 1)
        )
        == 1
    )


def guard_input(cfg, expected, mode, decision=None):
    now = store.utcnow()
    stats = rate_stats("", now)
    latest = max(
        (r.sent_at for r in dstore.sent_replies_since(now - timedelta(days=1)) if r.sent_at),
        default=None,
    )
    return guard.GuardInput(
        mode=mode,
        conv_id="android:" + hashlib.sha256(expected.encode()).hexdigest(),
        conv_name=expected,
        conv_kind="private",
        is_mutual=True,
        trigger_types=["text"],
        should_reply=decision.should_reply if decision else False,
        text=decision.text if decision else "",
        confidence=decision.confidence if decision else None,
        last_sent_in_conv=latest,
        sent_last_hour=stats.sent_last_hour,
        sent_last_day=stats.sent_last_day,
        now=now,
    )


def permitted(cfg, initial, allow_send):
    if cfg.douyin.android != initial.douyin.android:
        raise HumanRequired("安卓配置在运行中变化，请重新确认并启动")
    if store.get_account("douyin").status != "active":
        raise HumanRequired("账号已停止或冻结")
    if cfg.douyin.auto_reply == "off" or cfg.douyin.android.auto_reply == "off":
        raise HumanRequired("自动回复已关闭")
    if cfg.douyin.quiet_until(datetime.now().astimezone()):
        raise HumanRequired("进入休息时段，本次安卓监听结束")
    return "on" if allow_send and cfg.douyin.android.auto_reply == "on" else "dry_run"


async def watch(
    s,
    run,
    cfg,
    expected: str,
    self_name: str,
    *,
    confirmed=False,
    allow_send=False,
    generate=False,
    seconds=300,
    poll_s=3,
    load_config=None,
    decide=None,
    sleep=asyncio.sleep,
    on_ready=None,
    media_enabled=False,
):
    """最多一次模型决策和一次单条发送。重启重新基线，不追补离线期间消息。"""
    if not confirmed:
        raise AndroidError("需人工确认专用账号、唯一互关私聊、消息底部，运行期间不要操作手机")
    if not 1 <= seconds <= 3600 or not 1 <= poll_s <= 60:
        raise AndroidError("监听时长须为 1–3600 秒，轮询间隔须为 1–60 秒")
    if allow_send and (
        not generate or not cfg.douyin.android.allow_send or cfg.douyin.android.auto_reply != "on"
    ):
        raise AndroidError("真实自动回复需要 --generate、--allow-send 和两个安卓配置开关")
    loader = load_config or config.load
    decider = decide or decide_once
    mode = permitted(cfg, cfg, allow_send)
    previous = read_messages(s.source(), expected, self_name)
    if {m.from_me for m in previous} != {False, True}:
        raise AndroidError("初始可见记录必须同时包含双方头像与消息，以核对当前账号和私聊")
    if messaging.thread(s.source(), expected)["draft"]:
        raise AndroidError("存在草稿，请人工处理")
    run.audit("android.auto.baseline", count=len(previous), mode=mode, stable_ids=False)
    if on_ready is not None:
        on_ready()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        await sleep(poll_s)
        current_cfg = loader()
        mode = permitted(current_cfg, cfg, allow_send)
        root = s.source()
        if messaging.thread(root, expected)["draft"]:
            raise HumanRequired("出现人工草稿，本次监听停止")
        current = read_messages(root, expected, self_name)
        new = appended(previous, current)
        if not new:
            continue
        if any(m.from_me for m in new):
            raise HumanRequired("检测到己方新消息，本次监听停止以避免和人工回复冲突")
        new_shares = [m for m in new if m.kind != "text"]
        if new_shares and (not media_enabled or not generate or len(new_shares) != 1):
            run.audit("android.auto.deferred", reason="unverified_media", count=len(new))
            return {"status": "deferred", "reason": "新消息含未验证媒体，未调用模型或发送"}
        pre = guard.check(current_cfg.guard, guard_input(current_cfg, expected, mode))
        if not pre.ok:
            run.audit("android.auto.blocked", reasons=pre.reasons)
            return {"status": "blocked", "reasons": pre.reasons}
        if not generate:
            run.audit("android.auto.observed", count=len(new))
            return {"status": "observed", "new_messages": len(new), "model_called": False}
        summaries = {}
        request_id = None
        if new_shares:
            from agent_accounts.adapters.douyin.android import media_reply, shares

            card_hash = shares.fingerprint(new_shares[0].text)
            request_id = media_reply.request_key(expected, card_hash)

            def check_active(current_cfg=current_cfg):
                if loader() != current_cfg:
                    raise HumanRequired("媒体处理期间配置变化")
                permitted(current_cfg, cfg, allow_send)

            _, summary, _ = await media_reply.prepare(
                s,
                run,
                current_cfg,
                expected,
                self_name,
                card_hash,
                check_active=check_active,
                expected_snapshot=current,
            )
            summaries[card_hash] = summary
        return await respond(
            s,
            run,
            cfg,
            current_cfg,
            expected,
            self_name,
            current,
            new,
            mode,
            loader,
            decider,
            allow_send,
            summaries=summaries,
            request_id=request_id,
        )
    run.audit("android.auto.idle")
    return {"status": "idle", "decisions": 0}


async def respond(
    s,
    run,
    cfg,
    current_cfg,
    expected,
    self_name,
    current,
    new,
    mode,
    loader,
    decider,
    allow_send,
    *,
    summaries=None,
    historical_summary=None,
    request_id=None,
):
    lines = [
        reply.ChatLine(
            m.from_me,
            None,
            m.text
            if m.kind == "text"
            else (summaries or {}).get(
                hashlib.sha256(m.text.encode()).hexdigest(), "[历史分享尚未理解，内容未知]"
            ),
            i >= len(current) - len(new),
        )
        for i, m in enumerate(current)
    ]
    if historical_summary:
        lines.append(
            reply.ChatLine(
                False, None, "[明确选定的历史分享，针对该作品回复]\n" + historical_summary, True
            )
        )
    run.audit("android.auto.deciding", count=len(new), model=current_cfg.llm.reply.model)
    try:
        decision = await decider(
            current_cfg.llm.reply,
            persona.load()
            + (
                "\n本次最多回复一条单行消息。历史分享占位符不代表已看过内容。"
                "如果新消息需要理解历史分享才能回答，必须 should_reply=false；"
                "不能猜测分享内容，也不能声称看过。"
            ),
            lines,
            recent=persona.load_recent(),
        )
    except reply.ReplyError:
        raise AndroidError("回复模型调用失败；未发送，本次不重试") from None
    decision = single_bubble(decision, current_cfg.guard.max_messages)

    def verify_current(current_cfg=current_cfg, current=current):
        fresh_cfg = loader()
        if fresh_cfg != current_cfg:
            raise HumanRequired("生成回复期间配置变化，停止发送")
        permitted(fresh_cfg, cfg, allow_send)
        root = s.source()
        visible = read_messages(root, expected, self_name)
        if (
            not same_tail(current, visible)
            and messaging.thread(root, expected)["draft"] == decision.text
            and decision.text
        ):
            # 长草稿会让抖音保留上方位置、隐藏最后一条消息。仅在本次草稿
            # 精确回读后滑回底部一次；仍须完整匹配原尾部，不能忽略新消息。
            from agent_accounts.adapters.douyin.android.shares import gesture

            x1, y1, x2, y2 = bounds(one(root, "v65"))
            x = x1 + int((x2 - x1) * 0.8)
            gesture(s, x, y1 + (y2 - y1) * 3 // 4, x, y1 + (y2 - y1) // 4)
            time.sleep(0.7)
            root = s.source()
            if messaging.thread(root, expected)["draft"] != decision.text:
                raise HumanRequired("恢复聊天底部时草稿变化，停止发送")
            visible = read_messages(root, expected, self_name)
        if not same_tail(current, visible):
            raise HumanRequired("生成回复期间消息发生变化，丢弃草稿并停止")

    verify_current()
    g = guard_input(current_cfg, expected, mode, decision)
    reasons = guard.check(current_cfg.guard, g).reasons
    if decision.should_reply and (
        len(decision.messages) != 1 or any(c in decision.text for c in "\n\r\t")
    ):
        reasons.append("安卓有界自动回复只接受一条单行消息")
    status = (
        "blocked"
        if reasons
        else ("planned" if mode == "on" else "dry_run")
        if decision.should_reply
        else "skipped"
    )
    dstore.save_reply(
        dstore.DouyinReply(
            conv_id=g.conv_id,
            source="android_auto",
            should_reply=decision.should_reply,
            text=decision.text,
            status=status,
            reason="；".join(reasons) or decision.reason,
            model=current_cfg.llm.reply.model,
            confidence=decision.confidence,
            run_id=run.id,
        )
    )
    run.audit(
        "android.auto.decision",
        status=status,
        reasons=reasons,
        text_hash=hashlib.sha256(decision.text.encode()).hexdigest(),
        confidence=decision.confidence,
        model=current_cfg.llm.reply.model,
    )
    if reasons or not decision.should_reply:
        return {"status": status, "reasons": reasons}

    def verify_echo(current=current, decision=decision):
        root = s.source()  # 风控/断连立即传播，不因等待布局而吞掉。
        try:
            new_echo = appended(current, read_messages(root, expected, self_name))
        except (AndroidError, HumanRequired):
            return False
        own = [m for m in new_echo if m.from_me]
        return own == [Message(True, "text", decision.text)]

    result = messaging.send_one(
        s,
        run,
        current_cfg,
        expected,
        decision.text,
        request_id or "auto_" + run.id,
        execute=mode == "on",
        confirmed=True,
        automatic_guard=g,
        verify_current=verify_current,
        verify_echo=verify_echo,
    )
    return {**result, "reply": decision.text, "decisions": 1}
