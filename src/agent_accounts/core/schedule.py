"""休息时段（quiet hours）：这些时间里不打开平台、不同步、不回复。

时段写成 ``"HH:MM-HH:MM"``，按本机时间，左闭右开；结束早于开始表示跨午夜
（如 ``"23:00-07:00"``），结束可以写 ``24:00``。多个时段可以相邻或重叠。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_WINDOW = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")
_DAY = 24 * 60


@dataclass(frozen=True)
class QuietWindow:
    start: int  # 当天第几分钟
    end: int  # 1..1440；小于等于 start 表示跨午夜

    def contains(self, minute: int) -> bool:
        if self.start < self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end


def parse_window(text: str) -> QuietWindow:
    m = _WINDOW.match(text)
    if not m:
        raise ValueError(f"休息时段格式应为 HH:MM-HH:MM：{text!r}")
    sh, sm, eh, em = (int(g) for g in m.groups())
    if sh > 23 or sm > 59 or em > 59 or eh > 24 or (eh == 24 and em):
        raise ValueError(f"休息时段时间不合法：{text!r}")
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end or (start == 0 and end == _DAY):
        raise ValueError(f"休息时段不能是空的或整天：{text!r}")
    return QuietWindow(start, end)


def parse_windows(texts: list[str]) -> list[QuietWindow]:
    windows = [parse_window(t) for t in texts]
    if windows and all(any(w.contains(m) for w in windows) for m in range(_DAY)):
        raise ValueError("休息时段覆盖了一整天，程序将永远不会运行")
    return windows


def quiet_until(now: datetime, windows: list[QuietWindow]) -> datetime | None:
    """now 在休息时段里时返回休息结束的时刻（合并相邻/重叠时段），否则返回 None。"""
    t = now.replace(second=0, microsecond=0)
    if not any(w.contains(t.hour * 60 + t.minute) for w in windows):
        return None
    # 逐分钟向后找第一个不在任何时段里的时刻；parse_windows 已排除整天覆盖
    for _ in range(_DAY):
        t += timedelta(minutes=1)
        if not any(w.contains(t.hour * 60 + t.minute) for w in windows):
            return t
    raise RuntimeError("休息时段覆盖了一整天")
