"""回复模型工具（Notion「回复模型工具调用：设计方案」S1 本地查询 + S2 图片理解 + S3 分享补分析）。

回复模型在一次决策里可以按需调用这些工具，查完再给出 ReplyDecision：

- ``get_older_messages``：比已给出的聊天记录更早的消息，每次调用接着往前翻
- ``search_messages``：在本会话的本地记录里按关键词找消息
- ``get_share_detail``：某条分享（S1、S2…）的完整分析结果
- ``get_peer_info``：对方的昵称、关注关系、聊天时长等
- ``view_image``：显式启用后，查私信图片缓存或调用理解模型
- ``analyze_share``：显式启用后，复用平台 digest 补分析当前会话里的分享

约束：
- 会话由代码绑定，工具参数里没有会话 ID；分享只能用本次决策里出现过的编号引用
- 本地查询由适配器实现 ``ChatSource``；媒体工具复用已有浏览器，受付费预算限制，不发送
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

    def __init__(self, *, images_enabled: bool = False) -> None:
        self.images_enabled = images_enabled
        self._by_item: dict[str, str] = {}
        self._by_ref: dict[str, str] = {}
        self._images: dict[str, str] = {}

    def image(self, message_id: str) -> str:
        if message_id not in self._images:
            self._images[message_id] = f"I{len(self._images) + 1}"
        return self._images[message_id]

    def resolve_image(self, ref: str) -> str:
        for message_id, known in self._images.items():
            if known == ref.strip().upper():
                return message_id
        raise ToolError("图片编号不存在，请使用聊天记录中的 I 编号")

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
    paid: bool = False  # 是否已经开始调用理解或转写模型（失败也算）
    cached: bool = False

    def describe(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in self.args.items())
        result = f"{self.chars} 字" if self.ok else "出错"
        extra = "（缓存）" if self.cached else ("（已调用媒体模型）" if self.paid else "")
        return f"{self.name}({args}) → {result}{extra}"


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
class MediaBudget:
    """本轮剩余媒体处理次数，由自动分享分析、图片工具和补分析工具共用；失败不退还。"""

    remaining: int
    image_attempts: int = 0
    share_attempts: int = 0
    attempted: set[tuple[str, str]] = field(default_factory=set)


class ImageViewer(Protocol):
    available: bool

    def cached(self, message_id: str) -> str | None:
        """先核对会话归属及撤回状态，再查缓存。"""
        ...

    async def analyze(self, message_id: str, on_model: Callable[[], None]) -> str:
        """未缓存媒体的下载及理解；调用模型前通知审计，不接住人工介入异常。"""
        ...


@dataclass
class ReplyToolbox:
    """一次回复决策用的工具箱：绑定一个会话，记录调用过程。"""

    source: ChatSource
    refs: RefBook
    conv_id: str
    max_rounds: int = 4
    audit: Callable[..., None] | None = None
    calls: list[ToolCall] = field(default_factory=list)
    image_viewer: ImageViewer | None = None  # 只有明确启用付费工具的平台才提供
    share_analyzer: ImageViewer | None = None  # 同样的缓存 / analyze 接口，输入是作品 ID
    max_paid_calls: int = 2
    media_budget: MediaBudget | None = None
    _paid_attempts: int = field(default=0, init=False)

    def definitions(self, *, strict: bool) -> list[dict[str, Any]]:
        defs = _definitions()
        if self.image_viewer is not None:
            defs.append(
                {
                    "name": "view_image",
                    "description": (
                        "查看对方发来的私信图片（I 编号），返回画面描述和图中文字。"
                        "优先读缓存，未缓存时消耗媒体预算；只在需要时调用。"
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {"ref": {"type": "string", "description": "图片编号，如 I1"}},
                        "required": ["ref"],
                        "additionalProperties": False,
                    },
                }
            )
        if self.share_analyzer is not None:
            defs.append(
                {
                    "name": "analyze_share",
                    "description": (
                        "补分析当前会话里尚未分析或之前分析失败的分享（S 编号）。"
                        "已有结果直接读缓存；新分析消耗媒体预算。"
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {"ref": {"type": "string", "description": "分享编号，如 S1"}},
                        "required": ["ref"],
                        "additionalProperties": False,
                    },
                }
            )
        if strict:  # 代理不支持结构化输出时一般也不认 strict，跟着 structured_output 走
            for d in defs:
                d["strict"] = True
        return defs

    def calls_json(self) -> str:
        return json.dumps([asdict(c) for c in self.calls], ensure_ascii=False)

    async def execute(self, uses: list[ToolUse]) -> list[ToolOutput]:
        outputs = []
        for use in uses:
            if use.name == "view_image" and self.image_viewer is not None:
                outputs.append(
                    await self._run_paid(use, self.image_viewer, self.refs.resolve_image)
                )
            elif use.name == "analyze_share" and self.share_analyzer is not None:
                outputs.append(await self._run_paid(use, self.share_analyzer, self.refs.resolve))
            else:
                outputs.append(self._run(use))
        return outputs

    async def _run_paid(
        self, use: ToolUse, viewer: ImageViewer, resolve: Callable[[str], str]
    ) -> ToolOutput:
        start = time.monotonic()
        paid = cached = False
        summary: dict[str, Any] = {}

        def on_model() -> None:
            nonlocal paid
            paid = True

        try:
            ref = _ref(use.input).strip().upper()
            message_id = resolve(ref)
            summary = {"ref": ref}
            text = viewer.cached(message_id)
            if text is not None:
                cached = True
            else:
                if not viewer.available:
                    raise ToolError("当前没有浏览器，未缓存媒体暂不可用，请根据已有信息决定")
                if (
                    self._paid_attempts >= self.max_paid_calls
                    or self.media_budget is None
                    or self.media_budget.remaining <= 0
                ):
                    raise ToolError("媒体分析预算已用完，请根据已有信息决定")
                key = (use.name, message_id)
                if key in self.media_budget.attempted:
                    raise ToolError("本轮已尝试分析这条媒体且未成功，暂不重复，请根据已有信息决定")
                self.media_budget.attempted.add(key)
                self._paid_attempts += 1
                self.media_budget.remaining -= 1
                if use.name == "view_image":
                    self.media_budget.image_attempts += 1
                else:
                    self.media_budget.share_attempts += 1
                text = await viewer.analyze(message_id, on_model)
            out = ToolOutput(_clip(text, MAX_RESULT_CHARS))
        except ToolError as e:
            out = ToolOutput(str(e), is_error=True)
        except BaseException:
            self._record(
                ToolCall(
                    use.name,
                    summary,
                    False,
                    0,
                    int((time.monotonic() - start) * 1000),
                    paid,
                    cached,
                )
            )
            raise
        self._record(
            ToolCall(
                use.name,
                summary,
                not out.is_error,
                len(out.content),
                int((time.monotonic() - start) * 1000),
                paid,
                cached,
            )
        )
        return out

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
        self._record(call)
        return out

    def _record(self, call: ToolCall) -> None:
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
                paid=call.paid,
                cached=call.cached,
            )

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
