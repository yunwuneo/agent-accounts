"""Anthropic Messages 格式的结构化调用（媒体理解、回复生成共用）。

- endpoint、key、模型名来自配置里的某一段（如 ``[llm.understand]``）
- ``structured_output = true`` 时通过 ``output_config.format`` 传 JSON Schema；关掉时改为 prompt
  要求 JSON，本地用 Pydantic 校验（兼容不支持结构化输出的代理）
- 不用 ``messages.parse``：它在拒答 / 截断时会先抛校验错误，拿不到 stop_reason
- 上游错误信息可能回显（打码的）key，输出前一律 ``scrub``
"""

from __future__ import annotations

import json
import re
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
    extra = (
        {"output_config": {"format": json_schema_format(schema)}} if cfg.structured_output else {}
    )
    if not cfg.structured_output:
        system += f"\n\n只输出一个 JSON 对象，不要输出其他文字，格式：\n{json_hint}"
    try:
        resp = await client.messages.create(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            **extra,
        )
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

    if resp.stop_reason == "refusal":
        raise error(refusal_message)
    if resp.stop_reason == "max_tokens":
        raise error(f"输出被 max_tokens 截断，可调大 [{section}] max_tokens")
    text = "".join(b.text for b in resp.content if b.type == "text")
    return parse_json_text(text, model, error)
