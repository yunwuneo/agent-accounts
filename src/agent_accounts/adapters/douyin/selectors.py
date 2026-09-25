"""抖音网页版的选择器集中配置。

页面改版时只需要改这里。每个目标按「文本/role → 几何 → 稳定 class」的顺序给多种策略。
几何策略参考本机 douyin-messager skill 的经验：顶栏在页面最上方约 80px 内，
私信面板从右侧弹出，会话行是面板内重复出现的、带头像的多行条目。

⚠️ 以下策略基于 2026-09-25 手动跑通时的观察，需要用 ``douyin doctor`` 在真实页面上校准。
"""

from __future__ import annotations

import re

from agent_accounts.browser.locate import Target, css, js_mark, text

TOP_BAR_MAX_Y = 80

# 顶栏右侧「消息」入口（原来叫「私信」，带未读红点）
MESSAGES_ENTRY = Target(
    name="messages_entry",
    description="顶栏「消息」入口",
    strategies=(
        js_mark(
            "messages_entry",
            f"""
            const cands = [...document.querySelectorAll('body *')].filter(el => {{
                const t = (el.innerText || '').trim();
                if (!/^(消息|私信)\\s*\\d*$/.test(t)) return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.top >= 0 && r.bottom <= {TOP_BAR_MAX_Y}
                    && r.left > window.innerWidth * 0.5;
            }});
            // 取面积最小的（最内层）元素
            cands.sort((a, b) => {{
                const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
                return ra.width * ra.height - rb.width * rb.height;
            }});
            return cands[0];
            """,
            name="geometry:top-bar-right",
        ),
        text(re.compile(r"^(消息|私信)$"), name="text:消息"),
    ),
)

# 未登录时顶栏会出现「登录」按钮
LOGIN_BUTTON = Target(
    name="login_button",
    description="顶栏「登录」按钮（未登录标志）",
    strategies=(
        js_mark(
            "login_button",
            f"""
            return [...document.querySelectorAll('button, a, div, span')].find(el => {{
                if ((el.innerText || '').trim() !== '登录') return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.bottom <= {TOP_BAR_MAX_Y};
            }});
            """,
            name="geometry:top-bar",
        ),
    ),
)

# 右侧弹出的私信面板，标题形如「消息（N）」
MESSAGES_PANEL = Target(
    name="messages_panel",
    description="私信面板标题「消息（N）」",
    strategies=(
        text(re.compile(r"^(消息|私信)\s*[（(]\d+[)）]$"), name="text:消息（N）"),
        text("暂时没有更多了", name="text:列表底部"),
    ),
)

# 面板里的会话行：右半屏、高度 50–100px、含头像图片、至少两行文本，且重复出现
CONVERSATION_ROW = Target(
    name="conversation_row",
    description="私信面板里的会话行",
    strategies=(
        js_mark(
            "conversation_row",
            """
            const W = window.innerWidth;
            const ok = el => {
                const r = el.getBoundingClientRect();
                if (r.left < W * 0.4 || r.height < 50 || r.height > 100 || r.width < 220)
                    return false;
                if (!el.querySelector('img')) return false;
                const lines = (el.innerText || '').split('\\n').filter(s => s.trim());
                return lines.length >= 2 && lines.length <= 6;
            };
            // 找到拥有 ≥2 个符合条件的直接子元素的容器，这些子元素就是会话行
            let best = [];
            for (const box of document.querySelectorAll('body *')) {
                const rows = [...box.children].filter(ok);
                if (rows.length > best.length) best = rows;
            }
            return best.length >= 2 ? best : null;
            """,
            name="geometry:repeated-rows",
        ),
    ),
)

# 聊天详情底部的 Draft.js 输入框（Draft.js 的 public-* class 是库自带的，不会被混淆）
THREAD_INPUT = Target(
    name="thread_input",
    description="聊天输入框（Draft.js）",
    strategies=(
        css('.public-DraftEditor-content[contenteditable="true"]', name="css:draftjs"),
        js_mark(
            "thread_input",
            """
            return [...document.querySelectorAll('[contenteditable="true"]')].find(el => {
                const r = el.getBoundingClientRect();
                return r.width > 100 && r.left > window.innerWidth * 0.4;
            });
            """,
            name="geometry:contenteditable-right",
        ),
    ),
)

# 输入框右侧的发送按钮（红色圆形上箭头）：与输入框垂直居中对齐、近似正方形、位于其右侧
SEND_BUTTON = Target(
    name="send_button",
    description="发送按钮",
    strategies=(
        js_mark(
            "send_button",
            """
            const input = document.querySelector('[contenteditable="true"]');
            if (!input) return null;
            const ir = input.getBoundingClientRect();
            const cy = ir.top + ir.height / 2;
            const cands = [...document.querySelectorAll('button, svg, div, span')].filter(el => {
                const r = el.getBoundingClientRect();
                const sq = r.width >= 20 && r.width <= 56 && Math.abs(r.width - r.height) <= 6;
                return sq && r.left >= ir.right - 8 && Math.abs(r.top + r.height / 2 - cy) < 40;
            });
            cands.sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left);
            return cands[0];
            """,
            name="geometry:right-of-input",
        ),
    ),
)

# 风控 / 验证码信号。出现即停机通知人工，不做任何绕过。
BLOCK_URL_PATTERN = re.compile(r"verifycenter|captcha|verify\.snssdk|/security/", re.I)
BLOCK_TEXT_PATTERN = re.compile(
    r"安全验证|拖动滑块|请完成下列验证|操作过于频繁|账号存在(安全)?风险|访问异常"
)
