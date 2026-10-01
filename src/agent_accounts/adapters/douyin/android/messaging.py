"""当前会话快照、人工单条发送。界面证据不冒充服务端消息 ID/送达回执。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import timedelta

from sqlmodel import Field, SQLModel, select

from agent_accounts.adapters.douyin import store as dstore
from agent_accounts.adapters.douyin.android.session import AndroidError, label, one
from agent_accounts.adapters.douyin.replying import rate_stats
from agent_accounts.core import guard, store
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.run import RunContext


class AndroidSend(SQLModel, table=True):
    __tablename__ = "douyin_android_sends"
    request_id: str = Field(primary_key=True)
    state: str = "pending"
    text_hash: str
    run_id: str


def thread(root, expected: str) -> dict:
    title = label(one(root, "vw_"))
    if not expected or title != expected:
        raise AndroidError("当前会话标题与指定对象不一致；未导航、未输入")
    editor = one(root, "msg_et")
    draft = "" if editor.get("showing-hint") == "true" else editor.get("text", "")
    # 只取气泡容器，不再把内部 TextView 计一次；不推断发件人/消息时间/互关关系。
    kinds = {"sws": "text", "sww": "share_unknown"}
    bubbles = [n for n in root.iter() if n.get("resource-id", "").split(":id/")[-1] in kinds]
    messages = [
        {"kind": kinds[n.get("resource-id").split(":id/")[-1]], "visible_text": label(n)}
        for n in bubbles
    ]
    return {
        "title": title,
        "draft": draft,
        "visible_messages": messages,
        "stable_ids": False,
        "complete_history": False,
        "backend": "android",
    }


def snapshot(s, run: RunContext, expected: str) -> dict:
    data = thread(s.source(), expected)
    data.pop("draft")  # 不保存输入框内容
    s.screenshot(run.dir / "thread.png")
    (run.dir / "thread.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    run.audit("android.snapshot", count=len(data["visible_messages"]), stable_ids=False)
    return {
        "run_id": run.id,
        "path": str(run.dir),
        "count": len(data["visible_messages"]),
        "complete_history": False,
    }


def send_one(
    s,
    run: RunContext,
    cfg: Config,
    expected: str,
    text: str,
    request_id: str,
    *,
    execute: bool = False,
    confirmed: bool = False,
    automatic_guard: guard.GuardInput | None = None,
    verify_current=None,
    verify_echo=None,
) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
        raise AndroidError("request-id 须为 8–80 位字母、数字、下划线或连字符")
    if not text.strip() or any(ch in text for ch in "\r\n\t"):
        raise AndroidError("仅接受单行非空文本")
    current = thread(s.source(), expected)
    if current["draft"]:
        raise AndroidError("输入框已有草稿，请人工处理；未覆盖")
    now = store.utcnow()
    # 尚无跨后端身份绑定：以平台最近一次发送保守执行会话间隔。
    stats = rate_stats("", now)
    recent = dstore.sent_replies_since(now - timedelta(days=1))
    latest = max((r.sent_at for r in recent if r.sent_at), default=None)
    identity = "android:" + hashlib.sha256(expected.encode()).hexdigest()
    result = guard.check(
        cfg.guard,
        guard.GuardInput(
            mode="dry_run",
            conv_id=identity,
            conv_name=expected,
            conv_kind="private",
            is_mutual=False,
            trigger_types=[],
            should_reply=True,
            text=text,
            confidence=None,
            last_sent_in_conv=latest,
            sent_last_hour=stats.sent_last_hour,
            sent_last_day=stats.sent_last_day,
            now=now,
            manual=True,
        ),
    )
    if automatic_guard is not None:
        if automatic_guard.manual or automatic_guard.text != text:
            raise AndroidError("自动发送护栏输入不一致")
        automatic_guard.last_sent_in_conv = latest
        automatic_guard.sent_last_hour = stats.sent_last_hour
        automatic_guard.sent_last_day = stats.sent_last_day
        automatic_guard.now = now
        result = guard.check(cfg.guard, automatic_guard)
        if execute and (
            automatic_guard.mode != "on"
            or cfg.douyin.android.auto_reply != "on"
            or cfg.douyin.auto_reply == "off"
            or verify_current is None
        ):
            raise AndroidError("自动发送开关或复核器未就绪")
    if cfg.guard.allowlist and expected not in cfg.guard.allowlist:
        result.reasons.append("指定会话标题不在白名单")
    if not result.ok:
        raise AndroidError("发送被护栏拦截：" + "；".join(result.reasons))
    digest = hashlib.sha256(text.encode()).hexdigest()
    if not execute:
        run.audit("android.send.dry_run", length=len(text), text_hash=digest)
        return {"status": "dry_run", "typed": False, "sent": False, "length": len(text)}
    if not cfg.douyin.android.allow_send or not confirmed:
        raise AndroidError("实际发送需要 android.allow_send=true 及确认当前账号、唯一互关会话")
    if verify_current is not None:
        verify_current()
    with store.session() as db:
        if db.get(AndroidSend, request_id):
            raise AndroidError("该 request-id 已使用，禁止重复提交")
        if db.exec(select(AndroidSend).where(AndroidSend.state == "pending")).first():
            raise AndroidError("存在未确认发送，先人工核对并 resolve-send")
        db.add(AndroidSend(request_id=request_id, text_hash=digest, run_id=run.id))
        db.commit()  # 在任何输入前持久化；崩溃后不重试。
    reply = dstore.save_reply(
        dstore.DouyinReply(
            conv_id=identity,
            source="android_auto" if automatic_guard is not None else "android_manual",
            should_reply=True,
            text=None,
            status="partial",
            reason="安卓单条发送占位，实际结果待核对",
            run_id=run.id,
            sent_at=now,
        )
    )
    run.audit("android.send.pending", request_id=request_id, text_hash=digest, length=len(text))
    stage = "before_input"
    try:
        fresh = thread(s.source(), expected)
        if fresh["draft"]:
            raise AndroidError("输入前草稿发生变化")
        before = sum(m["visible_text"] == text for m in current["visible_messages"])
        editor = s.element("msg_et")
        stage = "typing"
        s.request(f"/element/{editor}/value", {"text": text})
        stage = "readback"
        if thread(s.source(), expected)["draft"] != text:
            raise AndroidError("输入回读不一致")
        if verify_current is not None:
            stage = "before_click_check"
            time.sleep(0.4)  # 输入框换行/键盘动画结束后再核对消息布局。
            verify_current()
        stage = "click"
        s.click("jaz")  # 只点一次；任何不确定结果都不得自动重试。
        stage = "echo"
        verified = False
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            after = thread(s.source(), expected)
            count = sum(m["visible_text"] == text for m in after["visible_messages"])
            if not after["draft"] and count > before:
                if verify_echo is not None and verify_echo() is False:
                    time.sleep(0.3)
                    continue  # 仅等待界面布局稳定，不重输、不重按发送。
                verified = True
                break
            time.sleep(0.3)
        if not verified:
            raise AndroidError("未取得完整 UI 回显")
        with store.session() as db:
            row = db.get(AndroidSend, request_id)
            row.state = "ui_verified"
            db.add(row)
            db.commit()
        reply.status = "sent"
        reply.reason = "仅 UI 回显；没有服务端/对端送达回执"
        dstore.save_reply(reply)
        run.audit("android.send.ui_verified", request_id=request_id, server_receipt=False)
        return {"status": "ui_verified", "server_receipt": False, "retried": False}
    except BaseException as exc:
        # 即使 Ctrl-C 或输入前断连，也不自动清除待核对状态。
        store.set_account_status("douyin", "frozen")
        run.audit(
            "android.send.uncertain",
            request_id=request_id,
            stage=stage,
            error_type=type(exc).__name__,
        )
        raise HumanRequired("安卓发送状态待人工核对，禁止重发", freeze=True) from None


def resolve_send(run: RunContext, request_id: str) -> dict:
    with store.session() as db:
        row = db.get(AndroidSend, request_id)
        if row is None or row.state != "pending":
            raise AndroidError("没有此待核对发送记录")
        row.state = "human_reviewed"
        db.add(row)
        db.commit()
    run.audit("android.send.human_reviewed", request_id=request_id)
    return {"status": "human_reviewed", "retry": False, "unfrozen": False}
