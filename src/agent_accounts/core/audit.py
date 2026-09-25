"""审计日志：每次读写动作都记一条，写入前统一脱敏。"""

from __future__ import annotations

import json
import re
from typing import Any

from agent_accounts.core import store

# 凭据类字段一律不落盘（原则：凭据不进 LLM、不进日志）
_SECRET_KEY = re.compile(r"pass(word)?|token|cookie|session_?id|secret|authorization|ticket", re.I)
REDACTED = "***"


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: REDACTED if _SECRET_KEY.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


def record(platform: str, action: str, run_id: str | None = None, **detail: Any) -> None:
    event = store.AuditEvent(
        platform=platform,
        action=action,
        run_id=run_id,
        detail=json.dumps(redact(detail), ensure_ascii=False, default=str),
    )
    with store.session() as s:
        s.add(event)
        s.commit()
