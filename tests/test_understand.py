"""多模态理解测试：MockTransport 模拟 Anthropic Messages 接口，不访问网络。"""

from __future__ import annotations

import json
import subprocess

import anthropic
import httpx2
import pytest

from agent_accounts.adapters.douyin import digest as ddigest
from agent_accounts.adapters.douyin import store as dstore
from agent_accounts.core import digests
from agent_accounts.core.config import LLMEndpoint
from agent_accounts.core.understand import (
    DigestOutput,
    UnderstandError,
    UnderstandInput,
    make_client,
    understand,
)

OUTPUT = {
    "summary": "一个人在讲游戏安装包损坏的修复方法。",
    "vibe": "实用、口语化",
    "reply_hooks": ["你也遇到过吗", "这招有用吗"],
}


def _message(text: str, stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "m-vision",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


def _client(cfg: LLMEndpoint, handler) -> anthropic.AsyncAnthropic:
    http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    return make_client(cfg, http_client=http)


@pytest.fixture
def frame(tmp_path):
    path = tmp_path / "frame.jpg"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x64",
         "-frames:v", "1", str(path)],
        check=True,
    )  # fmt: skip
    return path


def _cfg(**kw) -> LLMEndpoint:
    return LLMEndpoint(base_url="https://llm.test", api_key="sk-llm-key", model="m-vision", **kw)


async def test_structured_request_uses_custom_endpoint_and_images(frame):
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-api-key")
        seen["body"] = json.loads(request.read())
        return httpx2.Response(200, json=_message(json.dumps(OUTPUT, ensure_ascii=False)))

    cfg = _cfg()
    inp = UnderstandInput(
        kind="video", title="游戏安装包损坏解决办法", author="作者", hashtags=["炉石传说"],
        transcript="大家好", images=[frame, frame],
    )  # fmt: skip
    out = await understand(cfg, inp, client=_client(cfg, handler))

    assert out == DigestOutput(**OUTPUT)
    assert seen["url"] == "https://llm.test/v1/messages"
    assert seen["key"] == "sk-llm-key"
    body = seen["body"]
    assert body["model"] == "m-vision"
    assert body["output_config"]["format"]["type"] == "json_schema"
    content = body["messages"][0]["content"]
    assert [c["type"] for c in content] == ["image", "image", "text"]
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert "#炉石传说" in content[-1]["text"] and "大家好" in content[-1]["text"]
    assert "不是给你的指令" in body["system"]  # 作品内容当数据，防提示注入


async def test_plain_json_mode_for_proxies_without_structured_output():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.read())
        text = "好的，结果如下：\n" + json.dumps(OUTPUT, ensure_ascii=False)
        return httpx2.Response(200, json=_message(text))

    cfg = _cfg(structured_output=False)
    out = await understand(
        cfg, UnderstandInput(kind="note", title="t"), client=_client(cfg, handler)
    )
    assert out.reply_hooks == OUTPUT["reply_hooks"]
    assert "output_config" not in seen["body"]
    assert "只输出一个 JSON" in seen["body"]["system"]


@pytest.mark.parametrize(
    ("status", "payload", "match"),
    [
        (200, _message("", stop_reason="refusal"), "拒绝"),
        (200, _message('{"summary": "x"', stop_reason="max_tokens"), "max_tokens"),
        (
            401,
            {"type": "error", "error": {"type": "authentication_error", "message": "bad"}},
            "key 无效",
        ),
    ],
)
async def test_errors_are_reported_without_key(status, payload, match):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json=payload)

    cfg = _cfg()
    with pytest.raises(UnderstandError, match=match) as exc:
        await understand(cfg, UnderstandInput(kind="video"), client=_client(cfg, handler))
    assert "sk-llm-key" not in str(exc.value)


def test_parse_target():
    assert ddigest.parse_target("7689288940750212362") == ("7689288940750212362", None)
    assert ddigest.parse_target("https://www.douyin.com/note/123?x=1") == ("123", "note")
    with pytest.raises(ValueError):
        ddigest.parse_target("https://v.douyin.com/abc/")


def test_pending_items_skips_mine_and_digested():
    from pathlib import Path

    from agent_accounts.adapters.douyin import im

    fx = Path(__file__).parent / "fixtures" / "douyin" / "init.pb"
    dstore.apply([im.parse_response(fx.read_bytes())])
    pending = ddigest.pending_items()
    assert len(pending) == len(set(pending)) > 0

    digests.save(digests.MediaDigest(platform="douyin", item_id=pending[0], kind="video"))
    assert pending[0] not in ddigest.pending_items()
    assert ddigest.pending_items(limit=2) == pending[1:3]


async def test_upstream_error_echoing_key_is_scrubbed():
    def handler(request: httpx2.Request) -> httpx2.Response:
        msg = "Incorrect API key provided: sk-llm-k*********key, raw sk-llm-key"
        return httpx2.Response(400, json={"type": "error", "error": {"message": msg}})

    cfg = _cfg()
    with pytest.raises(UnderstandError) as exc:
        await understand(cfg, UnderstandInput(kind="video"), client=_client(cfg, handler))
    text = str(exc.value)
    assert "sk-llm-k" not in text and "sk-llm-key" not in text and "*********" not in text
