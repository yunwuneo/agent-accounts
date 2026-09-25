"""告警与人工介入通知。

告警通道还是开放问题（钉钉 / Notion / 其他），M0 先输出到终端并写审计日志。
"""

from __future__ import annotations

import sys
from typing import Literal

from agent_accounts.core import audit

Level = Literal["info", "warning", "critical"]

_PREFIX = {"info": "ℹ️ ", "warning": "⚠️ ", "critical": "🛑 "}


def alert(platform: str, level: Level, message: str, run_id: str | None = None) -> None:
    print(f"{_PREFIX[level]}[{platform}] {message}", file=sys.stderr)
    audit.record(platform, "alert", run_id=run_id, level=level, message=message)
