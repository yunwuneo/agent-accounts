"""``agent-accounts`` 命令行：账号总开关和各平台子命令。"""

from __future__ import annotations

import typer

from agent_accounts.adapters.douyin.cli import app as douyin_app
from agent_accounts.core import audit, console, paths, store

app = typer.Typer(help="Echo agent 的账号与能力框架", no_args_is_help=True)
app.add_typer(douyin_app, name="douyin")


@app.callback()
def _setup_console() -> None:
    """输出编码兜底（Windows 管道 / 重定向时 emoji 不会让命令崩溃）。"""
    console.setup()


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


config_app = typer.Typer(help="配置（~/.agent-accounts/config.toml）", no_args_is_help=True)
app.add_typer(config_app, name="config")


@config_app.command("init")
def config_init() -> None:
    """从 config.example.toml 生成配置文件（权限 600）；已存在则不覆盖。"""
    from pathlib import Path

    from agent_accounts.core import config

    target = paths.config_path()
    if target.exists():
        typer.secho(f"{target} 已存在，不覆盖", fg="yellow")
        raise typer.Exit(1)
    example = Path(__file__).resolve().parents[2] / "config.example.toml"
    paths.ensure_dir(target.parent)
    target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o600)
    perm = "权限 600" if config.POSIX else "位于当前用户目录，靠 NTFS 权限保护"
    typer.secho(f"已生成 {target}（{perm}），请编辑其中的 endpoint / key / model", fg="green")


@config_app.command("show")
def config_show() -> None:
    """显示当前生效的模型配置（key 只显示是否已设置）。"""
    from agent_accounts.core import config

    try:
        cfg = config.load()
    except config.ConfigError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from None
    path = paths.config_path()
    typer.echo(f"自动回复：douyin.auto_reply = {cfg.douyin.auto_reply}")
    typer.echo(f"配置文件：{path}{'' if path.exists() else '（不存在，使用默认值）'}")
    sections = {
        "llm.understand（媒体理解）": cfg.llm.understand,
        "llm.reply（回复生成）": cfg.llm.reply,
        "transcribe（语音转写）": cfg.transcribe,
    }
    for name, ep in sections.items():
        info = ep.redacted()
        typer.echo(
            f"[{name}] model={info['model']}  base_url={info['base_url']}  key={info['key']}"
        )
    typer.echo(f"[alerts（告警 webhook）] {cfg.alerts.describe()}")


@app.command("alert-test")
def alert_test() -> None:
    """向配置的告警 webhook 发一条测试告警（不论 min_level）。"""
    from agent_accounts.core import alerts, config

    try:
        cfg = config.load()
    except config.ConfigError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from None
    if not cfg.alerts.url():
        typer.secho(f"告警 webhook {cfg.alerts.describe()}", fg="yellow", err=True)
        raise typer.Exit(1)
    ok = alerts.send_webhook("agent-accounts", "critical", "这是一条测试告警", None)
    if not ok:
        raise typer.Exit(1)
    typer.secho("测试告警已发送", fg="green")
