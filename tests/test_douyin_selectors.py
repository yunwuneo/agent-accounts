"""在合成页面上跑一遍所有选择器策略。

合成页面只模拟布局特征，不代表真实抖音页面；真实页面的校准靠 ``douyin doctor``。
这里主要保证每段 JS 都能正常执行（locate 会吞掉单个策略的异常，脚本写错不会自己暴露）。
"""

from __future__ import annotations

import pytest

from agent_accounts.adapters.douyin import selectors as sel
from agent_accounts.adapters.douyin.page import detect_block
from agent_accounts.browser.locate import Strategy, Target, css, locate

pytestmark = pytest.mark.browser

ROW = """
<div style="display:flex;height:72px;width:320px">
  <img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" width="48" height="48">
  <div><div>{name}</div><div>{preview}</div></div><span>周二</span>
</div>"""

DOUYIN_LIKE = f"""
<body style="margin:0">
  <header style="position:fixed;top:0;left:0;right:0;height:60px;display:flex">
    <span style="margin-left:1000px">投稿</span><span style="margin-left:20px">消息</span>
  </header>
  <aside style="position:absolute;top:70px;left:1080px;width:340px">
    <div>消息（3）</div>
    <div id="rows">
      {ROW.format(name="小明", preview="分享[图集]")}
      {ROW.format(name="小红", preview="哈哈哈")}
    </div>
    <div>暂时没有更多了</div>
    <div style="display:flex;align-items:center;margin-top:20px">
      <div class="DraftEditor-root" style="width:260px">
        <div class="public-DraftEditor-content" contenteditable="true">&nbsp;</div>
      </div>
      <button style="width:32px;height:32px">↑</button>
    </div>
  </aside>
</body>"""


async def _hit(page, target):
    hit = await locate(page, target, timeout_ms=500)
    assert hit is not None, f"{target.name} 未命中"
    return hit


async def test_all_targets_on_synthetic_page(page):
    await page.set_content(DOUYIN_LIKE)
    entry = await _hit(page, sel.MESSAGES_ENTRY)
    assert entry.strategy == "geometry:top-bar-right"
    assert await entry.first.inner_text() == "消息"

    assert (await _hit(page, sel.MESSAGES_PANEL)).strategy == "text:消息（N）"

    rows = await _hit(page, sel.CONVERSATION_ROW)
    assert rows.count == 2

    assert (await _hit(page, sel.THREAD_INPUT)).strategy == "css:draftjs"
    send = await _hit(page, sel.SEND_BUTTON)
    assert await send.first.inner_text() == "↑"

    assert await locate(page, sel.LOGIN_BUTTON, timeout_ms=300) is None


async def test_login_button_in_top_bar(page):
    await page.set_content(
        '<header style="height:60px"><button style="margin-left:1200px">登录</button></header>'
    )
    await _hit(page, sel.LOGIN_BUTTON)


async def test_detect_block(page):
    await page.set_content("<div>正常页面</div>")
    assert await detect_block(page) is None
    await page.set_content("<div>请完成下列验证后继续</div>")
    assert "风控提示" in await detect_block(page)


async def test_locate_falls_back_and_times_out(page):
    await page.set_content('<p class="b">hi</p>')
    target = Target("t", "t", (css(".a", name="a"), css(".b", name="b")))
    assert (await locate(page, target, timeout_ms=300)).strategy == "b"

    async def boom(_page):
        raise RuntimeError("strategy error")

    broken = Target("t", "t", (Strategy("boom", boom), css(".missing")))
    assert await locate(page, broken, timeout_ms=300) is None
