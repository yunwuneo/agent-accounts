"""HTTP MCP 测试：Bearer 鉴权、token 写入配置、真实 uvicorn + MCP 客户端端到端。"""

from __future__ import annotations

import os
import socket
import stat
import threading
import time

import httpx2
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from typer.testing import CliRunner

from agent_accounts.cli import app
from agent_accounts.core import config, paths
from agent_accounts.mcp_server import BearerAuth, http_app

TOKEN = "t" * 43


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 401),
        ({"Authorization": "Bearer wrong"}, 401),
        ({"Authorization": TOKEN}, 401),  # 缺 Bearer 前缀
        ({"Authorization": f"Bearer {TOKEN}"}, 200),
    ],
)
async def test_bearer_auth(headers, status):
    transport = httpx2.ASGITransport(app=BearerAuth(_ok_app, TOKEN))
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/mcp", headers=headers)
    assert r.status_code == status
    if status == 401:
        assert r.headers["www-authenticate"].startswith("Bearer")


def test_http_requires_token(isolated_home):
    with pytest.raises(config.ConfigError, match="mcp-token"):
        http_app(config.McpConfig())
    result = CliRunner().invoke(app, ["mcp", "--http"])
    assert result.exit_code == 4 and "mcp-token" in result.output


def test_short_token_rejected(isolated_home):
    paths.ensure_dir(paths.home())
    paths.config_path().write_text('[mcp]\ntoken = "short"\n', encoding="utf-8")
    if os.name != "nt":
        paths.config_path().chmod(0o600)
    with pytest.raises(config.ConfigError, match="token 太短"):
        config.load()


def test_mcp_token_command_creates_config(isolated_home):
    result = CliRunner().invoke(app, ["mcp-token"])
    assert result.exit_code == 0, result.output
    token = result.stdout.strip().splitlines()[-1]
    assert len(token) >= config.MIN_TOKEN_CHARS
    assert config.load().mcp.bearer() == token
    if os.name != "nt":
        assert stat.S_IMODE(paths.config_path().stat().st_mode) == 0o600

    again = CliRunner().invoke(app, ["mcp-token"])
    assert again.exit_code == 4 and "--rotate" in again.output
    assert config.load().mcp.bearer() == token  # 没被覆盖

    rotated = CliRunner().invoke(app, ["mcp-token", "--rotate"])
    new = rotated.stdout.strip().splitlines()[-1]
    assert rotated.exit_code == 0 and new != token and config.load().mcp.bearer() == new
    assert paths.config_path().read_text(encoding="utf-8").count("token =") == 1


def test_write_token_keeps_other_content(isolated_home):
    original = (
        "# 我的配置\n"
        "[douyin]\n"
        'auto_reply = "dry_run"  # 先观察\n'
        "\n"
        "[mcp]\n"
        'host = "0.0.0.0"\n'
        "port = 9000\n"
        "\n"
        "[guard]\n"
        "max_len = 100\n"
    )
    paths.ensure_dir(paths.home())
    paths.config_path().write_text(original, encoding="utf-8")
    config.write_mcp_token(TOKEN)
    text = paths.config_path().read_text(encoding="utf-8")
    assert "# 我的配置" in text and "# 先观察" in text
    cfg = config.load()
    assert cfg.mcp.host == "0.0.0.0" and cfg.mcp.port == 9000 and cfg.mcp.bearer() == TOKEN
    assert cfg.guard.max_len == 100 and cfg.douyin.auto_reply == "dry_run"
    # token 写在 [mcp] 段里，不会跑到 [guard] 下面
    assert text.index("token =") < text.index("[guard]")


def test_write_token_refuses_invalid_toml(isolated_home):
    paths.ensure_dir(paths.home())
    paths.config_path().write_text("[mcp\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="不是合法的 TOML"):
        config.write_mcp_token(TOKEN)
    assert paths.config_path().read_text(encoding="utf-8") == "[mcp\n"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server(request, isolated_home):
    extra = getattr(request, "param", {})
    cfg = config.McpConfig(port=_free_port(), token=TOKEN, **extra)
    server = uvicorn.Server(
        uvicorn.Config(http_app(cfg), host=cfg.host, port=cfg.port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://{cfg.host}:{cfg.port}{cfg.path}"
    server.should_exit = True
    thread.join(timeout=10)


async def test_http_end_to_end(http_server):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    http = httpx2.AsyncClient(headers=headers, timeout=10)
    async with http, Client(streamable_http_client(http_server, http_client=http)) as c:
        await c.call_tool("update_recent", {"content": "HTTP 写入的近况"})
        got = await c.call_tool("get_recent", {})
    assert got.structured_content["recent"] == "HTTP 写入的近况"

    async with httpx2.AsyncClient(timeout=5) as http:
        r = await http.post(http_server, json={})
    assert r.status_code == 401


def test_config_show_hides_token(isolated_home):
    config.write_mcp_token(TOKEN)
    result = CliRunner().invoke(app, ["config", "show"])
    assert result.exit_code == 0 and TOKEN not in result.output
    assert "token（已设置）" in result.output


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


async def _post_via_proxy(url: str, host: str) -> int:
    """模拟 nginx / frp 转发：连的是本机端口，Host 头是对外域名。"""
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Host": host,
        "Accept": "application/json, text/event-stream",
    }
    async with httpx2.AsyncClient(timeout=10) as http:
        r = await http.post(url, json=INIT, headers=headers)
    return r.status_code


async def test_proxy_host_rejected_without_allowed_hosts(http_server):
    assert await _post_via_proxy(http_server, "mcp.example.com") == 421


@pytest.mark.parametrize("http_server", [{"allowed_hosts": ["mcp.example.com"]}], indirect=True)
async def test_proxy_host_allowed(http_server):
    assert await _post_via_proxy(http_server, "mcp.example.com") == 200
    assert await _post_via_proxy(http_server, "mcp.example.com:443") == 200
    assert await _post_via_proxy(http_server, "evil.example.com") == 421  # 防护仍然开启
    assert await _post_via_proxy(http_server, http_server.split("/")[2]) == 200  # 本机直连照常


def test_allowed_hosts_rejects_urls():
    with pytest.raises(ValueError, match="不带 http"):
        config.McpConfig(allowed_hosts=["https://mcp.example.com"])


def test_mcp_check_passes(http_server):
    config.write_mcp_token(TOKEN)
    result = CliRunner().invoke(app, ["mcp-check", "--url", http_server, "--timeout", "5"])
    assert result.exit_code == 0, result.output
    assert "调用 get_persona" in result.output and "全部通过" in result.output
    assert TOKEN not in result.output


def test_mcp_check_reports_stuck_step(http_server, monkeypatch):
    from agent_accounts.core import persona

    def slow_load():
        time.sleep(3)  # 模拟调用卡住（如代理缓冲了响应）
        return "p"

    monkeypatch.setattr(persona, "load", slow_load)
    config.write_mcp_token(TOKEN)
    result = CliRunner().invoke(app, ["mcp-check", "--url", http_server, "--timeout", "1"])
    assert result.exit_code == 1
    assert "✅ 列出工具" in result.output and "❌ 调用 get_persona" in result.output
    assert "超时" in result.output


def test_mcp_check_wrong_token(http_server):
    config.write_mcp_token("x" * 43)
    result = CliRunner().invoke(app, ["mcp-check", "--url", http_server, "--timeout", "5"])
    assert result.exit_code == 1 and "❌ 连接并握手" in result.output


async def test_access_and_tool_log(http_server, caplog):
    caplog.set_level("INFO", logger="agent_accounts.mcp")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    http = httpx2.AsyncClient(headers=headers, timeout=10)
    async with http, Client(streamable_http_client(http_server, http_client=http)) as c:
        await c.call_tool("update_recent", {"content": "不应出现在日志里"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "POST /mcp → 200" in text and "工具 update_recent 完成" in text
    assert "不应出现在日志里" not in text and TOKEN not in text
