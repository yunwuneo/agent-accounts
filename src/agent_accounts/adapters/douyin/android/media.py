"""官方导出和人工解析文件导入。只读取明确选定的文件，不扫描个人相册。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
from dataclasses import asdict
from pathlib import Path

from agent_accounts.adapters.douyin.android.session import AndroidError, label
from agent_accounts.core import media


def image_extension(path: Path) -> str:
    with path.open("rb") as f:
        head = f.read(16)
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    raise AndroidError("图片格式不受支持或文件损坏")


def file_hash(path: Path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


async def validate_files(
    run, inputs: list[Path], kind: str, source: str, expected_count: int, *, order_confirmed=False
) -> dict:
    if source not in {"official_export", "third_party_manual", "third_party_kukutool"}:
        raise AndroidError("不支持的媒体来源")
    if kind not in {"video", "gallery"} or not inputs or len(inputs) != expected_count:
        raise AndroidError("文件数量或媒体类型不符")
    if kind == "video" and expected_count != 1:
        raise AndroidError("一次只导入一个视频作品")
    out = run.dir / "media"
    out.mkdir(exist_ok=True)
    data = {
        "schema_version": 1,
        "source": source,
        "kind": kind,
        "complete": False,
        "content_match_verified": False,
        "order_confirmed": order_confirmed,
        "files": [],
        "limitations": ["文件可解码不等于与目标作品完整对应，需人工核对"],
    }
    try:
        for i, path in enumerate(inputs):
            if not path.is_file():
                raise AndroidError("指定媒体文件不存在")
            if kind == "video":
                with path.open("rb") as handle:
                    header = handle.read(12)
                if header[4:8] != b"ftyp":
                    raise AndroidError("目前只接收本地 MP4 文件，不解析播放列表或远端引用")
            extension = image_extension(path) if kind == "gallery" else ".mp4"
            target = out / f"item-{i + 1:03}{extension}"
            shutil.copyfile(path, target)
            info = await media.probe(target)
            if not info.has_video or not info.width or not info.height:
                raise AndroidError("媒体文件缺少有效画面")
            if kind == "video" and (not info.duration_s or info.duration_s <= 0):
                raise AndroidError("视频时长无效")
            await media._run(
                "ffmpeg", "-v", "error", "-xerror", "-i", str(target), "-f", "null", "-"
            )
            data["files"].append(
                {
                    "file": str(target.relative_to(run.dir)),
                    "sha256": file_hash(target),
                    "bytes": target.stat().st_size,
                    **asdict(info),
                }
            )
    except media.MediaError:
        raise AndroidError("媒体校验失败，未进入自动理解流水线") from None
    finally:
        (run.dir / "media.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    run.audit(
        "android.media.import", source=source, kind=kind, count=len(data["files"]), complete=False
    )
    return {
        "run_id": run.id,
        "path": str(run.dir),
        "validated_files": len(data["files"]),
        "complete": False,
        "automatic_digest": False,
    }


def exported_paths(text: str) -> list[str]:
    # MediaStore 展示名是 .png，实际可能是 WebP；只接受已验收目录的导出名。
    return re.findall(
        r"_data=(/storage/emulated/0/Pictures/douyin/share_[a-fA-F0-9]+\.png)(?=,|\s|$)", text
    )


def export_gallery(s, run, pages: int) -> dict:
    if not 1 <= pages <= 200:
        raise AndroidError("pages 必须为 1–200")
    # 人工先进入“选择图片保存”。不自动从不明内容/聊天定位作品。
    root = s.source()
    all_buttons = [n for n in root.iter() if label(n) == "全选"]
    if len(all_buttons) != 1:
        raise AndroidError("请停留在目标图集的“选择图片保存”页面")
    s.tap(all_buttons[0])
    root = s.source()
    save = [n for n in root.iter() if label(n) == f"保存({pages})张图片"]
    if len(save) != 1:
        raise AndroidError("选择张数与 pages 不一致，未保存")
    epoch = s.adb("shell", "date", "+%s").strip()
    if not epoch.isdigit():
        raise AndroidError("无法读取设备时间")
    query = (
        "content query --uri content://media/external/images/media "
        "--projection _data:date_added "
        f'--where "date_added>={epoch} AND _data LIKE '
        "'/storage/emulated/0/Pictures/douyin/share_%'\""
    )
    before = set(exported_paths(s.adb("shell", query)))
    s.tap(save[0])
    run.audit("android.media.official_save", expected_count=pages)
    deadline = time.monotonic() + 30
    found = set()
    while time.monotonic() < deadline:
        s.source()  # 验证/风控立即停止
        found = set(exported_paths(s.adb("shell", query))) - before
        if len(found) >= pages:
            break
        time.sleep(1)
    if len(found) != pages:
        raise AndroidError("新增导出文件数量不符；未扫描或拉取其他相册文件")
    raw = run.dir / "exports"
    raw.mkdir()
    files = []
    for i, device_path in enumerate(sorted(found)):
        dest = raw / f"export-{i:03}.bin"
        s.adb("pull", device_path, str(dest))
        files.append(dest)
    # MediaStore 顺序不是作品顺序，因此不承诺顺序或内容对应。
    return asyncio.run(validate_files(run, files, "gallery", "official_export", pages))
