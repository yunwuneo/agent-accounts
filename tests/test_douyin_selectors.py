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


async def test_data_e2e_preferred(page):
    # 结构摘自 2026-09-25 真实页面（内容已替换）
    await page.set_content(
        '<div data-e2e="im-entry" style="margin-left:1300px">消息</div>'
        '<div data-e2e="im-dialog"><div data-index="0">'
        '<div data-e2e="conversation-item">某人 hi</div></div></div>'
    )
    assert (await _hit(page, sel.MESSAGES_ENTRY)).strategy == "data-e2e:im-entry"
    assert (await _hit(page, sel.MESSAGES_PANEL)).strategy == "data-e2e:im-dialog"
    rows = await _hit(page, sel.CONVERSATION_ROW)
    assert (rows.strategy, rows.count) == ("data-e2e:conversation-item", 1)


MSG_INPUT = """
<div style="position:absolute;top:820px;left:1100px;width:300px;display:flex">
  <div data-e2e="msg-input" style="display:flex;align-items:center">
    <div><div data-slate-editor="true" contenteditable="true" style="width:180px">​</div></div>
    <svg width="32" height="32"></svg>
    <svg width="36" height="36" class="{send_class}"></svg>
  </div>
</div>
<!-- 右下角悬浮按钮：在输入框右侧、垂直位置相近，不能当成发送按钮 -->
<button style="position:fixed;left:1390px;top:830px;width:32px;height:32px">☰</button>
"""


async def test_msg_input_and_send_button(page):
    await page.set_content(MSG_INPUT.format(send_class="publishBtn e2e-send-msg-btn"))
    assert (await _hit(page, sel.THREAD_INPUT)).strategy == "data-e2e:msg-input"
    send = await _hit(page, sel.SEND_BUTTON)
    assert send.strategy == "e2e-class:send-msg-btn"


async def test_send_button_geometry_ignores_floating_button(page):
    await page.set_content(MSG_INPUT.format(send_class=""))
    send = await _hit(page, sel.SEND_BUTTON)
    assert send.strategy == "geometry:right-of-input"
    assert await send.first.get_attribute("width") == "36"


async def test_single_conversation_row_geometry(page):
    await page.set_content(
        '<aside style="position:absolute;left:1080px;width:340px">'
        + ROW.format(name="小明", preview="hi")
        + "</aside>"
    )
    rows = await _hit(page, sel.CONVERSATION_ROW)
    assert (rows.strategy, rows.count) == ("geometry:rows", 1)


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


async def test_detect_block_ignores_hidden_verify_iframe(page):
    verify_url = "https://verify.test/obj/rc-verifycenter/rmc-nocaptcha/index.html"
    await page.route(verify_url, lambda route: route.fulfill(body="<p>verify</p>"))

    await page.set_content(f'<iframe src="{verify_url}" style="display:none"></iframe>')
    await page.wait_for_load_state()
    assert len(page.frames) == 2
    assert await detect_block(page) is None

    await page.set_content(f'<iframe src="{verify_url}" width="300" height="200"></iframe>')
    await page.wait_for_load_state()
    assert await detect_block(page) == "页面中出现了验证码弹窗"


async def test_locate_falls_back_and_times_out(page):
    await page.set_content('<p class="b">hi</p>')
    target = Target("t", "t", (css(".a", name="a"), css(".b", name="b")))
    assert (await locate(page, target, timeout_ms=300)).strategy == "b"

    async def boom(_page):
        raise RuntimeError("strategy error")

    broken = Target("t", "t", (Strategy("boom", boom), css(".missing")))
    assert await locate(page, broken, timeout_ms=300) is None


async def test_dom_inbox_row_extraction(page):
    """DOM 兜底读会话行。结构摘自 2026-09-25 真实页面，内容已替换。"""
    from agent_accounts.adapters.douyin.sync import _DOM_ROWS_JS

    await page.set_content(
        '<div data-e2e="conversation-item" class="conversationConversationItemwrapper">'
        '<div class="conversationConversationItemtitle">某人</div>'
        '<div class="ConversationItemTagNextToTitletimeStr">10:47</div>'
        '<pre class="ConversationItemHinttextBox">hi</pre>'
        '<span class="semi-badge"><span x-semi-prop="count">3</span></span></div>'
        '<div data-e2e="conversation-item"><div class="conversationConversationItemtitle">另一人'
        "</div><pre>分享[图集]</pre></div>"
    )
    rows = await page.locator('[data-e2e="conversation-item"]').evaluate_all(_DOM_ROWS_JS)
    assert rows == [
        {"name": "某人", "time_text": "10:47", "preview": "hi", "unread": 3},
        {"name": "另一人", "time_text": "", "preview": "分享[图集]", "unread": 0},
    ]
