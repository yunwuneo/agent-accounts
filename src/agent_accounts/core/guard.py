"""回复护栏：决定一条回复能不能发出去。纯函数，不做 I/O，所有输入由调用方准备好。

检查分三类：
- 对象：总开关、会话类型、互关、黑白名单、触发消息是否全是系统/不支持的消息
- 内容：模型把握、长度、链接、联系方式、金钱、承诺、自定义禁用词
- 频率：同一会话最小间隔、全局每小时/每天上限
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agent_accounts.core.config import GuardConfig

# 内容规则：命中即拦截。宁可误拦，不可误发
CONTENT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("包含链接", re.compile(r"https?://|www\.|\b[\w-]+\.(com|cn|net|org|io|me|cc|top)\b", re.I)),
    ("包含手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    (
        "涉及联系方式",
        re.compile(r"微信|vx|v信|wx号|加我|qq号|QQ|电话|手机号|邮箱|@\w+\.(com|cn)", re.I),
    ),
    ("涉及金钱交易", re.compile(r"转账|红包|打钱|借钱|付款|收款|支付|汇款|￥|\d+\s*(块钱|元)")),
    ("包含承诺", re.compile(r"我保证|我承诺|我答应你|一定会给你|包在我身上")),
]


@dataclass
class GuardInput:
    mode: str  # on / off / dry_run
    conv_id: str
    conv_name: str | None
    conv_kind: str  # private / group
    is_mutual: bool
    trigger_types: list[str]  # 触发这次决策的对方消息类型
    should_reply: bool
    text: str
    confidence: float | None
    last_sent_in_conv: datetime | None
    sent_last_hour: int
    sent_last_day: int
    now: datetime
    manual: bool = False  # 人工触发（douyin reply）：跳过对象和把握度检查，只查内容和频率


@dataclass
class GuardResult:
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.reasons


def content_problems(text: str, cfg: GuardConfig) -> list[str]:
    problems = [name for name, pattern in CONTENT_RULES if pattern.search(text)]
    words = [w for w in cfg.extra_block_words if w and w in text]
    if words:
        problems.append("包含禁用词")
    if len(text) > cfg.max_len:
        problems.append(f"超过 {cfg.max_len} 字")
    if not text.strip():
        problems.append("内容为空")
    return problems


def check(cfg: GuardConfig, g: GuardInput) -> GuardResult:
    r = GuardResult()
    names = {g.conv_id, g.conv_name or ""}

    if not g.manual:
        if g.mode == "off":
            r.reasons.append("自动回复已关闭（auto_reply = off）")
        if g.conv_kind != "private":
            r.reasons.append("群聊不自动回复")
        if cfg.only_mutual and not g.is_mutual:
            r.reasons.append("不是互相关注的会话")
        if cfg.allowlist and not names & set(cfg.allowlist):
            r.reasons.append("不在白名单里")
        if g.trigger_types and all(t in ("system", "unsupported") for t in g.trigger_types):
            r.reasons.append("新消息只有系统消息或不支持的消息")
        if g.confidence is not None and g.confidence < cfg.min_confidence:
            r.reasons.append(f"模型把握 {g.confidence:.2f} 低于 {cfg.min_confidence}")
    if names & set(cfg.blocklist):
        r.reasons.append("在黑名单里")

    if g.should_reply or g.manual:
        r.reasons.extend(content_problems(g.text, cfg))

    if g.last_sent_in_conv and g.now - g.last_sent_in_conv < timedelta(seconds=cfg.min_interval_s):
        r.reasons.append(f"距离上次在这个会话发送不到 {cfg.min_interval_s} 秒")
    if g.sent_last_hour >= cfg.max_per_hour:
        r.reasons.append(f"最近一小时已发送 {g.sent_last_hour} 条，达到上限")
    if g.sent_last_day >= cfg.max_per_day:
        r.reasons.append(f"最近一天已发送 {g.sent_last_day} 条，达到上限")
    return r
