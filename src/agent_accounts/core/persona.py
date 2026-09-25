"""人设：M3 先用本地文件 ``~/.agent-accounts/persona.md``，M4 再改为由 Echo 提供。

文件不存在时写入一份占位人设（权限 600），用户可以随时修改。
"""

from __future__ import annotations

from agent_accounts.core import paths

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
