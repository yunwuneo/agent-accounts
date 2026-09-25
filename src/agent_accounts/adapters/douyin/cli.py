"""``douyin`` 命令行。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import typer

from agent_accounts.adapters.douyin import PLATFORM
from agent_accounts.core import config
from agent_accounts.core.errors import AccountFrozen, HumanRequired
from agent_accounts.core.run import start_run

app = typer.Typer(help="抖音适配器", no_args_is_help=True)

_MARK = {True: "✅", False: "❌", None: "➖"}


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(coro)
    except AccountFrozen as e:
        typer.secho(f"🧊 {e}", fg="blue", err=True)
        raise typer.Exit(3) from None
    except HumanRequired as e:
        typer.secho(f"🛑 需要人工介入：{e}", fg="red", err=True)
        raise typer.Exit(2) from None


@app.command()
def login(timeout: int = typer.Option(300, help="等待扫码的最长秒数")) -> None:
    """headed 模式打开专用 profile，由人扫码登录。"""
    from agent_accounts.adapters.douyin.login import login as do_login

    async def main() -> None:
        # 登录是人工操作，账号冻结时也允许
        with start_run(PLATFORM, "login", require_active=False) as run:
            state = await do_login(config.load(), run, timeout)
        typer.secho(f"✅ 已登录（run {run.id}）" if state.logged_in else "未登录", fg="green")

    _run(main())


@app.command()
def doctor(
    headless: bool | None = typer.Option(None, "--headless/--headed", help="覆盖配置"),
    open_thread: bool = typer.Option(
        False, help="点进第一个会话检查输入框和发送按钮（会标记已读）"
    ),
    dump: bool = typer.Option(False, help="无论成败都保存截图和 DOM 快照"),
) -> None:
    """检查登录态和关键元素能否定位（只读，不发送）。"""
    from agent_accounts.adapters.douyin.doctor import doctor as do_doctor

    async def main() -> bool:
        with start_run(PLATFORM, "doctor") as run:
            checks = await do_doctor(
                config.load(), run, headless=headless, open_thread=open_thread, dump=dump
            )
        width = max(len(c.name) for c in checks)
        for c in checks:
            typer.echo(f"{_MARK[c.ok]} {c.name:<{width}}  {c.detail}")
        return all(c.ok is not False for c in checks)

    if not _run(main()):
        raise typer.Exit(1)
