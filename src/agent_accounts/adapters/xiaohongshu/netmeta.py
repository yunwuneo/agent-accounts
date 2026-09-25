"""私信 Spike 的网络元数据；不读取或保存响应体、帧和查询参数。"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Page, Response, WebSocket

_STATIC_SEGMENTS = frozenset(
    {
        "api",
        "chat",
        "conversation",
        "get",
        "im",
        "list",
        "message",
        "messages",
        "sns",
        "v1",
        "v2",
        "web",
    }
)
_SAFE_HOST = re.compile(r"(?:[a-z0-9-]+\.)*xiaohongshu\.com", re.I)
_SAFE_MIME = re.compile(r"[a-z0-9.+-]+/[a-z0-9.+-]+", re.I)


def endpoint_shape(url: str) -> str:
    """只保留平台域名和已知静态路径段；动态 ID、查询串全部丢弃。"""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not _SAFE_HOST.fullmatch(host):
        return "external"
    segments = [segment for segment in parts.path.split("/") if segment]
    path = "/".join(segment if segment in _STATIC_SEGMENTS else ":id" for segment in segments[:8])
    return f"xiaohongshu.com/{path}" if path else "xiaohongshu.com"


class MetadataRecorder:
    def __init__(self) -> None:
        self.http: list[dict[str, str | int | None]] = []
        self.websockets: list[str] = []

    def attach(self, page: Page) -> None:
        page.on("response", self.on_response)
        page.on("websocket", self.on_websocket)

    def on_response(self, response: Response) -> None:
        request = response.request
        if request.resource_type not in {"xhr", "fetch"}:
            return
        headers = response.headers
        raw_mime = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        raw_size = headers.get("content-length", "")
        self.http.append(
            {
                "endpoint": endpoint_shape(response.url),
                "method": request.method if request.method in {"GET", "POST"} else "OTHER",
                "status": response.status,
                "content_type": raw_mime if _SAFE_MIME.fullmatch(raw_mime) else "unknown",
                "size": int(raw_size) if raw_size.isdecimal() else None,
            }
        )

    def on_websocket(self, websocket: WebSocket) -> None:
        # 不订阅 framesent/framereceived，帧可能包含私信或凭据。
        self.websockets.append(endpoint_shape(websocket.url))

    def summary(self) -> dict[str, object]:
        return {
            "responses": len(self.http),
            "websockets": len(self.websockets),
            "endpoints": dict(Counter(row["endpoint"] for row in self.http)),
        }

    def save(self, path: Path) -> None:
        with path.open("w", encoding="utf-8") as output:
            for row in self.http:
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
