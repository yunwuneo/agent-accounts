"""自动回复流程测试：fixture 数据 + 替换掉模型调用，不开浏览器（s=None → 只能 dry_run）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_accounts.adapters.douyin import autoreply, im
from agent_accounts.adapters.douyin import store as dstore
from agent_accounts.core import config, digests
from agent_accounts.core.reply import ReplyDecision
from agent_accounts.core.run import start_run

FIXTURES = Path(__file__).parent / "fixtures" / "douyin"
CONV = "0:1:10000001:10000002"


@pytest.fixture
def conv(isolated_home):
    users = im.parse_user_info(
        json.loads((FIXTURES / "user_info.json").read_text(encoding="utf-8"))
    )
    # fixture 里的昵称用户没有关注关系，这里补成互相关注
    users = [
        im.ImUser(u.uid, u.sec_uid, u.nickname, follow_status=2, follower_status=1) for u in users
    ]
    dstore.apply([im.parse_response((FIXTURES / "init.pb").read_bytes())], users)
    return dstore.find_conversation(CONV)


@pytest.fixture
def fake_decide(monkeypatch):
    calls = []

    def install(decision: ReplyDecision):
        async def fake(cfg, persona_text, lines, **kw):
            calls.append(lines)
            return decision

        monkeypatch.setattr(autoreply, "decide", fake)
        return calls

    return install


async def _act(conv, cfg=None, last=2):
    cfg = cfg or config.Config()
    with start_run("douyin", "test") as run:
        return await autoreply.decide_for(cfg, run, conv, last)


def test_effective_mode_needs_two_confirmations():
    cfg = config.Config(douyin=config.DouyinConfig(auto_reply="on"))
    assert autoreply.effective_mode(cfg, dry_run=False, allow_send=True) == "on"
    assert autoreply.effective_mode(cfg, dry_run=False, allow_send=False) == "dry_run"
    assert autoreply.effective_mode(cfg, dry_run=True, allow_send=True) == "dry_run"
    assert autoreply.effective_mode(config.Config(), dry_run=False, allow_send=True) == "dry_run"


async def test_dry_run_records_reply_with_digest_context(conv, fake_decide):
    calls = fake_decide(
        ReplyDecision(should_reply=True, text="哈哈这个好看", reason="r", confidence=0.9)
    )
    peer_share = next(m for m in reversed(dstore.list_messages(CONV, limit=100)) if m.aweme_id)
    digests.save(
        digests.MediaDigest(
            platform="douyin", item_id=peer_share.aweme_id, kind="video", summary="摘要内容",
            vibe="轻松", reply_hooks_json='["可以聊的点"]',
        )
    )  # fmt: skip
    o = await _act(conv)
    assert o.action == "dry_run" and o.reply.status == "dry_run" and o.reply.text == "哈哈这个好看"
    rendered = "\n".join(line.content for line in calls[0])
    assert "作品摘要：摘要内容" in rendered and "可以聊的点" in rendered
    assert sum(line.is_new for line in calls[0]) == 2


async def test_not_mutual_is_blocked_before_calling_model(conv, fake_decide):
    calls = fake_decide(ReplyDecision(should_reply=True, text="x", confidence=1))
    conv.peer_follow_status, conv.peer_follower_status = 1, 0
    o = await _act(conv)
    assert o.action == "blocked" and "互相关注" in o.detail
    assert calls == []  # 没有花钱调用模型


async def test_model_says_no(conv, fake_decide):
    fake_decide(ReplyDecision(should_reply=False, text="", reason="对方只发了表情"))
    o = await _act(conv)
    assert o.action == "skipped" and o.reply.reason == "对方只发了表情"


async def test_content_guard_blocks_model_output(conv, fake_decide):
    fake_decide(ReplyDecision(should_reply=True, text="加我微信聊", reason="", confidence=0.9))
    o = await _act(conv)
    assert o.action == "blocked" and "联系方式" in o.detail
    assert o.reply.status == "blocked"


def test_new_peer_messages_respects_handled_index(conv):
    msgs = dstore.list_messages(CONV, limit=100)
    peer = [m for m in msgs if not m.from_me]
    dstore.set_handled(CONV, peer[-3].msg_index)
    conv = dstore.find_conversation(CONV)
    assert [m.msg_id for m in autoreply.new_peer_messages(conv)] == [m.msg_id for m in peer[-2:]]
