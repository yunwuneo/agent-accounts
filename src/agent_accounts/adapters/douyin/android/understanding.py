"""由官方卡片绑定的文件 → 全图/全音轨及均匀采样 → 带覆盖说明的摘要。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from agent_accounts.adapters.douyin.android.media import file_hash
from agent_accounts.adapters.douyin.android.session import AndroidError
from agent_accounts.adapters.douyin.android.shares import fingerprint
from agent_accounts.adapters.douyin.parsers.kuku import parse_media
from agent_accounts.core import media, transcribe, understand


@dataclass
class MediaRun:
    parent: object
    key: str

    @property
    def id(self):
        return self.parent.id

    @property
    def dir(self):
        directory = self.parent.dir / "shares" / self.key
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def audit(self, action, **data):
        self.parent.audit(action, card_hash=self.key, **data)


async def understand_once(endpoint, inp):
    async with understand.make_client(endpoint) as client:
        return await understand.understand(endpoint, inp, client=client.with_options(max_retries=0))


def validate_manifest(manifest, evidence, run):
    parser = manifest.get("parser", {})
    if (
        manifest.get("kind") != evidence.kind
        or not (
            parser.get("title_match")
            or (evidence.kind == "gallery" and parser.get("submission_link_verified") is True)
        )
        or parser.get("count") != evidence.count
        or parser.get("link_hash") != fingerprint(evidence.link)
    ):
        raise AndroidError("媒体与官方作品类型、标题或数量不匹配")
    files = manifest.get("files", [])
    if len(files) != evidence.count:
        raise AndroidError("作品文件未取齐")
    paths = []
    for item in files:
        path = (run.dir / item["file"]).resolve()
        if not path.is_relative_to(run.dir.resolve()) or not path.is_file():
            raise AndroidError("媒体清单路径无效")
        if file_hash(path) != item["sha256"]:
            raise AndroidError("媒体哈希变化")
        paths.append(path)
    if evidence.kind == "video":
        actual = files[0].get("duration_s")
        expected = evidence.duration_s
        if not actual or not expected or abs(actual - expected) > max(2, expected * 0.03):
            raise AndroidError("下载时长与官方时长不符")
    elif parser.get("order_source") != "parser_image_number":
        raise AndroidError("图集没有完整编号顺序")
    return paths


async def analyze(
    cfg,
    run,
    evidence,
    *,
    parse=parse_media,
    transcriber=transcribe.transcribe,
    analyzer=understand_once,
    check_active=lambda: None,
):
    """每作品最多一次理解、视频最多一次整段转写；错误不重试、不用标题降级。"""
    if evidence.kind not in {"video", "gallery"} or not evidence.title.strip():
        raise AndroidError("作品证据不足")
    if evidence.kind == "gallery" and not 1 <= evidence.count <= cfg.media.max_images:
        raise AndroidError("图集超出图片预算，不截断作品")
    if evidence.kind == "video" and (
        not evidence.duration_s or evidence.duration_s > cfg.media.max_video_seconds
    ):
        raise AndroidError("视频时长超过完整处理上限")
    scoped = MediaRun(run, evidence.card_hash)
    evidence_file = scoped.dir / "evidence.json"
    evidence_file.write_text(json.dumps(evidence.as_dict(), ensure_ascii=False, indent=2), "utf-8")
    check_active()
    await parse(
        cfg,
        scoped,
        evidence.link,
        evidence.kind,
        evidence.count,
        execute=True,
        # 当前图集结果页不展示作品标题；以本次官方复制链接及提交回读绑定，
        # 不能将未提供的标题记为已匹配。视频仍强制核对标题。
        expected_title=evidence.title if evidence.kind == "video" else "",
    )
    check_active()
    manifest = json.loads((scoped.dir / "media.json").read_text("utf-8"))
    paths = validate_manifest(manifest, evidence, scoped)
    work = scoped.dir / "understanding"
    work.mkdir(exist_ok=True)
    notes = ["媒体来自官方卡片复制链接的第三方解析结果；类型及数量经核对。"]
    notes.append(
        "解析标题与官方详情匹配。"
        if manifest["parser"].get("title_match")
        else "第三方未提供图集标题；归属依据本次官方复制链接、提交回读及页数，未独立核验标题。"
    )
    inp = understand.UnderstandInput(
        kind="video" if evidence.kind == "video" else "note", title=evidence.title, notes=notes
    )
    transcript_calls = 0
    if evidence.kind == "gallery":
        for i, path in enumerate(paths):
            inp.images.append(
                await media.to_jpeg(path, work / f"page-{i + 1:03}.jpg", max_side=1600)
            )
        notes.append(
            f"全部 {len(paths)} 张按解析编号顺序输入；配乐未读取，仅分析静态图文，不推测音频。"
        )
    else:
        video = paths[0]
        info = await media.probe(video)
        inp.duration_s = info.duration_s
        if not info.duration_s or info.duration_s > cfg.media.max_video_seconds + 2:
            raise AndroidError("实际视频超出时长上限")
        frames = await media.extract_frames(
            video,
            work,
            min_frames=cfg.media.min_frames,
            max_frames=cfg.media.max_frames,
            max_side=cfg.media.frame_width,
            interval_s=cfg.media.frame_interval_s,
        )
        expected_frames = media.frame_times(
            info.duration_s, cfg.media.min_frames, cfg.media.max_frames, cfg.media.frame_interval_s
        )
        if frames.times != expected_frames or len(frames.paths) != len(expected_frames):
            raise AndroidError("视频采样缺帧")
        inp.images, inp.frame_times = frames.paths, frames.times
        notes.append("画面为全时段均匀采样，非逐帧观看，短暂细节可能遗漏；不得声称逐帧完整看过。")
        if info.has_audio:
            audio = await media.extract_audio(
                video, work / "audio.mp3", max_seconds=math.ceil(info.duration_s) + 1
            )
            if audio is None or audio.stat().st_size > 24 * 1024 * 1024:
                raise AndroidError("整段音轨缺失或超过单次转写大小预算")
            audio_info = await media.probe(audio)
            if not audio_info.duration_s or abs(audio_info.duration_s - info.duration_s) > 2:
                raise AndroidError("音轨未覆盖全片")
            check_active()
            scoped.audit("android.media.transcribe.start", model=cfg.transcribe.model)
            transcript_calls = 1
            inp.transcript = await transcriber(cfg.transcribe, audio)
            if not inp.transcript or not inp.transcript.strip():
                raise AndroidError("转写结果为空，暂缓理解与回复")
            (work / "transcript.txt").write_text(inp.transcript, "utf-8")
        else:
            notes.append("验证后的文件没有音轨；仅基于画面分析。")
    check_active()
    scoped.audit(
        "android.media.understand.start",
        model=cfg.llm.understand.model,
        images=len(inp.images),
        kind=evidence.kind,
    )
    result = await analyzer(cfg.llm.understand, inp)
    if not result.summary.strip():
        raise AndroidError("理解结果为空，未进入回复")
    check_active()
    output = {
        "schema_version": 1,
        "evidence": evidence.as_dict(),
        "digest": result.model_dump(),
        "notes": notes,
        "images": len(inp.images),
        "frame_times": inp.frame_times,
        "transcribe_calls": transcript_calls,
        "understand_calls": 1,
        "media_hashes": [f["sha256"] for f in manifest["files"]],
        "model": cfg.llm.understand.model,
        "ready_for_reply": True,
        "complete_per_frame": False,
    }
    (scoped.dir / "digest.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), "utf-8"
    )
    scoped.audit("android.media.understand.ok", kind=evidence.kind, images=len(inp.images))
    return "\n".join(
        [
            "[已分析的分享作品]",
            result.summary,
            result.vibe,
            "接话点：" + "；".join(result.reply_hooks),
            "覆盖说明：" + "；".join(notes),
        ]
    )
