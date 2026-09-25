"""作品 JSON 解析测试。fixture 由真实作品 JSON 脱敏生成（scripts/make_douyin_fixtures.py）。"""

from __future__ import annotations

import json
from pathlib import Path

from agent_accounts.adapters.douyin import media

FIXTURES = Path(__file__).parent / "fixtures" / "douyin"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_video_picks_smallest_mp4_at_least_720p():
    m = media.parse_aweme(load("aweme_video.json"))
    assert m.kind == "video" and m.available
    assert m.video_url == "https://example.invalid/720_4_1.mp4"  # 不选 dash，不选 1080p
    assert m.duration_s and 70 < m.duration_s < 80
    assert m.hashtags and m.author and m.title
    assert m.image_urls == []


def test_note_uses_images_not_video():
    m = media.parse_aweme(load("aweme_note.json"))
    assert m.kind == "note"
    assert m.image_urls == ["https://example.invalid/image1.webp"]
    assert m.video_url is None
    assert m.music_title


def test_select_video_url_fallbacks():
    low = {"format": "mp4", "play_addr": {"width": 640, "height": 360, "url_list": ["low"]}}
    mid = {"format": "mp4", "play_addr": {"width": 960, "height": 540, "url_list": ["mid"]}}
    dash = {"format": "dash", "play_addr": {"width": 1280, "height": 720, "url_list": ["dash"]}}
    assert media.select_video_url({"bit_rate": [low, mid, dash]}) == "mid"  # 都不到 720p 取最大
    assert media.select_video_url({"play_addr": {"url_list": ["main"]}}) == "main"
    assert media.select_video_url({}) is None


def test_find_aweme_and_filter_in_nested_json():
    detail = load("aweme_video.json")
    wrapped = {"aweme_list": [{"aweme_id": "other", "video": {}}, detail]}
    assert media.find_aweme(wrapped, str(detail["aweme_id"])) is detail
    blocked = {"filter_detail": {"aweme_id": "123", "filter_reason": "status_audit_self_see"}}
    assert media.find_aweme(blocked, "123") is None
    assert media.find_filter(blocked, "123")["filter_reason"] == "status_audit_self_see"
