import asyncio

import pytest
from test_android_autoreply import BASE, NEW, Phone

from agent_accounts.adapters.douyin.android import autoreply as auto
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.reply import ReplyDecision
from agent_accounts.core.run import start_run


@pytest.mark.asyncio
async def test_monitor_multiple_sends_without_replaying_echo_or_losing_next_peer():
    cfg = Config()
    cfg.douyin.android.allow_send = True
    cfg.douyin.android.auto_reply = "on"
    cfg.guard.min_interval_s = 0
    phone = Phone()
    ticks = calls = 0

    async def sleep(_):
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            phone.messages.append(NEW)
        elif ticks == 3:
            phone.messages.append(auto.Message(False, "text", "另一个问题"))
        elif ticks == 5:
            raise asyncio.CancelledError

    async def decide(*args, **kwargs):
        nonlocal calls
        calls += 1
        assert sum(line.is_new for line in args[2]) == 1
        return ReplyDecision(should_reply=True, messages=[f"回答{calls}"], confidence=0.9)

    with start_run("douyin", "android.monitor") as run, pytest.raises(asyncio.CancelledError):
        await auto.watch(
            phone,
            run,
            cfg,
            "测试对象",
            "自己",
            confirmed=True,
            continuous=True,
            poll_s=1,
            allow_send=True,
            generate=True,
            sleep=sleep,
            decide=decide,
            load_config=lambda: cfg,
        )
    assert calls == phone.clicks == 2


@pytest.mark.asyncio
async def test_long_interval_checks_switch_before_next_phone_read():
    cfg = Config()
    phone = Phone()
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        cfg.douyin.auto_reply = "off"

    with start_run("douyin", "android.monitor") as run, pytest.raises(HumanRequired):
        await auto.watch(
            phone,
            run,
            cfg,
            "测试对象",
            "自己",
            confirmed=True,
            continuous=True,
            poll_s=1800,
            sleep=sleep,
            load_config=lambda: cfg,
        )
    assert sleeps == [30]
    assert phone.messages == BASE


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["quiet", "limit", "media"])
async def test_monitor_waits_and_keeps_unprocessed_messages(mode, monkeypatch):
    cfg = Config()
    phone = Phone()
    ticks = calls = 0
    if mode == "limit":
        cfg.guard.max_per_hour = 0
    quiet = mode == "quiet"
    monkeypatch.setattr(type(cfg.douyin), "quiet_until", lambda *a: True if quiet else None)
    phone.request = lambda *args, **kwargs: False

    async def sleep(_):
        nonlocal ticks, quiet
        ticks += 1
        if ticks == 1:
            phone.messages.append(auto.Message(False, "share", "作品") if mode == "media" else NEW)
        if ticks == 2:
            quiet = False
            cfg.guard.max_per_hour = 20
        if ticks == 4:
            raise asyncio.CancelledError

    async def decide(*args, **kwargs):
        nonlocal calls
        calls += 1
        assert args[2][-1].is_new
        return ReplyDecision(should_reply=False, messages=[], confidence=0.9)

    with start_run("douyin", "android.monitor") as run, pytest.raises(asyncio.CancelledError):
        await auto.watch(
            phone,
            run,
            cfg,
            "测试对象",
            "自己",
            confirmed=True,
            continuous=True,
            poll_s=1,
            generate=True,
            sleep=sleep,
            decide=decide,
            load_config=lambda: cfg,
        )
    assert calls == (0 if mode == "media" else 1)
    assert phone.clicks == 0
