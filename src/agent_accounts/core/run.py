"""一次命令执行 = 一个 Run：落库、写审计、失败时的截图和快照都放在 ``runs/<id>/``。"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_accounts.core import alerts, audit, paths, store
from agent_accounts.core.errors import AccountFrozen, HumanRequired


def new_run_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


@dataclass
class RunContext:
    id: str
    platform: str
    command: str

    @property
    def dir(self) -> Path:
        return paths.ensure_dir(paths.runs_dir() / self.id)

    def audit(self, action: str, **detail: Any) -> None:
        audit.record(self.platform, action, run_id=self.id, **detail)

    def alert(self, level: alerts.Level, message: str) -> None:
        alerts.alert(self.platform, level, message, run_id=self.id)


def _finish(run_id: str, status: store.RunStatus, error: str | None = None) -> None:
    with store.session() as s:
        row = s.get(store.Run, run_id)
        if row is None:
            return
        row.status = status
        row.error = error
        row.finished_at = store.utcnow()
        s.add(row)
        s.commit()


@contextmanager
def start_run(platform: str, command: str, *, require_active: bool = True) -> Iterator[RunContext]:
    """开始一次运行。``require_active`` 为真时，账号被冻结就直接拒绝。"""
    account = store.get_account(platform)
    if require_active and account.status == "frozen":
        raise AccountFrozen(f"{platform} 账号已冻结，拒绝执行 {command}")

    ctx = RunContext(id=new_run_id(), platform=platform, command=command)
    with store.session() as s:
        s.add(store.Run(id=ctx.id, platform=platform, command=command))
        s.commit()
    ctx.audit("run.start", command=command)
    try:
        yield ctx
    except HumanRequired as e:
        _finish(ctx.id, "blocked", str(e))
        if e.freeze:
            store.set_account_status(platform, "frozen")
            ctx.audit("account.auto_freeze", reason=str(e))
            ctx.alert(
                "critical",
                f"需要人工介入：{e}。账号已自动冻结，处理后运行 agent-accounts unfreeze {platform}",
            )
        else:
            ctx.alert("critical", f"需要人工介入：{e}")
        raise
    except BaseException as e:
        _finish(ctx.id, "failed", f"{type(e).__name__}: {e}")
        raise
    else:
        _finish(ctx.id, "ok")
