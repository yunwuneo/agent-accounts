"""私信接口解析器测试。fixture 由 scripts/make_douyin_fixtures.py 从真实响应脱敏生成。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from agent_accounts.adapters.douyin import im
from agent_accounts.core.pb import LEN, VARINT, PbError, PbMessage

FIXTURES = Path(__file__).parent / "fixtures" / "douyin"
ME, PEER = "10000001", "10000002"
CONV = f"0:1:{ME}:{PEER}"


def load(name: str) -> im.ImBatch:
    return im.parse_response((FIXTURES / name).read_bytes())


@pytest.mark.parametrize("name", ["init.pb", "by_conversation.pb", "info_list.pb"])
def test_pb_roundtrip(name):
    raw = (FIXTURES / name).read_bytes()
    assert PbMessage.parse(raw).encode() == raw


def test_pb_rejects_garbage():
    with pytest.raises(PbError):
        PbMessage.parse(b"\x0a\xff\x01")  # 长度越界


def test_init_conversation_and_messages():
    batch = load("init.pb")
    assert batch.cmd == im.CMD_INIT
    assert batch.my_uid == ME
    [conv] = batch.conversations
    assert conv.conv_id == CONV and conv.kind == "private"
    assert {uid for uid, _ in conv.participants} == {ME, PEER}
    assert conv.read_index == max(m.index for m in batch.messages)  # 录制时已读到最后一条

    types = Counter(m.type for m in batch.messages)
    assert types == {"note_share": 13, "video_share": 3, "sticker": 2, "text": 2}
    assert all(m.conv_id == CONV for m in batch.messages)
    assert not batch.errors


def test_share_messages_carry_aweme_id():
    shares = [m for m in load("init.pb").messages if m.type in ("video_share", "note_share")]
    assert shares and all(m.aweme_id and m.aweme_id.isdigit() for m in shares)
    video = next(m for m in shares if m.type == "video_share")
    assert video.share_title and video.share_author and video.cover_url
    assert video.preview().startswith("[视频]")
    note = next(m for m in shares if m.type == "note_share")
    assert note.image_count and note.image_count >= 1


def test_by_conversation_includes_system_and_unsupported():
    batch = load("by_conversation.pb")
    types = Counter(m.type for m in batch.messages)
    assert types["system"] == 2 and types["unsupported"] == 1
    unsupported = next(m for m in batch.messages if m.type == "unsupported")
    assert unsupported.raw_type == 25  # 名片，保留原始类型便于以后支持
    system = next(m for m in batch.messages if m.type == "system")
    assert system.text


def test_user_message_skips_command_messages():
    batch = load("user_message.pb")
    assert batch.messages == [] and batch.skipped_commands == 1


def test_info_list():
    [conv] = load("info_list.pb").conversations
    assert conv.conv_id == CONV


def test_user_info():
    users = im.parse_user_info(
        json.loads((FIXTURES / "user_info.json").read_text(encoding="utf-8"))
    )
    assert "SEC_UID_2" in {u.sec_uid for u in users}
    assert all(u.nickname for u in users)


def _message(msg_type: int, content: dict) -> PbMessage:
    return PbMessage(
        [
            (1, LEN, CONV.encode()),
            (3, VARINT, 42),
            (6, VARINT, msg_type),
            (7, VARINT, int(PEER)),
            (8, LEN, json.dumps(content).encode()),
            (10, VARINT, 1_790_000_000_000),
        ]
    )


def test_share_type_decided_by_content_not_raw_type():
    note = im.parse_message(_message(8, {"itemId": "1", "awemeType": 68, "image_count": 3}))
    assert note.type == "note_share"
    video = im.parse_message(_message(77, {"itemId": "2", "awemeType": 0}))
    assert video.type == "video_share"


def test_share_without_item_id_is_unsupported():
    assert im.parse_message(_message(8, {"aweType": 800})).type == "unsupported"


def test_bad_content_json_does_not_crash():
    msg = im.parse_message(
        PbMessage([(1, LEN, CONV.encode()), (6, VARINT, 7), (8, LEN, b"{not json")])
    )
    assert msg.type == "text" and msg.text is None


def test_non_ok_status_reports_error():
    env = PbMessage([(1, VARINT, im.CMD_INIT), (4, LEN, b"FAIL")])
    batch = im.parse_response(env.encode())
    assert batch.errors and not batch.messages


def test_preview_is_single_line_and_truncated():
    msg = im.parse_message(_message(8, {"itemId": "1", "content_title": "第一行\n" + "长" * 200}))
    preview = msg.preview()
    assert "\n" not in preview and len(preview) == 60 and preview.endswith("…")
    assert msg.share_title.startswith("第一行\n")  # 原文完整保留
