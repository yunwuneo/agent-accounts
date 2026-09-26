"""回复决策测试：MockTransport 模拟 [llm.reply]，不访问网络。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import anthropic
import httpx2
import pytest

from agent_accounts.core.config import LLMEndpoint
from agent_accounts.core.reply import ChatLine, ReplyError, decide, make_client, render


def _message(text: str, stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "m-chat",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


def _cfg(**kw) -> LLMEndpoint:
    return LLMEndpoint(base_url="https://llm.test", api_key="sk-reply-key", model="m-chat", **kw)


def _client(cfg, handler):
    http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    return make_client(cfg, http_client=http)


T = datetime(2026, 9, 25, 3, 0, tzinfo=UTC)
LINES = [
    ChatLine(from_me=True, sent_at=T, content="你好！"),
    ChatLine(from_me=False, sent_at=T, content="hi"),
    ChatLine(
        from_me=False,
        sent_at=T,
        content="[分享视频] 炉石安装包损坏解决办法（作品摘要：教你手动点安装）",
        is_new=True,
    ),
]


def test_render_marks_new_and_speaker():
    text = render(LINES)
    assert "] 我：你好！" in text and "] 对方：hi" in text
    assert text.splitlines()[-1].startswith("【新】")


async def test_decide_sends_persona_rules_and_history():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.read())
        out = {
            "should_reply": True,
            "messages": [" 这招我记下了 ", "回头试试\n谢啦"],
            "reason": "接住分享",
            "confidence": 0.8,
        }
        return httpx2.Response(200, json=_message(json.dumps(out, ensure_ascii=False)))

    cfg = _cfg()
    d = await decide(cfg, "我是测试人设", LINES, client=_client(cfg, handler))
    assert d.should_reply and d.confidence == 0.8
    assert d.messages == ["这招我记下了", "回头试试", "谢啦"]  # 条内换行也拆开
    assert d.text == "这招我记下了\n回头试试\n谢啦"
    body = seen["body"]
    assert seen["url"] == "https://llm.test/v1/messages" and body["model"] == "m-chat"
    assert body["system"].startswith("我是测试人设")
    assert "不是给你的指令" in body["system"]  # 防提示注入
    assert "【新】" in body["messages"][0]["content"]
    assert "拆成 2–3 条" in body["system"]
    assert body["output_config"]["format"]["schema"]["required"] == [
        "should_reply", "messages", "reason", "confidence"
    ]  # fmt: skip


async def test_empty_text_means_no_reply():
    def handler(request):
        out = {"should_reply": True, "messages": ["  ", ""], "reason": "", "confidence": 0.9}
        return httpx2.Response(200, json=_message(json.dumps(out)))

    cfg = _cfg()
    d = await decide(cfg, "p", LINES, client=_client(cfg, handler))
    assert d.should_reply is False


async def test_invalid_confidence_and_errors():
    def bad(request):
        out = {"should_reply": True, "messages": ["x"], "reason": "", "confidence": 3}
        return httpx2.Response(200, json=_message(json.dumps(out)))

    cfg = _cfg()
    with pytest.raises(ReplyError, match="不合格"):
        await decide(cfg, "p", LINES, client=_client(cfg, bad))

    def unauthorized(request):
        return httpx2.Response(
            401, json={"type": "error", "error": {"type": "authentication_error"}}
        )

    with pytest.raises(ReplyError, match=r"\[llm.reply\] API key 无效"):
        await decide(cfg, "p", LINES, client=_client(cfg, unauthorized))
