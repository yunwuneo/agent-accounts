"""S3：补分析作用域、共用预算，以及抖音新分享未完成时不决策、不推进。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_accounts.adapters.douyin import autoreply as dauto
from agent_accounts.adapters.douyin import digest as ddigest
from agent_accounts.adapters.douyin import reply_tools as dtools
from agent_accounts.adapters.douyin import store as ds
from agent_accounts.adapters.xiaohongshu import digest as xdigest
from agent_accounts.adapters.xiaohongshu import reply_tools as xtools
from agent_accounts.adapters.xiaohongshu import store as xs
from agent_accounts.core import config, digests, store
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.llm import ToolUse
from agent_accounts.core.reply import ReplyDecision
from agent_accounts.core.reply_tools import MediaBudget, RefBook, ReplyToolbox, ToolError
from agent_accounts.core.run import start_run


def cached(platform, item_id):
    return digests.save(
        digests.MediaDigest(
            platform=platform, item_id=item_id, kind="video", summary="已分析的内容"
        )
    )


def dmessage(index, *, item=None, conv="conv", from_me=False):
    row = ds.DouyinMessage(
        msg_id=f"{conv}-{index}",
        conv_id=conv,
        msg_index=index,
        raw_type=7,
        type="video_share" if item else "text",
        sender_uid="peer",
        from_me=from_me,
        aweme_id=item,
        text="测试文本",
    )
    with store.session() as db:
        db.add(row)
        db.commit()
    return row


def xmessage(id_, *, peer="peer", revoked=False, token="scoped-token"):
    row = xs.XhsMessage(
        msg_id=id_,
        peer_id=peer,
        store_id=1,
        sender_id=peer,
        from_me=False,
        type="note",
        note_id="item",
        note_xsec_token=token,
        revoked=revoked,
    )
    with store.session() as db:
        db.add(row)
        db.commit()
    return row


@pytest.fixture
def tick_env(monkeypatch):
    with store.session() as db:
        db.add(
            ds.DouyinConversation(
                conv_id="conv", handled_index=0, peer_follow_status=2, last_index=0
            )
        )
        db.commit()
    calls = {"digest": [], "decide": []}

    @asynccontextmanager
    async def session(*args, **kwargs):
        assert kwargs["headless"] is False
        yield object()

    async def digest(s, cfg, run, ids):
        calls["digest"].append(ids)
        return [ddigest.DigestOutcome(id_, cached("douyin", id_)) for id_ in ids]

    async def decide(cfg, persona, lines, **kwargs):
        calls["decide"].append(lines)
        return ReplyDecision(should_reply=True, messages=["看到了"], confidence=0.9)

    monkeypatch.setattr(dauto, "BrowserSession", session)
    monkeypatch.setattr(dauto, "sync_in_session", AsyncMock())
    monkeypatch.setattr(dauto.ddigest, "digest_in_session", digest)
    monkeypatch.setattr(dauto, "decide", decide)
    monkeypatch.setattr("agent_accounts.core.alerts.alert", lambda *a, **kw: None)
    return calls


async def tick(budget=1, **kwargs):
    cfg = config.Config(douyin=config.DouyinConfig(digest_per_tick=budget, **kwargs))
    with start_run("douyin", "test") as run:
        return await dauto.run_once(cfg, run)


async def test_backlog_resumes_after_all_new_shares_are_analyzed(tick_env):
    dmessage(1, item="one")
    dmessage(2, item="two")
    dmessage(3)
    first = await tick()
    assert first.outcomes[0].action == "deferred" and tick_env["decide"] == []
    assert ds.find_conversation("conv").handled_index == 0
    second = await tick()
    assert second.outcomes[0].action == "dry_run"
    assert tick_env["digest"] == [["one"], ["two"]]
    assert ds.find_conversation("conv").handled_index == 3
    assert sum(line.is_new for line in tick_env["decide"][0]) == 3


async def test_failed_new_share_never_advances_or_calls_reply_model(tick_env, monkeypatch):
    dmessage(1, item="failed")
    download = AsyncMock(return_value=[ddigest.DigestOutcome("failed", error="failed")])
    monkeypatch.setattr(dauto.ddigest, "digest_in_session", download)
    for _ in range(2):
        assert (await tick()).outcomes[0].action == "deferred"
    assert download.await_count == 2 and tick_env["decide"] == []
    assert ds.find_conversation("conv").handled_index == 0


async def test_new_messages_are_not_truncated_at_200_or_context_limit(tick_env):
    dmessage(1, item="earliest")
    for index in range(2, 225):
        dmessage(index, from_me=index % 2 == 0)
    result = await tick(context_messages=3)
    assert result.outcomes[0].action == "dry_run" and tick_env["digest"] == [["earliest"]]
    lines = tick_env["decide"][0]
    assert len(lines) == 224 and "作品摘要" in lines[0].content
    assert sum(line.is_new for line in lines) == 112


async def test_unavailable_share_is_explicit_degraded_context(tick_env):
    dmessage(1, item="hidden")
    row = cached("douyin", "hidden")
    row.available = False
    digests.save(row)
    assert (await tick(budget=0)).outcomes[0].action == "dry_run"
    assert "不可见" in tick_env["decide"][0][0].content and tick_env["digest"] == []


async def test_guard_blocks_before_any_media_cost(tick_env):
    dmessage(1, item="one")
    with store.session() as db:
        row = db.get(ds.DouyinConversation, "conv")
        row.peer_follow_status = 0
        db.add(row)
        db.commit()
    assert (await tick()).outcomes[0].action == "blocked"
    assert tick_env["digest"] == [] and tick_env["decide"] == []


async def test_captcha_during_new_share_stops_and_freezes(tick_env, monkeypatch):
    dmessage(1, item="one")
    monkeypatch.setattr(
        dauto.ddigest,
        "digest_in_session",
        AsyncMock(side_effect=HumanRequired("验证", freeze=True)),
    )
    with pytest.raises(HumanRequired):
        await tick()
    assert store.get_account("douyin").status == "frozen"
    assert ds.find_conversation("conv").handled_index == 0 and tick_env["decide"] == []


class FakeAnalyzer:
    available = True

    def __init__(self, fail=False):
        self.result = None
        self.calls = 0
        self.fail = fail

    def cached(self, id_):
        return self.result

    async def analyze(self, id_, on_model):
        self.calls += 1
        on_model()
        if self.fail:
            raise ToolError("暂不可用")
        self.result = "描述结果"
        return self.result


def toolbox(share, image=None, *, limit=2, budget=None):
    refs = RefBook(images_enabled=True)
    refs.share("item")
    refs.image("image")
    return ReplyToolbox(
        SimpleNamespace(platform="xiaohongshu"),
        refs,
        "peer",
        share_analyzer=share,
        image_viewer=image,
        max_paid_calls=limit,
        media_budget=budget or MediaBudget(3),
    )


def call(name="analyze_share", ref="S1"):
    return ToolUse("test-call", name, {"ref": ref})


@pytest.mark.parametrize("limit,remaining", [(1, 3), (3, 1)])
async def test_share_and_image_use_same_budgets(limit, remaining):
    share, image = FakeAnalyzer(), FakeAnalyzer()
    tool = toolbox(share, image, limit=limit, budget=MediaBudget(remaining))
    results = await tool.execute([call(), call("view_image", "I1"), call()])
    assert not results[0].is_error and "预算" in results[1].content
    assert not results[2].is_error and tool.calls[2].cached
    assert share.calls == 1 and image.calls == 0 and tool.calls[0].paid


async def test_failed_share_not_retried_in_same_tick_across_toolboxes():
    analyzer = FakeAnalyzer(fail=True)
    budget = MediaBudget(3)
    one, two = toolbox(analyzer, budget=budget), toolbox(analyzer, budget=budget)
    assert (await one.execute([call()]))[0].is_error
    assert "本轮已尝试" in (await two.execute([call()]))[0].content
    assert analyzer.calls == 1 and budget.remaining == 2


async def test_disabled_tool_invalid_ref_and_no_browser():
    tool = toolbox(None)
    assert "analyze_share" not in [t["name"] for t in tool.definitions(strict=True)]
    assert (await tool.execute([call()]))[0].is_error
    analyzer = FakeAnalyzer()
    analyzer.available = False
    tool = toolbox(analyzer)
    assert (await tool.execute([call(ref="S999")]))[0].is_error
    assert "没有浏览器" in (await tool.execute([call()]))[0].content
    assert analyzer.calls == 0


@pytest.mark.parametrize("platform", ["douyin", "xiaohongshu"])
async def test_analyzer_passes_scoped_card_and_rejects_other_conversation(platform, monkeypatch):
    cfg = config.Config()
    if platform == "douyin":
        dmessage(1, item="item", conv="other")
        analyzer_type, module = dtools.DouyinShareAnalyzer, ddigest
        scoped_id = "conv"
        monkeypatch.setattr(dtools.DouyinShareAnalyzer, "_check", AsyncMock())
    else:
        xmessage("outside", peer="other", token="PRIVATE-OUTSIDE")
        analyzer_type, module = xtools.XhsShareAnalyzer, xdigest
        scoped_id = "peer"
        monkeypatch.setattr(xtools, "_check_session", AsyncMock())
    with start_run(platform, "test") as run:
        analyzer = analyzer_type(cfg, scoped_id, object(), run)
        cached(platform, "item")
        with pytest.raises(ToolError):
            analyzer.cached("item")
        if platform == "douyin":
            dmessage(2, item="item")
        else:
            xmessage("inside")
        assert "已分析" in analyzer.cached("item")
        seen = []

        async def digest(*args, message, on_model):
            seen.append(message)
            on_model()
            return SimpleNamespace(error=None, digest=cached(platform, "item"))

        monkeypatch.setattr(module, "_digest_one", digest)
        paid = []
        await analyzer.analyze("item", lambda: paid.append(True))
        assert paid == [True]
        assert (seen[0].conv_id if platform == "douyin" else seen[0].peer_id) == scoped_id
        if platform == "xiaohongshu":
            assert seen[0].note_xsec_token == "scoped-token"
            with store.session() as db:
                row = db.get(xs.XhsMessage, "inside")
                row.revoked = True
                db.add(row)
                db.commit()
            with pytest.raises(ToolError):
                analyzer.cached("item")


@pytest.mark.parametrize("platform", ["douyin", "xiaohongshu"])
async def test_cover_fallback_does_not_swallow_captcha(platform, monkeypatch, tmp_path):
    module = ddigest if platform == "douyin" else xdigest
    media = module.dmedia if platform == "douyin" else module.xmedia
    monkeypatch.setattr(
        media, "download", AsyncMock(side_effect=HumanRequired("验证", freeze=True))
    )
    message = SimpleNamespace(
        type="video_share",
        share_title="t",
        share_author="a",
        note_type="video",
        note_title="t",
        note_author="a",
        cover_url="https://test.invalid",
    )
    item = SimpleNamespace(filter_reason="不可见", unavailable_reason="不可见")
    with pytest.raises(HumanRequired):
        await module._unavailable_input(None, config.Config(), item, tmp_path, message=message)


def test_paid_config_accepts_both_tools():
    assert config.ReplyToolsConfig(paid=["view_image", "analyze_share"]).max_paid_calls == 2


@pytest.mark.parametrize("platform", ["douyin", "xiaohongshu"])
@pytest.mark.parametrize("fails", [False, True])
async def test_digest_pipeline_uses_scoped_fallback_and_sanitizes_errors(
    platform, fails, monkeypatch
):
    module = ddigest if platform == "douyin" else xdigest
    media = module.dmedia if platform == "douyin" else module.xmedia
    item = SimpleNamespace(available=False, filter_reason="不可见", unavailable_reason="不可见")
    resolve = AsyncMock(return_value=item)
    monkeypatch.setattr(media, "resolve", resolve)
    # 已提供会话内的卡片，不允许退回全局查找其他会话。
    monkeypatch.setattr(module, "share_message", lambda _: pytest.fail("unscoped lookup"))
    msg = SimpleNamespace(
        type="video_share",
        share_title="本会话标题",
        share_author="作者",
        note_type="video",
        note_title="本会话标题",
        note_author="作者",
        note_xsec_token="scoped-token",
        cover_url=None,
    )
    seen = []

    async def understand(endpoint, inp):
        seen.append(inp)
        if fails:
            raise module.UnderstandError("https://private.invalid?token=SECRET")
        return SimpleNamespace(summary="内容摘要", vibe="轻松", reply_hooks=["接话点"])

    monkeypatch.setattr(module, "understand", understand)
    cfg = config.Config(
        llm=config.LLMConfig(understand=config.LLMEndpoint(api_key="fake-key", model="fake"))
    )
    audit, paid = [], []
    run = SimpleNamespace(audit=lambda *args, **kwargs: audit.append((args, kwargs)))
    args = (None, cfg, run, "item") + (("video",) if platform == "douyin" else ())
    out = await module._digest_one(*args, message=msg, on_model=lambda: paid.append(True))
    assert seen[0].title == "本会话标题" and paid == [True]
    if platform == "xiaohongshu":
        assert resolve.call_args.args[2] == "scoped-token"
    assert "SECRET" not in str(audit) + str(out.error)
    if fails:
        assert out.error and digests.get(platform, "item") is None
    else:
        assert not out.error and not digests.get(platform, "item").available


async def test_tool_captcha_aborts_later_calls_and_freezes(monkeypatch):
    monkeypatch.setattr("agent_accounts.core.alerts.alert", lambda *args, **kwargs: None)
    share, image = FakeAnalyzer(), FakeAnalyzer()
    share.analyze = AsyncMock(side_effect=HumanRequired("验证", freeze=True))
    with pytest.raises(HumanRequired), start_run("douyin", "test"):
        await toolbox(share, image).execute([call(), call("view_image", "I1")])
    assert image.calls == 0 and store.get_account("douyin").status == "frozen"
