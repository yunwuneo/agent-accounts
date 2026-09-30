"""回复模型工具调用（S1）：多轮循环、预算、错误结果、本地查询工具、自动回复接线。

模型用 MockTransport 模拟，不访问网络；不开浏览器。
"""

from __future__ import annotations

import json
from pathlib import Path

import anthropic
import httpx2
import pytest

from agent_accounts.adapters.douyin import autoreply, im
from agent_accounts.adapters.douyin import store as dstore
from agent_accounts.adapters.xiaohongshu import autoreply as xautoreply
from agent_accounts.adapters.xiaohongshu import im as xim
from agent_accounts.adapters.xiaohongshu import store as xstore
from agent_accounts.core import config, digests
from agent_accounts.core.config import LLMEndpoint
from agent_accounts.core.llm import ToolUse
from agent_accounts.core.reply import ChatLine, ReplyDecision, ReplyError, decide, make_client
from agent_accounts.core.reply_tools import RefBook, ReplyToolbox, describe_calls
from agent_accounts.core.run import start_run

FIXTURES = Path(__file__).parent / "fixtures"
CONV = "0:1:10000001:10000002"
FIRST_SHARE = "7643765066207320442"  # fixture 里最早的一条分享
DECISION = {"should_reply": True, "messages": ["好呀"], "reason": "接话", "confidence": 0.8}


def _message(content: list[dict], stop_reason: str) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "m-chat",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


def _tool_use(id_: str, name: str, args: dict) -> dict:
    return {"type": "tool_use", "id": id_, "name": name, "input": args}


def _final(decision: dict = DECISION) -> dict:
    text = json.dumps(decision, ensure_ascii=False)
    return _message([{"type": "text", "text": text}], "end_turn")


def _cfg(**kw) -> LLMEndpoint:
    return LLMEndpoint(base_url="https://llm.test", api_key="sk-reply-key", model="m-chat", **kw)


class _Script:
    """按顺序返回预设的响应，并记下每次请求体。"""

    def __init__(self, *responses: dict):
        self.responses = list(responses)
        self.bodies: list[dict] = []

    def handler(self, request):
        self.bodies.append(json.loads(request.read()))
        return httpx2.Response(200, json=self.responses.pop(0))

    def client(self, cfg):
        http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self.handler))
        return make_client(cfg, http_client=http)


class _Source:
    platform = "douyin"

    def __init__(self, refs: RefBook | None = None, fail: Exception | None = None):
        self.refs = refs
        self.fail = fail
        self.searched: list[str] = []

    def older(self, limit):
        if self.fail:
            raise self.fail
        return [ChatLine(False, None, f"更早的消息（{limit} 条）")]

    def search(self, query, limit):
        self.searched.append(query)
        return []

    def peer_info(self):
        return {"昵称": "测试对方", "关系": "互相关注"}


def _toolbox(source=None, refs=None, max_rounds=4, audit=None) -> ReplyToolbox:
    refs = refs or RefBook()
    return ReplyToolbox(source or _Source(refs), refs, "conv-1", max_rounds=max_rounds, audit=audit)


LINES = [ChatLine(False, None, "还记得上次那个视频吗", is_new=True)]


# ---- 多轮循环 ----


async def test_parallel_tool_calls_then_decision():
    script = _Script(
        _message(
            [
                {"type": "text", "text": "我先查一下"},
                _tool_use("tu_1", "get_peer_info", {}),
                _tool_use("tu_2", "search_messages", {"query": "视频", "limit": 5}),
            ],
            "tool_use",
        ),
        _final(),
    )
    audits = []
    box = _toolbox(audit=lambda action, **kw: audits.append((action, kw)))
    cfg = _cfg()
    d = await decide(cfg, "人设", LINES, tools=box, client=script.client(cfg))

    assert d.should_reply and d.messages == ["好呀"]
    first, second = script.bodies
    assert [t["name"] for t in first["tools"]] == [
        "get_older_messages",
        "search_messages",
        "get_share_detail",
        "get_peer_info",
    ]
    assert all(t["strict"] for t in first["tools"])
    assert first["tool_choice"] == {"type": "auto"}
    assert "output_config" in first and "可以先调用工具" in first["system"]
    # 两个结果放在同一条 user 消息里，按调用顺序对应
    results = second["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_1", "tu_2"]
    assert "测试对方" in results[0]["content"] and not results[0]["is_error"]
    assert "没有找到" in results[1]["content"]
    assert second["messages"][1]["role"] == "assistant"  # 历史原样追加
    # 调用记录和审计只有摘要，不含查询词
    assert [c.name for c in box.calls] == ["get_peer_info", "search_messages"]
    assert box.calls[1].args == {"query_len": 2, "limit": 5, "got": 0}
    assert audits[1][0] == "douyin.reply.tool" and "视频" not in json.dumps(
        audits, ensure_ascii=False
    )


async def test_last_round_disables_tools():
    script = _Script(
        _message([_tool_use("tu_1", "get_older_messages", {"limit": 10})], "tool_use"),
        _final(),
    )
    cfg = _cfg()
    box = _toolbox(max_rounds=1)
    await decide(cfg, "人设", LINES, tools=box, client=script.client(cfg))
    assert [b["tool_choice"]["type"] for b in script.bodies] == ["auto", "none"]


async def test_tool_call_after_limit_is_an_error():
    loop = _message([_tool_use("tu_1", "get_peer_info", {})], "tool_use")
    script = _Script(loop, loop)
    cfg = _cfg()
    box = _toolbox(max_rounds=1)
    with pytest.raises(ReplyError, match="上限"):
        await decide(cfg, "人设", LINES, tools=box, client=script.client(cfg))
    assert len(box.calls) == 1  # 最后一轮的工具调用没有执行


async def test_bad_arguments_come_back_as_errors():
    script = _Script(
        _message(
            [
                _tool_use("tu_1", "get_share_detail", {"ref": "S9"}),
                _tool_use("tu_2", "search_messages", {"query": " ", "limit": 3}),
                _tool_use("tu_3", "open_profile", {}),
            ],
            "tool_use",
        ),
        _final(),
    )
    cfg = _cfg()
    box = _toolbox()
    await decide(cfg, "人设", LINES, tools=box, client=script.client(cfg))
    results = script.bodies[1]["messages"][-1]["content"]
    assert all(r["is_error"] for r in results)
    assert "没有编号为 S9" in results[0]["content"]
    assert "没有叫 open_profile" in results[2]["content"]
    assert [c.ok for c in box.calls] == [False, False, False]


async def test_unexpected_errors_abort_the_decision():
    script = _Script(_message([_tool_use("tu_1", "get_older_messages", {"limit": 5})], "tool_use"))
    cfg = _cfg()
    box = _toolbox(source=_Source(fail=RuntimeError("撞到风控")))
    with pytest.raises(RuntimeError, match="风控"):
        await decide(cfg, "人设", LINES, tools=box, client=script.client(cfg))


async def test_without_structured_output():
    script = _Script(
        _message([_tool_use("tu_1", "get_peer_info", {})], "tool_use"),
        _message([{"type": "text", "text": "结论：" + json.dumps(DECISION)}], "end_turn"),
    )
    cfg = _cfg(structured_output=False)
    d = await decide(cfg, "人设", LINES, tools=_toolbox(), client=script.client(cfg))
    assert d.confidence == 0.8
    first = script.bodies[0]
    assert "output_config" not in first and "strict" not in first["tools"][0]
    assert "只输出一个 JSON" in first["system"]


async def test_limit_is_clamped():
    box = _toolbox()
    out = await box.execute([ToolUse("tu_1", "get_older_messages", {"limit": 500})])
    assert "30 条" in out[0].content and box.calls[0].args["limit"] == 30


async def test_share_detail_uses_digest():
    refs = RefBook()
    ref = refs.share("item-1")
    digests.save(
        digests.MediaDigest(
            platform="douyin", item_id="item-1", kind="video", title="猫咪跳舞", summary="一只猫",
            vibe="可爱", reply_hooks_json='["问猫几岁"]', transcript="喵" * 3000,
            notes_json='["语音转写只到一半"]',
        )
    )  # fmt: skip
    box = _toolbox(refs=refs)
    out = await box.execute([ToolUse("tu_1", "get_share_detail", {"ref": ref.lower()})])
    text = out[0].content
    assert "标题：猫咪跳舞" in text and "可以聊的点：问猫几岁" in text
    assert "后面省略" in text and "语音转写只到一半" in text
    missing = await box.execute([ToolUse("tu_2", "get_share_detail", {"ref": refs.share("x")})])
    assert "还没有分析" in missing[0].content and not missing[0].is_error


def test_describe_calls():
    box = _toolbox()
    box._run(ToolUse("tu_1", "get_peer_info", {}))
    assert describe_calls(box.calls_json())[0].startswith("get_peer_info() → ")
    assert describe_calls(None) == []


# ---- 抖音接线 ----


@pytest.fixture
def conv():
    users = im.parse_user_info(
        json.loads((FIXTURES / "douyin" / "user_info.json").read_text(encoding="utf-8"))
    )
    users = [
        im.ImUser(u.uid, u.sec_uid, u.nickname, follow_status=2, follower_status=1) for u in users
    ]
    dstore.apply([im.parse_response((FIXTURES / "douyin" / "init.pb").read_bytes())], users)
    return dstore.find_conversation(CONV)


def test_douyin_source_pages_back_and_searches(conv):
    refs = RefBook()
    context = dstore.list_messages(CONV, limit=5)
    source = autoreply.DouyinChatSource(conv, refs, context[0].msg_index)
    first = source.older(10)
    second = source.older(10)
    assert len(first) == 10 and len(second) == 5 and source.older(10) == []
    # 往前翻出来的分享也按出现顺序拿到编号，可以直接查详情
    ref = second[0].content.split("]")[0].split()[-1]
    assert ref == "S9" and refs.resolve(ref) == FIRST_SHARE
    found = source.search("文本29", 5)
    assert len(found) == 1 and found[0].from_me
    info = source.peer_info()
    assert info["关系"] == "互相关注" and info["本地记录里的消息数"] == 20


@pytest.fixture
def capture_decide(monkeypatch):
    seen = {}

    async def fake(cfg, persona_text, lines, **kw):
        seen["lines"], seen["tools"] = lines, kw.get("tools")
        if seen["tools"] is not None:
            await seen["tools"].execute([ToolUse("tu_1", "get_older_messages", {"limit": 3})])
        return ReplyDecision(should_reply=False, reason="只是试试")

    monkeypatch.setattr(autoreply, "decide", fake)
    return seen


async def test_douyin_decide_with_tools_records_calls(conv, capture_decide):
    cfg = config.Config(douyin=config.DouyinConfig(context_messages=5))
    with start_run("douyin", "test") as run:
        outcome = await autoreply.decide_for(cfg, run, conv, 2, tools=True)
    assert capture_decide["tools"] is not None
    assert any("[分享视频 S" in line.content for line in capture_decide["lines"])
    calls = json.loads(outcome.reply.tool_calls_json)
    assert calls[0]["name"] == "get_older_messages" and calls[0]["args"]["got"] == 3


async def test_douyin_tools_follow_config(conv, capture_decide):
    with start_run("douyin", "test") as run:
        off = await autoreply.decide_for(config.Config(), run, conv, 2)
    assert capture_decide["tools"] is None and off.reply.tool_calls_json == "[]"
    assert not any(" S1]" in line.content for line in capture_decide["lines"])  # 关闭时 prompt 不变
    on = config.Config(reply_tools=config.ReplyToolsConfig(enabled=True, max_rounds=2))
    with start_run("douyin", "test") as run:
        await autoreply.decide_for(on, run, conv, 2)
    assert capture_decide["tools"].max_rounds == 2


def test_reply_tools_config_rejects_unknown_keys():
    with pytest.raises(ValueError):
        config.ReplyToolsConfig(enable=True)


# ---- 小红书接线 ----


def test_xiaohongshu_source_and_note_refs():
    load = lambda name: json.loads(  # noqa: E731
        (FIXTURES / "xiaohongshu" / f"{name}.json").read_text(encoding="utf-8")
    )
    xstore.apply_chats(xim.parse_chats(load("chats")), xim.parse_unread(load("unread")))
    xstore.apply_messages(xim.parse_history(load("history")), "a" * 24)
    conv = xstore.get_conversation("b" * 24)
    refs = RefBook()
    lines = xautoreply.chat_lines(conv.peer_id, set(), 20, refs=refs)
    assert any("[分享视频笔记 S" in line.content for line in lines)
    context = xstore.list_messages(conv.peer_id, limit=2)
    source = xautoreply.XhsChatSource(conv, refs, context[0].store_id)
    older = source.older(30)
    assert older and all(line.content for line in older)
    assert source.older(30) == []
    assert source.peer_info()["关系"] == "互相关注"
    assert source.search("一条视频笔记", 5)
