import xml.etree.ElementTree as ET

import pytest

from agent_accounts.adapters.douyin.android import autoreply as auto
from agent_accounts.adapters.douyin.android.session import PREFIX, AndroidError
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.reply import ReplyDecision
from agent_accounts.core.run import start_run


def xml(messages, draft=""):
    root = ET.Element("hierarchy")

    def add(rid, text, box):
        return ET.SubElement(
            root,
            "android.widget.TextView",
            {
                "resource-id": PREFIX + rid,
                "text": text,
                "bounds": box,
            },
        )

    add("vw_", "测试对象", "[100,40][800,100]")
    add("msg_et", draft, "[100,1800][800,1900]")
    for i, m in enumerate(messages):
        y = 200 + i * 180
        add(
            "dwh",
            ("自己" if m.from_me else "测试对象") + "的头像",
            f"[{900 if m.from_me else 0},{y}][{1000 if m.from_me else 100},{y + 100}]",
        )
        add("sws" if m.kind == "text" else "sww", m.text, f"[150,{y}][850,{y + 100}]")
    return root


BASE = [auto.Message(False, "text", "前文"), auto.Message(True, "text", "之前的回答")]
NEW = auto.Message(False, "text", "今天好吗")


class Phone:
    def __init__(self):
        self.messages = list(BASE)
        self.draft = ""
        self.writes = self.clicks = 0

    def source(self):
        return xml(self.messages, self.draft)

    def element(self, rid):
        return rid

    def request(self, path, data):
        self.writes += 1
        self.draft = data["text"]

    def click(self, rid):
        self.clicks += 1
        self.messages.append(auto.Message(True, "text", self.draft))
        self.draft = ""


def test_reader_checks_both_direction_and_identity():
    assert auto.read_messages(xml(BASE), "测试对象", "自己") == BASE
    with pytest.raises(AndroidError):
        auto.read_messages(xml(BASE), "测试对象", "其他自己")
    root = xml(BASE)
    root[2].set("bounds", "[900,200][1000,300]")
    with pytest.raises(AndroidError):
        auto.read_messages(root, "测试对象", "自己")


def test_overlap_does_not_guess_duplicate_or_gap():
    assert auto.appended(BASE, BASE + [NEW]) == [NEW]
    assert auto.appended(BASE, BASE[1:] + [NEW]) == [NEW]
    assert auto.appended(BASE, BASE) == []
    for previous, current in [([NEW, NEW], [NEW, NEW, NEW]), (BASE, [NEW]), (BASE, BASE[:1])]:
        with pytest.raises(HumanRequired):
            auto.appended(previous, current)


def test_clipped_leading_history_requires_scroll_boundary():
    root = xml(BASE + [NEW])
    root.remove(root[2])  # 顶部旧气泡的头像已滚出屏幕。
    root.set("scrollable", "true")
    root.set("bounds", "[0,200][1080,1700]")
    assert auto.read_messages(root, "测试对象", "自己") == [BASE[1], NEW]
    root.set("bounds", "[0,190][1080,1700]")
    with pytest.raises(AndroidError):
        auto.read_messages(root, "测试对象", "自己")


def test_keyboard_tail_keeps_recent_messages_and_rejects_new_or_ambiguous():
    assert auto.same_tail(BASE + [NEW], [BASE[1], NEW])
    assert not auto.same_tail(BASE, [BASE[1], NEW])
    assert not auto.same_tail(BASE, [BASE[1]])
    assert not auto.same_tail([NEW] * 3, [NEW] * 2)


def test_grouped_bubbles_need_matching_container_and_same_previous_sender():
    root = xml(BASE + [NEW, auto.Message(False, "text", "补充")])
    root.remove(root[-2])  # 连续对方消息隐藏第二个头像。
    for bubble in list(root):
        if bubble.get("resource-id") == PREFIX + "sws":
            root.remove(bubble)
            own = bubble.get("text") == BASE[1].text
            row = ET.SubElement(
                root,
                "android.view.ViewGroup",
                {
                    "resource-id": PREFIX + "m60",
                    "bounds": "[33,0][927,1700]" if own else "[153,0][1047,1700]",
                },
            )
            row.append(bubble)
    assert auto.read_messages(root, "测试对象", "自己")[-1].text == "补充"
    root[-1].set("bounds", "[33,0][927,1700]")
    with pytest.raises(AndroidError):
        auto.read_messages(root, "测试对象", "自己")


def test_combined_reply_still_checks_total_length_and_content():
    from agent_accounts.core import guard

    cfg = Config()
    cfg.guard.max_len = 8
    decision = auto.single_bubble(
        ReplyDecision(should_reply=True, messages=["第一段文字", "第二段文字"], confidence=0.9), 3
    )
    assert not guard.check(cfg.guard, auto.guard_input(cfg, "测试对象", "on", decision)).ok
    decision = auto.single_bubble(
        ReplyDecision(should_reply=True, messages=["你好", "转账"], confidence=0.9), 3
    )
    assert not guard.check(cfg.guard, auto.guard_input(cfg, "测试对象", "on", decision)).ok


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "observe",
        "dry_run",
        "send",
        "media",
        "own",
        "low",
        "mutate",
        "blocklist",
        "multi",
        "off",
        "draft",
    ],
)
async def test_bounded_auto_reply(mode):
    cfg = Config()
    cfg.douyin.android.auto_reply = "on" if mode == "send" else "dry_run"
    cfg.douyin.android.allow_send = mode == "send"
    if mode == "blocklist":
        cfg.guard.blocklist = ["测试对象"]
    phone = Phone()
    calls = 0

    async def sleep(_):
        phone.messages.append(auto.Message(False, "share", "视频") if mode == "media" else NEW)
        if mode == "own":
            phone.messages.append(auto.Message(True, "text", "人工回答"))
        if mode == "off":
            cfg.douyin.auto_reply = "off"
        if mode == "draft":
            phone.draft = "人工输入"

    async def decide(*args, **kwargs):
        nonlocal calls
        calls += 1
        assert args[2][-1].is_new and not args[2][-2].is_new
        if mode == "mutate":
            phone.messages.append(auto.Message(False, "text", "补充"))
        return ReplyDecision(
            should_reply=True,
            messages=["很好呀"] * (2 if mode == "multi" else 1),
            confidence=0.1 if mode == "low" else 0.9,
        )

    async def execute():
        with start_run("douyin", "android.run") as run:
            return await auto.watch(
                phone,
                run,
                cfg,
                "测试对象",
                "自己",
                confirmed=True,
                generate=mode != "observe",
                allow_send=mode == "send",
                sleep=sleep,
                load_config=lambda: cfg,
                decide=decide,
            )

    if mode in {"own", "off", "draft", "mutate"}:
        with pytest.raises(HumanRequired):
            await execute()
    else:
        result = await execute()
        assert (
            result["status"]
            == {
                "observe": "observed",
                "dry_run": "dry_run",
                "send": "ui_verified",
                "media": "deferred",
                "low": "blocked",
                "blocklist": "blocked",
                "multi": "dry_run",
            }[mode]
        )
    assert calls == (1 if mode in {"dry_run", "send", "low", "mutate", "multi"} else 0)
    assert phone.clicks == phone.writes == (1 if mode == "send" else 0)


@pytest.mark.asyncio
async def test_requires_confirmation_before_reading():
    with start_run("douyin", "android.run") as run, pytest.raises(AndroidError):
        await auto.watch(None, run, Config(), "测试对象", "自己")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["changed_after_type", "peer_echo", "lost_click"])
async def test_uncertain_send_freezes_without_retry(failure):
    from agent_accounts.core import store

    cfg = Config()
    cfg.douyin.android.auto_reply = "on"
    cfg.douyin.android.allow_send = True

    class FaultyPhone(Phone):
        def request(self, path, data):
            super().request(path, data)
            if failure == "changed_after_type":
                self.messages.append(auto.Message(False, "text", "又一条"))

        def click(self, rid):
            self.clicks += 1
            if failure == "lost_click":
                raise AndroidError("断线")
            self.messages.append(auto.Message(False, "text", self.draft))
            self.draft = ""

    phone = FaultyPhone()

    async def sleep(_):
        phone.messages.append(NEW)

    async def decide(*args, **kwargs):
        return ReplyDecision(should_reply=True, messages=["很好呀"], confidence=0.9)

    with pytest.raises(HumanRequired), start_run("douyin", "android.run") as run:
        await auto.watch(
            phone,
            run,
            cfg,
            "测试对象",
            "自己",
            confirmed=True,
            generate=True,
            allow_send=True,
            load_config=lambda: cfg,
            sleep=sleep,
            decide=decide,
        )
    assert phone.clicks == (0 if failure == "changed_after_type" else 1)
    assert store.get_account("douyin").status == "frozen"


@pytest.mark.asyncio
async def test_model_budget_disables_sdk_retries(monkeypatch):
    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            calls.append("closed")

        def with_options(self, **kwargs):
            assert kwargs == {"max_retries": 0}
            return self

    async def decide(*args, **kwargs):
        assert isinstance(kwargs["client"], Client)
        calls.append("request")
        return ReplyDecision(should_reply=False)

    monkeypatch.setattr(auto.reply, "make_client", lambda endpoint: Client())
    monkeypatch.setattr(auto.reply, "decide", decide)
    await auto.decide_once(Config().llm.reply, "persona", [])
    assert calls == ["request", "closed"]


@pytest.mark.asyncio
async def test_old_share_is_unknown_not_title_based_understanding():
    cfg = Config()
    phone = Phone()
    phone.messages[0] = auto.Message(False, "share", "不能作为理解依据的卡片标题")

    async def sleep(_):
        phone.messages.append(NEW)

    async def decide(endpoint, persona, lines, **kwargs):
        assert lines[0].content == "[历史分享尚未理解，内容未知]"
        assert not lines[0].is_new and lines[-1].is_new
        assert "必须 should_reply=false" in persona
        return ReplyDecision(should_reply=False, reason="需要历史内容", confidence=0.9)

    with start_run("douyin", "android.run") as run:
        result = await auto.watch(
            phone,
            run,
            cfg,
            "测试对象",
            "自己",
            confirmed=True,
            generate=True,
            load_config=lambda: cfg,
            sleep=sleep,
            decide=decide,
        )
    assert result["status"] == "skipped"
    assert phone.clicks == phone.writes == 0
