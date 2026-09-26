from __future__ import annotations

import json

import httpx2
import pytest
from sqlmodel import select
from typer.testing import CliRunner

from agent_accounts.cli import app
from agent_accounts.core import alerts, config, paths, store

HOOK = "https://hook.test/send?access_token=tok123"


def _write_config(home, text: str):
    paths.ensure_dir(home)
    paths.config_path().write_text(text, encoding="utf-8")
    paths.config_path().chmod(0o600)


@pytest.fixture
def posts(monkeypatch):
    sent = []

    def fake_post(url, json, timeout):
        sent.append((url, json))
        return httpx2.Response(200)

    monkeypatch.setattr(alerts.httpx2, "post", fake_post)
    return sent


def _audit_details(action):
    with store.session() as s:
        rows = s.exec(select(store.AuditEvent).where(store.AuditEvent.action == action)).all()
    return [r.detail for r in rows]


def test_no_webhook_configured_only_prints(posts, capsys):
    alerts.alert("douyin", "critical", "需要人工介入")
    assert posts == []
    assert "需要人工介入" in capsys.readouterr().err


def test_webhook_respects_min_level_and_hides_url(isolated_home, posts):
    _write_config(isolated_home, f'[alerts]\nwebhook_url = "{HOOK}"\nmin_level = "warning"\n')
    alerts.alert("douyin", "info", "普通信息", run_id="r1")
    alerts.alert("douyin", "critical", "撞到验证码", run_id="r1")
    assert len(posts) == 1
    url, payload = posts[0]
    assert url == HOOK
    assert payload["level"] == "critical" and payload["run_id"] == "r1"
    assert "撞到验证码" in payload["text"]
    details = _audit_details("alert.webhook")
    assert len(details) == 1 and json.loads(details[0])["ok"] is True
    assert "tok123" not in "".join(details)


def test_webhook_from_env(isolated_home, posts, monkeypatch):
    monkeypatch.setenv("AA_HOOK", HOOK)
    _write_config(isolated_home, '[alerts]\nwebhook_url_env = "AA_HOOK"\n')
    alerts.alert("douyin", "warning", "发送未确认")
    assert posts and posts[0][0] == HOOK


def test_webhook_failure_never_raises(isolated_home, monkeypatch, capsys):
    _write_config(isolated_home, f'[alerts]\nwebhook_url = "{HOOK}"\n')

    def boom(url, json, timeout):
        raise httpx2.ConnectError("down")

    monkeypatch.setattr(alerts.httpx2, "post", boom)
    alerts.alert("douyin", "critical", "登录失效")
    err = capsys.readouterr().err
    assert "登录失效" in err and "ConnectError" in err and "tok123" not in err
    assert json.loads(_audit_details("alert.webhook")[0])["ok"] is False


def test_plain_webhook_url_requires_private_file(isolated_home):
    _write_config(isolated_home, f'[alerts]\nwebhook_url = "{HOOK}"\n')
    if config.POSIX:
        paths.config_path().chmod(0o644)
        with pytest.raises(config.ConfigError):
            config.load()


def test_config_show_and_alert_test_hide_url(isolated_home, posts):
    _write_config(isolated_home, f'[alerts]\nwebhook_url = "{HOOK}"\n')
    shown = CliRunner().invoke(app, ["config", "show"])
    assert "webhook_url（已设置）" in shown.output and "tok123" not in shown.output
    result = CliRunner().invoke(app, ["alert-test"])
    assert result.exit_code == 0, result.output
    assert len(posts) == 1 and "tok123" not in result.output


def test_alert_test_without_webhook_fails(posts):
    result = CliRunner().invoke(app, ["alert-test"])
    assert result.exit_code == 1 and posts == []
