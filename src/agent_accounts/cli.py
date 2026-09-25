"""``agent-accounts`` 命令行：账号总开关和各平台子命令。"""

from __future__ import annotations

import typer

from agent_accounts.adapters.douyin.cli import app as douyin_app
from agent_accounts.core import audit, paths, store

app = typer.Typer(help="Echo agent 的账号与能力框架", no_args_is_help=True)
app.add_typer(douyin_app, name="douyin")


@app.command()
def status(platform: str = typer.Argument("douyin")) -> None:
    """查看账号状态和数据目录。"""
    account = store.get_account(platform)
    typer.echo(f"平台：{account.platform}")
    typer.echo(f"状态：{account.status}")
    typer.echo(f"数据目录：{paths.home()}")


@app.command()
def freeze(platform: str = typer.Argument(...)) -> None:
    """一键冻结账号：之后一切自动化操作都会被拒绝。"""
    store.set_account_status(platform, "frozen")
    audit.record(platform, "account.freeze")
    typer.secho(f"🧊 {platform} 已冻结", fg="blue")


@app.command()
def unfreeze(platform: str = typer.Argument(...)) -> None:
    """解除冻结。"""
    store.set_account_status(platform, "active")
    audit.record(platform, "account.unfreeze")
    typer.secho(f"✅ {platform} 已恢复", fg="green")
