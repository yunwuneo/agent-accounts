"""媒体摘要缓存：按 (平台, 作品 ID) 存，同一个作品只分析一次。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlmodel import Field, SQLModel

from agent_accounts.core import store


def _now() -> datetime:
    return datetime.now(UTC)


class MediaDigest(SQLModel, table=True):
    __tablename__ = "media_digests"

    platform: str = Field(primary_key=True)
    item_id: str = Field(primary_key=True)
    kind: str  # video / note
    available: bool = True  # 作品不可见时只基于分享卡片分析
    filter_reason: str | None = None
    title: str = ""
    body: str = ""  # 正文（小红书笔记；抖音作品的描述在 title 里）
    author: str = ""
    hashtags_json: str = "[]"
    duration_s: float | None = None
    music_title: str | None = None
    transcript: str | None = None
    frames_used: int = 0
    summary: str = ""
    vibe: str = ""
    reply_hooks_json: str = "[]"
    model: str = ""
    notes_json: str = "[]"  # 处理过程中的降级说明（如转写失败）
    created_at: datetime = Field(default_factory=_now)

    @property
    def hashtags(self) -> list[str]:
        return json.loads(self.hashtags_json)

    @property
    def reply_hooks(self) -> list[str]:
        return json.loads(self.reply_hooks_json)

    @property
    def notes(self) -> list[str]:
        return json.loads(self.notes_json)


def get(platform: str, item_id: str) -> MediaDigest | None:
    with store.session() as s:
        return s.get(MediaDigest, (platform, item_id))


def save(digest: MediaDigest) -> MediaDigest:
    with store.session() as s:
        merged = s.merge(digest)
        s.commit()
        s.refresh(merged)
        return merged
