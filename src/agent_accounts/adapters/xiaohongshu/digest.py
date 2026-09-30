"""``xiaohongshu digest``：把私信里分享的笔记变成 MediaDigest（M2）。

流程：查缓存 → 用私信卡片里的 note_id + xsec_token 打开笔记页取笔记数据 → 下载到临时目录
→ 视频笔记整段抽帧 + 分段转写 / 图文笔记全部图片转 JPEG → 多模态理解 → 入库。
临时目录处理完立即删除，媒体不保存。

笔记不可看（删除、仅自己可见等）时只用私信卡片的标题、作者、封面，并在 notes 里说明。
语音转写失败或没配置时不中断，只在 notes 里记录。
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Error as BrowserError
from sqlmodel import col, select

from agent_accounts.adapters.xiaohongshu import PLATFORM
from agent_accounts.adapters.xiaohongshu import media as xmedia
from agent_accounts.adapters.xiaohongshu import store as xstore
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core import digests, media, store, watch
from agent_accounts.core.config import Config, ConfigError
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.run import RunContext
from agent_accounts.core.transcribe import transcribe
from agent_accounts.core.understand import UnderstandError, UnderstandInput, understand

MAX_VIDEO_TRIES = 3
VIDEO_DOWNLOAD_TIMEOUT_S = 600


def share_message(note_id: str) -> xstore.XhsMessage | None:
    """最近一条分享了这篇笔记、带 xsec_token 的私信。"""
    with store.session() as s:
        return s.exec(
            select(xstore.XhsMessage)
            .where(
                xstore.XhsMessage.note_id == note_id,
                col(xstore.XhsMessage.note_xsec_token).is_not(None),
            )
            .order_by(col(xstore.XhsMessage.store_id).desc())
        ).first()


def pending_items(limit: int | None = None) -> list[str]:
    """对方分享过、还没有摘要的笔记，按分享时间从新到旧。"""
    with store.session() as s:
        rows = s.exec(
            select(xstore.XhsMessage)
            .where(
                col(xstore.XhsMessage.note_id).is_not(None),
                col(xstore.XhsMessage.from_me).is_(False),
            )
            .order_by(col(xstore.XhsMessage.sent_at).desc())
        ).all()
        done = {
            d.item_id
            for d in s.exec(
                select(digests.MediaDigest).where(digests.MediaDigest.platform == PLATFORM)
            ).all()
        }
    ids: list[str] = []
    for m in rows:
        if m.note_id and m.note_id not in done and m.note_id not in ids:
            ids.append(m.note_id)
    return ids[:limit] if limit else ids


@dataclass
class DigestOutcome:
    note_id: str
    digest: digests.MediaDigest | None = None
    cached: bool = False
    error: str | None = None


async def _build_input(
    s: BrowserSession,
    cfg: Config,
    note: xmedia.XhsNote,
    tmp: Path,
    *,
    on_model: Callable[[], None] | None = None,
) -> tuple[UnderstandInput, list[str]]:
    notes: list[str] = []
    inp = UnderstandInput(
        kind=note.kind,
        platform="xiaohongshu",
        title=note.title,
        body=note.body,
        author=note.author,
        hashtags=note.tags,
        duration_s=note.duration_s,
    )
    if note.kind == "video":

        async def fetch(url: str, dest: Path) -> Path:
            return await xmedia.download(s, url, dest, timeout_s=VIDEO_DOWNLOAD_TIMEOUT_S)

        urls = note.video_urls[:MAX_VIDEO_TRIES]
        video = await watch.download_full_video(fetch, urls, note.duration_s, tmp, notes)
        if video is None:
            notes.append("没有拿到视频播放地址，只根据文字信息理解")
        else:

            async def counted(endpoint, audio):
                if on_model:
                    endpoint.require_key("transcribe")
                    on_model()
                return await transcribe(endpoint, audio)

            frames, inp.transcript = await watch.frames_and_transcript(
                cfg, video, tmp, notes, transcribe=counted
            )
            inp.images, inp.frame_times = frames.paths, frames.times
            inp.duration_s = inp.duration_s or frames.duration_s
            video.unlink()
            inp.notes.extend(n for n in notes if "没有看到" in n or "没有听到" in n)
    else:
        urls = note.image_urls[: cfg.media.max_images]
        if len(note.image_urls) > len(urls):
            notes.append(f"笔记共 {len(note.image_urls)} 张图，只看了前 {len(urls)} 张")
            inp.notes.append(notes[-1])
        for i, url in enumerate(urls):
            raw = await xmedia.download(s, url, tmp / f"image_{i:02d}.raw")
            inp.images.append(
                await media.to_jpeg(raw, tmp / f"image_{i:02d}.jpg", max_side=cfg.media.frame_width)
            )
            raw.unlink()
    return inp, notes


async def _unavailable_input(
    s: BrowserSession,
    cfg: Config,
    note: xmedia.XhsNote,
    tmp: Path,
    *,
    message: xstore.XhsMessage | None = None,
) -> tuple[UnderstandInput, list[str]]:
    msg = message if message is not None else share_message(note.note_id)
    reason = (
        f"笔记当前不可看（{note.unavailable_reason}），"
        "只能根据私信分享卡片的标题、作者和封面理解，可能不完整"
    )
    inp = UnderstandInput(
        kind="video" if msg and msg.note_type == "video" else "note",
        platform="xiaohongshu",
        title=(msg.note_title if msg else "") or "",
        author=(msg.note_author if msg else "") or "",
        notes=[reason],
    )
    if msg and msg.cover_url:
        try:
            raw = await xmedia.download(s, msg.cover_url, tmp / "cover.raw")
            inp.images = [
                await media.to_jpeg(raw, tmp / "cover.jpg", max_side=cfg.media.frame_width)
            ]
        except HumanRequired:
            raise
        except Exception as e:  # 封面拿不到也继续，只用文字
            return inp, [reason, f"封面下载失败：{type(e).__name__}"]
    return inp, [reason]


async def digest_items(
    cfg: Config, run: RunContext, note_ids: list[str], *, force: bool = False
) -> list[DigestOutcome]:
    outcomes: list[DigestOutcome] = []
    todo: list[str] = []
    for note_id in note_ids:
        if not force and (cached := digests.get(PLATFORM, note_id)):
            outcomes.append(DigestOutcome(note_id, cached, cached=True))
        elif share_message(note_id) is None:
            outcomes.append(
                DigestOutcome(note_id, error="私信里没有这篇笔记的分享卡片（缺 xsec_token）")
            )
        else:
            todo.append(note_id)
    if not todo:
        return outcomes

    cfg.llm.understand.require_key("llm.understand")  # 先检查配置，避免白开浏览器
    async with BrowserSession(PLATFORM, cfg.browser, headless=False) as s:
        outcomes += await digest_in_session(s, cfg, run, todo)
    return outcomes


async def digest_in_session(
    s: BrowserSession, cfg: Config, run: RunContext, note_ids: list[str]
) -> list[DigestOutcome]:
    """在浏览器会话里逐个分析（不查缓存，调用方负责过滤）。会离开当前页面。"""
    outcomes = []
    for i, note_id in enumerate(note_ids):
        if i:
            await s.pause(2.0, 4.0)
        outcomes.append(await _digest_one(s, cfg, run, note_id))
    return outcomes


async def _digest_one(
    s: BrowserSession,
    cfg: Config,
    run: RunContext,
    note_id: str,
    *,
    message: xstore.XhsMessage | None = None,
    on_model: Callable[[], None] | None = None,
) -> DigestOutcome:
    msg = message if message is not None else share_message(note_id)
    if msg is None or not msg.note_xsec_token:
        return DigestOutcome(note_id, error="私信里没有这篇笔记的分享卡片（缺 xsec_token）")
    hint: xmedia.Kind = "video" if msg.note_type == "video" else "note"
    try:
        note = await xmedia.resolve(s, note_id, msg.note_xsec_token, hint)
        if note is None:
            return DigestOutcome(note_id, error="没有拿到笔记数据")
        with tempfile.TemporaryDirectory(prefix="aa-media-") as tmp_dir:
            tmp = Path(tmp_dir)
            if note.available:
                inp, notes = await _build_input(s, cfg, note, tmp, on_model=on_model)
            else:
                inp, notes = await _unavailable_input(s, cfg, note, tmp, message=msg)
            cfg.llm.understand.require_key("llm.understand")
            if on_model:
                on_model()
            out = await understand(cfg.llm.understand, inp)
    except HumanRequired:
        raise
    except (
        UnderstandError,
        media.MediaError,
        RuntimeError,
        ConfigError,
        BrowserError,
        OSError,
    ) as e:
        run.audit("xiaohongshu.digest.error", note_id=note_id, error=type(e).__name__)
        return DigestOutcome(note_id, error=f"媒体分析失败（{type(e).__name__}）")

    digest = digests.save(
        digests.MediaDigest(
            platform=PLATFORM,
            item_id=note_id,
            kind=inp.kind,
            available=note.available,
            filter_reason=note.unavailable_reason,
            title=inp.title,
            body=inp.body,
            author=inp.author,
            hashtags_json=json.dumps(inp.hashtags, ensure_ascii=False),
            duration_s=inp.duration_s,
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
        "xiaohongshu.digest",
        note_id=note_id,
        kind=digest.kind,
        available=digest.available,
        frames=digest.frames_used,
        transcript_chars=len(digest.transcript or ""),
        model=digest.model,
    )
    return DigestOutcome(note_id, digest)
