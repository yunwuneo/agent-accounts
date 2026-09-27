"""小红书 M0 命令行：仅开放只读 doctor。"""

from __future__ import annotations

import asyncio
import json
import re

import typer

from agent_accounts.adapters.xiaohongshu import PLATFORM
from agent_accounts.core import config
from agent_accounts.core.config import ConfigError
from agent_accounts.core.errors import AccountFrozen, HumanRequired
from agent_accounts.core.run import start_run

app = typer.Typer(help="小红书适配器", no_args_is_help=True)


@app.callback()
def main() -> None:
    """小红书适配器命令组。"""


def _run(coro):
    try:
        return asyncio.run(coro)
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


def _local(dt) -> str:
    return dt.astimezone().strftime("%m-%d %H:%M") if dt else "--"


def _conv_json(c) -> dict:
    return {
        "peer_id": c.peer_id,
        "name": c.name,
        "follow_status": c.follow_status,
        "is_friend": c.is_friend,
        "unread": c.unread,
        "has_new": c.has_new,
        "last_at": c.last_at.isoformat() if c.last_at else None,
        "last_preview": c.last_preview,
    }


def _msg_json(m) -> dict:
    return {
        "msg_id": m.msg_id,
        "peer_id": m.peer_id,
        "store_id": m.store_id,
        "from_me": m.from_me,
        "type": m.type,
        "revoked": m.revoked,
        "sent_at": m.sent_at.isoformat() if m.sent_at else None,
        "text": m.text,
        "note_id": m.note_id,
        "note_type": m.note_type,
        "note_title": m.note_title,
        "note_author": m.note_author,
        "image_url": m.image_url,
    }


def _print_conversations(convs) -> None:
    for c in convs:
        badge = f"({c.unread})" if c.unread else ("(新)" if c.has_new else "   ")
        typer.echo(f"{badge:>4} {_local(c.last_at)}  {c.name or c.peer_id}  {c.last_preview or ''}")


@app.command()
def sync(
    open_chats: bool = typer.Option(
        True, "--open/--no-open", help="点进有新消息的会话拉取消息（会被平台标为已读）"
    ),
    max_open: int = typer.Option(5, min=1, max=20, help="每轮最多点开几个会话"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """打开私信首页同步会话列表；只点开有新消息的会话拉取消息入库。

    注意：小红书必须点进会话才能读到完整消息，点进去对方就会看到「已读」。
    --no-open 只更新会话列表和未读数，不标已读。
    """
    from agent_accounts.adapters.xiaohongshu.sync import sync as do_sync

    async def main() -> None:
        with start_run(PLATFORM, "sync") as run:
            r = await do_sync(config.load(), run, open_chats=open_chats, max_open=max_open)
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "conversations": [_conv_json(c) for c in r.conversations],
                        "opened": [o.__dict__ for o in r.opened],
                        "skipped": r.skipped,
                        "new_messages": [_msg_json(m) for m in r.new_messages],
                        "revoked": r.revoked,
                        "read_requests": r.read_requests,
                        "errors": r.errors,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        typer.echo(
            f"会话 {len(r.conversations)} 个，点开 {len(r.opened)} 个，"
            f"新消息 {len(r.new_messages)} 条" + (f"，撤回 {r.revoked} 条" if r.revoked else "")
        )
        for o in r.opened:
            gap = "（仍有更早的消息没拉到）" if o.gap else ""
            status = o.error or f"拉到 {o.fetched} 条，新 {o.new} 条，{o.pages} 页{gap}"
            typer.echo(f"  👁 {o.name or o.peer_id}：{status}")
        for m in r.new_messages:
            who = "我" if m.from_me else "对方"
            typer.echo(f"  {_local(m.sent_at)} {who}：{m.preview()}")
        if r.skipped:
            typer.secho(f"有新消息但本轮没打开：{len(r.skipped)} 个会话", fg="yellow")
        if r.errors:
            typer.secho(f"问题 {len(r.errors)} 个：{r.errors[:3]}", fg="yellow")
        if r.opened:
            typer.echo(f"已读上报 {r.read_requests} 次（点开的会话对方会看到已读）")

    _run(main())


@app.command()
def inbox(as_json: bool = typer.Option(False, "--json", help="输出 JSON")) -> None:
    """本地数据库里的会话列表（不打开浏览器；先用 sync 同步）。"""
    from agent_accounts.adapters.xiaohongshu import store as xstore

    convs = xstore.list_conversations()
    if as_json:
        typer.echo(json.dumps([_conv_json(c) for c in convs], ensure_ascii=False, indent=2))
    else:
        _print_conversations(convs)


@app.command()
def thread(
    peer: str = typer.Argument(..., help="对方用户 ID 或昵称（包含匹配）"),
    limit: int = typer.Option(30, help="最多显示多少条"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """本地数据库里的会话消息（不打开浏览器；先用 sync 同步）。"""
    from agent_accounts.adapters.xiaohongshu import store as xstore

    row = xstore.find_conversation(peer)
    if row is None:
        typer.secho(f"找不到会话：{peer}（用 xiaohongshu inbox 查看）", fg="red", err=True)
        raise typer.Exit(1)
    msgs = xstore.list_messages(row.peer_id, limit=limit)
    if as_json:
        typer.echo(json.dumps([_msg_json(m) for m in msgs], ensure_ascii=False, indent=2))
        return
    typer.secho(f"{row.name or row.peer_id}（未读 {row.unread}）", bold=True)
    for m in msgs:
        who = "我" if m.from_me else "对方"
        extra = f"  note_id={m.note_id}" if m.note_id else ""
        typer.echo(f"{_local(m.sent_at)} {who}：{m.preview()}{extra}")


_NOTE_URL = re.compile(r"xiaohongshu\.com/(?:explore|discovery/item)/([0-9a-f]{24})")


@app.command()
def digest(
    targets: list[str] = typer.Argument(None, help="笔记 ID 或笔记链接（须在私信里被分享过）"),  # noqa: B008
    pending: bool = typer.Option(False, help="处理私信里对方分享过、还没有摘要的笔记"),
    limit: int = typer.Option(5, help="--pending 时最多处理几个"),
    list_only: bool = typer.Option(False, "--list", help="只列出待处理的笔记，不分析"),
    force: bool = typer.Option(False, help="忽略缓存重新分析"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """理解私信里分享的笔记（图文 / 视频），生成摘要（按笔记缓存）。"""
    from agent_accounts.adapters.xiaohongshu import digest as xdigest

    ids: list[str] = []
    for t in targets or []:
        m = _NOTE_URL.search(t)
        note_id = m.group(1) if m else t
        if not re.fullmatch(r"[0-9a-f]{24}", note_id):
            typer.secho(f"无法识别的笔记：{t}", fg="red", err=True)
            raise typer.Exit(1)
        ids.append(note_id)
    if list_only:
        todo = xdigest.pending_items()
        typer.echo(f"待处理 {len(todo)} 篇笔记")
        for note_id in todo:
            msg = xdigest.share_message(note_id)
            kind = "视频" if msg and msg.note_type == "video" else "图文"
            typer.echo(f"  [{kind}] {note_id}  {(msg.note_title if msg else '') or ''}")
        return
    if pending:
        ids += [i for i in xdigest.pending_items(limit) if i not in ids]
    if not ids:
        typer.echo("没有需要处理的笔记")
        return

    async def main() -> list:
        with start_run(PLATFORM, "digest") as run:
            return await xdigest.digest_items(config.load(), run, ids, force=force)

    outcomes = _run(main())
    if as_json:
        typer.echo(
            json.dumps(
                [
                    {"note_id": o.note_id, "cached": o.cached, "error": o.error}
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
            typer.secho(f"❌ {o.note_id}：{o.error}", fg="red")
            continue
        d = o.digest
        tag = "（缓存）" if o.cached else ""
        kind = "视频笔记" if d.kind == "video" else "图文笔记"
        typer.secho(f"\n[{kind}] {d.item_id}{tag}  {d.author}", bold=True)
        if d.title:
            typer.echo(f"标题：{' '.join(d.title.split())[:80]}")
        if d.body:
            typer.echo(f"正文：{' '.join(d.body.split())[:120]}")
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


def _digest_json(d) -> dict:
    return {
        "note_id": d.item_id,
        "kind": d.kind,
        "available": d.available,
        "unavailable_reason": d.filter_reason,
        "title": d.title,
        "body": d.body,
        "author": d.author,
        "tags": d.hashtags,
        "duration_s": d.duration_s,
        "transcript": d.transcript,
        "summary": d.summary,
        "vibe": d.vibe,
        "reply_hooks": d.reply_hooks,
        "frames_used": d.frames_used,
        "model": d.model,
        "notes": d.notes,
    }


spike_app = typer.Typer(help="M0 Spike：只记录数据结构，不保存取值", no_args_is_help=True)
app.add_typer(spike_app, name="spike")


@spike_app.command("chat")
def spike_chat(wait: int = typer.Option(15, min=3, max=120, help="打开后观察的秒数")) -> None:
    """只打开私信首页、不点任何东西，记录接口 / WebSocket 的数据结构和 DOM 骨架。"""
    from agent_accounts.adapters.xiaohongshu.doctor import CHAT_URL, detect_block, logged_in
    from agent_accounts.adapters.xiaohongshu.spike import ShapeRecorder, dom_skeleton
    from agent_accounts.browser.session import BrowserSession

    async def main() -> None:
        with start_run(PLATFORM, "spike.chat") as run:
            recorder = ShapeRecorder()
            async with BrowserSession(PLATFORM, config.load().browser, headless=False) as s:
                recorder.attach(s.page)
                await s.page.goto(CHAT_URL, wait_until="domcontentloaded")
                await s.page.wait_for_timeout(3000)
                if await detect_block(s.page):
                    raise HumanRequired("触发小红书验证或风控", freeze=True)
                if not await logged_in(s.page):
                    raise HumanRequired("小红书未登录")
                await s.page.wait_for_timeout(wait * 1000)
                if await detect_block(s.page):
                    raise HumanRequired("触发小红书验证或风控", freeze=True)
                skeleton = await dom_skeleton(s.page)
                await recorder.drain()
            recorder.save(run.dir / "shapes.jsonl")
            (run.dir / "dom-skeleton.json").write_text(
                json.dumps(skeleton, ensure_ascii=False), encoding="utf-8"
            )
            summary = recorder.summary()
            run.audit("spike.chat", **summary)
            typer.echo(json.dumps({"run": run.id, **summary}, ensure_ascii=False, indent=2))

    _run(main())


@spike_app.command("open")
def spike_open(wait: int = typer.Option(10, min=3, max=60, help="进入会话后观察的秒数")) -> None:
    """点开唯一的测试会话，记录消息数据结构；回到首页对比未读数（检验已读副作用）。

    会话不止一个时拒绝执行，避免点开测试对象以外的私信。
    """
    from agent_accounts.adapters.xiaohongshu.doctor import CHAT_URL, detect_block, logged_in
    from agent_accounts.adapters.xiaohongshu.spike import ShapeRecorder, dom_skeleton
    from agent_accounts.browser.session import BrowserSession

    async def check(page) -> None:
        if await detect_block(page):
            raise HumanRequired("触发小红书验证或风控", freeze=True)

    async def main() -> None:
        with start_run(PLATFORM, "spike.open") as run:
            recorder = ShapeRecorder()
            async with BrowserSession(PLATFORM, config.load().browser, headless=False) as s:
                recorder.attach(s.page)
                await s.page.goto(CHAT_URL, wait_until="domcontentloaded")
                await s.page.wait_for_timeout(5000)
                await check(s.page)
                if not await logged_in(s.page):
                    raise HumanRequired("小红书未登录")
                items = s.page.locator(".xhs-im-conv-item[data-conv-id]")
                if await items.count() != 1:
                    raise HumanRequired(f"会话数为 {await items.count()}，不是唯一的测试会话")
                before = len(recorder.unread_totals)
                await s.pause()
                await items.first.click()
                await s.page.wait_for_timeout(wait * 1000)
                await check(s.page)
                opened = await dom_skeleton(s.page)
                await s.pause()
                await s.page.goto(CHAT_URL, wait_until="domcontentloaded")
                await s.page.wait_for_timeout(5000)
                await check(s.page)
                await recorder.drain()
            recorder.save(run.dir / "shapes.jsonl")
            (run.dir / "dom-skeleton-open.json").write_text(
                json.dumps(opened, ensure_ascii=False), encoding="utf-8"
            )
            summary = recorder.summary()
            summary["unread_before_open"] = recorder.unread_totals[:before]
            summary["unread_after_open"] = recorder.unread_totals[before:]
            run.audit("spike.open", **summary)
            typer.echo(json.dumps({"run": run.id, **summary}, ensure_ascii=False, indent=2))

    _run(main())


def _url_facts(url: str, tokens: dict[str, str]) -> dict[str, object]:
    """笔记页地址里只取参数名、xsec_source 取值，以及 token 是否就是私信卡片里的那个。"""
    from urllib.parse import parse_qs, urlsplit

    parts = urlsplit(url)
    query = parse_qs(parts.query)
    note_id = parts.path.rstrip("/").rsplit("/", 1)[-1]
    token = (query.get("xsec_token") or [None])[0]
    return {
        "query_keys": sorted(query),
        "xsec_source": (query.get("xsec_source") or [None])[0],
        "token_from_message": token is not None and tokens.get(note_id) == token,
        "link_scheme": tokens.get(f"scheme:{note_id}"),
    }


@spike_app.command("note")
def spike_note(max_notes: int = typer.Option(2, min=1, max=2)) -> None:
    """在唯一的测试会话里依次点开笔记卡片，记录笔记数据结构与媒体能否下载（不保存媒体）。"""
    from agent_accounts.adapters.xiaohongshu.doctor import CHAT_URL, detect_block, logged_in
    from agent_accounts.adapters.xiaohongshu.spike import (
        ShapeRecorder,
        dom_skeleton,
        media_urls,
        note_state,
        shape,
    )
    from agent_accounts.browser.session import BrowserSession

    async def check(page) -> None:
        if await detect_block(page):
            raise HumanRequired("触发小红书验证或风控", freeze=True)

    async def probe(context, url: str) -> dict[str, object]:
        resp = await context.request.get(url, headers={"Range": "bytes=0-262143"}, timeout=20000)
        body = await resp.body()
        return {
            "status": resp.status,
            "content_type": resp.headers.get("content-type", "").split(";", 1)[0],
            "content_range_total": resp.headers.get("content-range", "").rsplit("/", 1)[-1],
            "bytes": len(body),
        }

    async def main() -> None:
        with start_run(PLATFORM, "spike.note") as run:
            recorder = ShapeRecorder()
            results: list[dict[str, object]] = []
            tokens: dict[str, str] = {}  # 笔记 id → 私信卡片里的 xsec_token / link 协议，只在内存

            async def on_history(response) -> None:
                if not response.url.split("?", 1)[0].endswith("/messages/history"):
                    return
                from agent_accounts.adapters.xiaohongshu import im

                for m in im.parse_history(await response.json()):
                    if m.note_id and m.note_xsec_token:
                        tokens[m.note_id] = m.note_xsec_token
                        inner = json.loads(json.loads(m.content_json)["content"])
                        tokens[f"scheme:{m.note_id}"] = str(inner.get("link", "")).split(":", 1)[0]

            async with BrowserSession(PLATFORM, config.load().browser, headless=False) as s:
                recorder.attach(s.page)
                s.context.on("page", recorder.attach)
                s.page.on("response", lambda r: asyncio.ensure_future(on_history(r)))
                await s.page.goto(CHAT_URL, wait_until="domcontentloaded")
                await s.page.wait_for_timeout(5000)
                await check(s.page)
                if not await logged_in(s.page):
                    raise HumanRequired("小红书未登录")
                items = s.page.locator(".xhs-im-conv-item[data-conv-id]")
                if await items.count() != 1:
                    raise HumanRequired(f"会话数为 {await items.count()}，不是唯一的测试会话")
                await s.pause()
                await items.first.click()
                await s.page.wait_for_timeout(5000)
                await check(s.page)
                cards = s.page.locator(".xhs-im-bubble-card-note")
                total = await cards.count()
                for index in range(min(total, max_notes)):
                    card = cards.nth(index)
                    is_video = bool(
                        await card.locator(".xhs-im-bubble-card-note-video-icon").count()
                    )
                    await s.pause(2.0, 4.0)
                    try:
                        async with s.context.expect_page(timeout=8000) as info:
                            await card.click()
                        page = await info.value
                    except Exception:  # noqa: BLE001 — 没开新标签就看当前页
                        page = s.page
                    await page.wait_for_load_state("domcontentloaded")
                    await page.wait_for_timeout(6000)
                    row: dict[str, object] = {
                        "card": index,
                        "video_icon": is_video,
                        "new_tab": page is not s.page,
                        "url_path": re.sub(r"[0-9a-f]{24}", ":id", page.url.split("?", 1)[0]),
                        **_url_facts(page.url, tokens),
                    }
                    blocked = await detect_block(page)
                    row["blocked"] = blocked
                    if blocked:
                        results.append(row)
                        (run.dir / "notes.json").write_text(
                            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                        raise HumanRequired("打开笔记触发小红书验证或风控", freeze=True)
                    state = await note_state(page)
                    row["state_shape"] = shape(state) if state is not None else None
                    urls = media_urls(state) if state is not None else []
                    row["media_fields"] = sorted({p for p, _ in urls})
                    picks: dict[str, str] = {}
                    for path, url in urls:
                        low = path.lower()
                        kind = "video" if ("stream" in low or "video" in low) else "image"
                        if "avatar" in low or ".user." in low:
                            continue
                        picks.setdefault(kind, url)
                    row["downloads"] = {k: await probe(s.context, u) for k, u in picks.items()}
                    row["skeleton"] = await dom_skeleton(page, 18)
                    results.append(row)
                    if page is not s.page:
                        await page.close()
                    else:
                        await s.page.go_back()
                    await s.page.wait_for_timeout(2000)
                    await check(s.page)
                await recorder.drain()
            recorder.save(run.dir / "shapes.jsonl")
            (run.dir / "notes.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            brief = [
                {
                    k: r.get(k)
                    for k in (
                        "card",
                        "video_icon",
                        "new_tab",
                        "url_path",
                        "blocked",
                        "query_keys",
                        "xsec_source",
                        "token_from_message",
                        "link_scheme",
                    )
                }
                | {
                    "media_fields": len(r.get("media_fields") or []),
                    "downloads": r.get("downloads"),
                }
                for r in results
            ]
            run.audit("spike.note", notes=brief)
            typer.echo(
                json.dumps(
                    {"run": run.id, "cards": total, "notes": brief}, ensure_ascii=False, indent=2
                )
            )

    _run(main())


@spike_app.command("send")
def spike_send(
    text: str = typer.Option(..., help="发给唯一测试会话的一条文本"),
    yes: bool = typer.Option(False, "--yes", help="确认真的发送"),
) -> None:
    """向唯一的测试会话发送一条文本，只试一次、不重试；三重校验：接口、页面回显、重新拉取历史。"""
    from agent_accounts.adapters.xiaohongshu.doctor import CHAT_URL, detect_block, logged_in
    from agent_accounts.adapters.xiaohongshu.spike import ShapeRecorder
    from agent_accounts.browser.session import BrowserSession

    if not yes:
        typer.secho("真实发送需要 --yes", fg="red", err=True)
        raise typer.Exit(1)

    async def check(page) -> None:
        if await detect_block(page):
            raise HumanRequired("触发小红书验证或风控", freeze=True)

    def contains_text(body: object) -> bool:
        messages = ((body or {}).get("data") or {}).get("out_message_list") or []
        for message in messages:
            try:
                if json.loads(message.get("content") or "{}").get("content") == text:
                    return True
            except (ValueError, AttributeError):
                continue
        return False

    async def open_chat(s) -> None:
        await s.page.goto(CHAT_URL, wait_until="domcontentloaded")
        await s.page.wait_for_timeout(5000)
        await check(s.page)
        if not await logged_in(s.page):
            raise HumanRequired("小红书未登录")
        items = s.page.locator(".xhs-im-conv-item[data-conv-id]")
        if await items.count() != 1:
            raise HumanRequired(f"会话数为 {await items.count()}，不是唯一的测试会话")
        await s.pause()
        await items.first.click()
        await s.page.wait_for_timeout(4000)
        await check(s.page)

    async def main() -> None:
        with start_run(PLATFORM, "spike.send") as run:
            recorder = ShapeRecorder()
            histories: list[bool] = []
            posts: list[dict[str, object]] = []

            async def on_response(response) -> None:
                url = response.url.split("?", 1)[0]
                if url.endswith("/messages/history"):
                    histories.append(contains_text(await response.json()))
                elif response.request.method == "POST" and "/im/" in url:
                    posts.append(
                        {
                            "path": re.sub(r"[0-9a-f]{24}", ":id", url.split(".com", 1)[-1]),
                            "status": response.status,
                        }
                    )

            async with BrowserSession(PLATFORM, config.load().browser, headless=False) as s:
                recorder.attach(s.page)
                s.page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
                await open_chat(s)
                history_before = list(histories)
                editor = s.page.locator(".xhs-im-input-bar-editor[contenteditable]")
                await editor.click()
                await s.page.keyboard.type(text, delay=120)
                await s.pause(1.0, 2.0)
                run.audit("send.attempt", chars=len(text))
                await s.page.keyboard.press("Enter")
                await s.page.wait_for_timeout(5000)
                await check(s.page)
                leftover = (await editor.inner_text()).strip()
                bubbles = s.page.locator("p.xhs-im-bubble__text")
                echoed = False
                for i in range(await bubbles.count() - 1, max(-1, await bubbles.count() - 6), -1):
                    if (await bubbles.nth(i).inner_text()).strip() == text:
                        echoed = True
                        break
                await s.pause()
                await open_chat(s)
                await s.page.wait_for_timeout(2000)
                await recorder.drain()
            recorder.save(run.dir / "shapes.jsonl")
            result = {
                "run": run.id,
                "editor_cleared": not leftover,
                "page_echo": echoed,
                "in_history_before": any(history_before),
                "in_history_after_reopen": histories[len(history_before) :],
                "im_posts": posts,
            }
            run.audit("send.result", **{k: v for k, v in result.items() if k != "run"})
            typer.echo(json.dumps(result, ensure_ascii=False, indent=2))

    _run(main())
