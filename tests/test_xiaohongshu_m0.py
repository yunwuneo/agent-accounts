"""小红书 M0：不接触真实账号或网络的护栏测试。"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from agent_accounts.adapters.xiaohongshu.cli import app
from agent_accounts.adapters.xiaohongshu.doctor import detect_block
from agent_accounts.adapters.xiaohongshu.login import login
from agent_accounts.adapters.xiaohongshu.netmeta import MetadataRecorder, endpoint_shape
from agent_accounts.core import paths, store
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired


def test_cli_exposes_login_and_doctor_subcommands():
    for command in ("login", "doctor"):
        result = CliRunner().invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage: " in result.output
        assert command in result.output


async def test_login_rejects_noninteractive_invocation(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(HumanRequired, match="交互终端"):
        await login(Config(), None)
    assert not (paths.home() / "profiles" / "xiaohongshu").exists()


def test_login_respects_frozen_account():
    store.set_account_status("xiaohongshu", "frozen")
    result = CliRunner().invoke(app, ["login"])
    assert result.exit_code == 3
    assert not (paths.home() / "profiles" / "xiaohongshu").exists()


def test_endpoint_shape_discards_identifiers_and_query():
    value = endpoint_shape("https://edith.xiaohongshu.com/api/sns/v1/chat/12345?token=secret")
    assert value == "xiaohongshu.com/api/sns/v1/chat/:id"
    assert endpoint_shape("https://other.example/private") == "external"


def test_recorder_never_reads_body_or_records_query(tmp_path):
    class Request:
        resource_type = "fetch"
        method = "GET"

    class Response:
        request = Request()
        url = "https://www.xiaohongshu.com/api/chat/user-id?token=secret"
        headers = {"content-type": "application/json; charset=utf-8", "content-length": "42"}
        status = 200

        def body(self):
            raise AssertionError("response body must not be read")

    class WebSocket:
        url = "wss://www.xiaohongshu.com/api/chat/user-id?token=secret"

    recorder = MetadataRecorder()
    recorder.on_response(Response())
    recorder.on_websocket(WebSocket())
    output = tmp_path / "netmeta.jsonl"
    recorder.save(output)
    data = output.read_text()
    assert "secret" not in data and "user-id" not in data
    assert "application/json" in data and '"size": 42' in data
    assert '"transport": "websocket"' in data


async def test_detect_block_only_when_visible():
    class Element:
        def __init__(self, visible):
            self.visible = visible

        async def is_visible(self):
            return self.visible

    class Locator:
        def __init__(self, visible):
            self.items = [Element(visible)]

        async def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class Page:
        def __init__(self, visible, url="https://www.xiaohongshu.com/chat"):
            self.url = url
            self.frames = [self]
            self.main_frame = self
            self.visible = visible

        def get_by_text(self, _pattern):
            return Locator(self.visible)

    assert not await detect_block(Page(False))
    assert await detect_block(Page(True))
    assert await detect_block(Page(False, "https://www.xiaohongshu.com/verify"))
