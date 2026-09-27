"""发送器测试：合成页面模拟输入框、发送按钮和消息气泡（结构按 2026-09-25 真实页面）。"""

from __future__ import annotations

import pytest

from agent_accounts.adapters.douyin.sender import (
    SendError,
    open_conversation,
    send_messages,
    send_text,
)

pytestmark = pytest.mark.browser

PAGE = """
<style>#box { width: 200px; min-height: 20px; }</style>
<div id="list"><div data-e2e="msg-item-content">hi</div></div>
<div data-e2e="msg-input" style="position:absolute;top:800px;left:1100px;display:flex">
  <div><div data-slate-editor="true" contenteditable="true" id="box"></div></div>
  <svg width="36" height="36" class="e2e-send-msg-btn" id="send"></svg>
</div>
<script>
  window.ignoreClicks = __IGNORE__;  // 前几次点击发送按钮无效
  document.getElementById('send').addEventListener('click', () => {
    if (window.ignoreClicks > 0) { window.ignoreClicks--; return; }
    const box = document.getElementById('box');
    const text = box.innerText.trim();
    if (!text) return;
    const b = document.createElement('div');
    b.setAttribute('data-e2e', 'msg-item-content');
    b.innerText = text;
    const list = document.getElementById('list');
    list.insertBefore(b, list.firstChild);  // 真实页面是倒序的：最新消息在最前
    box.innerText = '';
  });
</script>
"""


class FakeSession:
    def __init__(self, page):
        self.page = page
        self.pauses = []

    async def pause(self, lo=None, hi=None):
        self.pauses.append((lo, hi))


async def _setup(page, ignore: int = 0, prefill: str = ""):
    await page.set_content(PAGE.replace("__IGNORE__", str(ignore)))
    if prefill:
        await page.evaluate("t => document.getElementById('box').innerText = t", prefill)
    return FakeSession(page)


async def _bubbles(page):
    return await page.locator('[data-e2e="msg-item-content"]').all_inner_texts()


async def test_send_types_clicks_and_verifies(page):
    s = await _setup(page)
    result = await send_text(s, "你好呀", verify_timeout_s=2)
    assert result.ok and not result.retried
    assert await _bubbles(page) == ["你好呀", "hi"]


async def test_retry_once_when_text_still_in_box(page):
    s = await _setup(page, ignore=1)
    result = await send_text(s, "重试一下", verify_timeout_s=1)
    assert result.ok and result.retried
    assert (await _bubbles(page)).count("重试一下") == 1  # 不会重复发送


async def test_gives_up_after_one_retry(page):
    s = await _setup(page, ignore=5)
    result = await send_text(s, "发不出去", verify_timeout_s=0.5)
    assert not result.ok and result.retried
    assert "发不出去" not in await _bubbles(page)


async def test_refuses_when_box_not_empty(page):
    s = await _setup(page, prefill="别人没发完的草稿")
    with pytest.raises(SendError, match="已有内容"):
        await send_text(s, "新消息", verify_timeout_s=0.5)
    assert await _bubbles(page) == ["hi"]


async def test_newlines_are_flattened(page):
    s = await _setup(page)
    result = await send_text(s, "第一行\n第二行", verify_timeout_s=2)
    assert result.ok
    assert (await _bubbles(page))[0] == "第一行 第二行"


async def test_same_text_sent_twice_is_detected_by_count(page):
    s = await _setup(page)
    assert (await send_text(s, "哈哈", verify_timeout_s=2)).ok
    assert (await send_text(s, "哈哈", verify_timeout_s=2)).ok
    assert (await _bubbles(page)).count("哈哈") == 2


async def test_send_messages_one_by_one_with_pause(page):
    s = await _setup(page)
    result = await send_messages(s, ["哈哈哈", "这个也太真实了"], verify_timeout_s=2)
    assert result.ok and result.sent == 2 and result.detail == "已发送 2 条"
    assert (await _bubbles(page))[:2] == ["这个也太真实了", "哈哈哈"]  # 倒序：最新在最前
    assert (1.0, 3.0) in s.pauses  # 两条之间停顿


async def test_send_messages_stops_at_first_failure(page):
    s = await _setup(page, prefill="残留")  # 输入框非空：第一条就不发
    result = await send_messages(s, ["一", "二"], verify_timeout_s=0.5)
    assert not result.ok and result.sent == 0 and result.detail.startswith("第 1/2 条")
    assert await _bubbles(page) == ["hi"]


# 私信面板 + 会话行；顶部一条 discover-tab 栏（更高的 z-index）盖住会话行的上半部分，
# 复现 2026-09-27 Windows 上点会话行正中心一直超时的情况
PANEL = """
<div data-e2e="im-entry" style="position:fixed;top:850px;left:1300px;width:40px;height:30px"
  onclick="document.getElementById('dlg').style.display='block'">消息</div>
<div data-e2e="im-dialog" id="dlg"
  style="display:none;position:fixed;top:60px;left:1000px;width:360px;z-index:1">
  <div data-e2e="conversation-item" id="row" style="height:72px"
    onclick="window.opened = (window.opened || 0) + 1">
    <div class="conversationConversationItemtitle">小明</div><pre>分享[视频]</pre>
  </div>
</div>
<div class="discover-tab-container"
  style="position:fixed;top:0;left:0;width:100%;height:__COVER__px;z-index:10"></div>
"""


async def _panel(page, cover: int):
    await page.set_content(PANEL.replace("__COVER__", str(cover)))
    return FakeSession(page)


async def test_open_conversation_clicks_uncovered_part_of_row(page):
    s = await _panel(page, cover=60 + 36)  # 盖住会话行上半部分，正中心被挡
    await open_conversation(s, "小明")
    assert await page.evaluate("window.opened") == 1


async def test_open_conversation_refuses_when_row_fully_covered(page):
    s = await _panel(page, cover=60 + 72 + 10)
    with pytest.raises(SendError, match="整个盖住.*discover-tab-container"):
        await open_conversation(s, "小明")
    assert await page.evaluate("window.opened") is None


async def test_open_conversation_playwright_timeout_becomes_send_error(page):
    s = await _panel(page, cover=900)  # 连消息入口都被盖住：Playwright 点击超时
    page.set_default_timeout(1000)
    with pytest.raises(SendError, match="打不开会话"):  # 转成发送失败，不让整轮崩掉
        await open_conversation(s, "小明")
