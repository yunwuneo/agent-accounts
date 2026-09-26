"""``douyin`` 命令行。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import typer

from agent_accounts.adapters.douyin import PLATFORM
from agent_accounts.core import config, console
from agent_accounts.core.config import ConfigError
from agent_accounts.core.errors import AccountFrozen, HumanRequired
from agent_accounts.core.run import start_run

app = typer.Typer(help="抖音适配器", no_args_is_help=True)


@app.callback()
def _setup_console() -> None:
    """输出编码兜底（Windows 管道 / 重定向时 emoji 不会让命令崩溃）。"""
    console.setup()


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


def _digest_json(d) -> dict:
    return {
        "aweme_id": d.item_id,
        "kind": d.kind,
        "available": d.available,
        "filter_reason": d.filter_reason,
        "title": d.title,
        "author": d.author,
        "hashtags": d.hashtags,
        "duration_s": d.duration_s,
        "music_title": d.music_title,
        "transcript": d.transcript,
        "summary": d.summary,
        "vibe": d.vibe,
        "reply_hooks": d.reply_hooks,
        "frames_used": d.frames_used,
        "model": d.model,
        "notes": d.notes,
    }


@app.command()
def digest(
    targets: list[str] = typer.Argument(None, help="作品 ID 或 douyin.com/video|note/<id> 链接"),  # noqa: B008
    pending: bool = typer.Option(False, help="处理私信里对方分享过、还没有摘要的作品"),
    limit: int = typer.Option(5, help="--pending 时最多处理几个"),
    force: bool = typer.Option(False, help="忽略缓存重新分析"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """理解分享的视频/图集，生成摘要（结果按作品缓存）。"""
    import json

    from agent_accounts.adapters.douyin import digest as ddigest

    kinds: dict = {}
    ids: list[str] = []
    for t in targets or []:
        try:
            aweme_id, kind = ddigest.parse_target(t)
        except ValueError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(1) from None
        ids.append(aweme_id)
        if kind:
            kinds[aweme_id] = kind
    if pending:
        ids += [i for i in ddigest.pending_items(limit) if i not in ids]
    if not ids:
        typer.echo("没有需要处理的作品")
        return

    async def main() -> list:
        with start_run(PLATFORM, "digest") as run:
            return await ddigest.digest_items(config.load(), run, ids, force=force, kinds=kinds)

    outcomes = _run(main())
    if as_json:
        typer.echo(
            json.dumps(
                [
                    {"aweme_id": o.aweme_id, "cached": o.cached, "error": o.error}
                    | ({"digest": _digest_json(o.digest)} if o.digest else {})
                    for o in outcomes
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    for o in outcomes:
        if o.error:
            typer.secho(f"❌ {o.aweme_id}：{o.error}", fg="red")
            continue
        d = o.digest
        tag = "（缓存）" if o.cached else ""
        kind = "视频" if d.kind == "video" else "图集"
        typer.secho(f"\n[{kind}] {d.item_id}{tag}  {d.author}", bold=True)
        if d.title:
            typer.echo(f"标题：{' '.join(d.title.split())[:80]}")
        typer.echo(f"摘要：{d.summary}")
        typer.echo(f"氛围：{d.vibe}")
        for h in d.reply_hooks:
            typer.echo(f"  · {h}")
        extra = [f"{d.frames_used} 张图", f"转写 {len(d.transcript or '')} 字", d.model]
        typer.echo("（" + "，".join(extra) + "）")
        for n in d.notes:
            typer.secho(f"  ⚠️ {n}", fg="yellow")
    if any(o.error for o in outcomes):
        raise typer.Exit(1)


@app.command()
def reply(
    conv: str = typer.Argument(..., help="conv_id 或对方昵称"),
    texts: list[str] = typer.Option(  # noqa: B008
        ..., "--text", help="要发送的内容；写多次 --text 就分成多条依次发送"
    ),
    yes: bool = typer.Option(False, "--yes", help="不再确认，直接发送"),
) -> None:
    """手动发送私信（会点进会话、标记已读；内容和频率仍受护栏限制）。"""
    import json

    from agent_accounts.adapters.douyin import store as dstore
    from agent_accounts.adapters.douyin.replying import manual_reply

    row = dstore.find_conversation(conv)
    if row is None:
        typer.secho(f"找不到会话：{conv}（先运行 douyin sync）", fg="red", err=True)
        raise typer.Exit(1)
    text = "\n".join(texts)
    typer.echo(f"发送给「{row.name or row.conv_id}」：{' ⏎ '.join(texts)}")
    if not yes and not typer.confirm("确认发送？"):
        raise typer.Exit(1)

    async def main():
        with start_run(PLATFORM, "reply") as run:
            return await manual_reply(config.load(), run, row, text)

    r = _run(main())
    reasons = json.loads(r.guard_reasons)
    if r.status == "blocked":
        typer.secho(f"🛡️ 被护栏拦下：{'；'.join(reasons)}", fg="yellow")
        raise typer.Exit(1)
    if r.status == "sent":
        typer.secho("✅ 已发送", fg="green")
    elif r.status == "partial":
        typer.secho(f"⚠️ 只发出一部分：{r.error}", fg="yellow")
        raise typer.Exit(1)
    else:
        typer.secho(f"❌ 发送失败：{r.error}", fg="red")
        raise typer.Exit(1)


_ACTION = {
    "baseline": "📍 记录基线（不回复旧消息）",
    "blocked": "🛡️ 护栏拦截",
    "skipped": "💤 模型决定不回",
    "dry_run": "📝 dry_run（未发送）",
    "sent": "✅ 已发送",
    "failed": "❌ 发送失败",
    "partial": "⚠️ 只发出一部分",
    "error": "⚠️ 出错",
    "no_new": "（没有可处理的消息）",
}


def _joined(text: str) -> str:
    from agent_accounts.core.reply import split_messages

    return " ⏎ ".join(split_messages(text))


def _print_outcome(o) -> None:
    typer.echo(f"{_ACTION.get(o.action, o.action)}  {o.name or o.conv_id}")
    r = o.reply
    if r and r.text:
        typer.echo(f"    回复：{_joined(r.text)}")
    if r and r.reason:
        conf = f"（把握 {r.confidence:.2f}）" if r.confidence is not None else ""
        typer.echo(f"    理由：{r.reason}{conf}")
    if o.action in ("blocked", "error", "failed", "partial") and o.detail:
        typer.echo(f"    原因：{o.detail}")


@app.command()
def run(
    once: bool = typer.Option(False, "--once", help="只跑一轮"),
    dry_run: bool = typer.Option(
        False, "--dry-run/--no-dry-run", help="强制只生成不发送（即使已 --allow-send）"
    ),
    allow_send: bool = typer.Option(
        False, "--allow-send", help="真正发送的第二道确认（还需 auto_reply = on）"
    ),
) -> None:
    """自动读取 + 回复循环：sync → 新消息 → 决策 → 护栏 → dry_run 或发送。"""
    import asyncio
    import random
    from datetime import datetime

    from agent_accounts.adapters.douyin.autoreply import run_once

    cfg = config.load()

    async def tick() -> None:
        with start_run(PLATFORM, "run") as run_ctx:
            result = await run_once(cfg, run_ctx, dry_run=dry_run, allow_send=allow_send)
        stamp = datetime.now().strftime("%H:%M:%S")
        typer.secho(f"[{stamp}] 模式：{result.mode}，分析作品 {result.digested} 个", bold=True)
        for o in result.outcomes:
            _print_outcome(o)
        if not result.outcomes:
            typer.echo("    没有新消息")

    async def loop() -> None:
        while True:
            await tick()
            if once:
                return
            await asyncio.sleep(
                random.uniform(cfg.douyin.interval_min_s, cfg.douyin.interval_max_s)
            )

    _run(loop())


@app.command()
def decide(
    conv: str = typer.Argument(..., help="conv_id 或对方昵称"),
    last: int = typer.Option(2, help="把对方最近几条消息当作新消息"),
) -> None:
    """试运行一次回复决策（只记录，不发送，不打开浏览器）。"""
    from agent_accounts.adapters.douyin import store as dstore
    from agent_accounts.adapters.douyin.autoreply import decide_for

    row = dstore.find_conversation(conv)
    if row is None:
        typer.secho(f"找不到会话：{conv}", fg="red", err=True)
        raise typer.Exit(1)

    async def main():
        with start_run(PLATFORM, "decide") as run_ctx:
            return await decide_for(config.load(), run_ctx, row, last)

    _print_outcome(_run(main()))


@app.command()
def replies(limit: int = typer.Option(20, help="显示最近多少条")) -> None:
    """查看最近的回复决策记录（观察 dry_run 用）。"""
    import json

    from sqlmodel import col, select

    from agent_accounts.adapters.douyin import store as dstore
    from agent_accounts.core import store

    with store.session() as s:
        rows = s.exec(
            select(dstore.DouyinReply).order_by(col(dstore.DouyinReply.id).desc()).limit(limit)
        ).all()
    names = {c.conv_id: c.name for c in dstore.list_conversations()}
    for r in reversed(rows):
        when = r.created_at.astimezone().strftime("%m-%d %H:%M")
        typer.echo(f"{when} [{r.source}] {_ACTION.get(r.status, r.status)}  {names.get(r.conv_id)}")
        if r.text:
            typer.echo(f"    回复：{_joined(r.text)}")
        reasons = json.loads(r.guard_reasons or "[]")
        if reasons:
            typer.echo(f"    护栏：{'；'.join(reasons)}")
