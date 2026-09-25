"""运行数据目录。

所有运行数据（浏览器 profile、SQLite、运行快照）都放在仓库之外，默认 ``~/.agent-accounts/``，
可用环境变量 ``AGENT_ACCOUNTS_HOME`` 覆盖。目录权限统一为 0700。
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "AGENT_ACCOUNTS_HOME"
DIR_MODE = 0o700


def home() -> Path:
    raw = os.environ.get(ENV_HOME)
    return Path(raw).expanduser() if raw else Path.home() / ".agent-accounts"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    path.chmod(DIR_MODE)
    return path


def profile_dir(platform: str) -> Path:
    """每个平台一个独立的浏览器 profile（平台隔离）。"""
    return ensure_dir(home() / "profiles" / platform)


def runs_dir() -> Path:
    return ensure_dir(home() / "runs")


def db_path() -> Path:
    return ensure_dir(home()) / "agent_accounts.db"


def config_path() -> Path:
    return home() / "config.toml"
