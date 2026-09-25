from __future__ import annotations

import json
import os
import stat

import pytest
from sqlmodel import select

from agent_accounts.core import audit, config, paths, store
from agent_accounts.core.errors import AccountFrozen, HumanRequired
from agent_accounts.core.run import start_run


def _mode(p) -> int:
    return stat.S_IMODE(p.stat().st_mode)


# Windows 的 NTFS 不用 Unix 权限位，这些断言只在 macOS / Linux 上有意义
posix_only = pytest.mark.skipif(os.name == "nt", reason="Windows 不使用 Unix 权限位")


@posix_only
def test_home_respects_env_and_dirs_are_private(isolated_home):
    assert paths.home() == isolated_home
    assert _mode(paths.profile_dir("douyin")) == 0o700
    assert _mode(paths.runs_dir()) == 0o700


@posix_only
def test_db_file_is_private():
    store.get_account("douyin")
    assert _mode(paths.db_path()) == 0o600


def test_config_defaults_and_toml(isolated_home):
    assert config.load().douyin.auto_reply == "dry_run"
    paths.ensure_dir(isolated_home)
    paths.config_path().write_text('[browser]\nheadless = true\nchannel = ""\n', encoding="utf-8")
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

    with pytest.raises(HumanRequired), start_run("douyin", "not-logged-in") as run:
        raise HumanRequired("未登录")
    assert _run_row(run.id).status == "blocked"
    assert store.get_account("douyin").status == "active"  # 未登录不冻结


def test_risk_control_auto_freezes_account():
    with pytest.raises(HumanRequired), start_run("douyin", "captcha") as run:
        raise HumanRequired("触发平台验证或风控", "安全验证", freeze=True)
    row = _run_row(run.id)
    assert row.status == "blocked" and "安全验证" in row.error
    assert store.get_account("douyin").status == "frozen"
    # 冻结后下一次自动运行直接被拒绝，直到人工解冻
    with pytest.raises(AccountFrozen), start_run("douyin", "doctor"):
        pass


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


def test_scrub_url_hides_sensitive_query():
    from agent_accounts.browser.netlog import scrub_url

    url = "wss://x.test/ws/v2?aid=6383&access_key=abc&device_id=1&msToken=t&a_bogus=b&x=ok"
    assert scrub_url(url) == (
        "wss://x.test/ws/v2?aid=6383&access_key=***&device_id=***&msToken=***&a_bogus=***&x=ok"
    )


def _write_config(home, text: str, mode: int = 0o600):
    paths.ensure_dir(home)
    paths.config_path().write_text(text, encoding="utf-8")
    paths.config_path().chmod(mode)


def test_llm_endpoints_are_configurable_and_keys_hidden(isolated_home, monkeypatch):
    _write_config(
        isolated_home,
        '[llm.understand]\nbase_url = "https://proxy.test"\napi_key = "sk-secret-123"\n'
        'model = "m-vision"\n[llm.reply]\napi_key_env = "MY_REPLY_KEY"\nmodel = "m-chat"\n'
        '[transcribe]\nbase_url = "https://asr.test/v1"\napi_key = "sk-asr"\nmodel = "asr-1"\n',
    )
    monkeypatch.setenv("MY_REPLY_KEY", "sk-from-env")
    cfg = config.load()
    assert cfg.llm.understand.model == "m-vision"
    assert cfg.llm.understand.base_url == "https://proxy.test"
    assert cfg.llm.understand.key() == "sk-secret-123"
    assert cfg.llm.reply.key() == "sk-from-env"
    assert cfg.transcribe.model == "asr-1"
    # 打印配置对象、脱敏视图都不能出现明文 key
    for text in (repr(cfg), str(cfg.llm.understand.redacted()), cfg.model_dump_json()):
        assert "sk-secret-123" not in text and "sk-asr" not in text


@posix_only
def test_plain_key_requires_private_file(isolated_home):
    _write_config(isolated_home, '[llm.understand]\napi_key = "sk-x"\nmodel = "m"\n', mode=0o644)
    with pytest.raises(config.ConfigError, match="chmod 600"):
        config.load()


def test_missing_key_error_names_section(isolated_home, monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    _write_config(isolated_home, '[llm.understand]\napi_key_env = "NOPE_KEY"\nmodel = "m"\n')
    with pytest.raises(config.ConfigError, match="llm.understand"):
        config.load().llm.understand.require_key("llm.understand")


def test_key_pasted_into_api_key_env_is_rejected_without_echo(isolated_home):
    leaked = "ah-" + "0123456789abcdef" * 4
    _write_config(isolated_home, f'[llm.reply]\napi_key_env = "{leaked}"\nmodel = "m"\n')
    with pytest.raises(config.ConfigError) as exc:
        config.load()
    assert "api_key_env" in str(exc.value) and "环境变量名" in str(exc.value)
    assert leaked not in str(exc.value) and "0123456789abcdef" not in str(exc.value)
    assert exc.value.__cause__ is None  # 不链接原始 ValidationError（里面有输入值）


def test_other_validation_errors_do_not_echo_input(isolated_home):
    _write_config(isolated_home, '[llm.understand]\napi_key = 12345678\nmodel = "m"\n')
    with pytest.raises(config.ConfigError) as exc:
        config.load()
    assert "12345678" not in str(exc.value)


def test_douyin_unknown_key_is_rejected(isolated_home):
    # 写成 auto-reply 时不能静默回落到默认 dry_run
    _write_config(isolated_home, '[douyin]\nauto-reply = "on"\n')
    with pytest.raises(config.ConfigError) as exc:
        config.load()
    assert "douyin.auto-reply" in str(exc.value)


def test_migration_adds_new_columns_to_existing_table(isolated_home):
    import sqlite3

    paths.ensure_dir(isolated_home)
    # 模拟旧版本的库：accounts 表缺少 owner / status 列
    with sqlite3.connect(paths.db_path()) as db:
        db.execute(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, platform VARCHAR NOT NULL,"
            " handle VARCHAR, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        )
        db.execute(
            "INSERT INTO accounts (platform, created_at, updated_at)"
            " VALUES ('douyin', '2026-09-25 00:00:00', '2026-09-25 00:00:00')"
        )
    store._created_tables.clear()
    account = store.get_account("douyin")
    assert account.status == "active"  # 补列时带上了默认值
    assert account.owner is None


def test_persona_default_created_private(isolated_home):
    from agent_accounts.core import persona

    text = persona.load()
    assert "Echo" in text and "AI" in text
    if os.name != "nt":
        assert _mode(persona.path()) == 0o600
    persona.path().write_text("自定义人设", encoding="utf-8")
    assert persona.load() == "自定义人设"


def test_windows_config_check_uses_home_dir_not_mode_bits(isolated_home, monkeypatch):
    """模拟 Windows：不看权限位（NTFS 下总是 0o666），只要求配置文件在当前用户目录下。"""
    monkeypatch.setattr(config, "POSIX", False)
    _write_config(isolated_home, '[llm.understand]\napi_key = "sk-x"\nmodel = "m"\n', mode=0o666)
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: isolated_home.parent))
    assert config.load().llm.understand.key() == "sk-x"

    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: isolated_home / "other"))
    with pytest.raises(config.ConfigError, match="不在当前用户目录下"):
        config.load()


def test_console_setup_survives_gbk_output(monkeypatch):
    """模拟 Windows 管道输出（GBK）：emoji 不应让命令崩溃，中文照常输出。"""
    import io

    from agent_accounts.core import console

    buf = io.BytesIO()
    fake = io.TextIOWrapper(buf, encoding="gbk", newline="\n")  # Windows 默认会写成 \r\n
    monkeypatch.setattr("sys.stdout", fake)
    monkeypatch.setattr("sys.stderr", io.TextIOWrapper(io.BytesIO(), encoding="gbk"))
    console.setup()
    print("✅ 已发送")
    fake.flush()
    assert buf.getvalue().decode("gbk") == "? 已发送\n"
