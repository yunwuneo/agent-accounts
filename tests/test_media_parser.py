from __future__ import annotations

import io
import json
from email.message import Message

import pytest
from typer.testing import CliRunner

from agent_accounts.adapters.douyin.cli import app
from agent_accounts.adapters.douyin.parsers import download as dl
from agent_accounts.adapters.douyin.parsers import kuku
from agent_accounts.core.config import Config
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.run import start_run

VIDEO = "https://v3-test.douyinvod.com/sample.mp4?expires=123"
IMAGE = "https://p5-sign.douyinpic.com/sample.webp"
LINK = "https://v.douyin.com/Test123/"


def test_share_link_strips_tracking_but_rejects_non_work_links():
    assert dl.share_url(LINK + "?share_id=person") == LINK
    with pytest.raises(dl.ParseError):
        dl.share_url("https://v.douyin.com:bad/private")
    for url in (
        "https://example.com/x",
        "http://v.douyin.com/x",
        "https://www.douyin.com/user/123",
        "https://v.douyin.com@evil.test/x",
        "https://v.douyin.com/x#token",
        "复制打开 " + LINK,
        "https://v.douyin.com:444/x",
    ):
        with pytest.raises(dl.ParseError):
            dl.share_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://v3.douyinvod.com/x",
        "file:///etc/passwd",
        "https://127.0.0.1/x",
        "https://douyinvod.com.evil.test/x",
        "https://user:password@v3.douyinvod.com/x",
        "https://v3.douyinvod.com:444/x",
    ],
)
def test_download_host_restrictions(url):
    with pytest.raises(dl.ParseError):
        dl.media_url(url, resolve=False)


def test_private_dns_is_rejected(monkeypatch):
    monkeypatch.setattr(
        dl.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))]
    )
    with pytest.raises(dl.ParseError):
        dl.media_url(VIDEO)


def test_redirect_revalidated():
    with pytest.raises(dl.ParseError):
        dl.CheckedRedirect().redirect_request(None, None, 302, "", {}, "https://localhost/x")


@pytest.mark.parametrize(
    "notice", ["请完成安全验证", "请先登录", "请扫码付款", "余额不足", "拖动滑块"]
)
def test_human_required_does_not_claim_douyin_account_risk(notice):
    with pytest.raises(HumanRequired) as caught:
        kuku.check_notice(notice)
    assert not caught.value.freeze


def test_faq_not_confused_with_challenge():
    kuku.check_notice("为什么需要验证码？验证码是为了防止恶意刷量。免费使用，无需登录。")


def test_cover_not_mistaken_for_gallery_and_no_silent_truncation():
    elements = [{"tag": "VIDEO", "url": VIDEO}, {"tag": "IMG", "url": IMAGE, "alt": "Image 1"}]
    assert kuku.select_media(elements, "video", 1) == [VIDEO]
    with pytest.raises(dl.ParseError):
        kuku.select_media(elements, "gallery", 1)
    with pytest.raises(dl.ParseError):
        kuku.select_media(elements * 2, "video", 1)
    with pytest.raises(dl.ParseError):
        kuku.select_media(elements[1:], "gallery", 2)


def test_gallery_number_order_and_identical_pages_retained():
    elements = [
        {"tag": "IMG", "url": IMAGE, "alt": "Image 2"},
        {"tag": "IMG", "url": IMAGE, "alt": "Image 1"},
    ]
    assert kuku.select_media(elements, "gallery", 2) == [IMAGE, IMAGE]
    with pytest.raises(dl.ParseError):
        kuku.select_media(elements + elements, "gallery", 2)


class Response(io.BytesIO):
    def __init__(self, data, content_type="video/mp4", length=None):
        super().__init__(data)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(length if length is not None else len(data))
        self.status = 200
        self.url = VIDEO


def mock_download(monkeypatch, response):
    class Opener:
        def open(self, req, timeout):
            assert not req.has_header("Cookie") and not req.has_header("Authorization")
            return response

    monkeypatch.setattr(dl, "media_url", lambda value, **kw: value)
    monkeypatch.setattr(dl.urllib.request, "build_opener", lambda *a: Opener())


def test_download_exact_length_and_no_credentials(tmp_path, monkeypatch):
    mock_download(monkeypatch, Response(b"video data"))
    path = tmp_path / "video.bin"
    dl.download(VIDEO, path, "video", max_bytes=100, timeout_s=5)
    assert path.read_bytes() == b"video data"


@pytest.mark.parametrize(
    "response,limit",
    [
        (lambda: Response(b"video", length=99), 100),
        (lambda: Response(b"video", content_type="text/html"), 100),
        (lambda: Response(b"video"), 3),
    ],
)
def test_download_rejects_incomplete_wrong_type_oversize(tmp_path, monkeypatch, response, limit):
    mock_download(monkeypatch, response())
    path = tmp_path / "video.bin"
    with pytest.raises(dl.ParseError):
        dl.download(VIDEO, path, "video", max_bytes=limit, timeout_s=5)
    assert not path.exists() and not path.with_suffix(".part").exists()


async def test_dry_run_never_opens_browser(monkeypatch):
    monkeypatch.setattr(kuku, "async_playwright", lambda: pytest.fail("no browser in dry-run"))
    with start_run("douyin", "android.parse-media") as run:
        result = await kuku.parse_media(Config(), run, LINK, "video", 1)
    assert result["status"] == "dry_run" and not result["submitted"]


def test_cli_default_is_dry_run():
    result = CliRunner().invoke(app, ["android", "parse-media", LINK, "--kind", "video"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["submitted"] is False


async def mocked_site(page, content):
    # 所有请求都在本地拦截；测试不会向第三方提交任何作品。
    async def route(r):
        await r.fulfill(status=200, content_type="text/html; charset=utf-8", body=content)

    await page.route("**/*", route)


async def test_browser_submission_and_result_extraction(page):
    await mocked_site(
        page,
        f'''<input placeholder="粘贴带链接的文本">
    <button onclick="document.getElementById('result').hidden=false">开始解析</button>
    <section id="result" hidden><p>合成视频标题</p><h3>视频列表</h3>
    <video src="{VIDEO}"></video><h3>封面或图片列表</h3>
    <img alt="Image 1" src="{IMAGE}"></section>''',
    )
    result = await kuku.submit_and_resolve(
        page, LINK, "video", 1, expected_title="合成视频标题", timeout_s=5
    )
    assert result == [VIDEO]
    assert await page.get_by_placeholder("粘贴带链接的文本").input_value() == LINK


async def test_browser_challenge_stops_without_submit(page):
    await mocked_site(page, '<p>请完成安全验证</p><input placeholder="粘贴带链接的文本">')
    with pytest.raises(HumanRequired):
        await kuku.submit_and_resolve(page, LINK, "video", 1, expected_title="", timeout_s=5)
    assert await page.get_by_placeholder("粘贴带链接的文本").input_value() == ""


async def test_browser_title_mismatch_fails_before_download(page):
    await mocked_site(
        page,
        f'''<input placeholder="粘贴带链接的文本"><button>开始解析</button>
    <h3>视频列表</h3><video src="{VIDEO}"></video><p>其他标题</p>''',
    )
    with pytest.raises(dl.ParseError, match="预期标题"):
        await kuku.submit_and_resolve(
            page, LINK, "video", 1, expected_title="目标标题", timeout_s=5
        )


async def test_browser_dismisses_known_promotion_before_submit(page):
    await mocked_site(
        page,
        f'''<div id="promotion">
    <p>一次提交多个链接，批量解析更省时间</p>
    <button onclick="document.getElementById('promotion').remove()">7天不再提示</button></div>
    <input placeholder="粘贴带链接的文本"><button>开始解析</button>
    <h3>视频列表</h3><video src="{VIDEO}"></video>''',
    )
    result = await kuku.submit_and_resolve(page, LINK, "video", 1, expected_title="", timeout_s=5)
    assert result == [VIDEO]
    assert await page.locator("#promotion").count() == 0


async def test_browser_input_reset_never_submits(page):
    await mocked_site(
        page,
        """<input placeholder="粘贴带链接的文本"
    onblur="this.value=''">
    <button onclick="this.dataset.clicked='yes'">开始解析</button>""",
    )
    with pytest.raises(dl.ParseError, match="输入内容变化"):
        await kuku.submit_and_resolve(page, LINK, "video", 1, expected_title="", timeout_s=5)
    assert await page.get_by_role("button").get_attribute("data-clicked") is None
