"""纯截屏采集；保留时钟误差和缺失信息，不自动进入完整媒体摘要流水线。"""

from __future__ import annotations

import hashlib
import json
import re
import time

from agent_accounts.adapters.douyin.android.session import AndroidError, label, one


def save_manifest(run, data):
    (run.dir / "media.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    run.audit(
        "android.media.capture",
        kind=data["kind"],
        count=len(data["frames"]),
        complete=data["complete"],
        source=data["source"],
    )
    return {
        "run_id": run.id,
        "path": str(run.dir),
        "count": len(data["frames"]),
        "complete": data["complete"],
        "limitations": data["limitations"],
    }


def gallery_buttons(root):
    found = {}
    for n in root.iter():
        m = re.fullmatch(r"图片(\d+)，按钮", n.get("content-desc", ""))
        if m:
            index = int(m[1])
            if index in found:
                raise AndroidError("图集分页按钮不唯一")
            found[index] = n
    return found


def capture_gallery(s, run, pages: int) -> dict:
    if not 1 <= pages <= 200:
        raise AndroidError("pages 必须为 1–200；超过时需分段人工验收")
    root = s.source()
    one(root, "c_e")  # 人工已进入清屏模式；不盲点坐标打开作品。
    if set(gallery_buttons(root)) != set(range(1, pages + 1)):
        raise AndroidError("当前分页按钮与指定总页数不一致，未开始采集")
    play = one(root, "sg0")
    if label(play) != "播放视频":
        raise AndroidError("请先暂停图集自动播放")
    data = {
        "schema_version": 1,
        "source": "android_screenshot",
        "kind": "gallery",
        "complete": False,
        "audio": False,
        "expected_pages": pages,
        "frames": [],
        "limitations": [
            "逐页点击后截图，当前页状态尚需人工视觉核对",
            "不包含音频、动图动态或长图放大内容",
        ],
    }
    try:
        for page in range(1, pages + 1):
            root = s.source()
            one(root, "c_e")
            buttons = gallery_buttons(root)
            if set(buttons) != set(range(1, pages + 1)):
                raise AndroidError("采集中图集分页发生变化")
            s.tap(buttons[page])
            time.sleep(0.7)
            root = s.source()
            one(root, "c_e")
            if label(one(root, "sg0")) != "播放视频":
                raise AndroidError("图集开始自动播放，停止采集")
            filename = f"page-{page:03}.png"
            image = s.screenshot(run.dir / filename)
            data["frames"].append(
                {
                    "file": filename,
                    "requested_page": page,
                    "page_verified": False,
                    "sha256": hashlib.sha256(image).hexdigest(),
                }
            )
    finally:
        save_manifest(run, data)
    return save_manifest(run, data)


def progress(root) -> float:
    try:
        value = float(one(root, "6jy").get("text", ""))
    except ValueError:
        raise AndroidError("视频进度无法读取") from None
    if not 0 <= value <= 10000:
        raise AndroidError("视频进度越界")
    return value


def coverage(frames: list, wraps: list[int]) -> dict:
    complete_loop = frames[wraps[0] : wraps[1]] if len(wraps) >= 2 else []
    gaps = [
        b["end_s"] - a["start_s"] for a, b in zip(complete_loop, complete_loop[1:], strict=False)
    ]
    return {
        "loop_observed": len(wraps) >= 2,
        "loop_frame_count": len(complete_loop),
        "max_exposure_gap_bound_s": max(gaps, default=None),
        "loop_period_s": (frames[wraps[1]]["start_s"] - frames[wraps[0]]["start_s"])
        if len(wraps) >= 2
        else None,
        "exact_frame_times": False,
        "exact_endpoints": False,
    }


def capture_video(s, run, duration_s: float, interval_s: float = 1.0) -> dict:
    if not 0 < duration_s <= 1800 or not 0.2 <= interval_s <= 10:
        raise AndroidError("时长须在 0–1800 秒内，采样间隔须为 0.2–10 秒")
    root = s.source()
    one(root, "c_e")
    if gallery_buttons(root):
        raise AndroidError("当前为图集，不是视频")
    if label(one(root, "0mk")) != "1.0倍速":
        raise AndroidError("请人工将视频设为 1.0 倍速")
    progress(root)
    if label(one(root, "u6y")) != "播放视频":
        raise AndroidError("请先暂停视频，再开始采集")
    data = {
        "schema_version": 1,
        "source": "android_screenshot",
        "kind": "video",
        "complete": False,
        "audio": False,
        "declared_duration_s": duration_s,
        "frames": [],
        "wraps": [],
        "limitations": [
            "无音频；时长由人从 UI 核对输入",
            "进度读取与截图不同步，无精确首尾或帧时刻",
            "采样可能遗漏短暂画面；未完成循环时明确保留部分结果",
        ],
    }
    start = time.monotonic()
    previous = None
    # 从任意位置等到两次回绕，获得中间完整一轮。超时而非按固定帧数截断。
    deadline = start + duration_s * 2 + max(15, interval_s * 3)
    safe_to_pause = True
    try:
        s.click("u6y")
        while time.monotonic() < deadline:
            tick = time.monotonic()
            root = s.source()
            one(root, "c_e")
            if label(one(root, "0mk")) != "1.0倍速" or gallery_buttons(root):
                raise AndroidError("播放界面或倍速改变，停止采集")
            p = progress(root)
            if previous is not None and p < previous:
                if previous > 9000 and p < 1000:
                    data["wraps"].append(len(data["frames"]))
                else:
                    raise AndroidError("播放进度异常回退，停止采集")
            filename = f"frame-{len(data['frames']):05}.png"
            begin = time.monotonic() - start
            image = s.screenshot(run.dir / filename)
            end = time.monotonic() - start
            data["frames"].append(
                {
                    "file": filename,
                    "ui_progress": p,
                    "start_s": begin,
                    "end_s": end,
                    "sha256": hashlib.sha256(image).hexdigest(),
                }
            )
            previous = p
            if len(data["wraps"]) == 2:
                break
            time.sleep(max(0, interval_s - (time.monotonic() - tick)))
    except BaseException:
        # 风控/失联/用户中断时不再下发 UI 操作。
        safe_to_pause = False
        raise
    finally:
        data["coverage"] = coverage(data["frames"], data["wraps"])
        save_manifest(run, data)
        if safe_to_pause:
            root = s.source()
            if label(one(root, "u6y")) == "暂停视频":
                s.click("u6y")
    return save_manifest(run, data)
