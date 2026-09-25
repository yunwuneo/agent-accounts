"""小红书 M0 命令行：仅开放只读 doctor。"""

from __future__ import annotations

import asyncio
import json

import typer

from agent_accounts.adapters.xiaohongshu import PLATFORM
from agent_accounts.core import config
from agent_accounts.core.errors import AccountFrozen, HumanRequired
from agent_accounts.core.run import start_run

app = typer.Typer(help="小红书适配器（M0 只读探针）", no_args_is_help=True)


@app.callback()
def main() -> None:
    """小红书适配器命令组。"""


@app.command()
def doctor() -> None:
    """只打开私信首页；不进入会话、不发送、不保存消息内容。"""
    from agent_accounts.adapters.xiaohongshu.doctor import doctor as inspect

    async def main() -> None:
        with start_run(PLATFORM, "doctor") as run:
            result = await inspect(config.load(), run)
        typer.echo(json.dumps(result.__dict__, ensure_ascii=False, indent=2))

    try:
        asyncio.run(main())
    except AccountFrozen as exc:
        typer.secho(str(exc), fg="blue", err=True)
        raise typer.Exit(3) from None
    except HumanRequired as exc:
        typer.secho(f"需要人工介入：{exc}", fg="red", err=True)
        raise typer.Exit(2) from None
