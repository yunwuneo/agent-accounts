"""小红书私信的本地存储：会话和消息两张表，消息按 msg_id 去重。

会话以对方用户 ID 为主键。``max_store_id`` 来自会话列表（平台上最新一条），
``synced_store_id`` 是本地已拉到的最新一条；前者更大说明有新消息，需要点进会话拉取。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlmodel import Field, SQLModel, col, select

from agent_accounts.adapters.xiaohongshu import im
from agent_accounts.core import store

_EPOCH = datetime.min.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


class XhsConversation(SQLModel, table=True):
    __tablename__ = "xiaohongshu_conversations"

    peer_id: str = Field(primary_key=True)
    name: str | None = None
    follow_status: str | None = None
    is_friend: bool | None = None
    is_official: bool = False
    is_ai_assistant: bool = False
    max_store_id: int | None = None  # 会话列表里平台报告的最新一条
    synced_store_id: int | None = None  # 本地已入库的最新一条
    last_at: datetime | None = None
    last_preview: str | None = None
    unread: int = 0  # 平台报告的未读数（点进会话后会被平台清零）
    # 自动回复已经处理到的 store_id（M3 用）
    handled_store_id: int | None = None
    updated_at: datetime = Field(default_factory=_now)

    @property
    def has_new(self) -> bool:
        return self.max_store_id is not None and self.max_store_id > (self.synced_store_id or 0)


class XhsMessage(SQLModel, table=True):
    __tablename__ = "xiaohongshu_messages"

    msg_id: str = Field(primary_key=True)
    uuid: str | None = None
    peer_id: str = Field(index=True)
    store_id: int = Field(index=True)
    sender_id: str
    from_me: bool
    sent_at: datetime | None = None
    revoked: bool = False
    content_type: int | None = None
    type: str
    text: str | None = None
    preview_text: str | None = None
    note_id: str | None = Field(default=None, index=True)
    note_type: str | None = None
    note_title: str | None = None
    note_author: str | None = None
    note_xsec_token: str | None = None  # 打开笔记详情需要；只存在本地库
    cover_url: str | None = None
    image_url: str | None = None
    image_width: int | None = None
    image_height: int | None = None
    content_json: str = ""  # 原始内容，解析规则改进后可重新解析
    first_seen_at: datetime = Field(default_factory=_now)
    first_seen_run: str | None = None

    def preview(self, max_len: int = 60) -> str:
        return _as_parsed(self).preview(max_len)


def _as_parsed(row: XhsMessage) -> im.XhsMessage:
    return im.XhsMessage(
        msg_id=row.msg_id,
        uuid=row.uuid,
        store_id=row.store_id,
        sender_id=row.sender_id,
        receiver_id="",
        sent_at=row.sent_at,
        revoked=row.revoked,
        content_type=row.content_type,
        type=row.type,  # type: ignore[arg-type]
        text=row.text,
        preview_text=row.preview_text,
        note_type=row.note_type,
        note_title=row.note_title,
    )


def apply_chats(chats: Iterable[im.XhsChat], unread: dict[str, int]) -> list[XhsConversation]:
    """会话列表入库；返回涉及的会话（按最后消息时间倒序）。"""
    rows = []
    with store.session() as s:
        for chat in chats:
            row = s.get(XhsConversation, chat.peer_id) or XhsConversation(peer_id=chat.peer_id)
            row.name = chat.name or row.name
            row.follow_status = chat.follow_status
            row.is_friend = chat.is_friend
            row.is_official = chat.is_official
            row.is_ai_assistant = chat.is_ai_assistant
            if chat.max_store_id is not None:
                row.max_store_id = max(row.max_store_id or 0, chat.max_store_id)
            row.last_at = chat.last_at or row.last_at
            row.last_preview = chat.last_preview or row.last_preview
            row.unread = unread.get(chat.peer_id, 0)
            row.updated_at = _now()
            s.add(row)
            rows.append(row)
        s.commit()
    return sorted(rows, key=lambda c: c.last_at or _EPOCH, reverse=True)


@dataclass
class ApplyResult:
    new_messages: list[XhsMessage] = field(default_factory=list)
    revoked: int = 0  # 已入库、这次发现被撤回的消息数


def apply_messages(
    messages: Iterable[im.XhsMessage], my_id: str | None, *, run_id: str | None = None
) -> ApplyResult:
    result = ApplyResult()
    touched: set[str] = set()
    with store.session() as s:
        for m in messages:
            existing = s.get(XhsMessage, m.msg_id)
            if existing is not None:
                if m.revoked and not existing.revoked:
                    existing.revoked = True
                    s.add(existing)
                    result.revoked += 1
                continue
            peer = m.peer_of(my_id)
            row = XhsMessage(
                msg_id=m.msg_id,
                uuid=m.uuid,
                peer_id=peer,
                store_id=m.store_id,
                sender_id=m.sender_id,
                from_me=m.sender_id == my_id,
                sent_at=m.sent_at,
                revoked=m.revoked,
                content_type=m.content_type,
                type=m.type,
                text=m.text,
                preview_text=m.preview_text,
                note_id=m.note_id,
                note_type=m.note_type,
                note_title=m.note_title,
                note_author=m.note_author,
                note_xsec_token=m.note_xsec_token,
                cover_url=m.cover_url,
                image_url=m.image_url,
                image_width=m.image_width,
                image_height=m.image_height,
                content_json=m.content_json,
                first_seen_run=run_id,
            )
            s.add(row)
            result.new_messages.append(row)
            touched.add(peer)
        s.flush()

        for peer in touched:
            conv = s.get(XhsConversation, peer) or XhsConversation(peer_id=peer)
            last = s.exec(
                select(XhsMessage)
                .where(XhsMessage.peer_id == peer)
                .order_by(col(XhsMessage.store_id).desc())
            ).first()
            if last:
                conv.synced_store_id = max(conv.synced_store_id or 0, last.store_id)
                conv.max_store_id = max(conv.max_store_id or 0, last.store_id)
                conv.last_at = last.sent_at or conv.last_at
                conv.last_preview = last.preview()
            conv.updated_at = _now()
            s.add(conv)
        s.commit()
    result.new_messages.sort(key=lambda m: (m.peer_id, m.store_id))
    return result


def get_conversation(peer_id: str) -> XhsConversation | None:
    with store.session() as s:
        return s.get(XhsConversation, peer_id)


def list_conversations() -> list[XhsConversation]:
    with store.session() as s:
        rows = s.exec(select(XhsConversation)).all()
    return sorted(rows, key=lambda c: c.last_at or _EPOCH, reverse=True)


def find_conversation(query: str) -> XhsConversation | None:
    """按对方用户 ID 精确匹配，或按昵称包含匹配（唯一时）。"""
    with store.session() as s:
        if row := s.get(XhsConversation, query):
            return row
        rows = s.exec(
            select(XhsConversation).where(col(XhsConversation.name).contains(query))
        ).all()
    return rows[0] if len(rows) == 1 else None


def list_messages(peer_id: str, *, limit: int = 30) -> list[XhsMessage]:
    with store.session() as s:
        q = select(XhsMessage).where(XhsMessage.peer_id == peer_id)
        rows = s.exec(q.order_by(col(XhsMessage.store_id).desc()).limit(limit)).all()
    return list(reversed(rows))
