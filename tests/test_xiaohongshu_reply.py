"""小红书 M3：只处理已关注会话、系统消息不触发、笔记没看完不回、护栏、发送器。

不开真实浏览器（发送器用合成页面）、不调模型（decide 替换成假实现）。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agent_accounts.adapters.xiaohongshu import autoreply, im
from agent_accounts.adapters.xiaohongshu import store as xstore
from agent_accounts.adapters.xiaohongshu.sender import SendError, send_messages, send_text
from agent_accounts.adapters.xiaohongshu.sync import SyncResult, pick_to_open
from agent_accounts.core import config, digests, reply
from agent_accounts.core.reply import ReplyDecision
from agent_accounts.core.run import start_run

FIXTURES = Path(__file__).parent / "fixtures" / "xiaohongshu"
ME = "a" * 24
PEER = "b" * 24
STRANGER = "c" * 24
VIDEO_ID = "6600000000000000000000a1"
NORMAL_ID = "6600000000000000000000a2"


def load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _setup_chats():
    return xstore.apply_chats(im.parse_chats(load("chats")), im.parse_unread(load("unread")))


def _msg(store_id: int, text: str, sender: str = PEER, content_type: int = 1) -> im.XhsMessage:
    return im.parse_message(
        {
            "id": f"m{store_id}",
            "store_id": store_id,
            "sender_id": sender,
            "receiver_id": ME if sender == PEER else PEER,
            "created_at": 1790000000000 + store_id * 1000,
            "content": json.dumps({"content": text, "content_type": content_type}),
        }
    )


def test_only_followed_conversations_are_opened():
    convs = _setup_chats()
    by_id = {c.peer_id: c for c in convs}
    assert by_id[PEER].followed and not by_id[STRANGER].followed
    to_open, skipped = pick_to_open(convs, open_chats=True, max_open=5)
    assert [c.peer_id for c in to_open] == [PEER] and skipped == []  # 陌生人、官方号都不算
    to_open, skipped = pick_to_open(convs, open_chats=False, max_open=5)
    assert to_open == [] and skipped == [PEER]


def test_xiaohongshu_rules_and_douyin_rules():
    assert reply.rules("douyin") == reply.RULES and "抖音账号" in reply.RULES
    xhs = reply.build_system("人设", platform="xiaohongshu")
    assert "小红书账号" in xhs and "视频笔记或图文笔记" in xhs and "抖音" not in xhs


def test_chat_lines_render_notes_images_and_system():
    _setup_chats()
    xstore.apply_messages(im.parse_history(load("history")), ME)
    digests.save(
        digests.MediaDigest(
            platform="xiaohongshu", item_id=VIDEO_ID, kind="video", summary="跳舞", vibe="好笑",
            reply_hooks_json=json.dumps(["问膝盖"], ensure_ascii=False),
        )
    )  # fmt: skip
    lines = autoreply.chat_lines(PEER, {"x"}, 20, failed={NORMAL_ID})
    text = [line.content for line in lines]
    assert text[0] == "[系统提示] 你们已互相关注"
    assert "[图片]（图片内容没有分析）" in text
    assert any("[分享视频笔记] 一条视频笔记" in t and "笔记摘要：跳舞" in t for t in text)
    assert any("[分享图文笔记] 一条图文笔记" in t and "内容分析失败" in t for t in text)
    assert text[1] == "[已撤回]"


class _Session:
    """run_once 用的假浏览器会话。"""

    page = None

    async def pause(self, *a, **k):
        pass


@pytest.fixture
def fake_env(monkeypatch):
    """替换浏览器、同步、笔记分析和模型决策；记录调用。"""
    calls = {"decide": [], "digest": []}
    decision = {"value": ReplyDecision(should_reply=True, messages=["哈哈"], confidence=0.9)}

    @asynccontextmanager
    async def session(*a, **k):
        yield _Session()

    async def sync(s, run, **kw):
        return SyncResult()

    async def digest(s, cfg, run, ids):
        calls["digest"].append(list(ids))
        for note_id in ids:
            digests.save(digests.MediaDigest(platform="xiaohongshu", item_id=note_id, kind="note"))
        return []

    async def decide(cfg, persona, lines, **kw):
        calls["decide"].append(([line.content for line in lines if line.is_new], kw))
        return decision["value"]

    monkeypatch.setattr(autoreply, "BrowserSession", session)
    monkeypatch.setattr(autoreply, "sync_in_session", sync)
    monkeypatch.setattr(autoreply.xdigest, "digest_in_session", digest)
    monkeypatch.setattr(autoreply, "decide", decide)
    return calls, decision


def _cfg(**xhs) -> config.Config:
    return config.Config(xiaohongshu=config.XiaohongshuConfig(**xhs))


async def _tick(cfg):
    with start_run("xiaohongshu", "run") as run:
        return await autoreply.run_once(cfg, run)


async def test_run_baseline_then_dry_run_decision(fake_env):
    calls, _ = fake_env
    _setup_chats()
    xstore.apply_messages([_msg(1, "我们已相互关注，开始聊天吧[偷笑R]"), _msg(2, "旧消息")], ME)

    first = await _tick(_cfg())
    assert [o.action for o in first.outcomes] == ["baseline"] and calls["decide"] == []
    assert xstore.get_conversation(PEER).handled_store_id == 2

    xstore.apply_messages([_msg(3, "在吗"), _msg(4, "看看这个")], ME)
    second = await _tick(_cfg())
    assert second.mode == "dry_run" and [o.action for o in second.outcomes] == ["dry_run"]
    new_lines, kw = calls["decide"][0]
    assert new_lines == ["在吗", "看看这个"] and kw["platform"] == "xiaohongshu"
    assert xstore.get_conversation(PEER).handled_store_id == 4
    assert xstore.recent_replies()[-1].status == "dry_run"


async def test_greeting_only_is_blocked_without_model(fake_env):
    calls, _ = fake_env
    _setup_chats()
    xstore.apply_messages([_msg(1, "旧")], ME)
    await _tick(_cfg())  # 基线
    xstore.apply_messages([_msg(2, "我们已相互关注，开始聊天吧"), _msg(3, "", content_type=0)], ME)
    result = await _tick(_cfg())
    assert [o.action for o in result.outcomes] == ["blocked"] and calls["decide"] == []
    assert "系统消息" in result.outcomes[0].detail


async def test_stranger_messages_are_ignored(fake_env):
    calls, _ = fake_env
    _setup_chats()
    xstore.apply_messages([_msg(1, "旧", sender=STRANGER)], ME)
    # apply_messages 按 sender 找会话：陌生人发来的消息归到陌生人会话
    await _tick(_cfg())
    xstore.apply_messages([_msg(2, "你好", sender=STRANGER)], ME)
    result = await _tick(_cfg())
    assert result.outcomes == [] and calls["decide"] == []
    assert xstore.get_conversation(STRANGER).handled_store_id is None


async def test_unanalysed_note_defers_whole_conversation(fake_env):
    calls, _ = fake_env
    _setup_chats()
    xstore.apply_messages([_msg(1, "旧")], ME)
    await _tick(_cfg())
    history = [m for m in im.parse_history(load("history")) if m.type == "note"]
    history = [im.XhsMessage(**{**m.__dict__, "store_id": 10 + i}) for i, m in enumerate(history)]
    xstore.apply_messages(history, ME)

    result = await _tick(_cfg(digest_per_tick=1))
    assert [o.action for o in result.outcomes] == ["deferred"] and calls["decide"] == []
    assert len(calls["digest"]) == 1 and xstore.get_conversation(PEER).handled_store_id == 1

    result = await _tick(_cfg(digest_per_tick=1))  # 下一轮分析剩下那篇，然后才决策
    assert [o.action for o in result.outcomes] == ["dry_run"] and len(calls["digest"]) == 2
    assert xstore.get_conversation(PEER).handled_store_id == 11


async def test_low_confidence_and_content_guard(fake_env):
    _, decision = fake_env
    _setup_chats()
    xstore.apply_messages([_msg(1, "旧")], ME)
    await _tick(_cfg())
    decision["value"] = ReplyDecision(should_reply=True, messages=["加我微信聊"], confidence=0.9)
    xstore.apply_messages([_msg(2, "你微信多少")], ME)
    result = await _tick(_cfg())
    assert result.outcomes[0].action == "blocked" and "联系方式" in result.outcomes[0].detail


def test_effective_mode_needs_two_confirmations():
    on = _cfg(auto_reply="on")
    assert autoreply.effective_mode(on, dry_run=False, allow_send=True) == "on"
    assert autoreply.effective_mode(on, dry_run=False, allow_send=False) == "dry_run"
    assert autoreply.effective_mode(on, dry_run=True, allow_send=True) == "dry_run"
    assert autoreply.effective_mode(_cfg(), dry_run=False, allow_send=True) == "dry_run"


# ---- 发送器：合成页面 ----

_CHAT_HTML = """
<div class="xhs-im-msg-list">{bubbles}</div>
<div class="xhs-im-input-bar-editor" contenteditable="true"></div>
<script>
  const box = document.querySelector('.xhs-im-input-bar-editor');
  window.enterCount = 0;
  box.addEventListener('keydown', e => {{
    if (e.key !== 'Enter') return;
    e.preventDefault();
    window.enterCount += 1;
    if (window.dropFirst && window.enterCount === 1) return;  // 第一次回车没发出去
    if (window.neverSend) return;
    const p = document.createElement('p');
    p.className = 'xhs-im-bubble__text';
    p.innerText = box.innerText.trim();
    document.querySelector('.xhs-im-msg-list').appendChild(p);
    box.innerText = '';
  }});
</script>
"""


class _PageSession:
    def __init__(self, page):
        self.page = page

    async def pause(self, lo=None, hi=None):
        pass


async def _chat(page, bubbles: str = "", **flags):
    await page.set_content(_CHAT_HTML.format(bubbles=bubbles))
    for name, value in flags.items():
        await page.evaluate(f"window.{name} = {json.dumps(value)}")
    return _PageSession(page)


async def test_send_text_confirms_by_new_bubble(page):
    s = await _chat(page, bubbles='<p class="xhs-im-bubble__text">好的</p>')
    r = await send_text(s, "好的", verify_timeout_s=2)  # 同样的话之前出现过，也要多出一次才算
    assert r.ok and not r.retried
    assert await page.evaluate("document.querySelectorAll('p.xhs-im-bubble__text').length") == 2


async def test_send_text_retries_once_only_when_text_left_in_editor(page):
    s = await _chat(page, dropFirst=True)
    r = await send_text(s, "你好呀", verify_timeout_s=1)
    assert r.ok and r.retried and await page.evaluate("window.enterCount") == 2

    s = await _chat(page, neverSend=True)
    r = await send_text(s, "你好呀", verify_timeout_s=1)
    assert not r.ok and r.retried and await page.evaluate("window.enterCount") == 2


async def test_send_refuses_when_editor_not_empty(page):
    s = await _chat(page)
    await page.evaluate("document.querySelector('.xhs-im-input-bar-editor').innerText = '草稿'")
    with pytest.raises(SendError, match="已有内容"):
        await send_text(s, "你好")


async def test_send_messages_stops_after_first_failure(page):
    s = await _chat(page, neverSend=True)
    r = await send_messages(s, ["第一条", "第二条"], verify_timeout_s=0.5)
    assert not r.ok and r.sent == 0 and r.total == 2 and len(r.results) == 1
