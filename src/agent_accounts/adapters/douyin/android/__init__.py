"""可选 Android 真机辅助后端。稳定消息 ID 未验收，不接入自动回复循环。"""

from agent_accounts.core.capability import Capability

platform = "douyin"
capabilities = frozenset({Capability.MESSAGING_READ, Capability.CONTENT_READ})
# 人工单条发送是单独的显式能力，不向自动回复/模型暴露。
