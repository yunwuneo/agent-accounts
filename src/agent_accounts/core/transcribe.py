"""语音转写：OpenAI 兼容的 ``POST {base_url}/audio/transcriptions``。

endpoint、key、模型名都来自配置 ``[transcribe]``。
"""

from __future__ import annotations

from pathlib import Path

import httpx2

from agent_accounts.core.config import TranscribeConfig

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class TranscribeError(RuntimeError):
    pass


async def transcribe(
    cfg: TranscribeConfig, audio: Path, *, client: httpx2.AsyncClient | None = None
) -> str:
    key = cfg.require_key("transcribe")
    url = (cfg.base_url or DEFAULT_BASE_URL).rstrip("/") + "/audio/transcriptions"
    data = {"model": cfg.model, "response_format": "json"}
    if cfg.language:
        data["language"] = cfg.language
    own = client is None
    client = client or httpx2.AsyncClient(timeout=cfg.timeout_s)
    try:
        with audio.open("rb") as f:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                data=data,
                files={"file": (audio.name, f, "audio/mpeg")},
            )
    except httpx2.HTTPError as e:
        raise TranscribeError(f"请求失败：{type(e).__name__}") from e
    finally:
        if own:
            await client.aclose()
    if resp.status_code != 200:
        # 只带状态码和响应开头，不带请求头（里面有 key）
        raise TranscribeError(f"HTTP {resp.status_code}：{resp.text[:200]}")
    try:
        return (resp.json().get("text") or "").strip()
    except ValueError as e:
        raise TranscribeError("响应不是 JSON") from e
