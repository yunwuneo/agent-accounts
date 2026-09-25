from __future__ import annotations

import pytest
from playwright.async_api import async_playwright

from agent_accounts.core import paths


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """每个测试一个独立的数据目录，绝不碰真实的 ~/.agent-accounts。"""
    home = tmp_path / "aa-home"
    monkeypatch.setenv(paths.ENV_HOME, str(home))
    return home


@pytest.fixture
async def page():
    """用本机 Google Chrome 跑无头页面；没有 Chrome 时跳过。"""
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"本机没有可用的 Chrome：{e}")
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        yield await ctx.new_page()
        await browser.close()
