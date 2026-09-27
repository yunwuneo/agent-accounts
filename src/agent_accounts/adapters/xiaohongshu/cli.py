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
            async with BrowserSession(PLATFORM, config.load().browser, headless=False) as s:
                recorder.attach(s.page)
                s.context.on("page", recorder.attach)
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
                {k: r.get(k) for k in ("card", "video_icon", "new_tab", "url_path", "blocked")}
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
