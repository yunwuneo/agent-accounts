"""Read a USB-forwarded notification signal stream, never notification text."""

import asyncio
import hashlib
import json
import socket
import threading
import time
from contextlib import suppress

from agent_accounts.adapters.douyin.android.session import AndroidError

COMPONENT = "org.echo.accounts.notifications/.Listener"
SOCKET = "localabstract:echo_douyin_notifications_v1"


def decode_signal(line: bytes) -> int:
    try:
        value = json.loads(line)
        seq = value["sequence"]
        if (
            value["version"] != 1
            or value["connected"] is not True
            or type(seq) is not int
            or not 0 <= seq < 2**63
        ):
            raise ValueError
        return seq
    except (ValueError, KeyError, TypeError):
        raise AndroidError("通知桥未连接或协议无效；检查手机通知使用权后重新启动") from None


class NotificationTrigger:
    """One stream per monitor; monotonic counters retain arrivals while UI/model is busy.

    Reconnect/restart requires an explicit fresh baseline. Socket failure stops the
    monitor instead of silently pretending notifications are still being watched.
    """

    def __init__(self, session, target):
        self.session = session
        self.target = target
        self.port = None
        self.sock = None
        self.thread = None
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.sequence = self.consumed = 0
        self.error = False
        self.stats = {}
        self.next_fallback = None

    def __enter__(self):
        enabled = self.session.adb(
            "shell", "settings", "get", "secure", "enabled_notification_listeners"
        )
        components = enabled.strip().split(":")
        full_component = COMPONENT.replace(
            "/.Listener", "/org.echo.accounts.notifications.Listener"
        )
        if COMPONENT not in components and full_component not in components:
            raise AndroidError("请安装 Echo 通知桥，并在手机上手动授予通知使用权")
        try:
            port = self.session.adb("forward", "tcp:0", SOCKET).strip()
            if not port.isdecimal() or not 1 <= int(port) <= 65535:
                raise AndroidError("ADB 未返回有效通知桥端口")
            self.port = int(port)
            self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=20)
            self.sock.sendall(hashlib.sha256(self.target.encode()).hexdigest().encode() + b"\n")
            self.reader = self.sock.makefile("rb")
            self.sequence = self._decode(self.reader.readline(1025))
            # Heartbeat is 15 seconds; tolerate several delayed Android scheduling
            # intervals while still failing closed on EOF or a full minute of silence.
            self.sock.settimeout(60)
            self.thread = threading.Thread(target=self._read, daemon=True)
            self.thread.start()
            return self
        except (OSError, AndroidError):
            self.__exit__(None, None, None)
            raise AndroidError("通知桥连接失败；请检查应用、通知使用权及 USB 连接") from None

    def _read(self):
        try:
            while not self.closed.is_set():
                seq = self._decode(self.reader.readline(1025))
                with self.lock:
                    if seq < self.sequence:
                        raise AndroidError("通知桥计数回退")
                    self.sequence = seq
        except (OSError, AndroidError) as exc:
            if not self.closed.is_set():
                self.error = type(exc).__name__

    def _decode(self, line):
        seq = decode_signal(line)
        value = json.loads(line)
        self.stats = {
            key: value[key]
            for key in (
                "posted",
                "unmatched",
                "summaries",
                "groups",
                "unreadable",
                "active_douyin",
                "active_matches",
            )
            if type(value.get(key)) is int and value[key] >= 0
        }
        return seq

    def check(self):
        if self.error:
            raise AndroidError(f"通知桥已断开（{self.error}），监控停止；请检查后人工重新启动")

    async def wait(self, interval, check, *, sleep=asyncio.sleep, clock=time.monotonic):
        if self.next_fallback is None:
            self.next_fallback = clock() + interval
        while True:
            self.check()
            check()
            with self.lock:
                changed = self.sequence > self.consumed
            if changed:
                # Bounded burst coalescing; a busy sender cannot postpone work forever.
                await sleep(2)
                self.check()
                check()
                with self.lock:
                    self.consumed = self.sequence
                return "notification"
            remaining = self.next_fallback - clock()
            if remaining <= 0:
                self.next_fallback = clock() + interval
                return "fallback"
            # This checks an in-memory counter, never the phone UI or its notifications.
            await sleep(min(1, remaining))

    def __exit__(self, *args):
        self.closed.set()
        if self.sock:
            with suppress(OSError):
                self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
        if self.thread:
            self.thread.join(timeout=2)
        if hasattr(self, "reader"):
            self.reader.close()
        if self.port:
            with suppress(AndroidError):
                self.session.adb("forward", "--remove", f"tcp:{self.port}")
