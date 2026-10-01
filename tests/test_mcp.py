"""MCP server 测试：进程内 Client 连接，数据用脱敏 fixture，AGENT_ACCOUNTS_HOME 指向临时目录。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.client import Client

from agent_accounts.adapters.douyin import im
from agent_accounts.adapters.douyin import store as dstore
from agent_accounts.core import persona
from agent_accounts.mcp_server import server

FIXTURES = Path(__file__).parent / "fixtures" / "douyin"
CONV = "0:1:10000001:10000002"


@pytest.fixture
def synced(isolated_home):
    users = im.parse_user_info(
        json.loads((FIXTURES / "user_info.json").read_text(encoding="utf-8"))
    )
    dstore.apply([im.parse_response((FIXTURES / "init.pb").read_bytes())], users)
    return dstore.find_conversation(CONV)


async def _call(name: str, **args):
    async with Client(server) as c:
        return await c.call_tool(name, args)


def _data(result):
    assert not result.is_error, result.content
    return result.structured_content


async def test_tools_are_annotated():
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {
        "douyin_list_conversations", "douyin_recent_messages",
        "douyin_android_read_snapshot",
        "get_persona", "update_persona", "get_recent", "update_recent",
    }  # fmt: skip
    assert tools["douyin_recent_messages"].annotations.read_only_hint is True
    assert tools["douyin_android_read_snapshot"].annotations.read_only_hint is True
    assert tools["update_persona"].annotations.read_only_hint is False
    assert not any("send" in name for name in tools)  # 不暴露发送


async def test_recent_messages_for_one_conversation(synced):
    conv = synced
    data = _data(await _call("douyin_recent_messages", conversation=conv.name, limit=5))
    msgs = data["messages"]
    assert len(msgs) == 5 and all(m["conv_id"] == CONV for m in msgs)
    expected = dstore.list_messages(CONV, limit=5)
    assert [m["sent_at"] for m in msgs] == [e.sent_at.isoformat() for e in expected]
    assert [m["from_me"] for m in msgs] == [e.from_me for e in expected]


async def test_recent_messages_across_conversations_and_unknown(synced):
    data = _data(await _call("douyin_recent_messages", limit=3))
    assert len(data["messages"]) == 3
    assert data["messages"][-1]["conversation"] == synced.name

    result = await _call("douyin_recent_messages", conversation="不存在的人")
    assert result.is_error


async def test_list_conversations(synced):
    data = _data(await _call("douyin_list_conversations"))
    rows = data["result"]
    assert rows[0]["conv_id"] == CONV and rows[0]["name"] == synced.name


async def test_update_persona_backs_up_and_audits(isolated_home):
    old = persona.load()  # 生成占位人设
    _data(await _call("update_persona", content="  新的人设  "))
    assert persona.load() == "新的人设"
    backups = list((isolated_home / "history").glob("persona-*.md"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8").strip() == old
    assert _data(await _call("get_persona"))["persona"] == "新的人设"

    assert (await _call("update_persona", content="  ")).is_error  # 不能清空
    assert persona.load() == "新的人设"


async def test_update_recent_and_clear(isolated_home):
    assert _data(await _call("get_recent")) == {"recent": "", "updated_at": None}
    _data(await _call("update_recent", content="这周在学吉他，手指很疼"))
    got = _data(await _call("get_recent"))
    assert got["recent"] == "这周在学吉他，手指很疼" and got["updated_at"]

    assert (await _call("update_recent", content="长" * 2001)).is_error
    _data(await _call("update_recent", content=""))
    assert persona.load_recent() == ""
    assert len(list((isolated_home / "history").glob("recent-*.md"))) == 1


async def test_audit_does_not_store_content(isolated_home):
    from sqlmodel import select

    from agent_accounts.core import store

    _data(await _call("update_recent", content="秘密近况内容"))
    with store.session() as s:
        events = s.exec(select(store.AuditEvent)).all()
    assert any(e.action == "recent.update" for e in events)
    assert all("秘密近况内容" not in e.detail for e in events)
