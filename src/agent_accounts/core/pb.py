"""无 schema 的 protobuf 读写。

平台接口是 protobuf 但没有 .proto 文件。这里只解析线格式（wire format），由调用方按
已知字段号取值：``msg.get_int(1)``、``msg.get_str(8)``、``msg.get_msgs(2)``。嵌套消息和字符串在线格式上
都是 length-delimited，是否当作子消息由调用方决定，不做猜测。

``encode`` 能把解析结果原样写回（用于制作脱敏 fixture）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

VARINT, FIXED64, LEN, FIXED32 = 0, 1, 2, 5

Value = int | bytes


class PbError(ValueError):
    pass


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if i >= len(buf):
            raise PbError("varint 越界")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise PbError("varint 过长")


def _write_varint(n: int) -> bytes:
    if n < 0:
        n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


@dataclass
class PbMessage:
    # 保留字段出现顺序，保证 encode 可以原样写回
    items: list[tuple[int, int, Value]] = field(default_factory=list)

    @classmethod
    def parse(cls, buf: bytes) -> PbMessage:
        items: list[tuple[int, int, Value]] = []
        i = 0
        while i < len(buf):
            key, i = _read_varint(buf, i)
            num, wt = key >> 3, key & 7
            if num == 0:
                raise PbError("字段号为 0")
            if wt == VARINT:
                value, i = _read_varint(buf, i)
            elif wt == LEN:
                n, i = _read_varint(buf, i)
                if i + n > len(buf):
                    raise PbError("长度越界")
                value, i = buf[i : i + n], i + n
            elif wt in (FIXED64, FIXED32):
                size = 8 if wt == FIXED64 else 4
                if i + size > len(buf):
                    raise PbError("定长字段越界")
                value, i = buf[i : i + size], i + size
            else:
                raise PbError(f"不支持的 wire type {wt}")
            items.append((num, wt, value))
        return cls(items)

    def encode(self) -> bytes:
        out = bytearray()
        for num, wt, value in self.items:
            out += _write_varint(num << 3 | wt)
            if wt == VARINT:
                out += _write_varint(value)  # type: ignore[arg-type]
            elif wt == LEN:
                out += _write_varint(len(value)) + value  # type: ignore[arg-type]
            else:
                out += value  # type: ignore[operator]
        return bytes(out)

    # ---- 取值 ----
    def all(self, num: int) -> list[Value]:
        return [v for n, _, v in self.items if n == num]

    def first(self, num: int) -> Value | None:
        return next((v for n, _, v in self.items if n == num), None)

    def get_int(self, num: int, default: int = 0) -> int:
        v = self.first(num)
        return v if isinstance(v, int) else default

    def get_str(self, num: int, default: str = "") -> str:
        v = self.first(num)
        if not isinstance(v, bytes):
            return default
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return default

    def get_msg(self, num: int) -> PbMessage | None:
        v = self.first(num)
        return PbMessage.parse(v) if isinstance(v, bytes) else None

    def get_msgs(self, num: int) -> list[PbMessage]:
        return [PbMessage.parse(v) for v in self.all(num) if isinstance(v, bytes)]

    # ---- 修改（制作 fixture 用）----
    def replace(self, num: int, fn) -> None:
        """对字段 ``num`` 的每个值调用 ``fn(value) -> value``。"""
        self.items = [(n, wt, fn(v) if n == num else v) for n, wt, v in self.items]
