"""抖音私信的本地存储：会话和消息两张表，按 msg_id 去重。

时间统一用带时区的 UTC。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlmodel import Field, SQLModel, col, func, select

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
    updated_at: datetime = Field(default_factory=_now)


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

    def preview(self) -> str:
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
        ).preview()


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
    nicknames = {u.sec_uid: u.nickname for u in users}
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
            if conv.peer_sec_uid and conv.peer_sec_uid in nicknames:
                conv.name = nicknames[conv.peer_sec_uid]
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


def list_messages(
    conv_id: str, *, limit: int = 30, since: datetime | None = None
) -> list[DouyinMessage]:
    with store.session() as s:
        q = select(DouyinMessage).where(DouyinMessage.conv_id == conv_id)
        if since:
            q = q.where(col(DouyinMessage.sent_at) >= _utc(since))
        rows = s.exec(q.order_by(col(DouyinMessage.msg_index).desc()).limit(limit)).all()
    return list(reversed(rows))
