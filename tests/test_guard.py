from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agent_accounts.core.config import GuardConfig
from agent_accounts.core.guard import GuardInput, check

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
CFG = GuardConfig()
BASE = GuardInput(
    mode="on",
    conv_id="0:1:1:2",
    conv_name="小明",
    conv_kind="private",
    is_mutual=True,
    trigger_types=["video_share"],
    should_reply=True,
    text="哈哈这个教程挺实用的，你也遇到这个问题了？",
    confidence=0.8,
    last_sent_in_conv=None,
    sent_last_hour=0,
    sent_last_day=0,
    now=NOW,
)


def reasons(**changes) -> list[str]:
    return check(CFG, replace(BASE, **changes)).reasons


def test_normal_reply_passes():
    assert check(CFG, BASE).ok


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"mode": "off"}, "auto_reply = off"),
        ({"conv_kind": "group"}, "群聊"),
        ({"is_mutual": False}, "互相关注"),
        ({"trigger_types": ["system", "unsupported"]}, "系统消息"),
        ({"confidence": 0.3}, "把握"),
        ({"text": "长" * 121}, "超过 120 字"),
        ({"text": "看这个 https://example.com"}, "链接"),
        ({"text": "去 abc.com 看看"}, "链接"),
        ({"text": "我手机号13800138000"}, "手机号"),
        ({"text": "加我微信吧"}, "联系方式"),
        ({"text": "我给你转账"}, "金钱"),
        ({"text": "给你 50 块钱"}, "金钱"),
        ({"text": "我保证明天发你"}, "承诺"),
        ({"last_sent_in_conv": NOW - timedelta(seconds=30)}, "不到 60 秒"),
        ({"sent_last_hour": 20}, "一小时"),
        ({"sent_last_day": 100}, "一天"),
    ],
)
def test_blocks(changes, expected):
    got = reasons(**changes)
    assert any(expected in r for r in got), got


def test_blocklist_allowlist_and_extra_words():
    cfg = GuardConfig(blocklist=["小明"], extra_block_words=["内部"])
    assert "在黑名单里" in check(cfg, BASE).reasons
    cfg = GuardConfig(allowlist=["0:1:9:9"])
    assert "不在白名单里" in check(cfg, BASE).reasons
    cfg = GuardConfig(allowlist=["小明"], extra_block_words=["实用"])
    assert check(cfg, BASE).reasons == ["包含禁用词"]


def test_no_reply_skips_content_checks():
    assert reasons(should_reply=False, text="") == []


def test_manual_send_checks_only_content_and_rate():
    manual = {"manual": True, "is_mutual": False, "conv_kind": "group", "confidence": None}
    assert reasons(**manual) == []
    assert any("链接" in r for r in reasons(**manual, text="http://x.cn"))
    assert any("不到" in r for r in reasons(**manual, last_sent_in_conv=NOW))


def test_ordinary_chat_is_not_over_blocked():
    for text in ["哈哈笑死", "这个视频好好看", "你也玩炉石吗？", "晚安～", "明天见"]:
        assert reasons(text=text) == [], text
