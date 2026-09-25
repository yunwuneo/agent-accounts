"""从 ``douyin spike net`` 的录制结果生成脱敏 fixture。

用法::

    uv run python scripts/make_douyin_fixtures.py ~/.agent-accounts/runs/<id>/net

脱敏策略：
1. 只保留解析器用到的字段（见 adapters/douyin/im.py 的文档），其余字段一律丢弃；
2. uid / sec_uid 按出现顺序映射成假值，会话 ID 同步改写；
3. 内容 JSON 只保留类型字段和 itemId，文本、标题、作者、封面换成占位符；
4. 生成后扫描输出，任何原始文本、标题、作者、sec_uid、uid 出现都会直接报错。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_accounts.adapters.douyin import im
from agent_accounts.core.pb import LEN, PbMessage

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "douyin"

KEEP_ENVELOPE = {1, 4, 6, 13}
KEEP_CONV = {1, 2, 3, 6, 51}
KEEP_MESSAGE = {1, 3, 4, 6, 7, 8, 10, 14, 17}
KEEP_CONTENT = {"aweType", "awemeType", "is_slides", "image_count", "itemId"}


class Scrubber:
    def __init__(self) -> None:
        self.uids: dict[int, int] = {}
        self.sec_uids: dict[str, str] = {}
        self.counter = 0
        self.secrets: set[str] = set()  # 原始敏感串，最后用于扫描

    def uid(self, n: int) -> int:
        if n == 0:
            return 0
        self.secrets.add(str(n))
        return self.uids.setdefault(n, 10_000_001 + len(self.uids))

    def sec_uid(self, s: str) -> str:
        if not s:
            return s
        self.secrets.add(s)
        return self.sec_uids.setdefault(s, f"SEC_UID_{len(self.sec_uids) + 1}")

    def conv_id(self, s: str) -> str:
        parts = s.split(":")
        if len(parts) == 4 and all(p.isdigit() for p in parts[2:]):
            parts[2:] = [str(self.uid(int(p))) for p in parts[2:]]
        return ":".join(parts)

    def placeholder(self, kind: str, original: str) -> str:
        if original:
            self.secrets.add(original)
        self.counter += 1
        return f"{kind}{self.counter}"

    def content(self, raw: str) -> str:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return "{}"
        if not isinstance(data, dict):
            return "{}"
        out = {k: v for k, v in data.items() if k in KEEP_CONTENT}
        if "text" in data:
            out["text"] = self.placeholder("文本", data["text"])
        if "tips" in data:
            out["tips"] = self.placeholder("系统提示", data["tips"])
        if "content_title" in data:
            out["content_title"] = self.placeholder("作品标题", data["content_title"])
        if "content_name" in data:
            out["content_name"] = self.placeholder("作者", data["content_name"])
        if "cover_url" in data:
            out["cover_url"] = {"url_list": [f"https://example.invalid/cover{self.counter}.jpg"]}
        return json.dumps(out, ensure_ascii=False)


def _keep(m: PbMessage, fields: set[int]) -> PbMessage:
    return PbMessage([it for it in m.items if it[0] in fields])


def _set(m: PbMessage, num: int, value: int | bytes) -> None:
    m.replace(num, lambda _v: value)


def scrub_message(m: PbMessage, s: Scrubber) -> PbMessage:
    m = _keep(m, KEEP_MESSAGE)
    m.replace(1, lambda v: s.conv_id(v.decode()).encode())
    m.replace(7, s.uid)
    m.replace(8, lambda v: s.content(v.decode()).encode())
    m.replace(14, lambda v: s.sec_uid(v.decode()).encode())
    return m


def scrub_conversation(c: PbMessage, s: Scrubber) -> PbMessage:
    c = _keep(c, KEEP_CONV)
    c.replace(1, lambda v: s.conv_id(v.decode()).encode())

    def participants(v: bytes) -> bytes:
        box = PbMessage.parse(v)
        people = []
        for p in box.get_msgs(1):
            p = _keep(p, {1, 5})
            p.replace(1, s.uid)
            p.replace(5, lambda x: s.sec_uid(x.decode()).encode())
            people.append((1, LEN, p.encode()))
        return PbMessage(people).encode()

    c.replace(6, participants)
    c.replace(51, lambda v: _keep(PbMessage.parse(v), {5}).encode())
    return c


def scrub_response(body: bytes, s: Scrubber) -> bytes:
    env = _keep(PbMessage.parse(body), KEEP_ENVELOPE)
    cmd = env.get_int(1)
    env.replace(13, s.uid)
    wrapper = env.get_msg(6)
    inner = wrapper.get_msg(cmd) if wrapper else None
    if inner is None:
        return env.encode()

    def msgs(v: bytes) -> bytes:
        return scrub_message(PbMessage.parse(v), s).encode()

    if cmd == im.CMD_INIT:
        entries = []
        for entry in inner.get_msgs(1):
            entry = _keep(entry, {1, 2})
            entry.replace(1, lambda v: scrub_conversation(PbMessage.parse(v), s).encode())
            entry.replace(2, msgs)
            entries.append((1, LEN, entry.encode()))
        inner = PbMessage(entries)
    elif cmd == im.CMD_USER_MESSAGE:
        boxes = []
        for box in inner.get_msgs(2):
            box = _keep(box, {1})
            box.replace(1, msgs)
            boxes.append((2, LEN, box.encode()))
        inner = PbMessage(boxes)
    elif cmd == im.CMD_BY_CONVERSATION:
        inner = _keep(inner, {1})
        inner.replace(1, msgs)
    elif cmd == im.CMD_INFO_LIST:
        inner = _keep(inner, {1})
        inner.replace(1, lambda v: scrub_conversation(PbMessage.parse(v), s).encode())
    _set(env, 6, PbMessage([(cmd, LEN, inner.encode())]).encode())
    return env.encode()


def scrub_user_info(data: dict, s: Scrubber) -> dict:
    users = []
    for u in data.get("data") or []:
        item = {
            "sec_uid": s.sec_uid(u.get("sec_uid", "")),
            "nickname": s.placeholder("昵称", u.get("nickname", "")),
        }
        if u.get("uid"):
            item["uid"] = str(s.uid(int(u["uid"])))
        users.append(item)
    return {"data": users, "status_code": 0}


def main(net_dir: Path) -> None:
    s = Scrubber()
    rows = [json.loads(line) for line in (net_dir / "http.jsonl").open()]
    OUT.mkdir(parents=True, exist_ok=True)
    wanted = {
        im.CMD_INIT: "init.pb",
        im.CMD_BY_CONVERSATION: "by_conversation.pb",
        im.CMD_INFO_LIST: "info_list.pb",
        im.CMD_USER_MESSAGE: "user_message.pb",
    }
    written: list[Path] = []
    # 先处理 init，保证本人 uid 映射为 10000001
    pb_rows = sorted(
        (r for r in rows if r.get("file", "").endswith(".pb")),
        key=lambda r: "get_message_by_init" not in r["url"],
    )
    for row in pb_rows:
        body = (net_dir / row["file"]).read_bytes()
        cmd = PbMessage.parse(body).get_int(1)
        name = wanted.pop(cmd, None)
        parsed = im.parse_response(body)
        if name and (parsed.messages or parsed.conversations or parsed.skipped_commands):
            path = OUT / name
            path.write_bytes(scrub_response(body, s))
            written.append(path)
        elif name:
            wanted[cmd] = name  # 空响应不要，等下一个
    # 合并所有用户信息响应（每个响应只包含一部分用户）
    merged: dict[str, dict] = {}
    for row in rows:
        if "/im/user/info/" in row.get("url", "") and row.get("file"):
            for u in json.loads((net_dir / row["file"]).read_text()).get("data") or []:
                merged.setdefault(u.get("sec_uid", ""), u)
    if merged:
        path = OUT / "user_info.json"
        data = scrub_user_info({"data": list(merged.values())}, s)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        written.append(path)

    # 扫描：任何原始敏感串出现在输出里都算失败
    leaks = []
    for path in written:
        blob = path.read_bytes()
        for secret in s.secrets:
            if len(secret) >= 4 and secret.encode() in blob:
                leaks.append((path.name, secret[:12] + "…"))
    if leaks:
        for p in written:
            p.unlink()
        sys.exit(f"脱敏失败，已删除输出：{leaks[:5]}")
    for path in written:
        print(f"写入 {path.relative_to(OUT.parent.parent.parent)}（{path.stat().st_size} 字节）")
    missing = sorted(wanted.values())
    if missing:
        print(f"录制里没有可用的：{missing}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser())
