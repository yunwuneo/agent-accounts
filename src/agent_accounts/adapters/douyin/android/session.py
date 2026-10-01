"""本机 Appium W3C + ADB。无自动安装、登录、解锁、重试或远端 Appium。"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from agent_accounts.core.config import AndroidConfig, ConfigError
from agent_accounts.core.errors import HumanRequired

PACKAGE = "com.ss.android.ugc.aweme"
PREFIX = PACKAGE + ":id/"


class AndroidError(ConfigError):
    pass


def nodes(root, rid: str):
    return [n for n in root.iter() if n.get("resource-id") == PREFIX + rid]


def one(root, rid: str):
    found = nodes(root, rid)
    if len(found) != 1:
        raise AndroidError("界面元素缺失或不唯一，请人工检查当前页面与应用版本")
    return found[0]


def label(node) -> str:
    return node.get("text") or node.get("content-desc") or ""


def bounds(node) -> tuple[int, int, int, int]:
    values = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if not values:
        raise AndroidError("无有效元素坐标")
    return tuple(map(int, values.groups()))


def check_source(xml: str):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise AndroidError("Appium 返回无效页面结构") from None
    visible = " ".join(n.get("text", "") + " " + n.get("content-desc", "") for n in root.iter())
    if any(s in visible for s in ("安全验证", "请完成验证", "拖动滑块", "账号存在异常")):
        raise HumanRequired("安卓界面出现验证或风控信号", freeze=True)
    if any(s in visible for s in ("登录后继续", "登录抖音", "手机号登录")):
        raise HumanRequired("请人工登录安卓专用账号")
    packages = {n.get("package") for n in root.iter() if n.get("package")}
    if PACKAGE not in packages:
        raise HumanRequired("请解锁手机并打开抖音目标页面")
    if packages - {PACKAGE, "com.android.systemui"}:
        raise HumanRequired("存在系统弹窗或其他应用，请人工处理后重试")
    return root


class AndroidSession:
    def __init__(self, cfg: AndroidConfig):
        self.cfg = cfg
        self.sid: str | None = None
        # 禁止环境 HTTP 代理转发手机页面/草稿。
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def adb(self, *args: str) -> str:
        try:
            proc = subprocess.run(
                [self.cfg.adb, "-s", self.cfg.udid, *args],
                capture_output=True,
                timeout=self.cfg.request_timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise AndroidError("ADB 不可用或超时，请检查安装、USB 授权和设备连接") from None
        if proc.returncode:
            raise AndroidError("ADB 操作失败，请检查设备状态")
        return proc.stdout.decode("utf-8", errors="replace")

    def request(self, path: str, data=None, method: str | None = None, *, session=True):
        if session and not self.sid:
            raise AndroidError("尚未连接 Appium 会话")
        url = self.cfg.appium_url + (f"/session/{self.sid}" if session else "") + path
        req = urllib.request.Request(
            url,
            data=None if data is None else json.dumps(data, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with self.opener.open(req, timeout=self.cfg.request_timeout_s) as response:
                value = json.load(response)["value"]
            if isinstance(value, dict) and value.get("error"):
                raise AndroidError("Appium 操作失败（未自动重试）")
            return value
        except (OSError, ValueError, KeyError, urllib.error.URLError):
            # Appium 错误原文可能含页面、草稿、会话标识，不落审计。
            raise AndroidError("Appium 操作失败或超时（未自动重试）") from None

    def __enter__(self):
        if not self.cfg.enabled or not self.cfg.udid.strip():
            raise AndroidError("请配置 [douyin.android] enabled=true 和 udid")
        if self.adb("get-state").strip() != "device":
            raise AndroidError("设备未通过 USB 调试授权")
        window = self.adb("shell", "dumpsys", "window")
        if re.search(
            r"(?:mDreamingLockscreen|mShowingLockscreen|isStatusBarKeyguard)=true", window
        ):
            raise HumanRequired("请人工解锁手机；不会通过脚本解锁")
        info = self.adb("shell", "dumpsys", "package", PACKAGE)
        match = re.search(r"versionName=([^\s]+)", info)
        if not match or match[1] != self.cfg.tested_version:
            raise AndroidError("抖音版本不匹配，请先重新验收选择器")
        value = self.request(
            "/session",
            {
                "capabilities": {
                    "alwaysMatch": {
                        "platformName": "Android",
                        "appium:automationName": "UiAutomator2",
                        "appium:udid": self.cfg.udid,
                        "appium:noReset": True,
                        "appium:fullReset": False,
                        "appium:autoLaunch": False,
                        "appium:skipUnlock": True,
                        "appium:skipServerInstallation": True,
                        "appium:skipLogcatCapture": True,
                        "appium:printPageSourceOnFindFailure": False,
                        "appium:newCommandTimeout": 1200,
                    },
                    "firstMatch": [{}],
                }
            },
            session=False,
        )
        self.sid = value["sessionId"]
        try:
            self.request("/appium/settings", {"settings": {"waitForIdleTimeout": 0}})
            self.source()
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self.sid:
                self.request("", method="DELETE")
        except AndroidError:
            pass
        finally:
            self.sid = None

    def source(self):
        return check_source(self.request("/source"))

    def element(self, rid: str) -> str:
        value = self.request("/element", {"using": "id", "value": PREFIX + rid})
        return value["element-6066-11e4-a52e-4f735466cecf"]

    def click(self, rid: str):
        one(self.source(), rid)
        self.request(f"/element/{self.element(rid)}/click", {})

    def tap(self, node):
        x1, y1, x2, y2 = bounds(node)
        self.source()
        self.request(
            "/actions",
            {
                "actions": [
                    {
                        "type": "pointer",
                        "id": "finger",
                        "parameters": {"pointerType": "touch"},
                        "actions": [
                            {
                                "type": "pointerMove",
                                "duration": 0,
                                "x": (x1 + x2) // 2,
                                "y": (y1 + y2) // 2,
                            },
                            {"type": "pointerDown", "button": 0},
                            {"type": "pause", "duration": 80},
                            {"type": "pointerUp", "button": 0},
                        ],
                    }
                ]
            },
        )

    def screenshot(self, path: Path):
        data = base64.b64decode(self.request("/screenshot"), validate=True)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AndroidError("无效截图数据")
        path.write_bytes(data)
        return data
