"""``douyin`` 命令行。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import typer

from agent_accounts.adapters.douyin import PLATFORM
from agent_accounts.core import config
from agent_accounts.core.config import ConfigError
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
    except ConfigError as e:
        typer.secho(f"⚙️ 配置错误：{e}", fg="red", err=True)
        raise typer.Exit(4) from None


@app.command()
def login(
    timeout: int = typer.Option(300, help="等待扫码的最长秒数"),
    relogin: bool = typer.Option(False, help="已登录时先等人退出当前账号，再重新扫码"),
) -> None:
    """headed 模式打开专用 profile，由人扫码登录。"""
    from agent_accounts.adapters.douyin.login import login as do_login

    async def main() -> None:
        # 登录是人工操作，账号冻结时也允许
        with start_run(PLATFORM, "login", require_active=False) as run:
            state = await do_login(config.load(), run, timeout, relogin=relogin)
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


spike_app = typer.Typer(help="M0 Spike 探查脚本（产物在 runs/<id>/）", no_args_is_help=True)
app.add_typer(spike_app, name="spike")


@spike_app.command("net")
def spike_net(
    conv: int = typer.Option(0, help="点进第几个会话（从 0 开始，会标记已读）"),
    scrolls: int = typer.Option(3, help="向上翻历史的次数"),
    headless: bool | None = typer.Option(None, "--headless/--headed", help="覆盖配置"),
) -> None:
    """Spike-2：录制私信相关的接口和 WebSocket 帧。"""
    import json

    from agent_accounts.adapters.douyin.spike import spike_net as do_spike

    async def main() -> None:
        with start_run(PLATFORM, "spike.net") as run:
            summary = await do_spike(
                config.load(), run, conv_index=conv, scrolls=scrolls, headless=headless
            )
        typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))
        typer.secho(f"产物：{run.dir}", fg="green")

    _run(main())


@spike_app.command("media")
def spike_media(
    aweme_id: str = typer.Argument(..., help="作品 ID（私信分享卡片里的 itemId）"),
    kind: str = typer.Option("video", help="video 或 note（图集）"),
) -> None:
    """Spike-3：拦截作品详情接口，下载媒体并用 ffmpeg 验证（验证完即删除）。"""
    import json

    from agent_accounts.adapters.douyin.spike import spike_media as do_spike

    async def main() -> None:
        with start_run(PLATFORM, "spike.media") as run:
            result = await do_spike(config.load(), run, aweme_id, kind)
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))

    _run(main())


def _local(dt) -> str:
    return dt.astimezone().strftime("%m-%d %H:%M") if dt else "--"


def _conv_json(c) -> dict:
    return {
        "conv_id": c.conv_id,
        "name": c.name,
        "kind": c.kind,
        "unread": c.unread,
        "last_at": c.last_at.isoformat() if c.last_at else None,
        "last_preview": c.last_preview,
    }


def _msg_json(m) -> dict:
    return {
        "msg_id": m.msg_id,
        "conv_id": m.conv_id,
        "from_me": m.from_me,
        "type": m.type,
        "sent_at": m.sent_at.isoformat() if m.sent_at else None,
        "text": m.text,
        "aweme_id": m.aweme_id,
        "share_title": m.share_title,
        "share_author": m.share_author,
        "image_count": m.image_count,
    }


def _print_conversations(convs) -> None:
    for c in convs:
        badge = f"({c.unread})" if c.unread else "   "
        typer.echo(f"{badge:>4} {_local(c.last_at)}  {c.name or c.conv_id}  {c.last_preview or ''}")


@app.command()
def sync(
    save_raw: bool = typer.Option(
        False, help="把原始接口响应保存到 runs/<id>/raw（用于做 fixture）"
    ),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """打开首页拦截私信接口，新消息入库（不点进会话，不会标记已读）。"""
    import json

    from agent_accounts.adapters.douyin.sync import sync as do_sync

    async def main() -> None:
        with start_run(PLATFORM, "sync") as run:
            r = await do_sync(config.load(), run, save_raw=save_raw)
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "source": r.source,
                        "new_messages": [_msg_json(m) for m in r.new_messages],
                        "conversations": [_conv_json(c) for c in r.conversations],
                        "errors": r.errors,
                        "mark_read_requests": r.mark_read_requests,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        if r.source == "dom":
            typer.secho("⚠️ 接口数据未拦截到，以下为 DOM 会话列表（未入库）", fg="yellow")
            for d in r.dom_conversations:
                typer.echo(f"({d.unread}) {d.time_text}  {d.name}  {d.preview}")
            return
        typer.echo(
            f"接口响应 {r.responses} 个，会话 {len(r.conversations)} 个，"
            f"新消息 {len(r.new_messages)} 条，跳过命令消息 {r.skipped_commands} 条"
        )
        for m in r.new_messages:
            who = "我" if m.from_me else "对方"
            typer.echo(f"  {_local(m.sent_at)} {who}：{m.preview()}")
        if r.errors:
            typer.secho(f"解析错误 {len(r.errors)} 个：{r.errors[:3]}", fg="yellow")
        mark = "✅ 未触发 mark_read" if not r.mark_read_requests else "⚠️ 出现了 mark_read 请求"
        typer.echo(mark)

    _run(main())


@app.command()
def inbox(
    offline: bool = typer.Option(False, help="只读本地数据库，不打开浏览器"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """会话列表（默认先同步）。"""
    import json

    from agent_accounts.adapters.douyin import store as dstore
    from agent_accounts.adapters.douyin.sync import sync as do_sync

    if not offline:

        async def main() -> None:
            with start_run(PLATFORM, "inbox") as run:
                await do_sync(config.load(), run)

        _run(main())
    convs = dstore.list_conversations()
    if as_json:
        typer.echo(json.dumps([_conv_json(c) for c in convs], ensure_ascii=False, indent=2))
    else:
        _print_conversations(convs)


@app.command()
def thread(
    conv: str = typer.Argument(..., help="conv_id 或对方昵称（包含匹配）"),
    limit: int = typer.Option(30, help="最多显示多少条"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """读本地数据库里的会话消息（不打开浏览器；先用 sync 同步）。"""
    import json

    from agent_accounts.adapters.douyin import store as dstore

    row = dstore.find_conversation(conv)
    if row is None:
        typer.secho(f"找不到会话：{conv}（用 douyin inbox --offline 查看）", fg="red", err=True)
        raise typer.Exit(1)
    msgs = dstore.list_messages(row.conv_id, limit=limit)
    if as_json:
        typer.echo(json.dumps([_msg_json(m) for m in msgs], ensure_ascii=False, indent=2))
        return
    typer.secho(f"{row.name or row.conv_id}（未读 {row.unread}）", bold=True)
    for m in msgs:
        who = "我" if m.from_me else "对方"
        extra = f"  aweme_id={m.aweme_id}" if m.aweme_id else ""
        typer.echo(f"{_local(m.sent_at)} {who}：{m.preview()}{extra}")
