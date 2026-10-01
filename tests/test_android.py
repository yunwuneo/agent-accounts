from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import timedelta

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from agent_accounts.adapters.douyin.android import capture, media, messaging
from agent_accounts.adapters.douyin.android.session import (
    PACKAGE,
    PREFIX,
    AndroidError,
    AndroidSession,
    check_source,
)
from agent_accounts.adapters.douyin.cli import app
from agent_accounts.adapters.douyin.replying import rate_stats
from agent_accounts.core import store
from agent_accounts.core.config import AndroidConfig, Config
from agent_accounts.core.errors import AccountFrozen, HumanRequired
from agent_accounts.core.operation_lock import platform_lock
from agent_accounts.core.run import start_run


def screen(**items):
    root = ET.Element("hierarchy")
    for rid, value in items.items():
        ET.SubElement(
            root,
            "android.widget.TextView",
            {
                "resource-id": PREFIX + rid,
                "text": value,
                "package": PACKAGE,
                "bounds": "[0,0][100,100]",
            },
        )
    return root


class FakePhone:
    def __init__(self, *, fail_click=False, mismatch=False, draft=""):
        self.draft = draft
        self.title = "测试对象"
        self.text = ""
        self.clicks = 0
        self.writes = 0
        self.fail_click = fail_click
        self.mismatch = mismatch

    def source(self):
        return screen(vw_=self.title, msg_et=self.draft, sws=self.text, jaz="发送")

    def element(self, rid):
        return rid

    def request(self, path, data):
        self.writes += 1
        self.draft = "错误草稿" if self.mismatch else data["text"]

    def click(self, rid):
        self.clicks += 1
        if self.fail_click:
            raise AndroidError("模拟断线")
        self.text, self.draft = self.draft, ""

    def screenshot(self, path):
        content = b"synthetic screenshot"
        path.write_bytes(content)
        return content


def enabled_config():
    cfg = Config()
    cfg.douyin.android.allow_send = True
    return cfg


def test_default_no_android_and_local_endpoint_only():
    assert not Config().douyin.android.enabled
    assert not Config().douyin.android.allow_send
    for url in (
        "http://example.com",
        "http://127.0.0.1@evil.test",
        "https://localhost",
        "http://localhost:4725?token=secret",
        "http://localhost/path",
    ):
        with pytest.raises(ValidationError):
            AndroidConfig(appium_url=url)


def test_session_disabled_never_connects(monkeypatch):
    phone = AndroidSession(AndroidConfig())
    monkeypatch.setattr(phone, "adb", lambda *a: pytest.fail("must not touch USB"))
    with pytest.raises(AndroidError), phone:
        pass


def test_source_widgets_not_only_node_and_risk_freezes():
    xml = ET.tostring(screen(vw_="测试"), encoding="unicode")
    assert check_source(xml).find("android.widget.TextView") is not None
    with pytest.raises(HumanRequired) as caught:
        check_source(ET.tostring(screen(dialog="请完成验证"), encoding="unicode"))
    assert caught.value.freeze
    with pytest.raises(HumanRequired), start_run("douyin", "android.doctor"):
        check_source(ET.tostring(screen(dialog="安全验证"), encoding="unicode"))
    assert store.get_account("douyin").status == "frozen"


def test_account_operation_lock_released():
    with platform_lock("douyin"), pytest.raises(AndroidError.__base__), platform_lock("douyin"):
        pass
    with platform_lock("douyin"):
        pass


def test_dry_run_does_not_type_or_click():
    phone = FakePhone()
    with start_run("douyin", "android.send") as run:
        result = messaging.send_one(phone, run, Config(), "测试对象", "你好", "request_01")
    assert result["status"] == "dry_run"
    assert phone.writes == phone.clicks == 0


@pytest.mark.parametrize("kwargs", [{"draft": "保留我"}, {"mismatch": True}])
def test_draft_preservation_and_readback(kwargs):
    phone = FakePhone(**kwargs)
    with pytest.raises((AndroidError, HumanRequired)), start_run("douyin", "android.send") as run:
        messaging.send_one(
            phone,
            run,
            enabled_config(),
            "测试对象",
            "你好",
            "request_01",
            execute=True,
            confirmed=True,
        )
    assert phone.clicks == 0
    if kwargs.get("draft"):
        assert phone.draft == "保留我" and phone.writes == 0


@pytest.mark.parametrize(
    "expected,text",
    [("另一个人", "你好"), ("测试对象", "第一行\n第二行"), ("测试对象", "http://example.com")],
)
def test_send_preflight_blocks(expected, text):
    phone = FakePhone()
    with pytest.raises(AndroidError), start_run("douyin", "android.send") as run:
        messaging.send_one(
            phone, run, enabled_config(), expected, text, "request_01", execute=True, confirmed=True
        )
    assert phone.writes == phone.clicks == 0


def test_send_requires_both_switches():
    phone = FakePhone()
    with pytest.raises(AndroidError), start_run("douyin", "android.send") as run:
        messaging.send_one(
            phone, run, Config(), "测试对象", "你好", "request_01", execute=True, confirmed=True
        )
    assert phone.writes == 0


def test_send_once_audit_redacted_and_shared_rate_limit():
    phone = FakePhone()
    cfg = enabled_config()
    with start_run("douyin", "android.send") as run:
        result = messaging.send_one(
            phone, run, cfg, "测试对象", "私密测试内容", "request_01", execute=True, confirmed=True
        )
    assert result["status"] == "ui_verified" and not result["server_receipt"]
    assert phone.clicks == 1
    rates = rate_stats("web-conversation", store.utcnow())
    assert rates.sent_last_day == 1 and rates.sent_last_hour == 1
    assert rates.last_sent_in_conv is not None
    with store.session() as db:
        from sqlmodel import select

        events = db.exec(select(store.AuditEvent)).all()
        assert "私密测试内容" not in str([e.detail for e in events])
        assert "测试对象" not in str([e.detail for e in events])
    cfg.guard.min_interval_s = 0
    with pytest.raises(AndroidError), start_run("douyin", "android.send") as run:
        messaging.send_one(
            phone, run, cfg, "测试对象", "私密测试内容", "request_01", execute=True, confirmed=True
        )
    assert phone.clicks == 1


def test_uncertain_send_freezes_no_retry_and_requires_resolution():
    phone = FakePhone(fail_click=True)
    with pytest.raises(HumanRequired), start_run("douyin", "android.send") as run:
        messaging.send_one(
            phone,
            run,
            enabled_config(),
            "测试对象",
            "你好",
            "request_01",
            execute=True,
            confirmed=True,
        )
    assert phone.clicks == 1
    store.set_account_status("douyin", "active")  # 即使误解冻，pending 仍阻止网页/安卓。
    with pytest.raises(AccountFrozen), start_run("douyin", "sync"):
        pytest.fail("must not run")
    with start_run("douyin", "android.resolve-send", require_active=False) as run:
        messaging.resolve_send(run, "request_01")
    assert store.get_account("douyin").status == "frozen"


def test_snapshot_no_draft_no_fake_message_id():
    phone = FakePhone(draft="秘密草稿")
    with start_run("douyin", "android.snapshot") as run:
        messaging.snapshot(phone, run, "测试对象")
    from agent_accounts.mcp_server import douyin_android_read_snapshot

    snapshot = douyin_android_read_snapshot(run.id)
    assert not snapshot["complete_history"] and not snapshot["stable_ids"]
    assert "秘密草稿" not in json.dumps(snapshot, ensure_ascii=False)
    with pytest.raises(ValueError):
        douyin_android_read_snapshot("../../config.toml")


def test_coverage_does_not_claim_exact_timestamps():
    frames = [{"start_s": i * 1.5, "end_s": i * 1.5 + 1.2} for i in range(8)]
    result = capture.coverage(frames, [1, 6])
    assert result["loop_frame_count"] == 5
    assert result["max_exposure_gap_bound_s"] == pytest.approx(2.7)
    assert result["loop_period_s"] == pytest.approx(7.5)
    assert not result["exact_endpoints"] and not result["exact_frame_times"]
    assert not capture.coverage(frames, [1])["loop_observed"]


class GalleryPhone(FakePhone):
    def __init__(self):
        super().__init__()
        self.pages = []

    def source(self):
        root = screen(c_e="退出专注模式", sg0="播放视频")
        for i in range(1, 4):
            ET.SubElement(root, "android.widget.Button", {"content-desc": f"图片{i}，按钮"})
        return root

    def tap(self, node):
        self.pages.append(node.get("content-desc"))


def test_gallery_all_pages_keeps_identical_images(monkeypatch):
    monkeypatch.setattr(capture.time, "sleep", lambda _: None)
    phone = GalleryPhone()
    with start_run("douyin", "android.capture-gallery") as run:
        result = capture.capture_gallery(phone, run, 3)
        manifest = json.loads((run.dir / "media.json").read_text("utf-8"))
    assert result["count"] == 3 and len(phone.pages) == 3
    assert not result["complete"]
    assert [f["requested_page"] for f in manifest["frames"]] == [1, 2, 3]
    assert len({f["sha256"] for f in manifest["frames"]}) == 1  # 不能删掉真正重复页


def test_gallery_wrong_count_does_not_capture():
    phone = GalleryPhone()
    with pytest.raises(AndroidError), start_run("douyin", "android.capture-gallery") as run:
        capture.capture_gallery(phone, run, 2)
    assert not phone.pages


def test_export_path_and_magic_limits(tmp_path):
    data = (
        "_data=/storage/emulated/0/Pictures/douyin/share_ab12.png, date_added=1\n"
        "_data=/storage/emulated/0/DCIM/private.png, date_added=1\n"
        "_data=/storage/emulated/0/Pictures/douyin/share_../../bad.png, date_added=1"
    )
    assert media.exported_paths(data) == ["/storage/emulated/0/Pictures/douyin/share_ab12.png"]
    path = tmp_path / "misnamed.png"
    path.write_bytes(b"RIFF0000WEBPdata")
    assert media.image_extension(path) == ".webp"
    path.write_bytes(b"not an image")
    with pytest.raises(AndroidError):
        media.image_extension(path)


async def test_import_preserves_audio_and_provenance(tmp_path, monkeypatch):
    from agent_accounts.core.media import ProbeInfo

    async def probe(_):
        return ProbeInfo(81, True, True, 1080, 1920)

    async def decode(*args):
        assert "-xerror" in args
        return ""

    monkeypatch.setattr(media.media, "probe", probe)
    monkeypatch.setattr(media.media, "_run", decode)
    path = tmp_path / "download.mp4"
    path.write_bytes(b"\x00\x00\x00\x20ftypisom synthetic video")
    with start_run("douyin", "android.import-media") as run:
        result = await media.validate_files(run, [path], "video", "third_party_manual", 1)
        manifest = json.loads((run.dir / "media.json").read_text("utf-8"))
    assert result["validated_files"] == 1 and not result["automatic_digest"]
    assert manifest["files"][0]["has_audio"]
    assert not manifest["content_match_verified"]


def test_cli_registration_and_no_device_on_help():
    runner = CliRunner()
    result = runner.invoke(app, ["android", "--help"])
    assert result.exit_code == 0, result.output
    assert "capture-video" in result.output and "import-media" in result.output
    result = runner.invoke(app, ["android", "doctor"])
    assert result.exit_code == 2 and "enabled=true" in result.output


def test_sqlite_send_time_is_utc():
    from agent_accounts.adapters.douyin import store as dstore

    now = store.utcnow()
    dstore.save_reply(dstore.DouyinReply(conv_id="x", status="sent", sent_at=now))
    rates = rate_stats("x", now + timedelta(seconds=1))
    assert rates.sent_last_hour == 1


class VideoPhone(FakePhone):
    def __init__(self, risk=False):
        super().__init__()
        self.positions = iter([0, 9500, 100, 4000, 7000, 9600, 100, 100])
        self.playing = False
        self.reads = 0
        self.risk = risk

    def source(self):
        self.reads += 1
        if self.risk and self.reads == 4:
            raise HumanRequired("安全验证", freeze=True)
        return screen(
            **{
                "c_e": "退出专注模式",
                "0mk": "1.0倍速",
                "u6y": "暂停视频" if self.playing else "播放视频",
                "6jy": str(next(self.positions, 100)),
            }
        )

    def click(self, rid):
        self.clicks += 1
        self.playing = not self.playing


def test_video_two_wraps_and_pause(monkeypatch):
    monkeypatch.setattr(capture.time, "sleep", lambda _: None)
    phone = VideoPhone()
    with start_run("douyin", "android.capture-video") as run:
        result = capture.capture_video(phone, run, 81)
        data = json.loads((run.dir / "media.json").read_text("utf-8"))
    assert data["coverage"]["loop_observed"]
    assert data["coverage"]["loop_frame_count"] == 4
    assert result["count"] == 6 and not result["complete"]
    assert not data["audio"] and phone.clicks == 2 and not phone.playing


def test_video_risk_stops_all_ui_and_preserves_partial(monkeypatch):
    monkeypatch.setattr(capture.time, "sleep", lambda _: None)
    phone = VideoPhone(risk=True)
    with pytest.raises(HumanRequired), start_run("douyin", "android.capture-video") as run:
        capture.capture_video(phone, run, 81)
    data = json.loads((run.dir / "media.json").read_text("utf-8"))
    assert len(data["frames"]) == 2 and not data["coverage"]["loop_observed"]
    assert phone.clicks == 1  # 风控后不再自动暂停/点击
    assert store.get_account("douyin").status == "frozen"


def test_video_stalled_timeout_preserves_incomplete(monkeypatch):
    phone = VideoPhone()
    phone.positions = iter([0] * 100)
    with start_run("douyin", "android.capture-video") as run:
        with monkeypatch.context() as patch:
            ticks = iter(range(0, 200, 2))
            patch.setattr(capture.time, "monotonic", lambda: next(ticks))
            patch.setattr(capture.time, "sleep", lambda _: None)
            result = capture.capture_video(phone, run, 1)
        data = json.loads((run.dir / "media.json").read_text("utf-8"))
    assert not data["coverage"]["loop_observed"]
    assert not result["complete"]


async def test_import_rejects_playlist_before_ffprobe(tmp_path, monkeypatch):
    path = tmp_path / "fake.mp4"
    path.write_text("#EXTM3U\nhttps://example.com/private", "utf-8")
    monkeypatch.setattr(media.media, "probe", lambda p: pytest.fail("must not invoke ffprobe"))
    with pytest.raises(AndroidError), start_run("douyin", "android.import-media") as run:
        await media.validate_files(run, [path], "video", "third_party_manual", 1)


def test_official_export_only_pulls_new_matching_files(monkeypatch):
    class ExportPhone(FakePhone):
        saved = False
        pulled = []

        def source(self):
            return screen(all="全选", save="保存(1)张图片")

        def tap(self, node):
            if node.get("text").startswith("保存"):
                self.saved = True

        def adb(self, *args):
            if args == ("shell", "date", "+%s"):
                return "123456"
            if args[0] == "pull":
                self.pulled.append(args[1])
                from pathlib import Path

                Path(args[2]).write_bytes(b"RIFF0000WEBPdata")
                return ""
            assert "date_added>=123456" in args[1]
            old = "_data=/storage/emulated/0/Pictures/douyin/share_aa.png, date_added=123456"
            return old + (
                "\n_data=/storage/emulated/0/Pictures/douyin/share_bb.png, date_added=123456"
                if self.saved
                else ""
            )

    async def validate(run, files, kind, source, pages):
        return {"count": len(files), "source": source}

    monkeypatch.setattr(media, "validate_files", validate)
    phone = ExportPhone()
    with start_run("douyin", "android.export-gallery") as run:
        result = media.export_gallery(phone, run, 1)
    assert result["count"] == 1
    assert phone.pulled == ["/storage/emulated/0/Pictures/douyin/share_bb.png"]


def test_session_locked_phone_never_starts_appium(monkeypatch):
    phone = AndroidSession(AndroidConfig(enabled=True, udid="synthetic"))
    monkeypatch.setattr(
        phone,
        "adb",
        lambda *args: "device" if args[0] == "get-state" else "mShowingLockscreen=true",
    )
    monkeypatch.setattr(phone, "request", lambda *a, **k: pytest.fail("must not start Appium"))
    with pytest.raises(HumanRequired), phone:
        pass
