"""能力与适配器的最小协议。

按草案，通用的 ``messaging`` / ``content`` 接口要在 Phase 2 从抖音实现里提炼，
这里只先固定命名和适配器必须提供的最小集合，避免过早抽象。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class Capability(StrEnum):
    MESSAGING_READ = "messaging.read"
    MESSAGING_SEND = "messaging.send"
    CONTENT_READ = "content.read"
    CONTENT_COMMENT = "content.comment"


class Adapter(Protocol):
    platform: str
    capabilities: frozenset[Capability]
