"""小红书付费回复工具：本会话私信图片理解与分享补分析。"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError
from sqlmodel import col, select

from agent_accounts.adapters.xiaohongshu import PLATFORM
from agent_accounts.adapters.xiaohongshu import digest as xdigest
from agent_accounts.adapters.xiaohongshu import store as xstore
from agent_accounts.adapters.xiaohongshu.doctor import detect_captcha, login_visible
from agent_accounts.browser.session import BrowserSession
from agent_accounts.core import digests, image_descriptions, media, store
from agent_accounts.core.config import Config, ConfigError
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.reply_tools import ToolError, share_detail
from agent_accounts.core.run import RunContext
from agent_accounts.core.understand import UnderstandError

MAX_IMAGE_BYTES = 20 * 1024 * 1024


async def _check_session(s: BrowserSession) -> None:
    if await detect_captcha(s.page):
        raise HumanRequired("媒体分析期间触发小红书验证或风控", freeze=True)
    if await login_visible(s.page):
        raise HumanRequired("媒体分析期间小红书登录失效", freeze=True)


async def download_image(s: BrowserSession, url: str, dest: Path) -> Path:
    """地址只来自已同步消息；拒绝重定向、非图片和超限内容，不输出 URL/响应原文。"""
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ToolError("图片地址不可用")
    await _check_session(s)
    response = await s.context.request.get(
        url,
        headers={"Referer": "https://www.xiaohongshu.com/"},
        timeout=60_000,
        max_redirects=0,
    )
    try:
        await _check_session(s)
        location = response.headers.get("location", "")
        if re.search(r"captcha|verify|login|security", location, re.I):
            raise HumanRequired("图片下载要求登录或安全验证", freeze=True)
        if response.status in (401, 403, 461, 471):
            raise HumanRequired("图片下载被平台拒绝，请人工检查登录或风控状态", freeze=True)
        if not response.ok:
            raise ToolError(f"图片下载失败（HTTP {response.status}）")
        kind = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
        if kind not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            body = (await response.body())[:65536].decode("utf-8", errors="replace")
            if re.search(
                r"captcha|请完成验证|安全验证|访问异常|操作频繁|账号异常|请先登录", body, re.I
            ):
                raise HumanRequired("图片下载返回登录或安全验证提示", freeze=True)
            raise ToolError("图片地址没有返回支持的图片格式")
        size = response.headers.get("content-length", "")
        if size.isdigit() and int(size) > MAX_IMAGE_BYTES:
            raise ToolError("图片超过 20 MB，暂不处理")
        data = await response.body()
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ToolError("图片为空或超过 20 MB，暂不处理")
        # 防止把伪装成图片的文本交给媒体解码器。
        valid = (
            data.startswith(b"\xff\xd8\xff")
            or data.startswith(b"\x89PNG\r\n\x1a\n")
            or data.startswith((b"GIF87a", b"GIF89a"))
            or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
        )
        if not valid:
            raise ToolError("下载内容不是可识别的图片")
        dest.write_bytes(data)
        return dest
    finally:
        # 清理响应失败不能遮住 HumanRequired，验证码必须仍能传到冻结逻辑。
        with suppress(BrowserError):
            await response.dispose()


class XhsImageViewer:
    def __init__(self, cfg: Config, peer_id: str, session: BrowserSession | None):
        self.cfg, self.peer_id, self.session = cfg, peer_id, session

    @property
    def available(self) -> bool:
        return self.session is not None

    def _message(self, message_id: str) -> xstore.XhsMessage:
        with store.session() as db:
            msg = db.get(xstore.XhsMessage, message_id)
        if (
            msg is None
            or msg.peer_id != self.peer_id
            or msg.type != "image"
            or msg.revoked
            or msg.from_me
        ):
            raise ToolError("图片不属于当前会话的有效对方消息")
        return msg

    def cached(self, message_id: str) -> str | None:
        self._message(message_id)  # 撤回及会话检查优先于缓存
        row = image_descriptions.get(PLATFORM, message_id)
        return row.render() if row else None

    async def analyze(self, message_id: str, on_model: Callable[[], None]) -> str:
        msg = self._message(message_id)
        if self.session is None:
            raise ToolError("当前没有浏览器，图片理解暂不可用")
        if not msg.image_url:
            raise ToolError("本地消息没有图片地址")
        try:
            self.cfg.llm.understand.require_key("llm.understand")
            with tempfile.TemporaryDirectory(prefix="aa-image-") as tmp_dir:
                tmp = Path(tmp_dir)
                raw = await download_image(self.session, msg.image_url, tmp / "image.raw")
                jpeg = await media.to_jpeg(raw, tmp / "image.jpg", max_side=1600)
                await _check_session(self.session)
                self._message(message_id)
                on_model()
                result = await image_descriptions.describe(self.cfg.llm.understand, jpeg)
            await _check_session(self.session)
            self._message(message_id)
        except HumanRequired:
            raise
        except (BrowserError, UnderstandError, media.MediaError, ConfigError, OSError) as exc:
            # 不带异常原文：浏览器错误可能包含签名 URL，解码器错误可能含图像元数据。
            raise ToolError(f"图片理解失败（{type(exc).__name__}），请根据已有信息决定") from None
        return image_descriptions.save(
            PLATFORM, message_id, self.cfg.llm.understand.model, result
        ).render()


class XhsShareAnalyzer:
    def __init__(self, cfg: Config, peer_id: str, session: BrowserSession | None, run: RunContext):
        self.cfg, self.peer_id, self.session, self.run = cfg, peer_id, session, run

    @property
    def available(self) -> bool:
        return self.session is not None

    def _message(self, item_id: str) -> xstore.XhsMessage:
        with store.session() as db:
            msg = db.exec(
                select(xstore.XhsMessage)
                .where(
                    xstore.XhsMessage.peer_id == self.peer_id,
                    xstore.XhsMessage.note_id == item_id,
                    xstore.XhsMessage.type == "note",
                    col(xstore.XhsMessage.revoked).is_(False),
                )
                .order_by(col(xstore.XhsMessage.store_id).desc())
            ).first()
        if msg is None:
            raise ToolError("当前会话没有这条未撤回的笔记分享")
        return msg

    def cached(self, item_id: str) -> str | None:
        self._message(item_id)
        return share_detail(PLATFORM, item_id) if digests.get(PLATFORM, item_id) else None

    async def analyze(self, item_id: str, on_model: Callable[[], None]) -> str:
        msg = self._message(item_id)
        if self.session is None:
            raise ToolError("当前没有浏览器，分享补分析暂不可用")
        try:
            await _check_session(self.session)
            out = await xdigest._digest_one(
                self.session,
                self.cfg,
                self.run,
                item_id,
                message=msg,
                on_model=on_model,
            )
            await _check_session(self.session)
        except BrowserError as exc:
            raise ToolError(f"分享补分析失败（{type(exc).__name__}）") from None
        self._message(item_id)
        if out.error or out.digest is None:
            raise ToolError("分享补分析未成功，请根据已有信息决定")
        return share_detail(PLATFORM, item_id)
