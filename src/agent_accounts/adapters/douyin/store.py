"""抖音私信的本地存储：会话和消息两张表，按 msg_id 去重。

时间统一用带时区的 UTC。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlmodel import Field, SQLModel, col, func, or_, select

from agent_accounts.adapters.douyin import im
from agent_accounts.core import store

_EPOCH = datetime.min.replace(tzinfo=UTC)


def _utc(dt: datetime | None) -> datetime | None:
    return dt.astimezone(UTC) if dt else None


def _now() -> datetime:
    return datetime.now(UTC)


class DouyinConversation(SQLModel, table=True):
    __tablename__ = "douyin_conversations"

    conv_id: str = Field(primary_key=True)
    short_id: str | None = None
    kind: str = "private"
    peer_uid: str | None = None
    peer_sec_uid: str | None = None
    name: str | None = None  # 对方昵称（单聊）
    read_index: int | None = None  # 我已读到的消息 index
    last_index: int | None = None
    last_at: datetime | None = None
    last_preview: str | None = None
    unread: int = 0  # 对方发来、index 大于 read_index 的消息数
    peer_follow_status: int | None = None  # 我是否关注对方（2 = 互相关注）
    peer_follower_status: int | None = None  # 对方是否关注我
    # 自动回复已经处理到的消息 index。首次见到会话时设为当前最后一条（基线），不回旧消息
    handled_index: int | None = None
    updated_at: datetime = Field(default_factory=_now)

    @property
    def is_mutual(self) -> bool:
        follow, follower = self.peer_follow_status or 0, self.peer_follower_status or 0
        return follow == 2 or (follow >= 1 and follower >= 1)


class DouyinMessage(SQLModel, table=True):
    __tablename__ = "douyin_messages"

    msg_id: str = Field(primary_key=True)
    conv_id: str = Field(index=True)
    msg_index: int = Field(index=True)
    msg_order: int = 0
    raw_type: int
    type: str
    sender_uid: str
    from_me: bool
    sent_at: datetime | None = None
    text: str | None = None
    aweme_id: str | None = Field(default=None, index=True)
    share_title: str | None = None
    share_author: str | None = None
    cover_url: str | None = None
    image_count: int | None = None
    content_json: str = ""  # 原始内容，解析规则改进后可重新解析
    first_seen_at: datetime = Field(default_factory=_now)
    first_seen_run: str | None = None

    def preview(self, max_len: int = 60) -> str:
        return im.ImMessage(
            msg_id=self.msg_id,
            conv_id=self.conv_id,
            index=self.msg_index,
            order=self.msg_order,
            raw_type=self.raw_type,
            type=self.type,  # type: ignore[arg-type]
            sender_uid=self.sender_uid,
            sender_sec_uid="",
            sent_at=self.sent_at,
            text=self.text,
            share_title=self.share_title,
        ).preview(max_len)


@dataclass
class ApplyResult:
    new_messages: list[DouyinMessage] = field(default_factory=list)
    conversations: list[DouyinConversation] = field(default_factory=list)


def apply(
    batches: Iterable[im.ImBatch],
    users: Iterable[im.ImUser] = (),
    *,
    run_id: str | None = None,
) -> ApplyResult:
    """把解析结果写入数据库。返回新消息（按会话、index 排序）和涉及的会话。"""
    batches = list(batches)
    my_uid = next((b.my_uid for b in batches if b.my_uid), None)
    users_by_sec = {u.sec_uid: u for u in users}
    result = ApplyResult()
    touched: set[str] = set()

    with store.session() as s:
        for batch in batches:
            for c in batch.conversations:
                row = s.get(DouyinConversation, c.conv_id) or DouyinConversation(conv_id=c.conv_id)
                row.short_id, row.kind = c.short_id, c.kind
                peer = next(((u, sec) for u, sec in c.participants if u != my_uid), None)
                if peer and c.kind == "private":
                    row.peer_uid, row.peer_sec_uid = peer
                if c.read_index is not None:
                    row.read_index = max(row.read_index or 0, c.read_index)
                row.updated_at = _now()
                s.add(row)
                touched.add(c.conv_id)

        seen: set[str] = set()
        for batch in batches:
            for m in batch.messages:
                if m.msg_id in seen or s.get(DouyinMessage, m.msg_id) is not None:
                    continue
                seen.add(m.msg_id)
                row = DouyinMessage(
                    msg_id=m.msg_id,
                    conv_id=m.conv_id,
                    msg_index=m.index,
                    msg_order=m.order,
                    raw_type=m.raw_type,
                    type=m.type,
                    sender_uid=m.sender_uid,
                    from_me=m.sender_uid == my_uid,
                    sent_at=_utc(m.sent_at),
                    text=m.text,
                    aweme_id=m.aweme_id,
                    share_title=m.share_title,
                    share_author=m.share_author,
                    cover_url=m.cover_url,
                    image_count=m.image_count,
                    content_json=m.content_json,
                    first_seen_run=run_id,
                )
                s.add(row)
                result.new_messages.append(row)
                touched.add(m.conv_id)
        s.flush()

        for conv_id in touched:
            conv = s.get(DouyinConversation, conv_id)
            if conv is None:  # 只在消息里出现过的会话
                conv = DouyinConversation(conv_id=conv_id)
            if conv.peer_sec_uid and (user := users_by_sec.get(conv.peer_sec_uid)):
                conv.name = user.nickname
                conv.peer_follow_status = user.follow_status
                conv.peer_follower_status = user.follower_status
            last = s.exec(
                select(DouyinMessage)
                .where(DouyinMessage.conv_id == conv_id)
                .order_by(col(DouyinMessage.msg_index).desc())
            ).first()
            if last:
                conv.last_index, conv.last_at = last.msg_index, last.sent_at
                conv.last_preview = last.preview()
            if conv.read_index is not None:
                conv.unread = s.exec(
                    select(func.count())
                    .select_from(DouyinMessage)
                    .where(
                        DouyinMessage.conv_id == conv_id,
                        col(DouyinMessage.from_me).is_(False),
                        DouyinMessage.msg_index > conv.read_index,
                    )
                ).one()
            conv.updated_at = _now()
            s.add(conv)
            result.conversations.append(conv)
        s.commit()

    result.new_messages.sort(key=lambda m: (m.conv_id, m.msg_index))
    result.conversations.sort(key=lambda c: c.last_at or _EPOCH, reverse=True)
    return result


def list_conversations() -> list[DouyinConversation]:
    with store.session() as s:
        rows = s.exec(select(DouyinConversation)).all()
    return sorted(rows, key=lambda c: c.last_at or _EPOCH, reverse=True)


def find_conversation(query: str) -> DouyinConversation | None:
    """按 conv_id 精确匹配，或按昵称包含匹配（唯一时）。"""
    with store.session() as s:
        if row := s.get(DouyinConversation, query):
            return row
        rows = s.exec(
            select(DouyinConversation).where(col(DouyinConversation.name).contains(query))
        ).all()
    return rows[0] if len(rows) == 1 else None


def recent_messages(*, limit: int = 30) -> list[DouyinMessage]:
    """所有会话里最近的消息（按时间先后排列）。"""
    with store.session() as s:
        q = select(DouyinMessage).order_by(
            col(DouyinMessage.sent_at).desc(), col(DouyinMessage.msg_index).desc()
        )
        rows = s.exec(q.limit(limit)).all()
    return list(reversed(rows))


def list_messages(
    conv_id: str,
    *,
    limit: int | None = 30,
    since: datetime | None = None,
    before_index: int | None = None,
    after_index: int | None = None,
) -> list[DouyinMessage]:
    """会话里最近的 limit 条（按先后排列）；给了 before_index 时只取更早的。"""
    with store.session() as s:
        q = select(DouyinMessage).where(DouyinMessage.conv_id == conv_id)
        if since:
            q = q.where(col(DouyinMessage.sent_at) >= _utc(since))
        if before_index is not None:
            q = q.where(col(DouyinMessage.msg_index) < before_index)
        if after_index is not None:
            q = q.where(col(DouyinMessage.msg_index) > after_index)
        rows = s.exec(q.order_by(col(DouyinMessage.msg_index).desc()).limit(limit)).all()
    return list(reversed(rows))


def search_messages(conv_id: str, query: str, *, limit: int = 10) -> list[DouyinMessage]:
    """会话里文字或分享标题包含 query 的最近 limit 条（按先后排列）。"""
    with store.session() as s:
        q = select(DouyinMessage).where(
            DouyinMessage.conv_id == conv_id,
            or_(
                col(DouyinMessage.text).contains(query),
                col(DouyinMessage.share_title).contains(query),
            ),
        )
        rows = s.exec(q.order_by(col(DouyinMessage.msg_index).desc()).limit(limit)).all()
    return list(reversed(rows))


def message_stats(conv_id: str) -> tuple[int, datetime | None]:
    """本地记录里这个会话的消息数和最早一条的时间。"""
    with store.session() as s:
        n, first = s.exec(
            select(func.count(), func.min(DouyinMessage.sent_at)).where(
                DouyinMessage.conv_id == conv_id
            )
        ).one()
    return n, _utc(first)


class DouyinReply(SQLModel, table=True):
    """每一次回复决策（不管最后有没有发出去）都记一条，作为审计和观察 dry_run 的依据。"""

    __tablename__ = "douyin_replies"

    id: int | None = Field(default=None, primary_key=True)
    conv_id: str = Field(index=True)
    trigger_msg_ids: str = "[]"  # 触发这次决策的对方消息
    trigger_last_index: int | None = None
    source: str = "auto"  # auto（run）/ manual（douyin reply）
    should_reply: bool = False
    text: str | None = None  # 多条消息一行一条
    reason: str = ""
    confidence: float | None = None
    guard_reasons: str = "[]"  # 护栏拦截原因
    tool_calls_json: str = "[]"  # 回复模型调用过的工具（只有工具名、参数摘要、结果字数）
    # dry_run：只生成不发送；blocked：被护栏拦下；skipped：模型决定不回；sent；failed；
    # partial：多条消息只发出了一部分
    status: str
    model: str | None = None
    error: str | None = None
    run_id: str | None = None
    created_at: datetime = Field(default_factory=_now, index=True)
    sent_at: datetime | None = None


def save_reply(reply: DouyinReply) -> DouyinReply:
    with store.session() as s:
        s.add(reply)
        s.commit()
        s.refresh(reply)
        return reply


def sent_replies_since(since: datetime, conv_id: str | None = None) -> list[DouyinReply]:
    with store.session() as s:
        q = select(DouyinReply).where(
            # partial：多条消息只发出了一部分，也算一次发送
            col(DouyinReply.status).in_(["sent", "partial"]),
            col(DouyinReply.sent_at) >= since,
        )
        if conv_id:
            q = q.where(DouyinReply.conv_id == conv_id)
        rows = list(s.exec(q).all())
        for row in rows:
            # SQLite 丢失 timezone 标记；落库本身是 UTC，不按本机时区转换。
            if row.sent_at is not None and row.sent_at.tzinfo is None:
                row.sent_at = row.sent_at.replace(tzinfo=UTC)
        return rows


def set_handled(conv_id: str, index: int) -> None:
    with store.session() as s:
        if row := s.get(DouyinConversation, conv_id):
            row.handled_index = max(row.handled_index or 0, index)
            s.add(row)
            s.commit()
