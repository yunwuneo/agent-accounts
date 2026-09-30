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


@app.command()
def mcp(
    http: bool = typer.Option(
        False, "--http", help="以 HTTP 启动（Bearer 鉴权，地址见配置 [mcp]）；默认 stdio"
    ),
    host: str | None = typer.Option(None, help="覆盖 [mcp] host（仅 --http）"),
    port: int | None = typer.Option(None, help="覆盖 [mcp] port（仅 --http）"),
) -> None:
    """启动 MCP server：读最近的私信往来，读写人设和近况。"""
    from agent_accounts import mcp_server
    from agent_accounts.core import config

    if not http:
        mcp_server.main()
        return
    try:
        cfg = config.load().mcp
        if host is not None:
            cfg.host = host
        if port is not None:
            cfg.port = port
        mcp_server.main_http(cfg)
    except config.ConfigError as e:
        typer.secho(f"⚙️ {e}", fg="red", err=True)
        raise typer.Exit(4) from None


def _root_cause(e: BaseException) -> BaseException:
    """MCP 客户端内部用 TaskGroup，异常常被包成 ExceptionGroup；取出第一个实际原因。"""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        timeouts = [x for x in e.exceptions if isinstance(_root_cause(x), TimeoutError)]
        e = timeouts[0] if timeouts else e.exceptions[0]
    return e


@app.command("mcp-check")
def mcp_check(
    url: str | None = typer.Option(
        None, help="要检查的地址；默认本机 http://127.0.0.1:<port><path>。填对外地址可检查代理链路"
    ),
    timeout: float = typer.Option(15, help="每一步的超时秒数"),
) -> None:
    """连接 HTTP MCP，逐步检查：握手 → 列工具 → 调 get_persona，报告每步耗时和卡在哪一步。

    用配置里的 token 鉴权（不显示 token）；只输出耗时和字数，不输出人设内容。
    """
    import asyncio
    import time

    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client

    from agent_accounts.core import config

    try:
        cfg = config.load().mcp
        token = cfg.bearer()
    except config.ConfigError as e:
        typer.secho(f"⚙️ {e}", fg="red", err=True)
        raise typer.Exit(4) from None
    if not token:
        typer.secho("配置里没有 [mcp] token，先运行 agent-accounts mcp-token", fg="red", err=True)
        raise typer.Exit(4)
    target = url or f"http://127.0.0.1:{cfg.port}{cfg.path}"
    typer.echo(f"检查 {target}")
    step = "连接并握手（initialize）"

    async def main() -> None:
        nonlocal step
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
        t = time.monotonic()
        async with http, Client(streamable_http_client(target, http_client=http)) as c:
            typer.echo(f"  ✅ {step}  {time.monotonic() - t:.2f}s")
            step, t = "列出工具（tools/list）", time.monotonic()
            tools = await asyncio.wait_for(c.list_tools(), timeout)
            typer.echo(f"  ✅ {step}  {len(tools.tools)} 个，{time.monotonic() - t:.2f}s")
            step, t = "调用 get_persona（tools/call）", time.monotonic()
            r = await asyncio.wait_for(c.call_tool("get_persona", {}), timeout)
            if r.is_error:
                raise RuntimeError("工具返回错误")
            chars = len((r.structured_content or {}).get("persona", ""))
            typer.echo(f"  ✅ {step}  人设 {chars} 字，{time.monotonic() - t:.2f}s")

    try:
        asyncio.run(asyncio.wait_for(main(), timeout * 3))
    except Exception as e:  # 超时、连接失败、HTTP 错误都在这里报告卡在哪一步
        root = _root_cause(e)
        kind = "超时" if isinstance(root, TimeoutError) else type(root).__name__
        typer.secho(f"  ❌ {step}：{kind} {str(root)[:200]}", fg="red")
        raise typer.Exit(1) from None
    typer.secho("全部通过", fg="green")


@app.command("mcp-token")
def mcp_token(
    rotate: bool = typer.Option(False, "--rotate", help="已有 token 时换一个新的（旧的立即失效）"),
) -> None:
    """生成 HTTP MCP 的 Bearer token 并写入配置 [mcp] 段。

    token 只输出到 stdout（一行），提示信息走 stderr，方便脚本捕获后放进剪贴板。
    """
    import secrets

    from agent_accounts.core import config

    token = secrets.token_urlsafe(32)
    try:
        path = config.write_mcp_token(token, rotate=rotate)
    except config.ConfigError as e:
        typer.secho(f"⚙️ {e}", fg="red", err=True)
        raise typer.Exit(4) from None
    audit.record("core", "mcp.token", rotate=rotate)
    typer.secho(f"已写入 {path} 的 [mcp] 段", err=True)
    typer.echo(token)


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
    for name, d in (("douyin", cfg.douyin), ("xiaohongshu", cfg.xiaohongshu)):
        typer.echo(f"自动回复：{name}.auto_reply = {d.auto_reply}")
        typer.echo(
            f"[{name} 节奏] 每 {d.interval_min_s}–{d.interval_max_s} 秒一轮；"
            f"休息时段 {', '.join(d.quiet_hours) or '无'}"
        )
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
    m = cfg.media
    typer.echo(
        f"[media（看视频）] 约每 {m.frame_interval_s:g} 秒一帧，最多 {m.max_frames} 帧；"
        f"语音最多转写 {m.max_video_seconds} 秒，每段 {m.transcribe_segment_s} 秒"
    )
    t = cfg.reply_tools
    state = "开" if t.enabled else "关"
    typer.echo(f"[reply_tools（回复模型工具调用）] {state}，每次决策最多 {t.max_rounds} 轮")
    typer.echo(f"[alerts（告警 webhook）] {cfg.alerts.describe()}")
    typer.echo(f"[mcp（HTTP MCP）] {cfg.mcp.describe()}")


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
