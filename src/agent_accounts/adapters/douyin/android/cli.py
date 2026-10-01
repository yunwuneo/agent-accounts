"""显式 android 命令组；不重定向网页命令，不自动打开会话/作品。"""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from agent_accounts.adapters.douyin.android import capture, media, messaging
from agent_accounts.adapters.douyin.android.session import AndroidSession
from agent_accounts.core import config
from agent_accounts.core.errors import AgentAccountsError
from agent_accounts.core.run import start_run

app = typer.Typer(help="可选安卓真机后端；自动回复需显式运行 android run", no_args_is_help=True)


def invoke(command, fn, *, device=True, require_active=True):
    try:
        cfg = config.load()
        with start_run("douyin", "android." + command, require_active=require_active) as run:
            if device:
                with AndroidSession(cfg.douyin.android) as s:
                    result = fn(s, run, cfg)
            else:
                result = fn(None, run, cfg)
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    except AgentAccountsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None


@app.command()
def doctor():
    """检查已授权设备、已验收应用版本、本机 Appium 和当前前台。"""
    invoke(
        "doctor",
        lambda s, r, c: {
            "ok": True,
            "backend": "android",
            "auto_reply": c.douyin.android.auto_reply,
            "send_enabled": c.douyin.android.allow_send,
            "version": c.douyin.android.tested_version,
        },
    )


@app.command("run")
def auto_reply(
    thread: str = typer.Option(..., help="人工打开的唯一互关私聊精确标题"),
    self_name: str = typer.Option(..., help="专用账号自己的精确昵称，用于头像核验"),
    confirm_current_thread: bool = typer.Option(False, help="确认账号、互关私聊和消息底部"),
    generate: bool = typer.Option(False, help="允许最多一次付费回复模型调用；默认只观察"),
    allow_send: bool = typer.Option(False, help="允许最多实际发送一条；仍须配置开关"),
    seconds: int = typer.Option(300, min=1, max=3600),
    poll_s: float = typer.Option(3, min=1, max=60),
    media_enabled: bool = typer.Option(
        False, "--media", help="允许一张新作品卡片的解析、理解及视频单次整段转写"
    ),
):
    """有界监听新消息；--media 显式接通单作品理解，启动跳过历史，不自动续跑。"""
    from agent_accounts.adapters.douyin.android.autoreply import watch

    invoke(
        "run",
        lambda s, r, c: asyncio.run(
            watch(
                s,
                r,
                c,
                thread,
                self_name,
                confirmed=confirm_current_thread,
                allow_send=allow_send,
                generate=generate,
                seconds=seconds,
                poll_s=poll_s,
                media_enabled=media_enabled,
                on_ready=lambda: typer.echo("基线已确认，正在等待对方的新消息。", err=True),
            )
        ),
    )


@app.command("list-shares")
def list_shares(
    thread: str = typer.Option(...),
    self_name: str = typer.Option(...),
    older_pages: int = typer.Option(0, min=0, max=10, help="显式向上翻阅的屏数"),
):
    """列出当前可核对的对方分享及卡片指纹，不调用模型、不发送。"""
    from agent_accounts.adapters.douyin.android import shares

    def execute(s, r, c):
        for _ in range(older_pages):
            shares.scroll(s, thread, older=True)
        return {"run_id": r.id, "shares": shares.list_shares(s, thread, self_name)}

    invoke("list-shares", execute)


@app.command("reply-share")
def reply_share(
    thread: str = typer.Option(...),
    self_name: str = typer.Option(...),
    card_hash: str = typer.Option(..., help="list-shares 返回的当前可见卡片指纹"),
    confirm_history: bool = typer.Option(False, help="确认当前互关私聊，并明确回复这张历史分享"),
    generate: bool = typer.Option(False, help="提交该链接解析；最多一次转写、理解和回复模型调用"),
    allow_send: bool = typer.Option(False, help="实际发送最多一条，还须安卓配置开关"),
):
    """针对一张明确选定的历史分享理解并回复，默认只校验、不提交链接。"""
    from agent_accounts.adapters.douyin.android.media_reply import reply_share as process

    invoke(
        "reply-share",
        lambda s, r, c: asyncio.run(
            process(
                s,
                r,
                c,
                thread,
                self_name,
                card_hash,
                confirmed=confirm_history,
                execute=allow_send,
                generate=generate,
            )
        ),
    )


@app.command()
def snapshot(thread: str = typer.Option(..., help="人工已打开会话的精确标题")):
    """保存当前会话可见内容和截图；不冒充完整消息同步。"""
    invoke("snapshot", lambda s, r, c: messaging.snapshot(s, r, thread))


@app.command()
def send(
    thread: str = typer.Option(...),
    text: str = typer.Option(...),
    request_id: str = typer.Option(..., help="唯一操作编号；重复编号永不重发"),
    execute: bool = typer.Option(False, "--send", help="实际发送；默认仅检查，不输入"),
    confirm_current_thread: bool = typer.Option(False, help="已人工核对专用账号和唯一互关私聊"),
):
    """人工单条发送；配置开关 + --send + 当前会话确认三者齐备才输入。"""
    invoke(
        "send",
        lambda s, r, c: messaging.send_one(
            s, r, c, thread, text, request_id, execute=execute, confirmed=confirm_current_thread
        ),
    )


@app.command("resolve-send")
def resolve_send(request_id: str, confirmed: bool = typer.Option(False, "--human-reviewed")):
    """人工已核对手机和对端后关闭 pending；不补发，不自动解除冻结。"""
    if not confirmed:
        raise typer.BadParameter("需要 --human-reviewed，必须先人工核对")
    invoke(
        "resolve-send",
        lambda s, r, c: messaging.resolve_send(r, request_id),
        device=False,
        require_active=False,
    )


@app.command("capture-gallery")
def capture_gallery(pages: int = typer.Option(..., min=1, max=200)):
    """逐页截图：先人工打开图集、清屏并暂停，再传入核对的总页数。"""
    invoke("capture-gallery", lambda s, r, c: capture.capture_gallery(s, r, pages))


@app.command("capture-video")
def capture_video(
    duration: float = typer.Option(..., min=0.1, max=1800, help="人工核对总秒数"),
    interval: float = typer.Option(1.0, min=0.2, max=10, help="目标采样间隔，实际值记入清单"),
):
    """暂停、清屏、1.0倍速的视频采样两次回绕；无音频、不保证精确首尾。"""
    invoke("capture-video", lambda s, r, c: capture.capture_video(s, r, duration, interval))


@app.command("export-gallery")
def export_gallery(pages: int = typer.Option(..., min=1, max=200)):
    """在“选择图片保存”页全选保存并拉取本次新增文件（会写手机相册）。"""
    invoke("export-gallery", lambda s, r, c: media.export_gallery(s, r, pages))


@app.command("import-media")
def import_media(
    files: Annotated[list[Path], typer.Argument(exists=True, dir_okay=False)],
    kind: str = typer.Option(..., help="video 或 gallery"),
    source: str = typer.Option(..., help="official_export 或 third_party_manual"),
    count: int = typer.Option(..., min=1, max=200, help="预期文件数，视频必须是1"),
    order_confirmed: bool = typer.Option(False, help="人工已按图集原序传入文件"),
):
    """导入已由人下载的文件，用 ffprobe 和 ffmpeg 验证；不上传、不调用付费模型。"""
    invoke(
        "import-media",
        lambda s, r, c: asyncio.run(
            media.validate_files(r, files, kind, source, count, order_confirmed=order_confirmed)
        ),
        device=False,
    )


@app.command("parse-media")
def parse_media(
    link: str = typer.Argument(..., help="单个抖音作品 HTTPS 链接，不含分享文案"),
    kind: str = typer.Option(..., help="video 或 gallery"),
    count: int = typer.Option(1, min=1, max=200, help="预期文件数；图集须填总页数"),
    execute: bool = typer.Option(False, "--execute", help="允许向 KuKuTool 提交链接并下载"),
    expected_title: str = typer.Option("", help="可选：结果须包含该作品标题"),
    max_mb: int = typer.Option(200, min=1, max=2048, help="每个文件大小上限"),
    timeout: float = typer.Option(90, min=5, max=300, help="解析/单文件下载时限"),
):
    """自动操作 KuKuTool 可见网页并校验下载；无需连接手机，默认只检查参数。"""
    from agent_accounts.adapters.douyin.parsers.kuku import parse_media as parse

    invoke(
        "parse-media",
        lambda s, r, c: asyncio.run(
            parse(
                c,
                r,
                link,
                kind,
                count,
                execute=execute,
                expected_title=expected_title,
                max_mb=max_mb,
                timeout_s=timeout,
            )
        ),
        device=False,
    )
