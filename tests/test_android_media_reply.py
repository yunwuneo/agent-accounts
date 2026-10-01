import json

import pytest

from agent_accounts.adapters.douyin.android import media_reply, shares
from agent_accounts.adapters.douyin.android import understanding as u
from agent_accounts.adapters.douyin.android.media import file_hash
from agent_accounts.adapters.douyin.android.session import AndroidError
from agent_accounts.core import media
from agent_accounts.core.config import Config
from agent_accounts.core.run import start_run
from agent_accounts.core.understand import DigestOutput


def evidence(kind="video"):
    return shares.Evidence(
        "https://v.douyin.com/test/",
        "测试作品",
        kind,
        1 if kind == "video" else 2,
        80 if kind == "video" else None,
        shares.fingerprint("synthetic card"),
    )


def manifest_for(run, e):
    files = []
    for i in range(e.count):
        path = run.dir / f"file-{i}.bin"
        path.write_bytes(b"synthetic-media" + bytes([i]))
        files.append({"file": path.name, "sha256": file_hash(path), "duration_s": 80})
    return {
        "kind": e.kind,
        "files": files,
        "parser": {
            "count": e.count,
            "title_match": True,
            "link_hash": shares.fingerprint(e.link),
            "order_source": "parser_image_number",
        },
    }


@pytest.mark.parametrize("damage", ["duration", "kind", "count", "title", "hash", "link", "path"])
def test_binding_mismatch_stops_before_models(damage):
    e = evidence()
    with start_run("douyin", "test") as run:
        m = manifest_for(run, e)
        if damage == "duration":
            m["files"][0]["duration_s"] = 30
        if damage == "kind":
            m["kind"] = "gallery"
        if damage == "count":
            m["parser"]["count"] = 2
        if damage == "title":
            m["parser"]["title_match"] = False
        if damage == "hash":
            m["files"][0]["sha256"] = "changed"
        if damage == "link":
            m["parser"]["link_hash"] = "other"
        if damage == "path":
            m["files"][0]["file"] = "../outside.mp4"
        with pytest.raises(AndroidError):
            u.validate_manifest(m, e, run)


@pytest.mark.parametrize("text,seconds", [("01:20", 80), ("1:01:20", 3680)])
def test_native_duration(text, seconds):
    assert shares.clock_seconds(text) == seconds


@pytest.mark.parametrize("kind", ["gallery", "video"])
def test_titleless_link_evidence_is_gallery_only(kind):
    e = evidence(kind)
    with start_run("douyin", "test") as run:
        m = manifest_for(run, e)
        m["parser"]["title_match"] = False
        with pytest.raises(AndroidError):
            u.validate_manifest(m, e, run)
        m["parser"]["submission_link_verified"] = True
        if kind == "gallery":
            assert len(u.validate_manifest(m, e, run)) == e.count
        else:
            with pytest.raises(AndroidError):
                u.validate_manifest(m, e, run)


def test_copy_only_extracts_one_supported_url():
    assert (
        shares.copied_link("作品 https://v.douyin.com/abc/?share_user=private 文案")
        == "https://v.douyin.com/abc/"
    )
    for value in ["https://evil.test/", "https://v.douyin.com/a/ https://v.douyin.com/b/"]:
        with pytest.raises(AndroidError.__base__):
            shares.copied_link(value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,failure",
    [
        ("video", ""),
        ("gallery", ""),
        ("video", "transcript"),
        ("video", "frames"),
        ("video", "audio"),
        ("gallery", "order"),
    ],
)
async def test_pipeline_coverage_budget_and_failure(monkeypatch, kind, failure):
    cfg, e, calls = Config(), evidence(kind), []

    async def parse(cfg, run, link, media_kind, count, **kwargs):
        assert kwargs["execute"]
        assert kwargs["expected_title"] == (e.title if kind == "video" else "")
        calls.append("parse")
        m = manifest_for(run, e)
        if failure == "order":
            m["parser"]["order_source"] = "unknown"
        (run.dir / "media.json").write_text(json.dumps(m), "utf-8")

    async def probe(path):
        return media.ProbeInfo(
            20 if failure == "audio" and path.suffix == ".mp3" else 80, True, True, 1080, 1920
        )

    async def frames(video, dest, **kwargs):
        times = media.frame_times(
            80, cfg.media.min_frames, cfg.media.max_frames, cfg.media.frame_interval_s
        )
        if failure == "frames":
            times.pop()
        return media.Frames([dest / f"{i}.jpg" for i in range(len(times))], times, 80)

    async def audio(video, dest, **kwargs):
        assert kwargs["max_seconds"] >= 80
        dest.write_bytes(b"audio")
        return dest

    async def jpeg(path, dest, **kwargs):
        return dest

    async def transcribe(endpoint, audio):
        calls.append("transcribe")
        return "" if failure == "transcript" else "完整语音内容"

    async def analyze(endpoint, inp):
        calls.append("understand")
        assert len(inp.images) == (16 if kind == "video" else 2)
        if kind == "video":
            assert inp.transcript == "完整语音内容"
        else:
            assert "全部 2 张" in inp.notes[-1]
        return DigestOutput(summary="具体作品内容", vibe="轻松", reply_hooks=["接话"])

    monkeypatch.setattr(media, "probe", probe)
    monkeypatch.setattr(media, "extract_frames", frames)
    monkeypatch.setattr(media, "extract_audio", audio)
    monkeypatch.setattr(media, "to_jpeg", jpeg)
    with start_run("douyin", "test") as run:
        if failure:
            with pytest.raises(AndroidError):
                await u.analyze(cfg, run, e, parse=parse, transcriber=transcribe, analyzer=analyze)
            assert "understand" not in calls
        else:
            summary = await u.analyze(
                cfg, run, e, parse=parse, transcriber=transcribe, analyzer=analyze
            )
            assert "具体作品内容" in summary
            assert calls == (
                ["parse", "transcribe", "understand"]
                if kind == "video"
                else ["parse", "understand"]
            )


@pytest.mark.asyncio
async def test_gallery_over_budget_never_downloads():
    cfg = Config()
    cfg.media.max_images = 1

    async def fail(*args, **kwargs):
        pytest.fail("must not download")

    with start_run("douyin", "test") as run, pytest.raises(AndroidError):
        await u.analyze(cfg, run, evidence("gallery"), parse=fail)


def test_repeat_send_rejected_before_paid_work():
    from agent_accounts.adapters.douyin.android.messaging import AndroidSend
    from agent_accounts.core import store

    key = media_reply.request_key("test", "card")
    with store.session() as db:
        db.add(AndroidSend(request_id=key, text_hash="hash", run_id="test", state="ui_verified"))
        db.commit()
    with pytest.raises(AndroidError):
        media_reply.request_key("test", "card")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2])
async def test_watch_connects_summary_and_defers_over_budget(monkeypatch, count):
    from test_android_autoreply import Phone

    from agent_accounts.adapters.douyin.android import autoreply as auto
    from agent_accounts.core.reply import ReplyDecision

    cfg, phone, calls = Config(), Phone(), []

    async def sleep(_):
        for i in range(count):
            phone.messages.append(auto.Message(False, "share", f"卡片{i}"))

    async def prepare(*args, **kwargs):
        calls.append("understood")
        return evidence(), "已验证内容：朋友送来花束", list(phone.messages)

    async def decide(endpoint, persona, lines, **kwargs):
        calls.append("decide")
        assert "朋友送来花束" in lines[-1].content and lines[-1].is_new
        return ReplyDecision(should_reply=True, messages=["花束好漂亮"], confidence=0.9)

    monkeypatch.setattr(media_reply, "prepare", prepare)
    with start_run("douyin", "test") as run:
        result = await auto.watch(
            phone,
            run,
            cfg,
            "测试对象",
            "自己",
            confirmed=True,
            generate=True,
            media_enabled=True,
            load_config=lambda: cfg,
            decide=decide,
            sleep=sleep,
        )
    assert result["status"] == ("dry_run" if count == 1 else "deferred")
    assert calls == (["understood", "decide"] if count == 1 else [])
    assert phone.clicks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("execute", [False, True])
async def test_history_uses_common_guard_and_sender(monkeypatch, execute):
    from test_android_autoreply import Phone

    from agent_accounts.core.reply import ReplyDecision

    cfg, phone = Config(), Phone()
    cfg.douyin.android.auto_reply = "on" if execute else "dry_run"
    cfg.douyin.android.allow_send = execute
    monkeypatch.setattr(shares, "list_shares", lambda *a: [{"card_hash": "card"}])

    async def prepare(*args, **kwargs):
        return evidence(), "这是完整作品摘要", list(phone.messages)

    async def decide(endpoint, persona, lines, **kwargs):
        assert lines[-1].is_new and "历史分享" in lines[-1].content
        assert "完整作品摘要" in lines[-1].content
        return ReplyDecision(should_reply=True, messages=["很有意思"], confidence=0.9)

    monkeypatch.setattr(media_reply, "prepare", prepare)
    with start_run("douyin", "test") as run:
        result = await media_reply.reply_share(
            phone,
            run,
            cfg,
            "测试对象",
            "自己",
            "card",
            confirmed=True,
            generate=True,
            execute=execute,
            load_config=lambda: cfg,
            decider=decide,
        )
    assert result["status"] == ("ui_verified" if execute else "dry_run")
    assert phone.clicks == int(execute)


@pytest.mark.asyncio
async def test_message_change_during_understanding_stops(monkeypatch):
    from test_android_autoreply import NEW, Phone

    from agent_accounts.core.errors import HumanRequired

    cfg, phone = Config(), Phone()
    monkeypatch.setattr(shares, "inspect_share", lambda *a: evidence())
    monkeypatch.setattr(shares, "bottom", lambda *a: None)

    async def analyze(*args, check_active, **kwargs):
        phone.messages.append(NEW)
        check_active()
        pytest.fail("must stop")

    monkeypatch.setattr(u, "analyze", analyze)
    with start_run("douyin", "test") as run, pytest.raises(HumanRequired):
        await media_reply.prepare(
            phone, run, cfg, "测试对象", "自己", "card", check_active=lambda: None
        )


def test_echo_layout_wait_never_reclicks(monkeypatch):
    from test_android import FakePhone

    from agent_accounts.adapters.douyin.android import messaging

    phone, cfg, checks = FakePhone(), Config(), []
    cfg.douyin.android.allow_send = True
    monkeypatch.setattr(messaging.time, "sleep", lambda _: None)

    def echo():
        checks.append(1)
        return len(checks) >= 3

    with start_run("douyin", "test") as run:
        result = messaging.send_one(
            phone,
            run,
            cfg,
            "测试对象",
            "测试回复",
            "layout_echo_01",
            execute=True,
            confirmed=True,
            verify_echo=echo,
        )
    assert result["status"] == "ui_verified"
    assert phone.clicks == phone.writes == 1
    assert len(checks) == 3


def test_history_selection_requires_complete_card_and_own_peer_avatar():
    import xml.etree.ElementTree as ET

    from test_android_autoreply import xml

    from agent_accounts.adapters.douyin.android.autoreply import Message
    from agent_accounts.adapters.douyin.android.session import PREFIX

    root = xml([Message(False, "share", "作品"), Message(True, "share", "自己的作品")])
    viewport = ET.SubElement(
        root,
        "node",
        {
            "resource-id": PREFIX + "v65",
            "bounds": "[0,150][1080,1750]",
        },
    )
    ET.SubElement(
        root,
        "node",
        {
            "resource-id": PREFIX + "dwh",
            "text": "测试对象的头像",
            "bounds": "[0,150][100,155]",
        },
    )
    assert len(shares.peer_cards(root, "测试对象", "自己")) == 1
    viewport.set("bounds", "[0,200][1080,1750]")
    assert shares.peer_cards(root, "测试对象", "自己") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("new_message", [False, True])
async def test_long_draft_restores_bottom_but_new_message_still_blocks(monkeypatch, new_message):
    import xml.etree.ElementTree as ET

    from test_android_autoreply import Phone, xml

    from agent_accounts.adapters.douyin.android import autoreply as auto
    from agent_accounts.adapters.douyin.android.session import PREFIX
    from agent_accounts.core.errors import HumanRequired
    from agent_accounts.core.reply import ReplyDecision

    class LayoutPhone(Phone):
        aligned = False

        def source(self):
            visible = self.messages[:-1] if self.draft and not self.aligned else self.messages
            root = xml(visible, self.draft)
            ET.SubElement(
                root, "node", {"resource-id": PREFIX + "v65", "bounds": "[0,150][1080,1750]"}
            )
            return root

    cfg, phone, swipes = Config(), LayoutPhone(), []
    cfg.douyin.android.auto_reply = "on"
    cfg.douyin.android.allow_send = True

    def align(*args, **kwargs):
        swipes.append(1)
        phone.aligned = True
        if new_message:
            phone.messages.append(auto.Message(False, "text", "新的消息"))

    monkeypatch.setattr(shares, "gesture", align)
    monkeypatch.setattr(auto.time, "sleep", lambda _: None)

    async def decide(*args, **kwargs):
        return ReplyDecision(should_reply=True, messages=["这是一段长草稿回复"], confidence=0.9)

    async def perform():
        with start_run("douyin", "test") as run:
            return await auto.respond(
                phone,
                run,
                cfg,
                cfg,
                "测试对象",
                "自己",
                list(phone.messages),
                [],
                "on",
                lambda: cfg,
                decide,
                True,
            )

    if new_message:
        with pytest.raises(HumanRequired):
            await perform()
    else:
        assert (await perform())["status"] == "ui_verified"
    assert swipes == [1]
    assert phone.clicks == (0 if new_message else 1)
