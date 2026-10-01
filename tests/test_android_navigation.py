import xml.etree.ElementTree as ET

import pytest
from typer.testing import CliRunner

from agent_accounts.adapters.douyin.android import navigation
from agent_accounts.adapters.douyin.android.session import (
    PACKAGE,
    PREFIX,
    AndroidError,
    AndroidSession,
    check_source,
)
from agent_accounts.adapters.douyin.cli import app
from agent_accounts.core.config import AndroidConfig
from agent_accounts.core.errors import HumanRequired


def screen(ids=(), *, package=PACKAGE, labels=()):
    root = ET.Element("hierarchy")
    for rid, text in [(rid, "") for rid in ids] + [("label", t) for t in labels]:
        ET.SubElement(root, "node", {"package": package, "resource-id": PREFIX + rid, "text": text})
    if not len(root):
        ET.SubElement(root, "node", {"package": package})
    return root


def gallery(root):
    for i in (1, 2):
        ET.SubElement(root, "node", {"package": PACKAGE, "content-desc": f"图片{i}，按钮"})
    return root


@pytest.mark.parametrize(
    "root,page",
    [
        (screen(package="com.miui.home"), "desktop"),
        (screen(package="org.example.app"), "external_app"),
        (screen(package="com.android.permissioncontroller"), "system_overlay"),
        (screen(["vw_", "msg_et", "v65"]), "chat"),
        (screen(["c_e", "6jy", "u6y", "0mk"]), "video_clear"),
        (gallery(screen(["c_e", "sg0"])), "gallery_clear"),
        (screen(["back_btn", "desc", "z0m", "6jy"]), "video_detail"),
        (gallery(screen(["back_btn", "desc", "z0m"])), "gallery_detail"),
        (screen(["z0m"], labels=["分享链接"]), "share_panel"),
        (screen(labels=["选择图片保存", "保存"]), "gallery_save"),
        (screen(labels=["首页", "消息", "我", "搜索", "设置"]), "unknown"),
        (screen(["msg_et"]), "unknown"),
    ],
)
def test_pages(root, page):
    assert navigation.classify(root)["page"] == page


@pytest.mark.parametrize(
    "selected,page",
    [("首页", "home"), ("朋友", "friends"), ("消息", "inbox"), ("我", "self_profile")],
)
def test_selected_tabs(selected, page):
    root = screen(labels=["首页", "朋友", "消息", "我"])
    for node in root:
        node.set("resource-id", PREFIX + "0qf")
        node.set("selected", str(node.get("text") == selected).lower())
    assert navigation.classify(root)["page"] == page
    root[0].set("selected", "false")
    for node in root:
        node.set("selected", "false")
    assert navigation.classify(root)["page"] == "unknown"


def test_overlay_and_hidden_markers():
    root = screen(["vw_", "msg_et", "v65"])
    ET.SubElement(root, "node", {"package": "com.android.permissioncontroller"})
    assert navigation.classify(root)["page"] == "system_overlay"
    root[-1].set("displayed", "false")
    root[0].set("displayed", "false")
    assert navigation.classify(root)["page"] == "unknown"


def test_real_tabs_without_selected_state_and_duplicate_parent_labels():
    root = screen(labels=["首页", "朋友", "消息", "我"])
    for node in root:
        node.set("resource-id", PREFIX + "0qf")
    for text in ["首页", "朋友", "消息", "我"]:
        ET.SubElement(
            root, "node", {"package": PACKAGE, "resource-id": PREFIX + "gib", "text": text}
        )
    title = ET.SubElement(
        root, "node", {"package": PACKAGE, "resource-id": PREFIX + "tv_title", "text": "消息"}
    )
    assert navigation.classify(root)["page"] == "inbox"
    root.remove(title)
    assert navigation.classify(root)["page"] == "unknown"
    for text in ["推荐", "关注", "同城"]:
        ET.SubElement(
            root, "node", {"package": PACKAGE, "resource-id": PREFIX + "5fc", "text": text}
        )
    assert navigation.classify(root)["page"] == "unknown"
    ET.SubElement(root, "node", {"package": PACKAGE, "resource-id": PREFIX + "2ss"})
    assert navigation.classify(root)["page"] == "home"


def test_account_setup_prompt_requires_human():
    phone = Phone([screen(labels=["请完善账号安全设置"])])
    with pytest.raises(HumanRequired, match="账号安全设置"):
        navigation.open_app(phone, Run(), execute=True)
    assert not phone.actions


def test_profile_friends_and_shared_title_ids():
    root = screen(labels=["首页", "朋友", "消息", "我"])
    for node in root:
        node.set("resource-id", PREFIX + "0qf")
    ET.SubElement(
        root, "node", {"package": PACKAGE, "resource-id": PREFIX + "tv_title", "text": "其他标题"}
    )
    title = ET.SubElement(
        root, "node", {"package": PACKAGE, "resource-id": PREFIX + "tv_title", "text": "消息"}
    )
    assert navigation.classify(root)["page"] == "inbox"
    root.remove(title)
    editor = ET.SubElement(
        root, "node", {"package": PACKAGE, "resource-id": PREFIX + "wgq", "text": "编辑主页"}
    )
    assert navigation.classify(root)["page"] == "unknown"
    for text in ["作品", "日常", "收藏", "喜欢"]:
        ET.SubElement(
            root, "node", {"package": PACKAGE, "resource-id": "android:id/text1", "text": text}
        )
    assert navigation.classify(root)["page"] == "self_profile"
    root.remove(editor)
    assert navigation.classify(root)["page"] == "unknown"
    entry = ET.SubElement(
        root, "node", {"package": PACKAGE, "resource-id": PREFIX + "iyc", "text": "添加朋友"}
    )
    assert navigation.classify(root)["page"] == "friends"
    entry.set("resource-id", PREFIX + "unrelated")
    assert navigation.classify(root)["page"] == "unknown"


class Run:
    id = "test"

    def audit(self, *args, **kwargs):
        pass


class Phone:
    def __init__(self, screens):
        self.screens = list(screens)
        self.actions = []

    def observe(self):
        root = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        return check_source(ET.tostring(root, encoding="unicode"), allow_external=True)

    def source(self):
        return check_source(ET.tostring(self.screens[-1], encoding="unicode"))

    def request(self, path, data):
        self.actions.append((path, data))


def test_dry_run_and_already_open_do_not_activate():
    phone = Phone([screen(package="com.miui.home")])
    assert navigation.open_app(phone, Run())["status"] == "dry_run"
    assert not phone.actions
    phone = Phone([screen()])
    assert navigation.open_app(phone, Run(), execute=True)["status"] == "already_open"
    assert not phone.actions


@pytest.mark.parametrize("package", ["com.miui.home", "org.example.app"])
def test_launch_once_and_verify_foreground(package):
    origin = screen(package=package)
    phone = Phone([origin, origin, screen(["vw_", "msg_et", "v65"])])
    result = navigation.open_app(phone, Run(), execute=True)
    assert result["status"] == "opened" and result["page"] == "chat"
    assert phone.actions == [("/appium/device/activate_app", {"appId": PACKAGE})]


def test_launch_timeout_no_retry(monkeypatch):
    clock = iter([0, 2])
    monkeypatch.setattr(navigation.time, "monotonic", lambda: next(clock))
    phone = Phone([screen(package="com.miui.home")])
    with pytest.raises(AndroidError, match="未再次启动"):
        navigation.open_app(phone, Run(), execute=True, timeout_s=1)
    assert len(phone.actions) == 1


@pytest.mark.parametrize("text", ["安全验证", "登录抖音"])
def test_risk_login_stop_after_activation(text):
    origin = screen(package="com.miui.home")
    phone = Phone([origin, origin, screen(labels=[text])])
    with pytest.raises(HumanRequired):
        navigation.open_app(phone, Run(), execute=True)
    assert len(phone.actions) == 1


def test_overlay_and_race_never_activate():
    for screens in [
        [screen(package="com.android.permissioncontroller")],
        [screen(package="com.miui.home"), screen(package="com.android.systemui")],
    ]:
        phone = Phone(screens)
        with pytest.raises((HumanRequired, AndroidError)):
            navigation.open_app(phone, Run(), execute=True)
        assert not phone.actions


def test_strict_source_unchanged_and_lock_stops_observation(monkeypatch):
    xml = ET.tostring(screen(package="com.miui.home"), encoding="unicode")
    with pytest.raises(HumanRequired):
        check_source(xml)
    phone = AndroidSession(AndroidConfig(), allow_external=True)
    calls = []

    def request(path, *args, **kwargs):
        calls.append(path)
        return True

    monkeypatch.setattr(phone, "request", request)
    with pytest.raises(HumanRequired):
        phone.observe()
    assert calls == ["/appium/device/is_locked"]


def test_cli_open_defaults_and_inspect(monkeypatch):
    from agent_accounts.adapters.douyin.android import cli

    phone = Phone([screen(package="org.example.app", labels=["private content"])])
    calls = []

    def invoke(command, fn, **kwargs):
        calls.append((command, kwargs))
        result = fn(phone, Run(), None)
        assert "private content" not in str(result)

    monkeypatch.setattr(cli, "invoke", invoke)
    for command in ("open", "inspect"):
        result = CliRunner().invoke(app, ["android", command])
        assert result.exit_code == 0, result.output
    assert not phone.actions
    assert all(kwargs["allow_external"] for _, kwargs in calls)
