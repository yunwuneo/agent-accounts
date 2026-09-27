"""M0 Spike：只记录私信页数据的「形状」，不保存任何取值。

- JSON 响应 / WebSocket 文本帧 → 字段名 + 类型 + 字符串长度与字符类别；
- DOM → 标签、class、属性名和文字长度的骨架，用来找选择器；
私信原文、昵称、ID 取值、cookie 都不落盘。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from pathlib import Path

from playwright.async_api import Page, Response, WebSocket

from agent_accounts.adapters.xiaohongshu.netmeta import endpoint_shape

_ID_KEY = re.compile(r"^[0-9a-f]{12,}$|^\d{6,}$", re.I)
_MAX_DEPTH = 12
_MAX_FRAMES = 300


def _str_class(value: str) -> str:
    if value.isdecimal():
        return "digits"
    if re.fullmatch(r"[0-9a-f]+", value, re.I):
        return "hex"
    if re.fullmatch(r"[A-Za-z0-9_\-]+", value):
        return "token"
    if value.startswith(("http://", "https://", "//")):
        return "url"
    return "text"


def shape(value: object, depth: int = 0) -> object:
    """把任意 JSON 值变成只含结构的描述；嵌在字符串里的 JSON 也展开。"""
    if depth > _MAX_DEPTH:
        return "…"
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            out[":id" if _ID_KEY.match(key) else key] = shape(item, depth + 1)
        return out
    if isinstance(value, list):
        if not value:
            return {"list": 0}
        # 合并前几个元素的结构，覆盖不同消息类型
        items = []
        for item in value[:5]:
            s = shape(item, depth + 1)
            if s not in items:
                items.append(s)
        return {"list": len(value), "items": items}
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "{[":
            try:
                return {"json_str": shape(json.loads(stripped), depth + 1)}
            except ValueError:
                pass
        return f"str:{len(value)}:{_str_class(value)}" if value else "str:0"
    if isinstance(value, bool):
        return "bool"
    if value is None:
        return "null"
    return type(value).__name__


class ShapeRecorder:
    def __init__(self) -> None:
        self.http: list[dict[str, object]] = []
        self.frames: list[dict[str, object]] = []
        self.ws_endpoints: list[str] = []
        # 未读数是统计量，按出现顺序记下每次 get_unread 的总数
        self.unread_totals: list[int] = []
        self._tasks: set[asyncio.Task] = set()

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)
        page.on("websocket", self._on_websocket)

    def _on_response(self, response: Response) -> None:
        if response.request.resource_type not in {"xhr", "fetch"}:
            return
        task = asyncio.ensure_future(self._record(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _record(self, response: Response) -> None:
        endpoint = endpoint_shape(response.url)
        if endpoint == "external":
            return
        row: dict[str, object] = {
            "endpoint": endpoint,
            "method": response.request.method,
            "status": response.status,
        }
        mime = response.headers.get("content-type", "")
        if "json" in mime:
            try:
                body = await response.json()
                row["shape"] = shape(body)
                if endpoint.endswith("/get_unread"):
                    counts = (body.get("data") or {}).get("user_chat_unread_counts") or {}
                    self.unread_totals.append(sum(v for v in counts.values() if isinstance(v, int)))
            except Exception as exc:  # noqa: BLE001 — 响应体可能已被回收
                row["shape_error"] = type(exc).__name__
        else:
            row["content_type"] = mime.split(";", 1)[0]
        self.http.append(row)

    def _on_websocket(self, ws: WebSocket) -> None:
        endpoint = endpoint_shape(ws.url)
        self.ws_endpoints.append(endpoint)
        ws.on("framereceived", lambda payload: self._frame(endpoint, "recv", payload))
        ws.on("framesent", lambda payload: self._frame(endpoint, "sent", payload))

    def _frame(self, endpoint: str, direction: str, payload: str | bytes) -> None:
        if len(self.frames) >= _MAX_FRAMES:
            return
        row: dict[str, object] = {"endpoint": endpoint, "dir": direction, "size": len(payload)}
        if isinstance(payload, bytes):
            row["kind"] = "binary"
        else:
            row["kind"] = "text"
            try:
                row["shape"] = shape(json.loads(payload))
            except ValueError:
                row["shape"] = f"str:{len(payload)}:{_str_class(payload)}"
        self.frames.append(row)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def summary(self) -> dict[str, object]:
        return {
            "http": len(self.http),
            "http_endpoints": dict(Counter(str(r["endpoint"]) for r in self.http)),
            "websockets": self.ws_endpoints,
            "unread_totals": self.unread_totals,
            "frames": dict(Counter(f"{f['dir']}:{f['kind']}" for f in self.frames)),
        }

    def save(self, path: Path) -> None:
        with path.open("w", encoding="utf-8") as out:
            for row in self.http:
                out.write(json.dumps({"transport": "http", **row}, ensure_ascii=False) + "\n")
            for row in self.frames:
                out.write(json.dumps({"transport": "ws", **row}, ensure_ascii=False) + "\n")


# 在页面里生成 DOM 骨架：只留标签、class、属性名、文字长度
_SKELETON_JS = """
(maxDepth) => {
  const walk = (el, depth) => {
    if (depth > maxDepth) return null;
    const attrs = [...el.attributes].map(a => a.name).filter(n => n !== 'class' && n !== 'style');
    const own = [...el.childNodes].filter(n => n.nodeType === 3)
      .map(n => n.textContent.trim()).join('');
    const node = {tag: el.tagName.toLowerCase()};
    if (el.classList.length) node.cls = [...el.classList].join('.');
    if (attrs.length) node.attrs = attrs;
    if (own) node.text = own.length;
    const kids = [...el.children].map(c => walk(c, depth + 1)).filter(Boolean);
    if (kids.length) node.kids = kids;
    return node;
  };
  return walk(document.body, 0);
}
"""


async def dom_skeleton(page: Page, max_depth: int = 30) -> object:
    return await page.evaluate(_SKELETON_JS, max_depth)


_NOTE_STATE_JS = """
() => {
  const s = window.__INITIAL_STATE__;
  if (!s || !s.note) return null;
  const seen = new WeakSet();
  const clean = (v, depth) => {
    if (depth > 14) return null;
    if (v && typeof v === 'object') {
      if ('_value' in v && '__v_isRef' in v) v = v._value;
      if (v && typeof v === 'object') {
        if (seen.has(v)) return null;
        seen.add(v);
        if (Array.isArray(v)) return v.slice(0, 20).map(x => clean(x, depth + 1));
        const out = {};
        for (const k of Object.keys(v)) {
          if (k.startsWith('_') || k.startsWith('__v')) continue;
          out[k] = clean(v[k], depth + 1);
        }
        return out;
      }
    }
    return typeof v === 'function' || v === undefined ? null : v;
  };
  return clean(s.note.noteDetailMap, 0);
}
"""

_MEDIA_HOST = re.compile(r"^https?://[^/]*(xhscdn|xiaohongshu)\.[a-z]+/", re.I)


def media_urls(value: object, path: str = "") -> list[tuple[str, str]]:
    """收集笔记数据里的媒体地址，返回 (字段路径, 地址)；只在内存里用，不落盘。"""
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += media_urls(item, f"{path}.{key}" if not _ID_KEY.match(key) else f"{path}.:id")
    elif isinstance(value, list):
        for item in value:
            found += media_urls(item, f"{path}[]")
    elif isinstance(value, str) and _MEDIA_HOST.match(value):
        found.append((path, value))
    return found


async def note_state(page: Page) -> object:
    return await page.evaluate(_NOTE_STATE_JS)
