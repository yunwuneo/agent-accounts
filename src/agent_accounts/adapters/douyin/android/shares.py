"""把当前私聊的一张明确卡片绑定到官方详情页及本次主动复制的链接。"""

from __future__ import annotations

import base64
import hashlib
import re
import time
from dataclasses import asdict, dataclass

from agent_accounts.adapters.douyin.android import messaging
from agent_accounts.adapters.douyin.android.capture import gallery_buttons
from agent_accounts.adapters.douyin.android.session import AndroidError, bounds, label, nodes, one
from agent_accounts.adapters.douyin.parsers.download import share_url


@dataclass(frozen=True)
class Evidence:
    link: str
    title: str
    kind: str
    count: int
    duration_s: float | None
    card_hash: str

    def as_dict(self):
        return asdict(self)


def fingerprint(text):
    return hashlib.sha256(text.encode()).hexdigest()


def wait_node(s, rid, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root = s.source()
        if len(nodes(root, rid)) == 1:
            return root
        time.sleep(0.25)
    raise AndroidError("页面过渡后预期控件仍未就绪")


def gesture(s, x1, y1, x2, y2, *, hold=False):
    s.source()  # 风控优先于任何触摸。
    actions = [
        {"type": "pointerMove", "duration": 0, "x": x1, "y": y1},
        {"type": "pointerDown", "button": 0},
        {"type": "pause", "duration": 100},
        {"type": "pointerMove", "duration": 600, "x": x2, "y": y2},
        {"type": "pause", "duration": 400},
    ]
    if not hold:
        actions.append({"type": "pointerUp", "button": 0})
    s.request(
        "/actions",
        {
            "actions": [
                {
                    "type": "pointer",
                    "id": "finger",
                    "parameters": {"pointerType": "touch"},
                    "actions": actions,
                }
            ]
        },
    )


def scroll(s, expected, *, older):
    root = s.source()
    if messaging.thread(root, expected)["draft"]:
        raise AndroidError("存在草稿，未滚动聊天")
    x1, y1, x2, y2 = bounds(one(root, "v65"))
    x = x1 + int((x2 - x1) * 0.8)
    top, bottom = y1 + (y2 - y1) // 4, y1 + (y2 - y1) * 3 // 4
    gesture(s, x, top if older else bottom, x, bottom if older else top)
    time.sleep(0.7)
    messaging.thread(s.source(), expected)


def bottom(s, expected, max_scrolls=12):
    for _ in range(max_scrolls):
        before = messaging.thread(s.source(), expected)["visible_messages"]
        scroll(s, expected, older=False)
        after = messaging.thread(s.source(), expected)["visible_messages"]
        if before == after:
            return
    raise AndroidError("在滚动上限内无法确认聊天底部")


def list_shares(s, expected, self_name):
    root = s.source()
    cards = peer_cards(root, expected, self_name)
    return [{"card_hash": fingerprint(label(card)), "card": label(card)} for card in cards]


def peer_cards(root, expected, self_name):
    """历史选择只接受完整且自带对方头像的卡片，不推断屏幕外历史。"""
    messaging.thread(root, expected)
    if not self_name or self_name == expected:
        raise AndroidError("己方与对方身份必须不同")
    _, top, _, bottom = bounds(one(root, "v65"))
    result = []
    for card in nodes(root, "sww"):
        x1, y1, x2, y2 = bounds(card)
        if not top < y1 < y2 < bottom:
            continue
        candidates = []
        for avatar in nodes(root, "dwh"):
            ax1, ay1, ax2, ay2 = bounds(avatar)
            if max(y1, ay1) < min(y2, ay2) and (ax2 <= x1 or ax1 >= x2):
                candidates.append((avatar, ax2 <= x1))
        if len(candidates) != 1:
            continue
        avatar, peer_side = candidates[0]
        if peer_side and label(avatar) == expected + "的头像" and label(card).strip():
            result.append(card)
    return result


def clock_seconds(value):
    if not re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", value):
        raise AndroidError("原生时长格式不受支持")
    parts = list(map(int, value.split(":")))
    if any(p >= 60 for p in parts[1:]):
        raise AndroidError("原生时长无效")
    total = 0
    for part in parts:
        total = total * 60 + part
    return total


def duration(s):
    root = s.source()
    x1, y1, x2, y2 = bounds(one(root, "6jy"))
    y = (y1 + y2) // 2
    gesture(s, x1 + (x2 - x1) // 3, y, x1 + (x2 - x1) * 3 // 4, y, hold=True)
    # source 遇到风控直接传播；不在 finally 中继续触摸受阻界面。
    root = s.source()
    values = [
        clock_seconds(label(n))
        for rid in ("f7_", "2yk")
        for n in nodes(root, rid)
        if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", label(n))
    ]
    # UiAutomator2 不接受跨请求的孤立 pointerUp；完整点按视频区域退出进度预览。
    s.tap(one(root, "qbr"))
    wait_node(s, "z0m")
    if not values or max(values) <= 0:
        raise AndroidError("没有取得原生视频总时长，未进入付费理解")
    return float(max(values))


def copied_link(value):
    urls = re.findall(r"https://[^\s]+", value)
    if len(urls) != 1:
        raise AndroidError("官方复制结果不是唯一作品链接")
    return share_url(urls[0])


def copy_link(s):
    s.click("z0m")
    time.sleep(1.0)  # 原生面板动画结束后才使用坐标。
    for _ in range(3):
        root = s.source()
        matches = [n for n in root.iter() if label(n) == "分享链接"]
        if len(matches) != 1:
            raise AndroidError("分享链接入口不唯一")
        target = matches[0]
        x1, y1, x2, y2 = bounds(target)
        window = s.request("/window/rect", method="GET")
        width, height = window["width"], window["height"]
        if x2 - x1 >= 100 and 0 < y1 < y2 < height and x2 < width:
            s.tap(target)
            break
        if not 0 < y1 < height:
            raise AndroidError("分享面板尚未稳定")
        gesture(s, int(width * 0.85), (y1 + y2) // 2 - 70, int(width * 0.25), (y1 + y2) // 2 - 70)
        time.sleep(0.5)
    else:
        raise AndroidError("未找到完整可见的分享链接入口")
    # 仅读取刚才自己触发复制的剪贴板；等系统复制浮层自然消失，不关闭未知弹窗。
    time.sleep(8)
    root = s.source()
    success = nodes(root, "z0a")
    if len(success) != 1 or "链接已复制成功" not in label(success[0]):
        raise AndroidError("没有官方复制成功证据，未读取剪贴板")
    value = base64.b64decode(s.request("/appium/device/get_clipboard", {}), validate=True)
    result = copied_link(value.decode("utf-8"))
    s.request("/back", {})
    time.sleep(0.7)
    wait_node(s, "back_btn")
    return result


def inspect_share(s, expected, self_name, card_hash):
    root = s.source()
    if messaging.thread(root, expected)["draft"]:
        raise AndroidError("存在草稿，未打开作品")
    matching = [
        n for n in peer_cards(root, expected, self_name) if fingerprint(label(n)) == card_hash
    ]
    cards = [n for n in nodes(root, "sww") if fingerprint(label(n)) == card_hash]
    if len(matching) != 1 or len(cards) != 1:
        raise AndroidError("目标分享卡片缺失、重复或不属于对方")
    s.tap(cards[0])
    time.sleep(1)
    root = s.source()
    one(root, "back_btn")
    title = label(one(root, "desc")).strip()
    if not title:
        raise AndroidError("作品详情缺少标题")
    buttons = gallery_buttons(root)
    if buttons:
        if set(buttons) != set(range(1, len(buttons) + 1)):
            raise AndroidError("图集页码不连续")
        kind, count, seconds = "gallery", len(buttons), None
    else:
        if any(label(n) == "图文" for n in root.iter()):
            raise AndroidError("图集总页数无法确认")
        kind, count, seconds = "video", 1, duration(s)
    link = copy_link(s)
    s.click("back_btn")
    time.sleep(0.7)
    messaging.thread(s.source(), expected)
    return Evidence(link, title, kind, count, seconds, card_hash)
