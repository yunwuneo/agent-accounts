"""人设和近况：本地文件 ``~/.agent-accounts/persona.md`` 和 ``recent.md``，生成回复时一起用上。

- 人设文件不存在时写入一份占位人设（权限 600），用户可以随时修改
- 近况（最近在做什么、心情、发生了什么）可以为空
- 两者都可以由 Echo 通过 MCP 更新：覆盖前把旧版本存到 ``history/``，写入用临时文件 + 替换
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from agent_accounts.core import paths

MAX_PERSONA_CHARS = 8000
MAX_RECENT_CHARS = 2000

DEFAULT_PERSONA = """# 人设（占位版本，可随意修改）

你是 Echo，一个有自己抖音账号的 AI agent。你在用自己的身份和朋友私信聊天。

- 说话像朋友之间的日常聊天：简短、口语化、自然，一般一两句话，不用书面语，不用列表
- 对朋友分享的视频和图集，先接住内容本身（好笑、好看、有用在哪），再顺着聊一句
- 真诚，不装懂；没看懂就直说或者问
- 被问到是不是 AI 时如实说是，不假装是人
- 不做承诺，不谈钱和交易，不给出或索要联系方式，不发链接
- 对方明显在结束对话、只是发个表情、或者内容不需要回应时，可以不回
"""


def path():
    return paths.home() / "persona.md"


def load() -> str:
    p = path()
    if not p.exists():
        paths.ensure_dir(p.parent)
        p.write_text(DEFAULT_PERSONA, encoding="utf-8")
        p.chmod(0o600)
    return p.read_text(encoding="utf-8").strip()


def recent_path() -> Path:
    return paths.home() / "recent.md"


def load_recent() -> str:
    p = recent_path()
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def recent_updated_at() -> datetime | None:
    p = recent_path()
    return datetime.fromtimestamp(p.stat().st_mtime, UTC) if p.exists() else None


def _replace(p: Path, text: str) -> None:
    """旧版本存到 history/<名字>-<时间>.md，再原子替换为新内容（权限 600）。"""
    paths.ensure_dir(p.parent)
    if p.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = paths.ensure_dir(paths.home() / "history") / f"{p.stem}-{stamp}.md"
        backup.write_bytes(p.read_bytes())
        backup.chmod(0o600)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text + "\n" if text else "", encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, p)


def save(text: str) -> None:
    text = text.strip()
    if not text:
        raise ValueError("人设不能为空")
    if len(text) > MAX_PERSONA_CHARS:
        raise ValueError(f"人设超过 {MAX_PERSONA_CHARS} 字")
    _replace(path(), text)


def save_recent(text: str) -> None:
    """覆盖近况；传空字符串表示清空。"""
    text = text.strip()
    if len(text) > MAX_RECENT_CHARS:
        raise ValueError(f"近况超过 {MAX_RECENT_CHARS} 字")
    _replace(recent_path(), text)
