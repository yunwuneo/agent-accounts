"""发送器测试：合成页面模拟输入框、发送按钮和消息气泡（结构按 2026-09-25 真实页面）。"""

from __future__ import annotations

import pytest

from agent_accounts.adapters.douyin.sender import SendError, send_text

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
    document.getElementById('list').appendChild(b);
    box.innerText = '';
  });
</script>
"""


class FakeSession:
    def __init__(self, page):
        self.page = page

    async def pause(self, lo=None, hi=None):
        return None


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
    assert await _bubbles(page) == ["hi", "你好呀"]


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
    assert (await _bubbles(page))[-1] == "第一行 第二行"
