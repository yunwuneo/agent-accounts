import asyncio
import xml.etree.ElementTree as ET

import pytest
from test_android_autoreply import BASE, NEW, Phone, xml

from agent_accounts.adapters.douyin.android import autoreply, navigation, shares
from agent_accounts.adapters.douyin.android.session import PACKAGE, PREFIX, AndroidError
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.run import start_run


def node(parent, rid, text="", box="[0,0][100,100]"):
    return ET.SubElement(
        parent,
        "node",
        {"resource-id": PREFIX + rid, "text": text, "package": PACKAGE, "bounds": box},
    )


def chat(messages=BASE, title="测试对象", draft=""):
    root = xml(messages, draft)
    for n in root:
        n.set("package", PACKAGE)
    root[0].set("text", title)
    node(root, "v65", box="[0,100][1080,1800]")
    return root


def inbox(count=1):
    root = ET.Element("hierarchy")
    for t in ["首页", "朋友", "消息", "我"]:
        node(root, "0qf", t)
    node(root, "tv_title", "消息")
    node(root, "ef8", "测试对象")  # recommended avatar must be ignored
    listing = node(root, "zn6", box="[0,200][1080,1800]")
    for i in range(count):
        row = node(listing, "zn-", box=f"[0,{300 + i * 200}][1080,{450 + i * 200}]")
        node(row, "by3", "测试对象")
    return root


class Run:
    id = "test"

    def audit(self, *args, **kwargs):
        pass


class Navigator:
    def __init__(self, root):
        self.root = root
        self.actions = []

    def source(self):
        return self.root

    observe = source

    def tap(self, target):
        self.actions.append(target.get("resource-id"))
        self.root = chat() if target.get("resource-id") == PREFIX + "zn-" else inbox()

    def request(self, path, data):
        self.actions.append(path)
        self.root = inbox()


@pytest.fixture
def navigation_without_waits(monkeypatch):
    monkeypatch.setattr(navigation, "settled", lambda s: s.source())
    monkeypatch.setattr(shares, "bottom", lambda *a, **kw: None)
    monkeypatch.setattr(shares, "gesture", lambda *a, **kw: None)


def test_inbox_uses_existing_row_not_suggestion(navigation_without_waits):
    s = Navigator(inbox())
    result = navigation.ensure_thread(s, Run(), "测试对象", "自己", confirmed=True)
    assert navigation.classify(result)["page"] == "chat"
    assert s.actions == [PREFIX + "zn-"]


@pytest.mark.parametrize("count", [0, 2])
def test_missing_duplicate_never_clicks(count, navigation_without_waits):
    s = Navigator(inbox(count))
    with pytest.raises(AndroidError):
        navigation.ensure_thread(s, Run(), "测试对象", "自己", confirmed=True)
    assert not s.actions


def test_group_and_partial_row_rejected():
    root = inbox()
    row = navigation.target_row(root, "测试对象")
    row.set("text", "群聊")
    with pytest.raises(AndroidError):
        navigation.target_row(root, "测试对象")
    row.set("text", "")
    row.set("bounds", "[0,100][1080,400]")
    with pytest.raises(AndroidError):
        navigation.target_row(root, "测试对象")


def test_target_found_after_bounded_scroll(navigation_without_waits, monkeypatch):
    s = Navigator(inbox(0))
    scrolls = []

    def gesture(*args, **kwargs):
        scrolls.append(1)
        s.root = inbox()

    monkeypatch.setattr(shares, "gesture", gesture)
    navigation.ensure_thread(s, Run(), "测试对象", "自己", confirmed=True)
    assert len(scrolls) == 1
    assert s.actions == [PREFIX + "zn-"]


def test_wrong_chat_back_and_draft_preservation(navigation_without_waits):
    s = Navigator(chat(title="其他会话"))
    navigation.ensure_thread(s, Run(), "测试对象", "自己", confirmed=True)
    assert s.actions == ["/back", PREFIX + "zn-"]
    s = Navigator(chat(title="其他会话", draft="已有草稿"))
    with pytest.raises(HumanRequired):
        navigation.ensure_thread(s, Run(), "测试对象", "自己", confirmed=True)
    assert not s.actions


def test_recovery_preserves_new_messages_and_rejects_gap(navigation_without_waits):
    s = Navigator(chat(BASE + [NEW]))
    root = navigation.ensure_thread(s, Run(), "测试对象", "自己", confirmed=True, previous=BASE)
    assert autoreply.appended(BASE, autoreply.read_messages(root, "测试对象", "自己")) == [NEW]
    with pytest.raises(HumanRequired):
        navigation.ensure_thread(
            s, Run(), "测试对象", "自己", confirmed=True, previous=[NEW, BASE[0]]
        )


def test_unconfirmed_unknown_and_wrong_identity_stop(navigation_without_waits):
    with pytest.raises(AndroidError):
        navigation.ensure_thread(None, Run(), "测试对象", "自己")
    s = Navigator(ET.fromstring(f'<hierarchy><node package="{PACKAGE}"/></hierarchy>'))
    with pytest.raises(HumanRequired):
        navigation.ensure_thread(s, Run(), "测试对象", "自己", confirmed=True)
    assert not s.actions
    s = Navigator(chat())
    with pytest.raises(AndroidError):
        navigation.ensure_thread(s, Run(), "测试对象", "错误身份", confirmed=True)


@pytest.mark.asyncio
async def test_monitor_recovers_each_poll_without_new_baseline(monkeypatch):
    cfg = Config()
    s = Phone()
    baselines = []
    ticks = 0

    def recover(s, run, expected, self_name, **kw):
        baselines.append(kw["previous"])
        return s.source()

    monkeypatch.setattr(navigation, "ensure_thread", recover)

    async def sleep(_):
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            s.messages.append(NEW)
        elif ticks == 3:
            raise asyncio.CancelledError

    with start_run("douyin", "android.monitor") as run, pytest.raises(asyncio.CancelledError):
        await autoreply.watch(
            s,
            run,
            cfg,
            "测试对象",
            "自己",
            confirmed=True,
            continuous=True,
            recover_navigation=True,
            poll_s=1,
            sleep=sleep,
            load_config=lambda: cfg,
        )
    assert baselines == [None, BASE, BASE + [NEW]]
