"""MCP server（stdio）：给 Echo 用的读写接口。

- 读：最近的私信往来。只读本地库，不开浏览器、不点进会话、不标记已读；
  数据由 ``douyin sync`` / ``douyin run`` 写入，这里拿到的是最近一次同步时的内容
- 写：更新人设和近况。两者都会在生成回复时用上；覆盖前旧版本存到 ``history/``，
  每次更新记审计（只记字数，不记内容）

发消息不在这里暴露：发送仍然只走 ``douyin run`` 的两道确认和 ``douyin reply``。

启动：
- ``agent-accounts mcp``：stdio，由 MCP 客户端按需拉起（stdout 是协议通道，这里不能 print）
- ``agent-accounts mcp --http``：Streamable HTTP，监听 ``[mcp] host:port/path``，
  每个请求都要带 ``Authorization: Bearer <token>``（token 用 ``agent-accounts mcp-token`` 生成）
"""

from __future__ import annotations

import functools
import hmac
import ipaddress
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from agent_accounts.adapters.douyin import PLATFORM
from agent_accounts.adapters.douyin import store as dstore
from agent_accounts.core import audit, digests, persona
from agent_accounts.core.config import ConfigError, McpConfig

MAX_LIMIT = 200

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)

server = MCPServer(
    name="agent-accounts",
    instructions=(
        "Echo 自己的平台账号。可以读最近的抖音私信往来（本地库，读取没有副作用），"
        "以及读取和更新人设、近况（自动回复时会用上）。不能发送消息。"
    ),
)


# 只记方法、工具名、状态和耗时，不记参数和返回内容（私信、人设）
log = logging.getLogger("agent_accounts.mcp")


def _logged[F: Callable[..., Any]](fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            log.warning("工具 %s 出错（%s），%.2fs", fn.__name__, type(e).__name__, _since(start))
            raise
        log.info("工具 %s 完成，%.2fs", fn.__name__, _since(start))
        return result

    return wrapper  # type: ignore[return-value]


def _since(start: float) -> float:
    return time.monotonic() - start


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _limit(n: int) -> int:
    return max(1, min(n, MAX_LIMIT))


def _conv_json(c: dstore.DouyinConversation) -> dict[str, Any]:
    return {
        "conv_id": c.conv_id,
        "name": c.name,
        "kind": c.kind,
        "mutual": c.is_mutual,
        "unread": c.unread,
        "last_at": _iso(c.last_at),
        "last_preview": c.last_preview,
    }


def _msg_json(m: dstore.DouyinMessage, names: dict[str, str | None]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "conv_id": m.conv_id,
        "conversation": names.get(m.conv_id),
        "from_me": m.from_me,
        "sent_at": _iso(m.sent_at),
        "type": m.type,
        "text": m.text if m.text else m.preview(max_len=200),
    }
    if m.aweme_id:
        out["share"] = {"title": m.share_title, "author": m.share_author}
        if d := digests.get(PLATFORM, m.aweme_id):
            out["share"]["digest"] = {"summary": d.summary, "vibe": d.vibe}
    return out


@server.tool(annotations=READ)
@_logged
def douyin_list_conversations(limit: int = 20) -> list[dict[str, Any]]:
    """列出抖音私信会话（最近活跃的在前）：对方昵称、是否互关、未读数、最后一条预览。"""
    return [_conv_json(c) for c in dstore.list_conversations()[: _limit(limit)]]


@server.tool(annotations=READ)
@_logged
def douyin_android_read_snapshot(run_id: str) -> dict[str, Any]:
    """读取指定安卓运行的本地界面快照；无手机操作，不是完整历史或稳定消息 ID。"""
    import json
    import re

    from agent_accounts.core import paths, store

    if not re.fullmatch(r"\d{8}-\d{6}-[a-f0-9]{4}", run_id):
        raise ValueError("无效运行编号")
    with store.session() as db:
        run = db.get(store.Run, run_id)
        if not run or run.platform != "douyin" or run.command != "android.snapshot":
            raise ValueError("不是安卓会话快照运行")
        if run.status != "ok":
            raise ValueError("快照运行未成功")
    return json.loads((paths.runs_dir() / run_id / "thread.json").read_text("utf-8"))


@server.tool(annotations=READ)
@_logged
def douyin_recent_messages(conversation: str | None = None, limit: int = 20) -> dict[str, Any]:
    """读取最近的抖音私信往来（按时间先后排列，from_me=true 是自己发的）。

    conversation 可以是 conv_id 或对方昵称（昵称包含匹配，必须唯一）；不传就返回所有会话里
    最近的消息。分享的视频/图集如果分析过，会附上作品摘要。数据来自最近一次同步。
    """
    names = {c.conv_id: c.name for c in dstore.list_conversations()}
    if conversation:
        conv = dstore.find_conversation(conversation)
        if conv is None:
            raise ValueError(f"找不到会话或匹配不唯一：{conversation}")
        msgs = dstore.list_messages(conv.conv_id, limit=_limit(limit))
    else:
        msgs = dstore.recent_messages(limit=_limit(limit))
    return {"messages": [_msg_json(m, names) for m in msgs]}


@server.tool(annotations=READ)
@_logged
def get_persona() -> dict[str, Any]:
    """读取当前人设（生成回复时作为 system prompt 的开头）。"""
    return {"persona": persona.load()}


@server.tool(annotations=WRITE)
@_logged
def update_persona(content: str) -> dict[str, Any]:
    """用 content 整体替换人设（Markdown，不能为空，最多 8000 字）。旧版本会自动备份。

    先用 get_persona 读出当前内容，在它的基础上修改，而不是只写要改的那一句。
    """
    persona.save(content)
    audit.record("core", "persona.update", chars=len(content.strip()), via="mcp")
    return {"ok": True, "chars": len(content.strip())}


@server.tool(annotations=READ)
@_logged
def get_recent() -> dict[str, Any]:
    """读取当前的近况（最近在做什么、心情、发生了什么），以及上次更新时间。"""
    return {"recent": persona.load_recent(), "updated_at": _iso(persona.recent_updated_at())}


@server.tool(annotations=WRITE)
@_logged
def update_recent(content: str) -> dict[str, Any]:
    """用 content 整体替换近况（最多 2000 字；空字符串表示清空）。旧版本会自动备份。

    近况会和人设一起用于生成回复，聊天时可能被自然提到，所以只写可以对朋友说的内容，
    不要写密码、联系方式等隐私。
    """
    persona.save_recent(content)
    audit.record("core", "recent.update", chars=len(content.strip()), via="mcp")
    return {"ok": True, "chars": len(content.strip())}


def main() -> None:
    server.run("stdio")


class AccessLog:
    """ASGI 中间件：每个 HTTP 请求结束时记一行（来源、方法、路径、状态、耗时）。

    SSE 推送流（GET）会一直开着，开始时也记一行，便于判断客户端是否连上。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start, status = time.monotonic(), None
        client = scope.get("client") or ("?", 0)
        headers = dict(scope.get("headers") or [])
        # 经 nginx 转发时真实来源在 X-Forwarded-For / X-Real-IP 里
        origin = (headers.get(b"x-forwarded-for") or headers.get(b"x-real-ip") or b"").decode()
        who = origin.split(",")[0].strip() or client[0]
        line = f"{who} {scope['method']} {scope['path']}"

        async def send_logged(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                if scope["method"] == "GET" and status == 200:
                    log.info("%s → %s（推送流已打开）", line, status)
            await send(message)

        try:
            await self.app(scope, receive, send_logged)
        finally:
            log.info("%s → %s，%.2fs", line, status or "未响应", _since(start))


class BearerAuth:
    """ASGI 中间件：HTTP 请求必须带正确的 ``Authorization: Bearer <token>``，否则 401。

    lifespan 等非 HTTP 事件直接放行。比较用 hmac.compare_digest，防计时攻击。
    """

    def __init__(self, app, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            got = headers.get(b"authorization", b"")
            if not hmac.compare_digest(got, self._expected):
                await _unauthorized(send)
                return
        await self.app(scope, receive, send)


async def _unauthorized(send) -> None:
    body = b'{"error": "unauthorized"}'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer realm="agent-accounts"'),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]


def transport_security(cfg: McpConfig) -> TransportSecuritySettings | None:
    """DNS rebinding 防护：只接受 Host 在白名单里的请求。

    没配 allowed_hosts 时用 SDK 默认（监听本机时只认 127.0.0.1 / localhost）。
    配了就在本机地址之外再放行这些对外域名（反向代理 / 内网穿透场景），防护仍然开启。
    """
    if not cfg.allowed_hosts:
        return None
    hosts, origins = list(LOOPBACK_HOSTS), [f"http://{h}" for h in LOOPBACK_HOSTS]
    for h in cfg.allowed_hosts:
        hosts += [h, f"{h}:*"]
        origins += [f"https://{h}", f"http://{h}", f"https://{h}:*", f"http://{h}:*"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def http_app(cfg: McpConfig):
    token = cfg.bearer()
    if not token:
        raise ConfigError("HTTP MCP 需要 Bearer token：先运行 agent-accounts mcp-token")
    app = server.streamable_http_app(
        streamable_http_path=cfg.path, host=cfg.host, transport_security=transport_security(cfg)
    )
    return AccessLog(BearerAuth(app, token))


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main_http(cfg: McpConfig) -> None:
    import uvicorn

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    app = http_app(cfg)
    url = f"http://{cfg.host}:{cfg.port}{cfg.path}"
    print(f"MCP HTTP server：{url}（Bearer 鉴权）", file=sys.stderr)
    if not _is_loopback(cfg.host):
        print(
            "⚠️ 监听的不是本机地址：流量是明文 HTTP，token 和私信内容会在网络上以明文传输。"
            "只在可信内网使用，或放在 HTTPS 反向代理 / Tailscale 等加密通道后面。",
            file=sys.stderr,
        )
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")
