from __future__ import annotations

import json
import stat

import pytest
from sqlmodel import select

from agent_accounts.core import audit, config, paths, store
from agent_accounts.core.errors import AccountFrozen, HumanRequired
from agent_accounts.core.run import start_run


def _mode(p) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_home_respects_env_and_dirs_are_private(isolated_home):
    assert paths.home() == isolated_home
    assert _mode(paths.profile_dir("douyin")) == 0o700
    assert _mode(paths.runs_dir()) == 0o700


def test_db_file_is_private():
    store.get_account("douyin")
    assert _mode(paths.db_path()) == 0o600


def test_config_defaults_and_toml(isolated_home):
    assert config.load().douyin.auto_reply == "dry_run"
    paths.ensure_dir(isolated_home)
    paths.config_path().write_text('[browser]\nheadless = true\nchannel = ""\n')
    cfg = config.load()
    assert cfg.browser.headless is True
    assert cfg.douyin.auto_reply == "dry_run"


def test_redact_nested_secrets():
    data = {"user": "a", "Cookie": "x", "nested": [{"sessionid": "s", "ok": 1}], "api_token": "t"}
    assert audit.redact(data) == {
        "user": "a",
        "Cookie": "***",
        "nested": [{"sessionid": "***", "ok": 1}],
        "api_token": "***",
    }


def test_audit_record_is_redacted():
    audit.record("douyin", "test", password="p", note="hi")
    with store.session() as s:
        event = s.exec(select(store.AuditEvent)).one()
    assert json.loads(event.detail) == {"password": "***", "note": "hi"}


def _run_row(run_id: str) -> store.Run:
    with store.session() as s:
        return s.get(store.Run, run_id)


def test_run_status_ok_failed_blocked():
    with start_run("douyin", "ok") as run:
        pass
    assert _run_row(run.id).status == "ok"

    with pytest.raises(ValueError), start_run("douyin", "bad") as run:
        raise ValueError("boom")
    assert _run_row(run.id).status == "failed"

    with pytest.raises(HumanRequired), start_run("douyin", "captcha") as run:
        raise HumanRequired("触发平台验证或风控", "安全验证")
    row = _run_row(run.id)
    assert row.status == "blocked" and "安全验证" in row.error


def test_frozen_account_refuses_runs():
    store.set_account_status("douyin", "frozen")
    with pytest.raises(AccountFrozen), start_run("douyin", "doctor"):
        pass
    # 登录这类人工操作不受冻结影响
    with start_run("douyin", "login", require_active=False):
        pass
    store.set_account_status("douyin", "active")
    with start_run("douyin", "doctor"):
        pass
