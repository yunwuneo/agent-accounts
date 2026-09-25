"""配置：``~/.agent-accounts/config.toml``，不存在时全部使用默认值。

示例::

    [browser]
    channel = "chrome"
    headless = false

    [douyin]
    auto_reply = "dry_run"
"""

from __future__ import annotations

import tomllib
from typing import Literal

from pydantic import BaseModel, Field

from agent_accounts.core import paths


class BrowserConfig(BaseModel):
    # "chrome" 使用本机安装的 Google Chrome；为空则使用 Playwright 自带的 Chromium
    channel: str | None = "chrome"
    headless: bool = False
    locale: str = "zh-CN"
    timezone_id: str = "Asia/Shanghai"
    viewport_width: int = 1440
    viewport_height: int = 900
    # 拟人节奏：两次页面操作之间的随机停顿（秒）
    pause_min: float = 0.6
    pause_max: float = 1.8


class DouyinConfig(BaseModel):
    base_url: str = "https://www.douyin.com/"
    # 默认保守：新能力先以 dry_run 上线
    auto_reply: Literal["on", "off", "dry_run"] = "dry_run"


class Config(BaseModel):
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    douyin: DouyinConfig = Field(default_factory=DouyinConfig)


def load() -> Config:
    path = paths.config_path()
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        return Config.model_validate(tomllib.load(f))
