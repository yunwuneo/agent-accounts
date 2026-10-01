"""小红书 M2：笔记解析、视频地址选择、理解输入。不开浏览器、不调模型、不访问网络。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_accounts.adapters.xiaohongshu import digest as xdigest
from agent_accounts.adapters.xiaohongshu import im
from agent_accounts.adapters.xiaohongshu import media as xmedia
from agent_accounts.adapters.xiaohongshu import store as xstore
from agent_accounts.core import config, digests
from agent_accounts.core.understand import SYSTEM, UnderstandInput, build_content, system_prompt

FIXTURES = Path(__file__).parent / "fixtures" / "xiaohongshu"
ME = "a" * 24
VIDEO_ID = "6600000000000000000000a1"
NORMAL_ID = "6600000000000000000000a2"


def _stream(w, h, size, name):
    return {
        "width": w,
        "height": h,
        "size": size,
        "duration": 42000,
        "format": "mp4",
        "masterUrl": f"https://sns-video.example/{name}.mp4",
        "backupUrls": [f"https://sns-video-bak.example/{name}.mp4"],
    }


def _state():
    """noteDetailMap 的形状取自真实页面（M0 spike），取值虚构；另带一篇推荐笔记干扰。"""
    video_note = {
        "noteId": VIDEO_ID,
        "type": "video",
        "title": "",
        "desc": "跳舞教学 #热点[话题]# #舞蹈[话题]# 第二段",
        "user": {"userId": "e" * 24, "nickname": "作者"},
        "tagList": [{"id": "f" * 24, "name": "热点", "type": "topic"}],
        "imageList": [{"urlDefault": "https://sns-img.example/cover.webp", "infoList": []}],
        "video": {
            "capa": {"duration": 42},
            "media": {
                "stream": {
                    "EF6": [],
                    "EF5": [
                        _stream(1080, 1920, 9_000_000, "1080"),
                        _stream(720, 1280, 4_000_000, "720"),
                    ],
                    "EF4": [_stream(540, 960, 2_000_000, "540")],
                }
            },
        },
    }
    other = {"noteId": "7" * 24, "type": "normal", "title": "别的笔记", "imageList": []}
    return {
        VIDEO_ID: {"note": video_note, "comments": {"list": []}},
        "7" * 24: {"note": other},
    }


def test_find_and_parse_video_note():
    assert xmedia.find_note(_state(), "0" * 24) is None
    note = xmedia.parse_note(xmedia.find_note(_state(), VIDEO_ID), VIDEO_ID)
    assert note.kind == "video" and note.duration_s == 42
    assert note.body == "跳舞教学 #热点 #舞蹈 第二段"
    assert note.author == "作者" and note.tags == ["热点"]
    # 720p 以上体积最小的优先，然后 1080，最后低于 720 的；每档 master 在前、backup 在后
    assert note.video_urls == [
        "https://sns-video.example/720.mp4",
        "https://sns-video-bak.example/720.mp4",
        "https://sns-video.example/1080.mp4",
        "https://sns-video-bak.example/1080.mp4",
        "https://sns-video.example/540.mp4",
        "https://sns-video-bak.example/540.mp4",
    ]


def test_parse_image_note_prefers_default_scene():
    note = xmedia.parse_note(
        {
            "type": "normal",
            "title": "标题",
            "desc": "正文",
            "imageList": [
                {
                    "urlDefault": "https://sns-img.example/a-default.webp",
                    "infoList": [
                        {"imageScene": "WB_PRV", "url": "https://sns-img.example/a-prv.webp"},
                        {"imageScene": "WB_DFT", "url": "https://sns-img.example/a-dft.webp"},
                    ],
                },
                {"urlDefault": "https://sns-img.example/b.webp"},
                {},
            ],
        },
        NORMAL_ID,
    )
    assert note.kind == "note" and note.video_urls == [] and note.duration_s is None
    assert note.image_urls == [
        "https://sns-img.example/a-dft.webp",
        "https://sns-img.example/b.webp",
    ]


def test_note_url_uses_card_token():
    url = xmedia.note_url(NORMAL_ID, "AB+c/d=")
    assert url == (
        f"https://www.xiaohongshu.com/explore/{NORMAL_ID}"
        "?xsec_token=AB%2Bc%2Fd%3D&xsec_source=app_share"
    )


def test_xiaohongshu_prompt_and_douyin_prompt_unchanged():
    inp = UnderstandInput(kind="note", platform="xiaohongshu", title="标题", body="正文内容")
    assert "小红书笔记" in system_prompt(inp) and "抖音" not in system_prompt(inp)
    text = build_content(inp)[-1]["text"]
    assert "作品类型：图文笔记" in text and "正文：\n正文内容" in text
    douyin = UnderstandInput(kind="note", title="标题")
    assert system_prompt(douyin) is SYSTEM and "抖音作品" in SYSTEM
    assert "作品类型：图集" in build_content(douyin)[-1]["text"]


def _load_history():
    body = json.loads((FIXTURES / "history.json").read_text(encoding="utf-8"))
    xstore.apply_messages(im.parse_history(body), ME)


def test_pending_items_and_share_message():
    _load_history()
    assert xdigest.pending_items() == [VIDEO_ID, NORMAL_ID]
    assert xdigest.share_message(VIDEO_ID).note_xsec_token == "TOKENvideo="
    digests.save(digests.MediaDigest(platform="xiaohongshu", item_id=VIDEO_ID, kind="video"))
    assert xdigest.pending_items() == [NORMAL_ID]
    digests.save(digests.MediaDigest(platform="douyin", item_id=NORMAL_ID, kind="video"))
    assert xdigest.pending_items() == [NORMAL_ID]  # 别的平台的同名 ID 不算


@pytest.fixture
def jpeg_source(tmp_path):
    src = tmp_path / "src.png"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=1600x1200",
         "-frames:v", "1", str(src)],
        check=True,
    )  # fmt: skip
    return src


async def test_build_input_for_image_note(monkeypatch, tmp_path, jpeg_source):
    fetched = []

    async def download(s, url, dest, timeout_s=120):
        fetched.append(url)
        shutil.copy(jpeg_source, dest)
        return dest

    monkeypatch.setattr(xmedia, "download", download)
    note = xmedia.XhsNote(
        NORMAL_ID, "note", title="标题", body="正文", author="作者",
        image_urls=[f"https://sns-img.example/{i}.webp" for i in range(4)],
    )  # fmt: skip
    cfg = config.Config(media=config.MediaConfig(max_images=3))
    work = tmp_path / "work"
    work.mkdir()
    inp, notes = await xdigest._build_input(None, cfg, note, work)
    assert inp.platform == "xiaohongshu" and inp.body == "正文" and inp.kind == "note"
    assert len(fetched) == 3 and len(inp.images) == 3
    assert all(p.suffix == ".jpg" and p.exists() for p in inp.images)
    assert not list(work.glob("*.raw"))  # 原图转完即删
    assert notes == ["笔记共 4 张图，只看了前 3 张"] and inp.notes == notes


async def test_digest_items_requires_share_card():
    outcomes = await xdigest.digest_items(config.Config(), None, [VIDEO_ID])
    assert outcomes[0].error and "xsec_token" in outcomes[0].error
