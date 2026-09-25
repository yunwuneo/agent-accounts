"""抖音网页版私信接口（imapi.douyin.com，protobuf）的解析。

字段号来自 2026-09-25 的实测（见 Notion 抖音子页面第 11 节 Spike-2），没有官方保证。
解析不认识的消息不会中断，统一标为 ``unsupported`` 并保留原始内容，便于以后重新解析。

响应外层::

    1=cmd  4="OK"  6={cmd: body}  13=当前账号 uid

各 cmd 的 body::

    2043 get_message_by_init    1=会话条目[] {1=会话信息, 2=消息[]}
    2048 get_user_message       2={1=消息[]}（增量，含大量命令消息）
     301 get_by_conversation    1=消息[]
     610 get_info_list          1=会话信息[]

会话信息::

    1=conv_id  2=short_id  3=会话类型(1=单聊)  6={1=参与者[] {1=uid, 5=sec_uid}}
    51={5=我已读到的消息 index}

消息::

    1=conv_id  3=server_msg_id  4=会话内 index  6=消息类型  7=发送者 uid
    8=内容 JSON  10=创建时间(ms)  14=发送者 sec_uid  17=会话内序号
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from agent_accounts.core.pb import PbError, PbMessage

CMD_INIT = 2043
CMD_USER_MESSAGE = 2048
CMD_BY_CONVERSATION = 301
CMD_INFO_LIST = 610

MessageType = Literal[
    "text", "video_share", "note_share", "image", "sticker", "system", "unsupported"
]

# 字段 6 → 类型；分享类再按内容 JSON 细分
_RAW_TYPES: dict[int, MessageType] = {
    7: "text",
    8: "video_share",
    77: "note_share",
    5: "sticker",
    15: "sticker",  # 打招呼贴纸
    1: "system",
}
# 平台内部的命令消息（已读同步等），不是用户发的消息
COMMAND_TYPE_MIN = 50000
NOTE_AWEME_TYPE = 68


@dataclass
class ImMessage:
    msg_id: str
    conv_id: str
    index: int
    order: int
    raw_type: int
    type: MessageType
    sender_uid: str
    sender_sec_uid: str
    sent_at: datetime | None
    text: str | None = None
    aweme_id: str | None = None
    share_title: str | None = None
    share_author: str | None = None
    cover_url: str | None = None
    image_count: int | None = None
    content_json: str = ""

    def preview(self) -> str:
        match self.type:
            case "text" | "system":
                return self.text or ""
            case "video_share":
                return f"[视频] {self.share_title or ''}".strip()
            case "note_share":
                return f"[图集] {self.share_title or ''}".strip()
            case "sticker":
                return "[表情]"
            case _:
                return f"[不支持的消息 {self.raw_type}]"


@dataclass
class ImConversation:
    conv_id: str
    short_id: str
    conv_type: int
    participants: list[tuple[str, str]]  # (uid, sec_uid)
    read_index: int | None = None

    @property
    def kind(self) -> Literal["private", "group"]:
        return "private" if self.conv_type == 1 else "group"


@dataclass
class ImBatch:
    cmd: int
    my_uid: str | None
    conversations: list[ImConversation] = field(default_factory=list)
    messages: list[ImMessage] = field(default_factory=list)
    skipped_commands: int = 0
    errors: list[str] = field(default_factory=list)


def _ms_to_dt(ms: int) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, UTC) if ms > 0 else None


def parse_message(m: PbMessage) -> ImMessage:
    raw_type = m.get_int(6)
    content_raw = m.get_str(8)
    try:
        content: dict[str, Any] = json.loads(content_raw) if content_raw else {}
        if not isinstance(content, dict):
            content = {}
    except json.JSONDecodeError:
        content = {}

    msg_type: MessageType = _RAW_TYPES.get(raw_type, "unsupported")
    msg = ImMessage(
        msg_id=str(m.get_int(3)),
        conv_id=m.get_str(1),
        index=m.get_int(4),
        order=m.get_int(17),
        raw_type=raw_type,
        type=msg_type,
        sender_uid=str(m.get_int(7)),
        sender_sec_uid=m.get_str(14),
        sent_at=_ms_to_dt(m.get_int(10)),
        content_json=content_raw,
    )

    if "itemId" in content:  # 视频 / 图集分享
        is_note = (
            content.get("awemeType") == NOTE_AWEME_TYPE
            or content.get("is_slides") is True
            or bool(content.get("image_count"))
        )
        msg.type = "note_share" if is_note else "video_share"
        msg.aweme_id = str(content["itemId"])
        msg.share_title = content.get("content_title") or None
        msg.share_author = content.get("content_name") or None
        urls = (content.get("cover_url") or {}).get("url_list") or []
        msg.cover_url = urls[0] if urls else None
        msg.image_count = content.get("image_count")
    elif msg.type == "text":
        msg.text = content.get("text")
    elif msg.type == "system":
        msg.text = content.get("tips") or content.get("text")
    elif msg.type in ("video_share", "note_share"):
        msg.type = "unsupported"  # 分享消息却没有 itemId，不做猜测
    return msg


def parse_conversation(c: PbMessage) -> ImConversation:
    participants = []
    if part := c.get_msg(6):
        participants = [(str(p.get_int(1)), p.get_str(5)) for p in part.get_msgs(1)]
    setting = c.get_msg(51)
    return ImConversation(
        conv_id=c.get_str(1),
        short_id=str(c.get_int(2)),
        conv_type=c.get_int(3),
        participants=participants,
        read_index=setting.get_int(5) if setting else None,
    )


def _add_messages(batch: ImBatch, raw: list[PbMessage]) -> None:
    for m in raw:
        try:
            if m.get_int(6) >= COMMAND_TYPE_MIN:
                batch.skipped_commands += 1
                continue
            batch.messages.append(parse_message(m))
        except PbError as e:
            batch.errors.append(f"消息解析失败：{e}")


def parse_response(body: bytes) -> ImBatch:
    """解析一个 imapi 响应。未知 cmd 返回空批次，不抛异常。"""
    env = PbMessage.parse(body)
    cmd = env.get_int(1)
    my_uid = str(env.get_int(13)) or None
    batch = ImBatch(cmd=cmd, my_uid=my_uid if my_uid != "0" else None)
    if env.get_str(4) != "OK":
        batch.errors.append(f"cmd {cmd} 返回状态 {env.get_str(4)!r}")
        return batch
    wrapper = env.get_msg(6)
    body_msg = wrapper.get_msg(cmd) if wrapper else None
    if body_msg is None:
        return batch

    if cmd == CMD_INIT:
        for entry in body_msg.get_msgs(1):
            if info := entry.get_msg(1):
                batch.conversations.append(parse_conversation(info))
            _add_messages(batch, entry.get_msgs(2))
    elif cmd == CMD_USER_MESSAGE:
        for container in body_msg.get_msgs(2):
            _add_messages(batch, container.get_msgs(1))
    elif cmd == CMD_BY_CONVERSATION:
        _add_messages(batch, body_msg.get_msgs(1))
    elif cmd == CMD_INFO_LIST:
        batch.conversations.extend(parse_conversation(c) for c in body_msg.get_msgs(1))
    return batch


@dataclass(frozen=True)
class ImUser:
    uid: str | None
    sec_uid: str
    nickname: str


def parse_user_info(data: dict[str, Any]) -> list[ImUser]:
    """解析 ``/aweme/v1/web/im/user/info/`` 的 JSON。uid 有时缺失，以 sec_uid 为准。"""
    users = []
    for u in data.get("data") or []:
        if sec_uid := u.get("sec_uid"):
            users.append(
                ImUser(uid=u.get("uid") or None, sec_uid=sec_uid, nickname=u.get("nickname", ""))
            )
    return users
