"""小红书私信接口（JSON）的解析。

- ``im/web/<id>/chats``：会话列表，每个会话带预览、最后消息时间和 ``max_store_id``；
- ``im/web/chat/get_unread``：按对方用户 ID 的未读数；
- ``im/web/messages/history``：一个会话的消息，每条有递增的 ``store_id``；
- ``sns/web/v2/user/me``：自己的用户 ID。

消息 ``content`` 是 JSON 字符串，内层 ``content`` 对文本是正文，对图片、笔记分享又是一层 JSON。
``content_type`` 的取值没有文档，类型按内层结构判断，原值照存，方便以后细化。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

MessageType = Literal["text", "image", "note", "other"]


def _dt(value: object) -> datetime | None:
    if not isinstance(value, int | float) or value <= 0:
        return None
    seconds = value / 1000 if value > 1e11 else value
    return datetime.fromtimestamp(seconds, UTC)


def _data(body: Any) -> dict[str, Any]:
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else {}


def _loads(value: object) -> Any:
    if isinstance(value, str) and value.strip()[:1] in "{[":
        try:
            return json.loads(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class XhsChat:
    peer_id: str
    name: str | None
    last_at: datetime | None
    last_preview: str | None
    max_store_id: int | None
    follow_status: str | None
    is_friend: bool | None
    is_official: bool
    is_ai_assistant: bool


@dataclass(frozen=True)
class XhsMessage:
    msg_id: str
    uuid: str | None
    store_id: int
    sender_id: str
    receiver_id: str
    sent_at: datetime | None
    revoked: bool
    content_type: int | None
    type: MessageType
    text: str | None
    preview_text: str | None  # 平台给的一行预览（front_chain）
    note_id: str | None = None
    note_type: str | None = None  # normal（图文）/ video
    note_title: str | None = None
    note_author: str | None = None
    note_xsec_token: str | None = None
    cover_url: str | None = None
    image_url: str | None = None
    image_width: int | None = None
    image_height: int | None = None
    content_json: str = ""

    def peer_of(self, my_id: str | None) -> str:
        return self.receiver_id if self.sender_id == my_id else self.sender_id

    def preview(self, max_len: int = 60) -> str:
        if self.revoked:
            body = "[已撤回]"
        elif self.type == "text":
            body = self.text or ""
        elif self.type == "image":
            body = "[图片]"
        elif self.type == "note":
            kind = "视频笔记" if self.note_type == "video" else "笔记"
            body = f"[{kind}] {self.note_title or ''}".strip()
        else:
            body = self.preview_text or "[其他消息]"
        return body if len(body) <= max_len else body[: max_len - 1] + "…"


def parse_me(body: Any) -> str | None:
    user_id = _data(body).get("user_id")
    return user_id if isinstance(user_id, str) and user_id else None


def parse_unread(body: Any) -> dict[str, int]:
    counts = _data(body).get("user_chat_unread_counts") or {}
    return (
        {k: v for k, v in counts.items() if isinstance(v, int)} if isinstance(counts, dict) else {}
    )


def parse_chats(body: Any) -> list[XhsChat]:
    chats = []
    for item in _data(body).get("chats") or []:
        if not isinstance(item, dict) or not item.get("chat_user_id"):
            continue
        info = item.get("info") if isinstance(item.get("info"), dict) else {}
        max_store = item.get("max_store_id")
        chats.append(
            XhsChat(
                peer_id=str(item["chat_user_id"]),
                name=info.get("nickname") or info.get("user_name"),
                last_at=_dt(item.get("last_msg_time")),
                last_preview=item.get("last_msg_content"),
                max_store_id=max_store if isinstance(max_store, int) else None,
                follow_status=info.get("follow_status"),
                is_friend=info.get("is_friend")
                if isinstance(info.get("is_friend"), bool)
                else None,
                is_official=bool(info.get("is_official")),
                is_ai_assistant=bool(info.get("is_ai_assistant")),
            )
        )
    return chats


def _xsec_token(link: object) -> str | None:
    if not isinstance(link, str):
        return None
    values = parse_qs(urlsplit(link).query).get("xsec_token")
    return values[0] if values else None


def parse_message(item: dict[str, Any]) -> XhsMessage | None:
    store_id = item.get("store_id")
    msg_id = item.get("id") or item.get("uuid")
    if not isinstance(store_id, int) or not msg_id:
        return None
    raw = item.get("content") if isinstance(item.get("content"), str) else ""
    outer = _loads(raw)
    outer = outer if isinstance(outer, dict) else {}
    inner_raw = outer.get("content")
    inner = _loads(inner_raw)
    content_type = outer.get("content_type")
    fields: dict[str, Any] = {}

    if isinstance(inner, dict) and isinstance(inner.get("imageDataMap"), dict):
        images = inner["imageDataMap"]
        best = next(
            (images[k] for k in ("IM_ORG", "IM_DTL", "IM_PRV") if isinstance(images.get(k), dict)),
            {},
        )
        size = inner.get("size") if isinstance(inner.get("size"), dict) else {}
        fields = {
            "type": "image",
            "image_url": best.get("url") or inner.get("link"),
            "image_width": size.get("width"),
            "image_height": size.get("height"),
        }
    elif isinstance(inner, dict) and (inner.get("noteType") or inner.get("type") == "note"):
        user = inner.get("user") if isinstance(inner.get("user"), dict) else {}
        fields = {
            "type": "note",
            "note_id": inner.get("id"),
            "note_type": inner.get("noteType"),
            "note_title": inner.get("title"),
            "note_author": user.get("nickname"),
            "note_xsec_token": _xsec_token(inner.get("link")),
            "cover_url": inner.get("cover") or inner.get("image"),
        }
    elif isinstance(inner_raw, str) and inner_raw and inner is None:
        fields = {"type": "text", "text": inner_raw}
    else:
        fields = {"type": "other"}

    fields.setdefault("text", None)
    return XhsMessage(
        msg_id=str(msg_id),
        uuid=item.get("uuid"),
        store_id=store_id,
        sender_id=str(item.get("sender_id") or ""),
        receiver_id=str(item.get("receiver_id") or ""),
        sent_at=_dt(item.get("created_at")),
        revoked=bool(item.get("revoked")),
        content_type=content_type if isinstance(content_type, int) else None,
        preview_text=outer.get("front_chain"),
        content_json=raw,
        **fields,
    )


def parse_history(body: Any) -> list[XhsMessage]:
    items = _data(body).get("out_message_list") or []
    return [m for item in items if isinstance(item, dict) and (m := parse_message(item))]
