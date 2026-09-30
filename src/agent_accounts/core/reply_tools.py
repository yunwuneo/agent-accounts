"""回复模型的只读工具（Notion「回复模型工具调用：设计方案」S1：只查本地库）。

回复模型在一次决策里可以按需调用这些工具，查完再给出 ReplyDecision：

- ``get_older_messages``：比已给出的聊天记录更早的消息，每次调用接着往前翻
- ``search_messages``：在本会话的本地记录里按关键词找消息
- ``get_share_detail``：某条分享（S1、S2…）的完整分析结果
- ``get_peer_info``：对方的昵称、关注关系、聊天时长等

约束：
- 会话由代码绑定，工具参数里没有会话 ID；分享只能用本次决策里出现过的编号引用
- 不开浏览器、不调其他模型、不发送；平台相关的查询由适配器实现 ``ChatSource``
- 参数不对、编号不存在时作为错误结果交回模型；其他异常不接住，直接中止这次决策
- 审计只记工具名、参数摘要、结果字数和耗时，不记内容
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from agent_accounts.core import digests
from agent_accounts.core.llm import ToolOutput, ToolUse
from agent_accounts.core.reply import ChatLine, render_lines

MAX_RESULT_CHARS = 6000
MAX_OLDER = 30
MAX_SEARCH = 10
MAX_QUERY_LEN = 50


class ToolError(Exception):
    """参数不对、编号不存在等：作为 is_error 结果交回模型，让它换个做法。"""


class RefBook:
    """分享的短编号（S1、S2…）↔ 作品 / 笔记 ID。只有出现在本次决策里的分享才有编号。"""

    def __init__(self) -> None:
        self._by_item: dict[str, str] = {}
        self._by_ref: dict[str, str] = {}

    def share(self, item_id: str) -> str:
        if item_id not in self._by_item:
            ref = f"S{len(self._by_item) + 1}"
            self._by_item[item_id] = ref
            self._by_ref[ref] = item_id
        return self._by_item[item_id]

    def resolve(self, ref: str) -> str:
        item_id = self._by_ref.get(ref.strip().upper())
        if item_id is None:
            known = "、".join(self._by_ref) or "（没有）"
            raise ToolError(f"没有编号为 {ref} 的分享；可用的编号：{known}")
        return item_id


class ChatSource(Protocol):
    """适配器提供的本会话查询。渲染出的内容里的分享要通过同一个 RefBook 编号。"""

    platform: str

    def older(self, limit: int) -> list[ChatLine]:
        """比已经给模型看过的更早的消息（按时间先后），每次调用接着往前翻；翻到头返回空。"""
        ...

    def search(self, query: str, limit: int) -> list[ChatLine]:
        """本会话本地记录里文字或分享标题包含 query 的消息（按时间先后，取最近的 limit 条）。"""
        ...

    def peer_info(self) -> dict[str, Any]:
        """对方的基本情况（不含用户 ID 这类标识）。"""
        ...


@dataclass
class ToolCall:
    """一次工具调用的摘要（存进回复记录、写审计；不含查询词和结果内容）。"""

    name: str
    args: dict[str, Any]
    ok: bool
    chars: int
    ms: int

    def describe(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in self.args.items())
        result = f"{self.chars} 字" if self.ok else "出错"
        return f"{self.name}({args}) → {result}"


def describe_calls(tool_calls_json: str | None) -> list[str]:
    """回复记录里的 tool_calls_json → 每次调用一行（CLI 展示用）。"""
    return [ToolCall(**c).describe() for c in json.loads(tool_calls_json or "[]")]


def _limit(args: dict[str, Any], upper: int) -> int:
    value = args.get("limit", upper)
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"limit 应该是 1–{upper} 的整数") from None
    return max(1, min(n, upper))


def _query(args: dict[str, Any]) -> str:
    q = args.get("query")
    if not isinstance(q, str) or not q.strip():
        raise ToolError("query 不能为空")
    return q.strip()[:MAX_QUERY_LEN]


def _ref(args: dict[str, Any]) -> str:
    ref = args.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        raise ToolError("ref 应该是分享编号，如 S1")
    return ref


def _clip(text: str | None, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…（后面省略）"


def share_detail(platform: str, item_id: str) -> str:
    d = digests.get(platform, item_id)
    if d is None:
        return "这条分享的内容还没有分析过（或分析失败），只有聊天记录里的标题和作者。"
    parts = []
    if not d.available:
        parts.append(f"（作品当前不可看，只根据分享卡片理解：{d.filter_reason or '原因未知'}）")
    parts.append(f"类型：{'视频' if d.kind == 'video' else '图文'}")
    if d.title:
        parts.append(f"标题：{_clip(d.title, 200)}")
    if d.author:
        parts.append(f"作者：{d.author}")
    if d.body:
        parts.append(f"正文：{_clip(d.body, 1500)}")
    if d.hashtags:
        parts.append("话题：" + " ".join(f"#{t}" for t in d.hashtags))
    if d.duration_s:
        parts.append(f"时长：{d.duration_s:.0f} 秒")
    if d.music_title:
        parts.append(f"音乐：{d.music_title}")
    parts.append(f"内容摘要：{d.summary}")
    parts.append(f"氛围：{d.vibe}")
    parts.append("可以聊的点：" + "；".join(d.reply_hooks))
    if d.transcript:
        parts.append(f"语音转写：{_clip(d.transcript, 2000)}")
    if d.notes:
        parts.append("说明：" + "；".join(d.notes))
    return "\n".join(parts)


def _definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "get_older_messages",
            "description": (
                "查看这个会话里比上面聊天记录更早的消息，每次调用会接着往前翻。"
                "只在需要更早的上下文时用，比如对方提到以前聊过的事或以前发过的分享。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": f"条数，1–{MAX_OLDER}"},
                },
                "required": ["limit"],
                "additionalProperties": False,
            },
        },
        {
            "name": "search_messages",
            "description": "在这个会话的全部本地聊天记录里按关键词查找消息（匹配文字和分享标题）。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "关键词，越短越容易匹配"},
                    "limit": {"type": "integer", "description": f"最多返回几条，1–{MAX_SEARCH}"},
                },
                "required": ["query", "limit"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_share_detail",
            "description": (
                "查看某条分享（聊天记录里标了 S1、S2 这类编号的视频或笔记）的完整分析："
                "正文、语音转写、摘要、可以聊的点等。想接住作品里的具体细节时用。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {"ref": {"type": "string", "description": "分享编号，如 S1"}},
                "required": ["ref"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_peer_info",
            "description": (
                "查看对方的基本情况：昵称、关注关系、第一次聊天的时间、本地记录里的消息数、"
                "你最近几次给对方发消息的时间。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    ]


@dataclass
class ReplyToolbox:
    """一次回复决策用的工具箱：绑定一个会话，记录调用过程。"""

    source: ChatSource
    refs: RefBook
    conv_id: str
    max_rounds: int = 4
    audit: Callable[..., None] | None = None
    calls: list[ToolCall] = field(default_factory=list)

    def definitions(self, *, strict: bool) -> list[dict[str, Any]]:
        defs = _definitions()
        if strict:  # 代理不支持结构化输出时一般也不认 strict，跟着 structured_output 走
            for d in defs:
                d["strict"] = True
        return defs

    def calls_json(self) -> str:
        return json.dumps([asdict(c) for c in self.calls], ensure_ascii=False)

    async def execute(self, uses: list[ToolUse]) -> list[ToolOutput]:
        return [self._run(use) for use in uses]

    def _run(self, use: ToolUse) -> ToolOutput:
        start = time.monotonic()
        summary: dict[str, Any] = {}
        try:
            text, summary = self._dispatch(use.name, use.input)
            out = ToolOutput(_clip(text, MAX_RESULT_CHARS))
        except ToolError as e:
            out = ToolOutput(str(e), is_error=True)
        call = ToolCall(
            name=use.name,
            args=summary,
            ok=not out.is_error,
            chars=len(out.content),
            ms=int((time.monotonic() - start) * 1000),
        )
        self.calls.append(call)
        if self.audit:
            self.audit(
                f"{self.source.platform}.reply.tool",
                conv=self.conv_id,
                tool=call.name,
                args=call.args,
                ok=call.ok,
                chars=call.chars,
                ms=call.ms,
            )
        return out

    def _dispatch(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if name == "get_older_messages":
            n = _limit(args, MAX_OLDER)
            lines = self.source.older(n)
            text = render_lines(lines) if lines else "没有更早的消息了。"
            return text, {"limit": n, "got": len(lines)}
        if name == "search_messages":
            q, n = _query(args), _limit(args, MAX_SEARCH)
            lines = self.source.search(q, n)
            text = render_lines(lines) if lines else "没有找到包含这个关键词的消息。"
            return text, {"query_len": len(q), "limit": n, "got": len(lines)}
        if name == "get_share_detail":
            ref = _ref(args)
            return share_detail(self.source.platform, self.refs.resolve(ref)), {"ref": ref}
        if name == "get_peer_info":
            info = self.source.peer_info()
            return "\n".join(f"{k}：{v}" for k, v in info.items()), {}
        raise ToolError(f"没有叫 {name} 的工具")
