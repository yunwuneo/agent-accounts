"""显式 android 命令组；不重定向网页命令，不自动打开会话/作品。"""

import asyncio
import json
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Annotated

import typer

from agent_accounts.adapters.douyin.android import capture, media, messaging
from agent_accounts.adapters.douyin.android.session import AndroidSession
from agent_accounts.core import config
from agent_accounts.core.errors import AgentAccountsError
from agent_accounts.core.run import start_run

app = typer.Typer(help="可选安卓真机后端；自动回复需显式运行 android run", no_args_is_help=True)


def invoke(command, fn, *, device=True, require_active=True, allow_external=False):
    try:
        cfg = config.load()
        with start_run("douyin", "android." + command, require_active=require_active) as run:
            if device:
                with AndroidSession(cfg.douyin.android, allow_external=allow_external) as s:
                    result = fn(s, run, cfg)
            else:
                result = fn(None, run, cfg)
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    except AgentAccountsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None


@app.command()
def inspect():
    """只读定位当前页面；可在手机桌面或其他 App 运行，不输出页面原文。"""
    from agent_accounts.adapters.douyin.android import navigation

    invoke("inspect", lambda s, r, c: navigation.inspect(s, r), allow_external=True)


@app.command("open")
def open_app(execute: bool = typer.Option(False, "--execute", help="实际启动或切回抖音")):
    """从桌面/其他 App 进入抖音并识别页面；默认仅检查，不切换。"""
    from agent_accounts.adapters.douyin.android import navigation

    invoke("open", lambda s, r, c: navigation.open_app(s, r, execute=execute), allow_external=True)


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


@app.command("monitor")
def monitor(
    confirm_current_thread: bool = typer.Option(
        False, help="确认配置双方昵称、专用账号、互关私聊及底部"
    ),
    generate: bool = typer.Option(False, help="允许每批新消息调用回复模型"),
    allow_send: bool = typer.Option(False, help="允许实际自动回复，仍须配置开关"),
    media_enabled: bool = typer.Option(False, "--media", help="允许新分享解析和媒体理解"),
):
    """长驻监控配置指定的当前私聊；使用 monitor_interval_s，不按时长或回复次数退出。"""
    from agent_accounts.adapters.douyin.android.autoreply import watch

    def execute(s, r, c):
        ac = c.douyin.android
        if not ac.thread_name.strip() or not ac.self_name.strip():
            raise config.ConfigError("请配置安卓 thread_name 和 self_name 精确昵称")
        from agent_accounts.adapters.douyin.android.notifications import NotificationTrigger

        bridge = (
            NotificationTrigger(s, ac.thread_name)
            if ac.monitor_trigger == "notification"
            else nullcontext()
        )
        with bridge as trigger:
            return asyncio.run(
                watch(
                    s,
                    r,
                    c,
                    ac.thread_name,
                    ac.self_name,
                    confirmed=confirm_current_thread,
                    generate=generate,
                    allow_send=allow_send,
                    media_enabled=media_enabled,
                    continuous=True,
                    recover_navigation=True,
                    poll_s=ac.monitor_interval_s,
                    trigger=trigger,
                    on_ready=lambda: typer.echo(
                        f"安卓监控已就绪；触发 {ac.monitor_trigger}；"
                        f"补漏 {ac.monitor_interval_s:g} 秒；run_id={r.id}",
                        err=True,
                    ),
                )
            )

    invoke("monitor", execute, allow_external=True)


@app.command("monitor-config")
def monitor_config(
    interval_minutes: float = typer.Option(120, min=1 / 60, max=10080, help="补漏间隔，分钟"),
    trigger: str = typer.Option("notification", help="notification 或 poll"),
):
    """修改持久监控配置；不启动进程、不改变发送权限。运行中修改会停止，须重新启动。"""
    try:
        config.write_android_monitor(interval_minutes * 60, trigger)
    except AgentAccountsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    typer.echo(f"已保存：触发={trigger}，补漏间隔={interval_minutes:g}分钟；重新启动监控后生效。")


@app.command("notification-check")
def notification_check(
    seconds: int = typer.Option(30, min=1, max=300),
    until_update: bool = typer.Option(False, help="收到目标通知后立即结束验证"),
):
    """只检查通知桥并统计目标触发数；不读取私聊、不导航、不调用模型或发送。"""
    from agent_accounts.adapters.douyin.android.notifications import NotificationTrigger

    def execute(s, r, c):
        ac = c.douyin.android
        if not ac.enabled or not ac.udid or not ac.thread_name:
            raise config.ConfigError("请先配置安卓设备及目标昵称")
        with NotificationTrigger(AndroidSession(ac), ac.thread_name) as bridge:
            typer.echo("通知桥已连接；正在等待目标通知（不发送）。", err=True)
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                bridge.check()
                if until_update and bridge.sequence:
                    break
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
            bridge.check()
            return {
                "connected": True,
                "target_updates": bridge.sequence,
                "diagnostics": bridge.stats,
                "sent": False,
            }

    invoke("notification-check", execute, device=False)


@app.command("enter-thread")
def enter_thread(confirm_mutual: bool = typer.Option(False, help="确认配置目标是唯一互关私聊")):
    """按配置昵称返回目标私聊并校验，不调用模型或发送。进入会话会标记已读。"""
    from agent_accounts.adapters.douyin.android.navigation import ensure_thread

    def execute(s, r, c):
        ac = c.douyin.android
        ensure_thread(s, r, ac.thread_name, ac.self_name, confirmed=confirm_mutual)
        return {"run_id": r.id, "status": "thread_verified", "sent": False}

    invoke("enter-thread", execute, allow_external=True)


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
