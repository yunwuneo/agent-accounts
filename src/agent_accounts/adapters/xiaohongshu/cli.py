"""小红书 M0 命令行：仅开放只读 doctor。"""

from __future__ import annotations

import asyncio
import json

import typer

from agent_accounts.adapters.xiaohongshu import PLATFORM
from agent_accounts.core import config
from agent_accounts.core.config import ConfigError
from agent_accounts.core.errors import AccountFrozen, HumanRequired
from agent_accounts.core.run import start_run

app = typer.Typer(help="小红书适配器（M0 只读探针）", no_args_is_help=True)


@app.callback()
def main() -> None:
    """小红书适配器命令组。"""


def _run(coro) -> None:
    try:
        asyncio.run(coro)
    except AccountFrozen as exc:
        typer.secho(str(exc), fg="blue", err=True)
        raise typer.Exit(3) from None
    except HumanRequired as exc:
        typer.secho(f"需要人工介入：{exc}", fg="red", err=True)
        raise typer.Exit(2) from None
    except ConfigError as exc:
        typer.secho(f"配置错误：{exc}", fg="red", err=True)
        raise typer.Exit(4) from None


@app.command()
def login(timeout: int = typer.Option(300, min=1, help="等待人工登录的最长秒数")) -> None:
    """打开独立 headed 会话，由人在窗口中完成登录。"""
    from agent_accounts.adapters.xiaohongshu.login import login as handoff

    async def run_login() -> None:
        with start_run(PLATFORM, "login") as run:
            await handoff(config.load(), run, timeout_s=timeout)
        typer.echo("浏览器会话已保存；登录态仍需由 xiaohongshu doctor 验收。")

    _run(run_login())


@app.command()
def doctor() -> None:
    """只打开私信首页；不进入会话、不发送、不保存消息内容。"""
    from agent_accounts.adapters.xiaohongshu.doctor import doctor as inspect

    async def main() -> None:
        with start_run(PLATFORM, "doctor") as run:
            result = await inspect(config.load(), run)
        typer.echo(json.dumps(result.__dict__, ensure_ascii=False, indent=2))

    _run(main())
