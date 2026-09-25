"""命令行输出编码兜底。

Windows 上 stdout/stderr 被管道或重定向时，Python 默认用本地编码（中文系统是 GBK）。
GBK 能编码中文，但编码不了 ✅🛑⚠️ 这类 emoji，会抛 UnicodeEncodeError 让命令直接崩溃。
这里把无法编码的字符替换成 ``?``，不改变编码本身（避免把 UTF-8 灌给期待 GBK 的下游）。
"""

from __future__ import annotations

import sys


def setup() -> None:
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
