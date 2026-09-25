"""网络录制：记录页面的 XHR/fetch 响应和 WebSocket 帧，用于分析平台接口（Spike-2）。

- 只记录 xhr/fetch 和 WebSocket，不记录请求头（cookie 在请求头里）。
- URL 里 token、签名类查询参数的值一律替换成 ``***``。
- 响应体和帧原样保存到 ``out_dir``（位于 ``~/.agent-accounts/runs/``，0700），可能含私信内容，
  不得提交到仓库；要做 fixture 必须先脱敏。
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from playwright.async_api import Page, Response, WebSocket

from agent_accounts.core import paths

_SENSITIVE_QUERY = re.compile(
    r"token|sign|bogus|verify|ticket|session|cookie|webid|uid|fp$|device|install|secret|key", re.I
)
_EXT = {"json": "json", "protobuf": "pb", "octet-stream": "bin", "text": "txt", "html": "html"}


def scrub_url(url: str) -> str:
    parts = urlsplit(url)
    query = [
        f"{k}={'***' if _SENSITIVE_QUERY.search(k) else v[:40]}"
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return f"{parts.scheme}://{parts.netloc}{parts.path}" + (f"?{'&'.join(query)}" if query else "")


def endpoint(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}"


def _ext(content_type: str) -> str:
    return next((ext for key, ext in _EXT.items() if key in content_type), "bin")


class NetRecorder:
    def __init__(self, out_dir: Path, max_body_bytes: int = 4 * 1024 * 1024):
        self.out_dir = paths.ensure_dir(out_dir)
        self.max_body_bytes = max_body_bytes
        self.http: list[dict[str, Any]] = []
        self.ws: list[dict[str, Any]] = []
        self.frames: list[dict[str, Any]] = []
        self.markers: list[dict[str, Any]] = []
        self._t0 = time.monotonic()

    def _ts(self) -> float:
        return round(time.monotonic() - self._t0, 3)

    def mark(self, name: str) -> None:
        """记录一个时间点（如「面板已打开」），方便把请求和页面动作对上。"""
        self.markers.append({"ts": self._ts(), "name": name})

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)
        page.on("websocket", self._on_websocket)

    async def _on_response(self, response: Response) -> None:
        request = response.request
        if request.resource_type not in ("xhr", "fetch"):
            return
        seq = len(self.http)
        content_type = response.headers.get("content-type", "")
        row: dict[str, Any] = {
            "seq": seq,
            "ts": self._ts(),
            "method": request.method,
            "url": scrub_url(response.url),
            "status": response.status,
            "content_type": content_type,
        }
        self.http.append(row)
        try:
            body = await response.body()
        except Exception as e:  # 页面跳转后 body 可能已不可读
            row["error"] = type(e).__name__
            return
        row["size"] = len(body)
        if body and len(body) <= self.max_body_bytes:
            name = f"http/{seq:04d}.{_ext(content_type)}"
            path = paths.ensure_dir(self.out_dir / "http") / name.split("/")[1]
            path.write_bytes(body)
            row["file"] = name

    def _on_websocket(self, ws: WebSocket) -> None:
        idx = len(self.ws)
        self.ws.append({"idx": idx, "ts": self._ts(), "url": scrub_url(ws.url)})
        ws.on("framesent", lambda payload: self._on_frame(idx, "sent", payload))
        ws.on("framereceived", lambda payload: self._on_frame(idx, "recv", payload))
        ws.on("close", lambda _ws: self.ws[idx].update(closed_at=self._ts()))

    def _on_frame(self, idx: int, direction: str, payload: str | bytes) -> None:
        seq = len(self.frames)
        data = payload.encode() if isinstance(payload, str) else payload
        name = f"ws/{idx}-{seq:05d}-{direction}.{'txt' if isinstance(payload, str) else 'bin'}"
        (paths.ensure_dir(self.out_dir / "ws") / name.split("/")[1]).write_bytes(data)
        self.frames.append(
            {
                "seq": seq,
                "ts": self._ts(),
                "ws": idx,
                "dir": direction,
                "kind": "text" if isinstance(payload, str) else "binary",
                "size": len(data),
                "file": name,
            }
        )

    def save_index(self) -> None:
        for name, rows in (
            ("http", self.http),
            ("ws", self.ws),
            ("frames", self.frames),
            ("markers", self.markers),
        ):
            with (self.out_dir / f"{name}.jsonl").open("w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def summary(self) -> dict[str, Any]:
        by_endpoint: dict[str, Counter[str]] = defaultdict(Counter)
        for row in self.http:
            by_endpoint[endpoint(row["url"])][row["content_type"].split(";")[0] or "?"] += 1
        frames: dict[int, Counter[str]] = defaultdict(Counter)
        for fr in self.frames:
            frames[fr["ws"]][f"{fr['dir']}:{fr['kind']}"] += 1
        return {
            "http": {ep: dict(c) for ep, c in sorted(by_endpoint.items())},
            "ws": [{**w, "frames": dict(frames[w["idx"]])} for w in self.ws],
        }
