import asyncio
import json

import pytest
from test_android_autoreply import NEW, Phone

from agent_accounts.adapters.douyin.android import autoreply as auto
from agent_accounts.adapters.douyin.android.notifications import NotificationTrigger, decode_signal
from agent_accounts.adapters.douyin.android.session import AndroidError
from agent_accounts.core.config import AndroidConfig, Config
from agent_accounts.core.run import start_run


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"{}",
        b"[]",
        b"null",
        b"x",
        json.dumps({"version": 1, "connected": False, "sequence": 0}).encode(),
        json.dumps({"version": 1, "connected": True, "sequence": True}).encode(),
    ],
)
def test_invalid_or_disconnected_signal(payload):
    with pytest.raises(AndroidError):
        decode_signal(payload)


@pytest.mark.asyncio
async def test_notification_coalesces_and_preserves_next_arrival():
    trigger = NotificationTrigger(None, "target")
    now = 0
    sleeps = []

    async def sleep(seconds):
        nonlocal now
        sleeps.append(seconds)
        now += seconds
        trigger.sequence += 1

    assert await trigger.wait(7200, lambda: None, sleep=sleep, clock=lambda: now) == "notification"
    assert sleeps == [1, 2]
    assert trigger.consumed == 2
    trigger.sequence += 1  # arrived while navigating/generating, not lost
    assert await trigger.wait(7200, lambda: None, sleep=sleep, clock=lambda: now) == "notification"
    assert trigger.consumed == 4


@pytest.mark.asyncio
async def test_fallback_deadline_and_failure():
    trigger = NotificationTrigger(None, "target")
    now = 0

    async def sleep(seconds):
        nonlocal now
        now += seconds

    assert await trigger.wait(7, lambda: None, sleep=sleep, clock=lambda: now) == "fallback"
    assert now == 7
    trigger.error = True
    with pytest.raises(AndroidError):
        await trigger.wait(7, lambda: None, sleep=sleep, clock=lambda: now)


@pytest.mark.asyncio
async def test_trigger_uses_existing_baseline_and_navigation(monkeypatch):
    cfg = Config()
    phone = Phone()
    keys = []
    phone.request = lambda path, data, **kwargs: keys.append(data.get("keycode"))
    recovered = []

    def recover(*args, previous=None, **kwargs):
        recovered.append(previous)
        return phone.source()

    monkeypatch.setattr("agent_accounts.adapters.douyin.android.navigation.ensure_thread", recover)

    class Trigger:
        calls = 0

        def check(self):
            pass

        async def wait(self, interval, check):
            assert interval == 7200
            self.calls += 1
            if self.calls == 2:
                raise asyncio.CancelledError
            phone.messages.append(NEW)
            return "notification"

    with start_run("douyin", "android.monitor") as run, pytest.raises(asyncio.CancelledError):
        await auto.watch(
            phone,
            run,
            cfg,
            "测试对象",
            "自己",
            confirmed=True,
            continuous=True,
            recover_navigation=True,
            trigger=Trigger(),
            poll_s=7200,
            load_config=lambda: cfg,
        )
    assert recovered[0] is None
    assert recovered[1] and NEW not in recovered[1]
    assert keys == [3, 3]
    assert phone.clicks == 0


def test_default_two_hours_and_configurable():
    assert AndroidConfig().monitor_interval_s == 7200
    assert AndroidConfig().monitor_trigger == "notification"
    assert AndroidConfig(monitor_interval_s=86400).monitor_interval_s == 86400


def test_diagnostics_never_forward_arbitrary_fields():
    trigger = NotificationTrigger(None, "fixture")
    assert (
        trigger._decode(
            json.dumps(
                {
                    "version": 1,
                    "connected": True,
                    "sequence": 2,
                    "posted": 3,
                    "unmatched": True,
                    "groups": -1,
                    "text": "must not forward",
                }
            ).encode()
        )
        == 2
    )
    assert trigger.stats == {"posted": 3}


def test_write_interval_preserves_secrets_and_other_settings(isolated_home):
    from agent_accounts.core import config, paths

    paths.ensure_dir(isolated_home)
    path = paths.config_path()
    path.write_text(
        '[llm.reply]\nmodel="test-model"\napi_key="test-only-secret"\n[douyin.android]\n'
        "allow_send=true\nmonitor_interval_s=1800\n# keep\n[guard]\nmin_interval_s=42\n",
        encoding="utf-8",
    )
    config.write_android_monitor(7200, "notification")
    text = path.read_text(encoding="utf-8")
    assert 'api_key="test-only-secret"' in text
    assert "allow_send=true" in text and "# keep" in text
    assert config.load().douyin.android.monitor_interval_s == 7200
    assert config.load().guard.min_interval_s == 42
    config.write_android_monitor(14400, "poll")
    assert config.load().douyin.android.monitor_trigger == "poll"
    assert config.load().douyin.android.monitor_interval_s == 14400


def test_invalid_interval_does_not_write(isolated_home):
    from agent_accounts.core import config, paths

    with pytest.raises(config.ConfigError):
        config.write_android_monitor(0, "notification")
    assert not paths.config_path().exists()


def test_usb_stream_handshake_update_and_disconnect():
    import hashlib
    import socket
    import threading
    import time

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    handshake = []
    release = threading.Event()

    def server():
        with listener, listener.accept()[0] as connection, connection.makefile("rb") as reader:
            handshake.append(reader.readline())
            connection.sendall(b'{"version":1,"connected":true,"sequence":0}\n')
            connection.sendall(b'{"version":1,"connected":true,"sequence":3}\n')
            release.wait(3)

    worker = threading.Thread(target=server)
    worker.start()

    class Session:
        removed = False

        def adb(self, *args):
            if args[0] == "shell":
                return "org.echo.accounts.notifications/org.echo.accounts.notifications.Listener"
            if args[1] == "--remove":
                self.removed = True
                return ""
            return str(port)

    session = Session()
    with NotificationTrigger(session, "fixture-target") as bridge:
        deadline = time.monotonic() + 3
        while bridge.sequence < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert bridge.sequence == 3
        release.set()
        worker.join(timeout=3)
        while not bridge.error and time.monotonic() < deadline:
            time.sleep(0.01)
        with pytest.raises(AndroidError):
            bridge.check()
    assert session.removed
    assert handshake == [hashlib.sha256(b"fixture-target").hexdigest().encode() + b"\n"]


def test_no_permission_does_not_forward():
    class Session:
        def adb(self, *args):
            assert args[0] == "shell"
            return "null"

    with pytest.raises(AndroidError), NotificationTrigger(Session(), "fixture"):
        pytest.fail("must not connect")
