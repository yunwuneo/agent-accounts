"""``douyin digest``：把私信里分享的作品变成 MediaDigest（M2）。

流程：查缓存 → 打开作品页拿作品信息 → 下载到临时目录 → 视频抽帧 + 抽音轨转写 / 图集转 JPEG
→ 多模态理解 → 入库。临时目录处理完立即删除（草案：视频只用于临时分析，不保存）。

作品不可见（仅作者可见、审核中等）时，用私信分享卡片里的标题、作者、封面生成摘要，并在
notes 里说明。语音转写失败或没配置时不中断，只在 notes 里记录。
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import col, select

from agent_accounts.adapters.douyin import PLATFORM
from agent_accounts.adapters.douyin import media as dmedia
from agent_accounts.adapters.douyin import store as dstore
from agent_accounts.adapters.douyin.page import login_state, open_home
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core import digests, media, store
from agent_accounts.core.config import Config, ConfigError
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.run import RunContext
from agent_accounts.core.transcribe import TranscribeError, transcribe
from agent_accounts.core.understand import UnderstandError, UnderstandInput, understand

_URL = re.compile(r"douyin\.com/(video|note)/(\d+)")


def parse_target(text: str) -> tuple[str, dmedia.Kind | None]:
    """接受作品 ID 或 douyin.com/video|note/<id> 链接。"""
    if text.isdigit():
        return text, None
    if m := _URL.search(text):
        return m.group(2), m.group(1)  # type: ignore[return-value]
    raise ValueError(f"无法识别的作品：{text}（支持作品 ID 或 douyin.com/video|note/<id> 链接）")


def share_message(aweme_id: str) -> dstore.DouyinMessage | None:
    with store.session() as s:
        return s.exec(
            select(dstore.DouyinMessage).where(dstore.DouyinMessage.aweme_id == aweme_id)
        ).first()


def kind_hint(aweme_id: str) -> dmedia.Kind:
    msg = share_message(aweme_id)
    return "note" if msg and msg.type == "note_share" else "video"


def pending_items(limit: int | None = None) -> list[str]:
    """对方分享过、还没有摘要的作品，按分享时间从新到旧。"""
    with store.session() as s:
        rows = s.exec(
            select(dstore.DouyinMessage)
            .where(
                col(dstore.DouyinMessage.aweme_id).is_not(None),
                col(dstore.DouyinMessage.from_me).is_(False),
            )
            .order_by(col(dstore.DouyinMessage.msg_index).desc())
        ).all()
        done = {
            d.item_id
            for d in s.exec(
                select(digests.MediaDigest).where(digests.MediaDigest.platform == PLATFORM)
            ).all()
        }
    ids: list[str] = []
    for m in rows:
        if m.aweme_id and m.aweme_id not in done and m.aweme_id not in ids:
            ids.append(m.aweme_id)
    return ids[:limit] if limit else ids


@dataclass
class DigestOutcome:
    aweme_id: str
    digest: digests.MediaDigest | None = None
    cached: bool = False
    error: str | None = None


async def _build_input(
    s: BrowserSession, cfg: Config, m: dmedia.DouyinMedia, tmp: Path
) -> tuple[UnderstandInput, list[str]]:
    notes: list[str] = []
    inp = UnderstandInput(
        kind=m.kind,
        title=m.title,
        author=m.author,
        hashtags=m.hashtags,
        duration_s=m.duration_s,
        music_title=m.music_title,
    )
    if m.kind == "video" and m.video_url:
        video = await dmedia.download(s, m.video_url, tmp / "video.mp4")
        inp.images = await media.extract_frames(
            video,
            tmp,
            min_frames=cfg.media.min_frames,
            max_frames=cfg.media.max_frames,
            max_side=cfg.media.frame_width,
        )
        audio = await media.extract_audio(
            video, tmp / "audio.mp3", max_seconds=cfg.media.max_video_seconds
        )
        video.unlink()
        if audio is None:
            notes.append("视频没有音轨")
        else:
            try:
                inp.transcript = await transcribe(cfg.transcribe, audio) or None
            except (ConfigError, TranscribeError) as e:
                notes.append(f"语音转写失败：{e}")
    elif m.kind == "video":
        notes.append("没有拿到视频播放地址，只根据文字信息理解")
    elif m.kind == "note":
        for i, url in enumerate(m.image_urls[: cfg.media.max_images]):
            raw = await dmedia.download(s, url, tmp / f"image_{i:02d}.raw")
            inp.images.append(
                await media.to_jpeg(raw, tmp / f"image_{i:02d}.jpg", max_side=cfg.media.frame_width)
            )
            raw.unlink()
    return inp, notes


async def _unavailable_input(
    s: BrowserSession, cfg: Config, m: dmedia.DouyinMedia, tmp: Path
) -> tuple[UnderstandInput, list[str]]:
    msg = share_message(m.aweme_id)
    note = (
        f"作品当前不可见（{m.filter_reason}），"
        "只能根据私信分享卡片的标题、作者和封面理解，可能不完整"
    )
    inp = UnderstandInput(
        kind="note" if msg and msg.type == "note_share" else "video",
        title=(msg.share_title if msg else "") or "",
        author=(msg.share_author if msg else "") or "",
        notes=[note],
    )
    if msg and msg.cover_url:
        try:
            raw = await dmedia.download(s, msg.cover_url, tmp / "cover.raw")
            inp.images = [
                await media.to_jpeg(raw, tmp / "cover.jpg", max_side=cfg.media.frame_width)
            ]
        except Exception as e:  # 封面拿不到也继续，只用文字
            return inp, [note, f"封面下载失败：{type(e).__name__}"]
    return inp, [note]


async def digest_items(
    cfg: Config,
    run: RunContext,
    aweme_ids: list[str],
    *,
    force: bool = False,
    kinds: dict[str, dmedia.Kind] | None = None,
) -> list[DigestOutcome]:
    outcomes: list[DigestOutcome] = []
    todo: list[str] = []
    for aweme_id in aweme_ids:
        if not force and (cached := digests.get(PLATFORM, aweme_id)):
            outcomes.append(DigestOutcome(aweme_id, cached, cached=True))
        else:
            todo.append(aweme_id)
    if not todo:
        return outcomes

    cfg.llm.understand.require_key("llm.understand")  # 先检查配置，避免白开浏览器
    async with BrowserSession(PLATFORM, cfg.browser) as s:
        await open_home(s, cfg.douyin.base_url)
        if not (await login_state(s)).logged_in:
            raise HumanRequired("未登录", "请先运行 douyin login")
        outcomes += await digest_in_session(s, cfg, run, todo, kinds=kinds)
    return outcomes


async def digest_in_session(
    s: BrowserSession,
    cfg: Config,
    run: RunContext,
    aweme_ids: list[str],
    *,
    kinds: dict[str, dmedia.Kind] | None = None,
) -> list[DigestOutcome]:
    """在已登录的浏览器会话里逐个分析（不查缓存，调用方负责过滤）。会离开当前页面。"""
    outcomes = []
    for i, aweme_id in enumerate(aweme_ids):
        if i:
            await s.pause(2.0, 4.0)
        hint = (kinds or {}).get(aweme_id) or kind_hint(aweme_id)
        outcomes.append(await _digest_one(s, cfg, run, aweme_id, hint))
    return outcomes


async def _digest_one(
    s: BrowserSession, cfg: Config, run: RunContext, aweme_id: str, hint: dmedia.Kind
) -> DigestOutcome:
    try:
        m = await dmedia.resolve(s, aweme_id, hint)
        if m is None:
            return DigestOutcome(aweme_id, error="没有拿到作品信息")
        with tempfile.TemporaryDirectory(prefix="aa-media-") as tmp_dir:
            tmp = Path(tmp_dir)
            build = _build_input if m.available else _unavailable_input
            inp, notes = await build(s, cfg, m, tmp)
            out = await understand(cfg.llm.understand, inp)
    except HumanRequired:
        raise
    except (UnderstandError, media.MediaError, RuntimeError) as e:
        run.audit("douyin.digest.error", aweme_id=aweme_id, error=str(e)[:300])
        return DigestOutcome(aweme_id, error=str(e))

    digest = digests.save(
        digests.MediaDigest(
            platform=PLATFORM,
            item_id=aweme_id,
            kind=inp.kind,
            available=m.available,
            filter_reason=m.filter_reason,
            title=inp.title,
            author=inp.author,
            hashtags_json=json.dumps(inp.hashtags, ensure_ascii=False),
            duration_s=inp.duration_s,
            music_title=inp.music_title,
            transcript=inp.transcript,
            frames_used=len(inp.images),
            summary=out.summary,
            vibe=out.vibe,
            reply_hooks_json=json.dumps(out.reply_hooks, ensure_ascii=False),
            model=cfg.llm.understand.model,
            notes_json=json.dumps(notes, ensure_ascii=False),
        )
    )
    run.audit(
        "douyin.digest",
        aweme_id=aweme_id,
        kind=digest.kind,
        available=digest.available,
        frames=digest.frames_used,
        transcript_chars=len(digest.transcript or ""),
        model=digest.model,
    )
    return DigestOutcome(aweme_id, digest)
