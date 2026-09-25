"""配置：``~/.agent-accounts/config.toml``，不存在时全部使用默认值。

完整示例见仓库根目录的 ``config.example.toml``。

模型相关的三段配置各自独立，可以指向不同的 endpoint、key 和模型：

- ``[llm.understand]``：媒体理解，Anthropic Messages 格式，必须支持图片输入
- ``[llm.reply]``：回复生成（M3），Anthropic Messages 格式
- ``[transcribe]``：语音转写，OpenAI 兼容的 ``/v1/audio/transcriptions``

key 可以直接写 ``api_key``，也可以写 ``api_key_env`` 指定环境变量名。
key 用 SecretStr 保存，打印配置对象时不会显示明文。
"""

from __future__ import annotations

import os
import re
import stat
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from agent_accounts.core import paths
from agent_accounts.core.errors import AgentAccountsError


class ConfigError(AgentAccountsError):
    pass


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
    # 写错键名（如 auto-reply）时报错，而不是静默回落到默认的 dry_run
    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://www.douyin.com/"
    # 默认保守：新能力先以 dry_run 上线
    auto_reply: Literal["on", "off", "dry_run"] = "dry_run"
    context_messages: int = 20  # 回复决策时带上的最近消息条数
    digest_per_tick: int = 3  # 每轮最多自动分析几个新分享的作品（控制成本）
    interval_min_s: int = 60  # douyin run 两轮之间的随机间隔
    interval_max_s: int = 120


_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")


class Endpoint(BaseModel):
    """一个模型服务的连接信息。"""

    base_url: str | None = None  # 为空时用 SDK 默认地址
    api_key: SecretStr | None = None
    api_key_env: str | None = None
    model: str

    @field_validator("api_key_env")
    @classmethod
    def _env_name(cls, v: str | None) -> str | None:
        # 这里填的是环境变量名；误填成 key 本身时报错，且错误信息不包含输入值
        if v is not None and not _ENV_NAME.fullmatch(v):
            raise ValueError(
                "api_key_env 应该是环境变量名（如 ANTHROPIC_API_KEY）；key 本身请写在 api_key"
            )
        return v

    def key(self) -> str | None:
        if self.api_key is not None:
            return self.api_key.get_secret_value()
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        return None

    def require_key(self, section: str) -> str:
        if not (k := self.key()):
            where = f"环境变量 {self.api_key_env}" if self.api_key_env else "api_key"
            raise ConfigError(f"[{section}] 缺少 API key（{where}），见 config.example.toml")
        return k

    def redacted(self) -> dict[str, str | None]:
        source = (
            "api_key" if self.api_key else (f"${self.api_key_env}" if self.api_key_env else None)
        )
        return {
            "base_url": self.base_url or "(SDK 默认)",
            "model": self.model,
            "key": f"{source}（{'已设置' if self.key() else '未设置'}）" if source else "未配置",
        }


class LLMEndpoint(Endpoint):
    """Anthropic Messages 格式的模型。"""

    max_tokens: int = 8000  # 含自适应思考的 token
    # 部分兼容代理不支持 output_config 结构化输出；关掉后改用 prompt 要求 JSON、本地校验
    structured_output: bool = True
    timeout_s: float = 120


class LLMConfig(BaseModel):
    understand: LLMEndpoint = Field(default_factory=lambda: LLMEndpoint(model="claude-sonnet-5"))
    reply: LLMEndpoint = Field(default_factory=lambda: LLMEndpoint(model="claude-sonnet-5"))


class TranscribeConfig(Endpoint):
    """OpenAI 兼容的 /v1/audio/transcriptions。"""

    model: str = "whisper-1"
    language: str | None = "zh"
    timeout_s: float = 120


class MediaConfig(BaseModel):
    max_frames: int = 10  # 视频最多抽几帧
    min_frames: int = 3
    frame_width: int = 768  # 抽帧缩放宽度，控制图片 token 数
    max_images: int = 12  # 图集最多用几张
    max_video_seconds: int = 600  # 超过的视频只取前面这段做转写


class GuardConfig(BaseModel):
    """自动回复护栏（Notion 抖音子页面第 7 节）。"""

    only_mutual: bool = True  # 只回互相关注的私聊；陌生人和群聊一律不回
    allowlist: list[str] = Field(default_factory=list)  # 非空时只回这些会话（conv_id 或昵称）
    blocklist: list[str] = Field(default_factory=list)  # conv_id 或昵称
    min_confidence: float = 0.6
    max_len: int = 120
    min_interval_s: int = 60  # 同一会话两次发送的最小间隔
    max_per_hour: int = 20
    max_per_day: int = 100
    extra_block_words: list[str] = Field(default_factory=list)


class AlertsConfig(BaseModel):
    """告警 webhook：POST JSON {platform, level, message, run_id, time, text}。

    webhook URL 里常带 token，按密钥处理：可写 ``webhook_url``（文件须私有），
    也可写 ``webhook_url_env`` 指定环境变量名；日志和 ``config show`` 里都不显示。
    """

    model_config = ConfigDict(extra="forbid")

    webhook_url: SecretStr | None = None
    webhook_url_env: str | None = None
    min_level: Literal["info", "warning", "critical"] = "warning"
    timeout_s: float = 5

    @field_validator("webhook_url_env")
    @classmethod
    def _env_name(cls, v: str | None) -> str | None:
        if v is not None and not _ENV_NAME.fullmatch(v):
            raise ValueError("webhook_url_env 应该是环境变量名；URL 本身请写在 webhook_url")
        return v

    def url(self) -> str | None:
        if self.webhook_url is not None:
            return self.webhook_url.get_secret_value()
        if self.webhook_url_env:
            return os.environ.get(self.webhook_url_env) or None
        return None

    def describe(self) -> str:
        if self.webhook_url is not None:
            source = "webhook_url"
        elif self.webhook_url_env:
            source = f"${self.webhook_url_env}"
        else:
            return "未配置（只输出到终端和审计日志）"
        return f"{source}（{'已设置' if self.url() else '未设置'}），min_level={self.min_level}"


class Config(BaseModel):
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    douyin: DouyinConfig = Field(default_factory=DouyinConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    transcribe: TranscribeConfig = Field(default_factory=TranscribeConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    guard: GuardConfig = Field(default_factory=GuardConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)


_PLAIN_SECRETS = {"api_key", "webhook_url"}


def _contains_plain_key(data: dict) -> bool:
    if isinstance(data, dict):
        return any(k in _PLAIN_SECRETS or _contains_plain_key(v) for k, v in data.items())
    return False


# Windows 的 NTFS 不用 Unix 权限位（stat 总是报 0o666，chmod 也改不掉），权限检查只在 POSIX 上做；
# Windows 上靠用户目录自带的 ACL 保护，配置文件必须放在当前用户目录下
POSIX = os.name != "nt"


def _check_private(path: Path) -> None:
    if POSIX:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ConfigError(f"{path} 含有明文密钥，但权限过宽；请执行 chmod 600 {path}")
        return
    try:
        path.resolve().relative_to(Path.home().resolve())
    except ValueError:
        raise ConfigError(f"{path} 含有明文密钥，但不在当前用户目录下，其他用户可能读到") from None


def load() -> Config:
    path = paths.config_path()
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        data = tomllib.load(f)
    # 文件里有明文 key 时，只能本人可读
    if _contains_plain_key(data):
        _check_private(path)
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        # 不用 str(e)：Pydantic 默认会把输入值（可能是 key）写进错误信息
        problems = "；".join(
            f"[{'.'.join(str(x) for x in err['loc'])}] {err['msg']}"
            for err in e.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"{path} 有误：{problems}") from None
