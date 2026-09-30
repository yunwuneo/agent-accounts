"""Anthropic Messages 格式的结构化调用（媒体理解、回复生成共用）。

- endpoint、key、模型名来自配置里的某一段（如 ``[llm.understand]``）
- ``structured_output = true`` 时通过 ``output_config.format`` 传 JSON Schema；关掉时改为 prompt
  要求 JSON，本地用 Pydantic 校验（兼容不支持结构化输出的代理）
- 不用 ``messages.parse``：它在拒答 / 截断时会先抛校验错误，拿不到 stop_reason
- 上游错误信息可能回显（打码的）key，输出前一律 ``scrub``
- ``call_json_with_tools``：同样的 JSON 结果，但允许模型先调用若干轮工具（回复决策用）
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

from agent_accounts.core.config import LLMEndpoint


class LLMError(RuntimeError):
    pass


_MASKED_KEY = re.compile(r"[A-Za-z0-9_-]{2,}\*{3,}[A-Za-z0-9_-]*")


def scrub(text: str, key: str | None) -> str:
    """去掉上游错误信息里回显的 key：打码形式（如 sk-ab****cd）和 key 本身的片段。"""
    text = _MASKED_KEY.sub("***", text)
    if key:
        for part in {key, key[:12], key[-8:]}:
            if len(part) >= 6:
                text = text.replace(part, "***")
    return text[:200]


def make_client(cfg: LLMEndpoint, section: str, **kwargs: Any) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=cfg.require_key(section),
        base_url=cfg.base_url,
        timeout=cfg.timeout_s,
        max_retries=2,
        **kwargs,
    )


def json_schema_format(schema: dict[str, Any]) -> dict[str, Any]:
    return {"type": "json_schema", "schema": schema}


def parse_json_text[T: BaseModel](text: str, model: type[T], error: type[LLMError]) -> T:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise error("模型没有返回 JSON")
    try:
        return model.model_validate(json.loads(match.group()))
    except (json.JSONDecodeError, ValidationError) as e:
        raise error(f"模型返回的 JSON 不合格：{type(e).__name__}") from e


async def _create(
    client: anthropic.AsyncAnthropic,
    cfg: LLMEndpoint,
    section: str,
    error: type[LLMError],
    **kwargs: Any,
) -> Any:
    try:
        return await client.messages.create(model=cfg.model, max_tokens=cfg.max_tokens, **kwargs)
    except anthropic.AuthenticationError as e:
        raise error(f"[{section}] API key 无效") from e
    except anthropic.NotFoundError as e:
        raise error(f"[{section}] 模型或 endpoint 不存在：{cfg.model}") from e
    except anthropic.BadRequestError as e:
        hint = (
            "；若代理不支持结构化输出，可设 structured_output = false"
            if cfg.structured_output
            else ""
        )
        raise error(f"请求被拒绝：{scrub(e.message, cfg.key())}{hint}") from e
    except anthropic.APIConnectionError as e:
        raise error(f"连接 {cfg.base_url or '默认 endpoint'} 失败") from e
    except anthropic.APIStatusError as e:
        raise error(f"HTTP {e.status_code}：{scrub(e.message, cfg.key())}") from e


def _check_stop(resp: Any, section: str, error: type[LLMError], refusal_message: str) -> None:
    if resp.stop_reason == "refusal":
        raise error(refusal_message)
    if resp.stop_reason == "max_tokens":
        raise error(f"输出被 max_tokens 截断，可调大 [{section}] max_tokens")


def _format_kwargs(cfg: LLMEndpoint, schema: dict[str, Any]) -> dict[str, Any]:
    return (
        {"output_config": {"format": json_schema_format(schema)}} if cfg.structured_output else {}
    )


def _json_system(cfg: LLMEndpoint, system: str, json_hint: str) -> str:
    if cfg.structured_output:
        return system
    return system + f"\n\n只输出一个 JSON 对象，不要输出其他文字，格式：\n{json_hint}"


def _text(resp: Any) -> str:
    return "".join(b.text for b in resp.content if b.type == "text")


async def call_json[T: BaseModel](
    cfg: LLMEndpoint,
    *,
    section: str,
    system: str,
    content: str | list[dict[str, Any]],
    schema: dict[str, Any],
    json_hint: str,
    model: type[T],
    client: anthropic.AsyncAnthropic | None = None,
    error: type[LLMError] = LLMError,
    refusal_message: str = "模型拒绝了这个请求",
) -> T:
    client = client or make_client(cfg, section)
    resp = await _create(
        client,
        cfg,
        section,
        error,
        system=_json_system(cfg, system, json_hint),
        messages=[{"role": "user", "content": content}],
        **_format_kwargs(cfg, schema),
    )
    _check_stop(resp, section, error, refusal_message)
    return parse_json_text(_text(resp), model, error)


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolOutput:
    content: str
    is_error: bool = False


async def call_json_with_tools[T: BaseModel](
    cfg: LLMEndpoint,
    *,
    section: str,
    system: str,
    content: str | list[dict[str, Any]],
    schema: dict[str, Any],
    json_hint: str,
    model: type[T],
    tools: list[dict[str, Any]],
    execute: Callable[[list[ToolUse]], Awaitable[list[ToolOutput]]],
    max_rounds: int,
    client: anthropic.AsyncAnthropic | None = None,
    error: type[LLMError] = LLMError,
    refusal_message: str = "模型拒绝了这个请求",
) -> T:
    """带工具的有界多轮调用：模型可以先调用工具，最后给出和 ``call_json`` 一样的 JSON。

    - 最多 ``max_rounds`` 轮工具调用；之后的一轮用 ``tool_choice = none``，要求直接给结论
    - 只用 auto / none，不用强制调用（部分新模型不支持 any / tool）
    - 一轮里的多个工具调用交给 ``execute`` 一起执行，结果放在同一条 user 消息里返回
    - 历史只追加不改写；``execute`` 抛出的异常（如撞到风控）不接住，直接中止
    """
    client = client or make_client(cfg, section)
    system = _json_system(cfg, system, json_hint)
    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
    for round_no in range(max_rounds + 1):
        last = round_no == max_rounds
        resp = await _create(
            client,
            cfg,
            section,
            error,
            system=system,
            messages=messages,
            tools=tools,
            tool_choice={"type": "none" if last else "auto"},
            **_format_kwargs(cfg, schema),
        )
        _check_stop(resp, section, error, refusal_message)
        uses = [
            ToolUse(b.id, b.name, dict(b.input or {})) for b in resp.content if b.type == "tool_use"
        ]
        if resp.stop_reason != "tool_use" or not uses:
            return parse_json_text(_text(resp), model, error)
        if last:  # tool_choice = none 仍返回工具调用（个别代理不支持 none）
            break
        outputs = await execute(uses)
        messages.append({"role": "assistant", "content": resp.content})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": use.id,
                        "content": out.content,
                        "is_error": out.is_error,
                    }
                    for use, out in zip(uses, outputs, strict=True)
                ],
            }
        )
    raise error("模型在工具调用上限之后仍没有给出结论")
