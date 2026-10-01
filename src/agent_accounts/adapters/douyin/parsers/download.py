"""受限的无凭据媒体下载：域名、重定向、大小、超时与文件类型均校验。"""

from __future__ import annotations

import ipaddress
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from agent_accounts.core.config import ConfigError


class ParseError(ConfigError):
    pass


MEDIA_DOMAINS = ("douyinvod.com", "douyinpic.com", "byteimg.com")


def share_url(value: str) -> str:
    """只提交必要的作品链接，不把带私信/评论的整段文本交给第三方。"""
    try:
        u = urlsplit(value.strip())
        port = u.port
    except ValueError:
        raise ParseError("作品链接格式无效") from None
    if (
        u.scheme != "https"
        or u.username
        or u.password
        or u.fragment
        or u.hostname not in {"v.douyin.com", "www.douyin.com", "douyin.com"}
        or port not in (None, 443)
    ):
        raise ParseError("请提供单个 HTTPS 抖音作品分享链接，不要粘贴整段聊天或分享文案")
    import re

    pattern = r"/[A-Za-z0-9_-]+/?" if u.hostname == "v.douyin.com" else r"/(video|note)/\d+/?"
    if not re.fullmatch(pattern, u.path) or any(c.isspace() for c in value):
        raise ParseError("不是支持的作品链接格式")
    # 分享参数可能含分享者标识；必要定位信息已在路径中。
    return f"https://{u.hostname}{u.path}"


def media_url(value: str, *, resolve: bool = True) -> str:
    try:
        u = urlsplit(value)
        valid = (
            u.scheme == "https"
            and not u.username
            and not u.password
            and not u.fragment
            and u.port in (None, 443)
            and any(u.hostname == d or (u.hostname or "").endswith("." + d) for d in MEDIA_DOMAINS)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ParseError("解析结果媒体域名或协议不在已允许范围，未下载")
    if resolve:
        try:
            addresses = socket.getaddrinfo(u.hostname, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise ParseError("媒体域名没有解析到公开地址")
        except OSError:
            raise ParseError("媒体域名解析失败") from None
    return value


class CheckedRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        media_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, dest: Path, kind: str, *, max_bytes: int, timeout_s: float) -> Path:
    if max_bytes <= 0 or timeout_s <= 0:
        raise ParseError("下载大小和时限必须大于零")
    media_url(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), CheckedRedirect())
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    partial = dest.with_suffix(".part")
    start = time.monotonic()
    try:
        with opener.open(req, timeout=timeout_s) as response:
            media_url(response.url)
            if response.status != 200:
                raise ParseError("媒体服务器未返回完整文件")
            content_type = response.headers.get_content_type()
            allowed = {"application/octet-stream"}
            allowed |= (
                {"video/mp4"} if kind == "video" else {"image/png", "image/jpeg", "image/webp"}
            )
            if content_type not in allowed:
                raise ParseError("下载响应不是预期媒体类型")
            size = response.headers.get("Content-Length")
            expected = int(size) if size is not None else None
            if expected is not None and not 0 < expected <= max_bytes:
                raise ParseError("媒体文件超过大小限制或为空")
            total = 0
            with partial.open("xb") as output:
                while chunk := response.read(256 * 1024):
                    total += len(chunk)
                    if total > max_bytes or time.monotonic() - start > timeout_s:
                        raise ParseError("媒体下载超过大小或时间限制")
                    output.write(chunk)
            if not total or (expected is not None and total != expected):
                raise ParseError("媒体下载长度不完整")
        partial.replace(dest)
        return dest
    except (OSError, ValueError, urllib.error.URLError):
        raise ParseError("媒体下载失败（未重试，地址与响应正文不写入日志）") from None
    finally:
        partial.unlink(missing_ok=True)
