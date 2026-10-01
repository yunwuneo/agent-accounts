"""安卓入口和确定性页面定位；只输出结构证据，不输出其他 App 内容。"""

from __future__ import annotations

import re
import time

from agent_accounts.adapters.douyin.android.session import (
    PACKAGE,
    PREFIX,
    AndroidError,
    bounds,
    label,
    nodes,
    one,
)
from agent_accounts.core.errors import HumanRequired

LAUNCHERS = {"com.miui.home", "com.android.launcher", "com.android.launcher3"}
SYSTEM = {"com.android.systemui"}


def classify(root) -> dict:
    visible = [n for n in root.iter() if n.get("displayed", "true") != "false"]
    packages = {n.get("package") for n in visible if n.get("package")}
    foreign = packages - {PACKAGE} - SYSTEM

    def result(page, *evidence):
        return {"page": page, "recognized": page != "unknown", "evidence": list(evidence)}

    if PACKAGE not in packages:
        if foreign and foreign <= LAUNCHERS:
            return result("desktop", "launcher_package")
        if len(foreign) == 1 and not any(
            "permissioncontroller" in p or "packageinstaller" in p for p in foreign
        ):
            return result("external_app", "external_package")
        return result("system_overlay", "no_unobstructed_app")
    if foreign:
        return result("system_overlay", "foreign_overlay")
    own = [n for n in visible if n.get("package") == PACKAGE]
    ids = {n.get("resource-id", "").removeprefix(PREFIX) for n in own}
    texts = {label(n) for n in own}
    # Modal screens take precedence over their still-visible background.
    if {"选择图片保存", "保存"} <= texts:
        return result("gallery_save", "save_selection_controls")
    if "分享链接" in texts and "z0m" in ids:
        return result("share_panel", "share_link_and_media_controls")
    if {"vw_", "msg_et", "v65"} <= ids:
        return result("chat", "chat_title_editor_list")
    pages = {
        int(m[1]) for n in own if (m := re.fullmatch(r"图片(\d+)，按钮", n.get("content-desc", "")))
    }
    if "c_e" in ids:
        if pages and "sg0" in ids and pages == set(range(1, max(pages) + 1)):
            return result("gallery_clear", "clear_gallery_pagination")
        if {"6jy", "u6y", "0mk"} <= ids and not pages:
            return result("video_clear", "clear_video_playback_controls")
    if {"back_btn", "desc", "z0m"} <= ids:
        if pages and pages == set(range(1, max(pages) + 1)):
            return result("gallery_detail", "detail_gallery_pagination")
        if "6jy" in ids and not pages and "图文" not in texts:
            return result("video_detail", "detail_video_progress")
    # A lone word in a caption/chat is never a page marker. Require a complete
    # bottom tab set plus one selected tab, each with the same resource ID.
    groups = {}
    for n in own:
        rid = n.get("resource-id", "")
        if rid == PREFIX + "0qf" and label(n) in {"首页", "朋友", "消息", "我"}:
            groups.setdefault(rid, []).append(n)
    tabs = [
        group
        for group in groups.values()
        if {label(n) for n in group} == {"首页", "朋友", "消息", "我"}
    ]
    if len(tabs) == 1:
        profile_tabs = {label(n) for n in own if n.get("resource-id") == "android:id/text1"}
        editors = [n for n in own if n.get("resource-id") == PREFIX + "wgq"]
        if (
            len(editors) == 1
            and label(editors[0]) == "编辑主页"
            and {"作品", "日常", "收藏", "喜欢"} <= profile_tabs
        ):
            return result("self_profile", "complete_tabs_profile_sections_and_edit_control")
        # 40.4.0 reports selected=false on every bottom-tab ancestor. The
        # message page has a separate, observed toolbar title.
        titles = [
            n for n in own if n.get("resource-id") == PREFIX + "tv_title" and label(n) == "消息"
        ]
        if len(titles) == 1:
            return result("inbox", "complete_tabs_and_message_toolbar")
        channels = {label(n) for n in own if n.get("resource-id") == PREFIX + "5fc"}
        if {"推荐", "关注", "同城"} <= channels and "2ss" in ids:
            return result("home", "complete_tabs_and_home_channels_search")
        friend_entries = [
            n for n in own if n.get("resource-id") == PREFIX + "iyc" and label(n) == "添加朋友"
        ]
        if len(friend_entries) == 1:
            return result("friends", "complete_tabs_and_friends_entry")
        selected = [label(n) for n in tabs[0] if n.get("selected") == "true"]
        if len(selected) == 1:
            page = {"首页": "home", "朋友": "friends", "消息": "inbox", "我": "self_profile"}[
                selected[0]
            ]
            return result(page, "complete_tabs_with_unique_selection")
    return result("unknown", "insufficient_structural_evidence")


def inspect(s, run) -> dict:
    state = classify(s.observe())
    run.audit("android.inspect", **state)
    return {"run_id": run.id, **state}


def open_app(s, run, *, execute=False, timeout_s=10) -> dict:
    if not 0 < timeout_s <= 30:
        raise AndroidError("启动等待时限须为 0–30 秒")
    state = classify(s.observe())
    before = state["page"]
    if before == "system_overlay":
        raise HumanRequired("存在系统弹窗或遮挡，请人工处理后再打开抖音")
    if before not in {"desktop", "external_app"}:
        run.audit("android.open", status="already_open", page=before)
        return {"run_id": run.id, "status": "already_open", **state}
    if not execute:
        run.audit("android.open", status="dry_run", page=before)
        return {"run_id": run.id, "status": "dry_run", **state}
    # Recheck immediately before the single activation; never dismiss overlays,
    # force-stop, reinstall, unlock, or repeatedly launch on failure.
    current = classify(s.observe())
    if current != state:
        raise AndroidError("启动前页面发生变化，未切换应用")
    run.audit("android.open", status="activation_requested", page=before)
    s.request("/appium/device/activate_app", {"appId": PACKAGE})
    deadline = time.monotonic() + timeout_s
    while True:
        state = classify(s.observe())
        if state["page"] == "system_overlay":
            raise HumanRequired("启动后存在系统弹窗或遮挡，请人工处理")
        if state["page"] not in {"desktop", "external_app"}:
            # unknown is a successful foreground switch, not page recognition.
            s.source()
            run.audit("android.open", status="opened", page=state["page"])
            return {"run_id": run.id, "status": "opened", **state}
        if time.monotonic() >= deadline:
            raise AndroidError("未确认抖音进入前台，未再次启动")
        time.sleep(0.25)


def settled(s, timeout_s=8):
    """Wait for two matching relevant layouts; never act on transition geometry."""
    deadline = time.monotonic() + timeout_s
    previous = None
    relevant = {"0qf", "vw_", "msg_et", "v65", "dwh", "sws", "sww", "zn-", "by3"}
    while time.monotonic() < deadline:
        root = s.source()  # login/risk errors propagate immediately
        signature = tuple(
            (n.get("resource-id"), label(n), n.get("bounds"))
            for n in root.iter()
            if n.get("resource-id", "").removeprefix(PREFIX) in relevant
        )
        if signature and signature == previous:
            return root
        previous = signature
        time.sleep(0.4)
    raise AndroidError("导航后页面未稳定，未继续操作")


def target_row(root, expected):
    """Only existing conversation rows, never suggested contact avatars."""
    container = one(root, "zn6")
    candidates = [
        row
        for row in nodes(container, "zn-")
        if any(label(n) == expected for n in nodes(row, "by3"))
    ]
    if len(candidates) != 1:
        raise AndroidError("当前会话列表中目标昵称缺失或重复，未打开任何会话")
    row = candidates[0]
    x1, y1, x2, y2 = bounds(row)
    cx1, cy1, cx2, cy2 = bounds(container)
    if not (cx1 <= x1 < x2 <= cx2 and cy1 <= y1 < y2 <= cy2):
        raise AndroidError("目标会话行不完整可见，未点击")
    if any("群聊" in label(n) for n in row.iter()):
        raise AndroidError("目标行出现群聊标记，不支持群聊")
    return row


def locate_target(s, expected):
    """Bounded search of existing conversation list; never use global/user search."""
    from agent_accounts.adapters.douyin.android.shares import gesture

    root = settled(s)
    for toward_top, limit in ((True, 6), (False, 10)):
        for _ in range(limit):
            if classify(root)["page"] != "inbox":
                raise HumanRequired("查找期间离开消息列表")
            listing = one(root, "zn6")
            if any(label(n) == expected for n in nodes(listing, "by3")):
                target_row(root, expected)  # duplicate/group/partial row fails closed
                return root
            before = tuple((label(n), n.get("bounds")) for n in nodes(listing, "zn-"))
            x1, y1, x2, y2 = bounds(listing)
            x = (x1 + x2) // 2
            top, bottom = y1 + (y2 - y1) // 4, y1 + (y2 - y1) * 3 // 4
            gesture(s, x, top if toward_top else bottom, x, bottom if toward_top else top)
            root = settled(s)
            after = tuple((label(n), n.get("bounds")) for n in nodes(root, "zn-"))
            if before == after:
                break
    # Check the last scanned screen too.
    if classify(root)["page"] != "inbox":
        raise HumanRequired("查找期间离开消息列表")
    target_row(root, expected)
    return root


def ensure_thread(s, run, expected, self_name, *, confirmed=False, previous=None):
    """Navigate to the human-confirmed unique mutual private chat, preserving baseline."""
    from agent_accounts.adapters.douyin.android import autoreply, messaging, shares

    if not confirmed or not expected.strip() or not self_name.strip() or expected == self_name:
        raise AndroidError("需确认配置中的唯一互关私聊与双方不同的精确昵称")
    activated = open_app(s, run, execute=True)["status"] == "opened"
    for _step in range(7):
        root = settled(s)
        state = classify(root)["page"]
        if state == "chat":
            editor = one(root, "msg_et")
            if editor.get("showing-hint") != "true" and editor.get("text", ""):
                raise HumanRequired("聊天中存在草稿，未离开会话或恢复监控")
            if label(one(root, "vw_")) == expected:
                break
            s.source()
            s.request("/back", {})
        elif state in {"home", "friends", "self_profile"}:
            tabs = [n for n in nodes(root, "0qf") if label(n) == "消息"]
            if len(tabs) != 1:
                raise AndroidError("消息底栏入口不唯一")
            s.tap(tabs[0])
        elif state == "inbox":
            # Re-read immediately before the tap; the list may reorder on new messages.
            locate_target(s, expected)
            fresh = s.source()
            if classify(fresh)["page"] != "inbox":
                raise AndroidError("选择目标前页面变化")
            s.tap(target_row(fresh, expected))
        elif state in {
            "video_detail",
            "gallery_detail",
            "video_clear",
            "gallery_clear",
            "share_panel",
        }:
            s.source()
            s.request("/back", {})
        else:
            raise HumanRequired("当前页面未识别或存在遮挡，未盲目返回/关闭弹窗")
    else:
        raise AndroidError("到达导航步数上限，未进入目标私聊")
    # Confirm title, both identities and no draft before any scrolling.
    messaging.thread(root, expected)
    messages = autoreply.read_messages(root, expected, self_name)
    if {m.from_me for m in messages} != {False, True}:
        raise AndroidError("缺少双方可核对消息，不恢复监控")
    # Preserve existing baseline; only initial/returned navigation seeks the bottom.
    if _step or activated or previous is None:
        shares.bottom(s, expected, max_scrolls=6)
        root = settled(s)
        messages = autoreply.read_messages(root, expected, self_name)
    if previous is not None and not autoreply.same_tail(previous, messages):
        new = autoreply.appended(previous, messages)
        if any(m.from_me for m in new):
            raise HumanRequired("导航期间出现己方新消息，未自动恢复回复")
    run.audit(
        "android.navigation.ready", navigated=bool(_step), baseline_preserved=previous is not None
    )
    return root
