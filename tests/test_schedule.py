from __future__ import annotations

from datetime import datetime

import pytest
from typer.testing import CliRunner

from agent_accounts.adapters.douyin import autoreply
from agent_accounts.adapters.douyin.cli import app
from agent_accounts.core import config, paths
from agent_accounts.core.schedule import parse_windows, quiet_until

WINDOWS = parse_windows(["03:00-08:00", "11:00-12:00"])


def at(hh: int, mm: int, day: int = 26) -> datetime:
    return datetime(2026, 9, day, hh, mm, 30)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (at(2, 59), None),
        (at(3, 0), at(8, 0).replace(second=0)),
        (at(7, 59), at(8, 0).replace(second=0)),
        (at(8, 0), None),  # 左闭右开
        (at(11, 30), at(12, 0).replace(second=0)),
        (at(12, 0), None),
    ],
)
def test_quiet_until(now, expected):
    assert quiet_until(now, WINDOWS) == expected


def test_overnight_and_adjacent_windows_merge():
    ws = parse_windows(["23:00-02:00", "01:00-05:00"])  # 跨午夜 + 重叠：23:00 休息到次日 05:00
    assert quiet_until(at(23, 10), ws) == datetime(2026, 9, 27, 5, 0)
    assert quiet_until(at(1, 30), ws) == at(5, 0).replace(second=0)
    assert quiet_until(at(22, 59), ws) is None
    adjacent = parse_windows(["03:00-08:00", "08:00-09:00"])
    assert quiet_until(at(3, 10), adjacent) == at(9, 0).replace(second=0)
    assert quiet_until(at(0, 30), parse_windows(["22:00-07:00"])) == at(7, 0).replace(second=0)
    assert quiet_until(at(12, 0), []) is None


@pytest.mark.parametrize(
    "bad", [["3:00~8:00"], ["25:00-08:00"], ["03:00-24:30"], ["08:00-08:00"], ["00:00-24:00"]]
)
def test_invalid_windows_rejected(bad):
    with pytest.raises(ValueError):
        parse_windows(bad)


def test_windows_covering_whole_day_rejected():
    with pytest.raises(ValueError, match="一整天"):
        parse_windows(["00:00-12:00", "12:00-24:00"])


def test_config_validates_quiet_hours(isolated_home):
    paths.ensure_dir(isolated_home)
    paths.config_path().write_text('[douyin]\nquiet_hours = ["03:00-08:00"]\n', encoding="utf-8")
    assert config.load().douyin.quiet_hours == ["03:00-08:00"]
    paths.config_path().write_text('[douyin]\nquiet_hours = ["3点-8点"]\n', encoding="utf-8")
    with pytest.raises(config.ConfigError, match="quiet_hours"):
        config.load()


def test_run_once_in_quiet_hours_skips_without_opening_browser(monkeypatch):
    async def must_not_run(*a, **kw):
        raise AssertionError("休息时段不应打开浏览器")

    monkeypatch.setattr(config, "load", lambda: config.Config())
    monkeypatch.setattr(config.DouyinConfig, "quiet_until", lambda self, now: at(8, 0))
    monkeypatch.setattr(autoreply, "run_once", must_not_run)
    result = CliRunner().invoke(app, ["run", "--once", "--allow-send"])
    assert result.exit_code == 0, result.output
    assert "休息时段" in result.output and "本轮跳过" in result.output


def test_sync_watch_rests_then_resumes(isolated_home, monkeypatch):
    """休息结束后才同步；醒来后照常继续（这里靠冻结让循环停下）。"""
    from agent_accounts.adapters.douyin import sync as dsync
    from agent_accounts.core import store

    quiet = [datetime(2000, 1, 1)]  # 第一次判断在休息中，结束时刻已过，不用真的等
    calls = []

    async def fake_sync(cfg, run, *, save_raw=False):
        calls.append(run.id)
        store.set_account_status("douyin", "frozen")
        return dsync.SyncResult(source="api")

    cfg = config.Config(
        douyin=config.DouyinConfig(interval_min_s=0, interval_max_s=0, quiet_wake_jitter_s=0)
    )
    monkeypatch.setattr(config, "load", lambda: cfg)
    monkeypatch.setattr(
        config.DouyinConfig, "quiet_until", lambda self, now: quiet.pop() if quiet else None
    )
    monkeypatch.setattr(dsync, "sync", fake_sync)
    result = CliRunner().invoke(app, ["sync", "--watch"])
    assert result.exit_code == 3, result.output
    assert len(calls) == 1
    assert result.output.index("休息时段") < result.output.index("同步")
