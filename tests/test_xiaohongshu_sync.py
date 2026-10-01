"""小红书 M1：解析、入库、会话定位。fixture 用真实字段名、虚构取值。"""

from __future__ import annotations

import json
from pathlib import Path

from agent_accounts.adapters.xiaohongshu import im
from agent_accounts.adapters.xiaohongshu import store as xstore
from agent_accounts.adapters.xiaohongshu.sync import Collector, click_conversation

FIXTURES = Path(__file__).parent / "fixtures" / "xiaohongshu"
ME = "a" * 24
PEER = "b" * 24
STRANGER = "c" * 24


def load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_parse_chats_unread_me():
    chats = im.parse_chats(load("chats"))
    assert [c.peer_id for c in chats] == [PEER, STRANGER, "d" * 24]
    assert chats[0].name == "测试好友" and chats[0].max_store_id == 6 and chats[0].is_friend
    assert chats[2].is_official
    assert chats[0].last_at is not None and chats[0].last_at.year == 2026
    assert im.parse_unread(load("unread")) == {PEER: 5, STRANGER: 1}
    assert im.parse_me(load("me")) == ME


def test_parse_history_message_types():
    msgs = {m.store_id: m for m in im.parse_history(load("history"))}
    assert len(msgs) == 6
    video, normal, image, text, revoked, system = (msgs[i] for i in (6, 5, 4, 3, 2, 1))
    assert video.type == "note" and video.note_type == "video"
    assert video.note_id == "6600000000000000000000a1"
    assert video.note_xsec_token == "TOKENvideo="
    assert video.note_title == "一条视频笔记" and video.note_author == "作者"
    assert normal.type == "note" and normal.note_type == "normal"
    assert image.type == "image" and image.image_url == "https://im-img.example/org.jpg"
    assert (image.image_width, image.image_height) == (1080, 1440)
    assert text.type == "text" and text.text == "你好呀" and text.peer_of(ME) == PEER
    assert revoked.revoked and revoked.peer_of(ME) == PEER and revoked.preview() == "[已撤回]"
    assert system.type == "system" and system.preview() == "[系统提示] 你们已互相关注"
    assert video.preview() == "[视频笔记] 一条视频笔记"


def test_platform_greeting_is_system_message():
    item = {
        "id": "x",
        "store_id": 1,
        "sender_id": PEER,
        "receiver_id": ME,
        "content": json.dumps(
            {"content": "我们已相互关注，开始聊天吧[偷笑R]", "content_type": 1}, ensure_ascii=False
        ),
    }
    m = im.parse_message(item)
    assert m.type == "system" and m.preview().startswith("[系统提示] 我们已相互关注")
    item["content"] = json.dumps({"content": "我们已相互关注这件事挺好", "content_type": 1})
    assert im.parse_message(item).type == "text"


def test_parse_ignores_malformed_items():
    body = {"data": {"out_message_list": [{"id": "x"}, "junk", {"store_id": 1}]}}
    assert im.parse_history(body) == []
    assert im.parse_chats({"data": {"chats": [{"info": {}}]}}) == []
    assert im.parse_unread({}) == {} and im.parse_me(None) is None


def test_store_apply_dedup_and_progress():
    convs = xstore.apply_chats(im.parse_chats(load("chats")), im.parse_unread(load("unread")))
    peer = next(c for c in convs if c.peer_id == PEER)
    assert peer.has_new and peer.unread == 5 and peer.synced_store_id is None

    history = im.parse_history(load("history"))
    first = xstore.apply_messages(history, ME, run_id="r1")
    assert [m.store_id for m in first.new_messages] == [1, 2, 3, 4, 5, 6]
    assert [m.from_me for m in first.new_messages] == [False, True, False, False, False, False]
    peer = xstore.get_conversation(PEER)
    assert peer.synced_store_id == 6 and not peer.has_new
    assert peer.last_preview == "[视频笔记] 一条视频笔记"

    again = xstore.apply_messages(history, ME, run_id="r2")
    assert again.new_messages == [] and again.revoked == 0

    # 会话列表里平台报告了更新的消息 → 又有新消息
    newer = [im.XhsChat(PEER, "测试好友", None, "新消息", 8, "both", True, False, False)]
    xstore.apply_chats(newer, {})
    assert xstore.get_conversation(PEER).has_new
    assert [m.store_id for m in xstore.list_messages(PEER, limit=3)] == [4, 5, 6]
    assert xstore.find_conversation("测试").peer_id == PEER


def test_store_marks_later_revocation():
    text = next(m for m in im.parse_history(load("history")) if m.store_id == 3)
    xstore.apply_messages([text], ME)
    revoked = im.XhsMessage(**{**text.__dict__, "revoked": True})
    result = xstore.apply_messages([revoked], ME)
    assert result.revoked == 1 and result.new_messages == []
    assert xstore.list_messages(PEER)[0].revoked


class _Request:
    def __init__(self, method: str = "GET"):
        self.method = method


class _Response:
    def __init__(self, path: str, body, method: str = "GET"):
        self.url = f"https://edith.xiaohongshu.com{path}?cursor=1"
        self.request = _Request(method)
        self._body = body

    async def json(self):
        return self._body


async def test_collector_routes_responses():
    c = Collector()
    await c._on_response(_Response("/api/im/web/xyz/chats", load("chats")))
    await c._on_response(_Response("/api/im/web/chats/group", {"data": {"chats": []}}))
    await c._on_response(_Response("/api/im/web/chat/get_unread", load("unread")))
    await c._on_response(_Response("/api/sns/web/v2/user/me", load("me")))
    await c._on_response(_Response("/api/im/web/messages/history", load("history")))
    await c._on_response(_Response("/api/im/web/messages/history", {}, method="POST"))
    assert c.chats_seen.is_set() and set(c.chats) == {PEER, STRANGER, "d" * 24}
    assert c.unread[PEER] == 5 and c.my_id == ME
    assert len(c.history) == 1 and len(c.history[0]) == 6

    class Req:
        method = "POST"
        url = "https://edith.xiaohongshu.com/api/im/web/v2/messages/read?x=1"

    c._on_request(Req())
    assert c.read_requests == 1


_LIST_HTML = """
<div class="xhs-im-conv-list__scroll">
  <div class="xhs-im-conv-item" data-conv-id="{peer}" data-conv-kind="user"
       onclick="window.clicked = '{peer}'">
    <span class="xhs-im-conv-item__name">测试好友</span></div>
  <div class="xhs-im-conv-item" data-conv-id="opaque-1" data-conv-kind="user"
       onclick="window.clicked = 'opaque-1'">
    <span class="xhs-im-conv-item__name">只能按昵称</span></div>
  <div class="xhs-im-conv-item" data-conv-id="opaque-2" data-conv-kind="user">
    <span class="xhs-im-conv-item__name">重名</span></div>
  <div class="xhs-im-conv-item" data-conv-id="opaque-3" data-conv-kind="user">
    <span class="xhs-im-conv-item__name">重名</span></div>
</div>
"""


async def testclick_conversation_by_id_then_unique_name(page):
    await page.set_content(_LIST_HTML.replace("{peer}", PEER))
    assert await click_conversation(page, PEER, "whatever")
    assert await page.evaluate("window.clicked") == PEER
    assert await click_conversation(page, "e" * 24, "只能按昵称")
    assert await page.evaluate("window.clicked") == "opaque-1"
    assert not await click_conversation(page, "f" * 24, "重名")  # 昵称不唯一不点
    assert not await click_conversation(page, "f" * 24, None)
