from __future__ import annotations

import json
from pathlib import Path

from agent_accounts.adapters.douyin import im
from agent_accounts.adapters.douyin import store as dstore

FIXTURES = Path(__file__).parent / "fixtures" / "douyin"
CONV = "0:1:10000001:10000002"


def _batch(name: str) -> im.ImBatch:
    return im.parse_response((FIXTURES / name).read_bytes())


def _users() -> list[im.ImUser]:
    return im.parse_user_info(json.loads((FIXTURES / "user_info.json").read_text()))


def test_apply_dedupes_and_fills_conversation():
    first = dstore.apply([_batch("init.pb")], _users(), run_id="r1")
    assert len(first.new_messages) == 20
    [conv] = first.conversations
    assert conv.conv_id == CONV
    assert conv.peer_uid == "10000002" and conv.name  # 昵称来自 user_info
    assert conv.unread == 0  # 录制时已读到最后一条
    assert conv.last_preview

    # 重复同步不产生新消息
    again = dstore.apply([_batch("init.pb")], _users(), run_id="r2")
    assert again.new_messages == []
    # by_conversation 是更早的历史（序号 1–15），和 init（16–35）拼起来正好是完整会话
    more = dstore.apply([_batch("by_conversation.pb")], run_id="r3")
    assert len(more.new_messages) == 15
    assert all(m.first_seen_run == "r3" for m in more.new_messages)
    orders = [m.msg_order for m in dstore.list_messages(CONV, limit=100)]
    assert orders == list(range(1, 36))
    # 补历史不改变「最后一条」
    assert more.conversations[0].last_index == first.conversations[0].last_index


def test_from_me_and_unread_count():
    dstore.apply([_batch("init.pb")], _users())
    msgs = dstore.list_messages(CONV, limit=100)
    assert any(m.from_me for m in msgs) and any(not m.from_me for m in msgs)

    # 模拟已读位置回退：对方在 read_index 之后的消息计为未读
    batch = _batch("info_list.pb")
    peer_msgs = [m for m in msgs if not m.from_me]
    cut = peer_msgs[-3].msg_index
    batch.conversations[0].read_index = cut
    with dstore.store.session() as s:
        row = s.get(dstore.DouyinConversation, CONV)
        row.read_index = None  # apply 取历史最大值，这里先清掉
        s.add(row)
        s.commit()
    [conv] = dstore.apply([batch]).conversations
    assert conv.unread == sum(1 for m in peer_msgs if m.msg_index > cut) == 2


def test_find_conversation_and_list_messages():
    dstore.apply([_batch("init.pb")], _users())
    conv = dstore.find_conversation(CONV)
    assert conv is not None
    assert dstore.find_conversation(conv.name) is not None
    assert dstore.find_conversation("不存在的人") is None
    last5 = dstore.list_messages(CONV, limit=5)
    assert len(last5) == 5
    assert [m.msg_index for m in last5] == sorted(m.msg_index for m in last5)
