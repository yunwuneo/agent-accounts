"""告警与人工介入通知。

所有告警都输出到终端并写审计日志；配置了 ``[alerts]`` webhook 时，
达到 ``min_level`` 的告警再 POST 一份 JSON 出去（常驻运行时没人看终端）。
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Literal

import httpx2

from agent_accounts.core import audit

Level = Literal["info", "warning", "critical"]

_PREFIX = {"info": "ℹ️ ", "warning": "⚠️ ", "critical": "🛑 "}
_RANK = {"info": 0, "warning": 1, "critical": 2}


def alert(platform: str, level: Level, message: str, run_id: str | None = None) -> None:
    print(f"{_PREFIX[level]}[{platform}] {message}", file=sys.stderr)
    audit.record(platform, "alert", run_id=run_id, level=level, message=message)
    send_webhook(platform, level, message, run_id)


def send_webhook(platform: str, level: Level, message: str, run_id: str | None) -> bool | None:
    """发送 webhook；没配置或级别不够返回 None。告警常在异常路径上调用，这里绝不抛异常。"""
    from agent_accounts.core import config

    try:
        cfg = config.load().alerts
    except config.ConfigError:
        print("⚠️ 配置文件有误，告警未发送到 webhook", file=sys.stderr)
        return False
    url = cfg.url()
    if not url or _RANK[level] < _RANK[cfg.min_level]:
        return None
    payload = {
        "platform": platform,
        "level": level,
        "message": message,
        "run_id": run_id,
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "text": f"{_PREFIX[level]}[agent-accounts/{platform}] {message}",
    }
    try:
        resp = httpx2.post(url, json=payload, timeout=cfg.timeout_s)
        ok, detail = resp.status_code < 300, f"HTTP {resp.status_code}"
    except httpx2.HTTPError as e:
        ok, detail = False, type(e).__name__
    # URL 里可能带 token，审计只记结果
    audit.record(platform, "alert.webhook", run_id=run_id, ok=ok, detail=detail)
    if not ok:
        print(f"⚠️ 告警 webhook 发送失败：{detail}", file=sys.stderr)
    return ok
