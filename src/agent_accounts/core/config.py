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
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from agent_accounts.core import paths, schedule
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


class AutoReplyConfig(BaseModel):
    """各平台自动回复循环共用的配置。"""

    # 写错键名（如 auto-reply）时报错，而不是静默回落到默认的 dry_run
    model_config = ConfigDict(extra="forbid")

    # 默认保守：新能力先以 dry_run 上线
    auto_reply: Literal["on", "off", "dry_run"] = "dry_run"
    context_messages: int = 20  # 回复决策时带上的最近消息条数
    digest_per_tick: int = 3  # 每轮最多自动分析几个新分享的作品（控制成本）
    interval_min_s: int = 60  # run 两轮之间的随机间隔
    interval_max_s: int = 120
    # 休息时段（本机时间，如 ["03:00-08:00", "11:00-12:00"]）：run 和 sync --watch 在这些时间里
    # 不开浏览器、不同步、不回复；结束后再随机晚 0–quiet_wake_jitter_s 秒醒来，积压的新消息合并处理
    quiet_hours: list[str] = Field(default_factory=list)
    quiet_wake_jitter_s: int = Field(default=300, ge=0)

    @field_validator("quiet_hours")
    @classmethod
    def _check_quiet_hours(cls, v: list[str]) -> list[str]:
        schedule.parse_windows(v)
        return v

    def quiet_until(self, now: datetime) -> datetime | None:
        return schedule.quiet_until(now, schedule.parse_windows(self.quiet_hours))


class AndroidConfig(BaseModel):
    """显式启用的真机辅助入口；不会接管网页端 run/sync。"""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    udid: str = ""
    adb: str = "adb"
    appium_url: str = "http://127.0.0.1:4725"
    allow_send: bool = False
    auto_reply: Literal["on", "off", "dry_run"] = "dry_run"
    tested_version: str = "40.4.0"
    request_timeout_s: float = Field(default=60, gt=0, le=120)

    @field_validator("appium_url")
    @classmethod
    def _local_server(cls, value: str) -> str:
        u = urlsplit(value)
        if (
            u.scheme != "http"
            or u.hostname not in {"127.0.0.1", "localhost", "::1"}
            or u.username
            or u.password
            or u.query
            or u.fragment
            or u.path not in {"", "/"}
        ):
            raise ValueError("Appium 必须是无凭据的本机 HTTP 地址，且不带路径")
        return value.rstrip("/")


class DouyinConfig(AutoReplyConfig):
    base_url: str = "https://www.douyin.com/"
    android: AndroidConfig = Field(default_factory=AndroidConfig)


class XiaohongshuConfig(AutoReplyConfig):
    # 小红书读完整消息要点进会话；每轮最多点开几个有新消息的会话
    max_open: int = Field(default=5, ge=1, le=20)


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
    # 抽帧：整段视频均匀切段、每段取一帧，约每 frame_interval_s 秒一帧，总数不超过 max_frames。
    # 视频再长也覆盖从头到尾，只是帧间隔变大
    frame_interval_s: float = Field(default=5.0, gt=0)
    max_frames: int = 30
    min_frames: int = 3
    frame_width: int = 768  # 抽帧缩放宽度，控制图片 token 数
    max_images: int = 12  # 图集最多用几张
    max_video_seconds: int = 3600  # 语音转写最多取多长（超过的部分在摘要里注明没听到）
    transcribe_segment_s: int = 600  # 长音轨分段转写，每段秒数（避开转写接口的文件大小上限）


class GuardConfig(BaseModel):
    """自动回复护栏（Notion 抖音子页面第 7 节）。"""

    only_mutual: bool = True  # 只回互相关注的私聊；陌生人和群聊一律不回
    allowlist: list[str] = Field(default_factory=list)  # 非空时只回这些会话（conv_id 或昵称）
    blocklist: list[str] = Field(default_factory=list)  # conv_id 或昵称
    min_confidence: float = 0.6
    max_len: int = 120  # 单条消息
    max_messages: int = 3  # 一次回复最多分几条发
    min_interval_s: int = 60  # 同一会话两次发送的最小间隔
    max_per_hour: int = 20
    max_per_day: int = 100
    extra_block_words: list[str] = Field(default_factory=list)


class ReplyToolsConfig(BaseModel):
    """回复模型的工具调用（Notion「回复模型工具调用：设计方案」）。

    打开后回复模型在一次决策里可以按需调用只读工具（翻更早的消息、搜索、看分享的完整分析、
    看对方关系），查完再给出决定。paid 可单独启用小红书图片理解；默认不启用付费工具。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False  # 默认关：先用 decide --tools 试运行对比
    max_rounds: int = Field(default=4, ge=1, le=8)  # 每次决策最多调用几轮工具
    paid: list[Literal["view_image", "analyze_share"]] = Field(default_factory=list)
    max_paid_calls: int = Field(default=2, ge=0, le=8)


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


MIN_TOKEN_CHARS = 32


class McpConfig(BaseModel):
    """HTTP MCP server（``agent-accounts mcp --http``）。stdio 模式不用这些。

    token 是 Bearer 鉴权用的密钥：用 ``agent-accounts mcp-token`` 生成并写入（文件须私有），
    也可写 ``token_env`` 指定环境变量名。没有 token 时拒绝以 HTTP 启动。
    """

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"  # 只本机访问；给其他机器用时改成 0.0.0.0 或具体网卡地址
    port: int = Field(default=8765, ge=1, le=65535)
    path: str = "/mcp"
    # 经过反向代理 / 内网穿透访问时，请求的 Host 是对外域名（如 mcp.example.com），
    # 要列在这里，否则 SDK 的 DNS rebinding 防护会返回 421 Invalid Host header。
    # 写域名即可（不带协议）；带端口的写法如 "example.com:8443" 也支持
    allowed_hosts: list[str] = Field(default_factory=list)
    token: SecretStr | None = None
    token_env: str | None = None

    @field_validator("allowed_hosts")
    @classmethod
    def _bare_hosts(cls, v: list[str]) -> list[str]:
        for h in v:
            if not h or "/" in h or h != h.strip():
                raise ValueError("allowed_hosts 只写域名或 域名:端口，不带 http:// 和路径")
        return v

    @field_validator("token_env")
    @classmethod
    def _env_name(cls, v: str | None) -> str | None:
        if v is not None and not _ENV_NAME.fullmatch(v):
            raise ValueError("token_env 应该是环境变量名；token 本身请写在 token")
        return v

    @field_validator("token")
    @classmethod
    def _strong(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and len(v.get_secret_value()) < MIN_TOKEN_CHARS:
            raise ValueError(f"token 太短（至少 {MIN_TOKEN_CHARS} 个字符），用 mcp-token 生成")
        return v

    def bearer(self) -> str | None:
        if self.token is not None:
            return self.token.get_secret_value()
        if self.token_env:
            value = os.environ.get(self.token_env) or None
            if value and len(value) < MIN_TOKEN_CHARS:
                raise ConfigError(f"环境变量 {self.token_env} 里的 token 太短")
            return value
        return None

    def describe(self) -> str:
        if self.token is not None:
            source = "token"
        elif self.token_env:
            source = f"${self.token_env}"
        else:
            source = None
        auth = f"{source}（{'已设置' if self.bearer() else '未设置'}）" if source else "未配置"
        extra = f"  allowed_hosts={self.allowed_hosts}" if self.allowed_hosts else ""
        return f"http://{self.host}:{self.port}{self.path}  Bearer token={auth}{extra}"


class Config(BaseModel):
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    douyin: DouyinConfig = Field(default_factory=DouyinConfig)
    xiaohongshu: XiaohongshuConfig = Field(default_factory=XiaohongshuConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    transcribe: TranscribeConfig = Field(default_factory=TranscribeConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    guard: GuardConfig = Field(default_factory=GuardConfig)
    reply_tools: ReplyToolsConfig = Field(default_factory=ReplyToolsConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)


_PLAIN_SECRETS = {"api_key", "webhook_url", "token"}


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


# 行首空白只能是空格/制表符：\s 在 re.M 下会吞掉前一行的换行
_SECTION = re.compile(r"^[ \t]*\[mcp\][ \t]*(#.*)?$", re.M)
_HEADER = re.compile(r"^[ \t]*\[", re.M)
_TOKEN_LINE = re.compile(r"^[ \t]*token[ \t]*=.*$", re.M)


def write_mcp_token(token: str, *, rotate: bool = False) -> Path:
    """把 token 写进配置文件的 [mcp] 段，保留文件里其他内容和注释。

    已有 token 时只有 rotate=True 才覆盖。写入前用 tomllib 校验结果，出错就不写。
    文件不存在时新建（POSIX 上权限 600）。
    """
    if len(token) < MIN_TOKEN_CHARS or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise ConfigError("token 格式不对")
    path = paths.config_path()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ConfigError(f"{path} 不是合法的 TOML，先修好再生成 token") from None
    mcp = data.get("mcp", {})
    if isinstance(mcp, dict) and mcp.get("token") and not rotate:
        raise ConfigError("[mcp] 已经有 token；要换新的请加 --rotate（旧 token 立即失效）")

    line = f'token = "{token}"'
    if m := _SECTION.search(text):
        start = m.end()
        nxt = _HEADER.search(text, start)
        end = nxt.start() if nxt else len(text)
        body = text[start:end]
        if _TOKEN_LINE.search(body):
            body = _TOKEN_LINE.sub(line, body, count=1)
        else:
            body = f"\n{line}" + body
        new = text[:start] + body + text[end:]
    else:
        new = text.rstrip("\n") + ("\n\n" if text.strip() else "") + f"[mcp]\n{line}\n"

    try:
        written = tomllib.loads(new).get("mcp", {}).get("token")
    except tomllib.TOMLDecodeError:
        written = None
    if written != token:
        raise ConfigError(f"{path} 的 [mcp] 段结构特殊，无法自动写入，请手动编辑")

    paths.ensure_dir(path.parent)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(new, encoding="utf-8")
    if POSIX:
        tmp.chmod(0o600)
    os.replace(tmp, path)
    return path
