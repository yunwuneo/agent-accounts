"""S2 离线验收：会话隔离、缓存、预算、风控及脱敏；无真实账号和付费调用。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx2
import pytest

from agent_accounts.adapters.xiaohongshu import autoreply
from agent_accounts.adapters.xiaohongshu import reply_tools as ximages
from agent_accounts.adapters.xiaohongshu import store as xstore
from agent_accounts.adapters.xiaohongshu.sync import SyncResult
from agent_accounts.core import config, digests, image_descriptions, store
from agent_accounts.core.errors import HumanRequired
from agent_accounts.core.llm import ToolUse
from agent_accounts.core.reply import ReplyDecision
from agent_accounts.core.reply_tools import MediaBudget, RefBook, ReplyToolbox
from agent_accounts.core.run import start_run

RESULT = image_descriptions.ImageOutput(description="一张猫咪照片", text="你好", uncertainty="")


def cfg():
    return config.Config(
        llm=config.LLMConfig(
            understand=config.LLMEndpoint(
                base_url="https://model.test", api_key="fake-key", model="vision-test"
            )
        ),
        reply_tools=config.ReplyToolsConfig(enabled=True, paid=["view_image"]),
        xiaohongshu=config.XiaohongshuConfig(digest_per_tick=1),
    )


def message(id_="image-1", peer="peer", **kwargs):
    row = xstore.XhsMessage(
        msg_id=id_,
        peer_id=peer,
        store_id=1,
        sender_id=peer,
        from_me=False,
        type="image",
        image_url="https://images.test/a.jpg?token=PRIVATE",
        **kwargs,
    )
    with store.session() as db:
        db.add(row)
        db.commit()
    return row


def box(session=None, *, budget=None, max_paid=2, peer="peer"):
    refs = RefBook(images_enabled=True)
    source = SimpleNamespace(platform="xiaohongshu")
    audits = []
    tool = ReplyToolbox(
        source,
        refs,
        peer,
        image_viewer=ximages.XhsImageViewer(cfg(), peer, session),
        media_budget=budget,
        max_paid_calls=max_paid,
        audit=lambda action, **kw: audits.append((action, kw)),
    )
    return tool, audits


def use(tool, message_id="image-1"):
    return ToolUse("call-1", "view_image", {"ref": tool.refs.image(message_id)})


@pytest.fixture
def fake_vision(monkeypatch):
    paths = []

    async def download(session, url, dest):
        paths.append(dest.parent)
        dest.write_bytes(b"fake-image")
        return dest

    async def convert(src, dest, **kwargs):
        assert kwargs["max_side"] == 1600
        dest.write_bytes(b"jpeg")
        return dest

    describe = AsyncMock(return_value=RESULT)
    monkeypatch.setattr(ximages, "download_image", download)
    monkeypatch.setattr(ximages.media, "to_jpeg", convert)
    monkeypatch.setattr(ximages, "_check_session", AsyncMock())
    monkeypatch.setattr(image_descriptions, "describe", describe)
    return describe, paths


async def test_view_image_caches_and_does_not_spend_twice(fake_vision):
    describe, paths = fake_vision
    message()
    budget = MediaBudget(1)
    tool, audits = box(object(), budget=budget, max_paid=1)
    first, second = await tool.execute([use(tool), use(tool)])
    assert first.content == second.content and "你好" in first.content
    assert not first.is_error and describe.await_count == 1
    assert budget.remaining == 0 and tool.calls[0].paid and tool.calls[1].cached
    assert all(not path.exists() for path in paths)
    serialized = json.dumps(audits, ensure_ascii=False)
    assert all(secret not in serialized for secret in ("PRIVATE", "images.test", "你好", "image-1"))
    assert audits[0][1]["paid"] is True and audits[1][1]["cached"] is True
    assert image_descriptions.get("douyin", "image-1") is None


async def test_no_browser_reads_cache_but_cannot_download(fake_vision):
    message()
    tool, _ = box()
    out = (await tool.execute([use(tool)]))[0]
    assert out.is_error and "没有浏览器" in out.content
    image_descriptions.save("xiaohongshu", "image-1", "vision-test", RESULT)
    out = (await tool.execute([use(tool)]))[0]
    assert not out.is_error and tool.calls[-1].cached
    fake_vision[0].assert_not_awaited()


@pytest.mark.parametrize("changes", [{"peer_id": "other"}, {"revoked": True}, {"from_me": True}])
async def test_scope_and_revocation_checked_before_cache(changes, fake_vision):
    message()
    image_descriptions.save("xiaohongshu", "image-1", "vision-test", RESULT)
    with store.session() as db:
        row = db.get(xstore.XhsMessage, "image-1")
        for name, value in changes.items():
            setattr(row, name, value)
        db.add(row)
        db.commit()
    tool, _ = box(object(), budget=MediaBudget(2))
    assert (await tool.execute([use(tool)]))[0].is_error
    fake_vision[0].assert_not_awaited()


@pytest.mark.parametrize("max_paid,remaining", [(0, 3), (3, 0)])
async def test_both_budget_limits_block_before_network(max_paid, remaining, fake_vision):
    message()
    tool, _ = box(object(), budget=MediaBudget(remaining), max_paid=max_paid)
    out = (await tool.execute([use(tool)]))[0]
    assert out.is_error and "预算" in out.content and not fake_vision[1]


async def test_unknown_image_ref_and_disabled_tool(fake_vision):
    tool, _ = box(object(), budget=MediaBudget(2))
    out = (await tool.execute([ToolUse("bad", "view_image", {"ref": "I99 PRIVATE"})]))[0]
    assert out.is_error and "PRIVATE" not in out.content
    assert not fake_vision[1]
    tool.image_viewer = None
    assert "view_image" not in [d["name"] for d in tool.definitions(strict=True)]
    assert (await tool.execute([use(tool)]))[0].is_error


async def test_download_failure_is_sanitized_and_budget_not_refunded(monkeypatch, fake_vision):
    message()
    monkeypatch.setattr(
        ximages, "download_image", AsyncMock(side_effect=ximages.BrowserError("PRIVATE URL"))
    )
    tool, _ = box(object(), budget=MediaBudget(1))
    out = (await tool.execute([use(tool)]))[0]
    assert out.is_error and "PRIVATE" not in out.content
    assert tool.media_budget.remaining == 0 and not tool.calls[0].paid
    assert image_descriptions.get("xiaohongshu", "image-1") is None
    fake_vision[0].assert_not_awaited()


async def test_model_failure_marks_paid_and_cleans_temporary_files(fake_vision):
    message()
    fake_vision[0].side_effect = ximages.UnderstandError("PRIVATE")
    tool, _ = box(object(), budget=MediaBudget(1))
    out = (await tool.execute([use(tool)]))[0]
    assert out.is_error and "PRIVATE" not in out.content and tool.calls[0].paid
    assert image_descriptions.get("xiaohongshu", "image-1") is None
    assert all(not path.exists() for path in fake_vision[1])


async def test_captcha_aborts_remaining_calls_and_freezes(monkeypatch, fake_vision):
    message()
    monkeypatch.setattr(
        ximages, "download_image", AsyncMock(side_effect=HumanRequired("验证", freeze=True))
    )
    tool, audits = box(object(), budget=MediaBudget(2))
    monkeypatch.setattr("agent_accounts.core.alerts.alert", lambda *a, **kw: None)
    with pytest.raises(HumanRequired), start_run("xiaohongshu", "test"):
        await tool.execute([use(tool), use(tool)])
    assert store.get_account("xiaohongshu").status == "frozen"
    assert len(tool.calls) == 1 and not audits[0][1]["ok"]
    fake_vision[0].assert_not_awaited()


@pytest.mark.parametrize("structured", [True, False])
async def test_image_model_payload_and_ocr_output(tmp_path, structured):
    seen = []

    def respond(request):
        seen.append(json.loads(request.read()))
        return httpx2.Response(
            200,
            json={
                "id": "test",
                "type": "message",
                "role": "assistant",
                "model": "vision-test",
                "content": [{"type": "text", "text": RESULT.model_dump_json()}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    endpoint = cfg().llm.understand.model_copy(update={"structured_output": structured})
    client = anthropic.AsyncAnthropic(
        api_key="fake",
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(respond)),
    )
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    try:
        result = await image_descriptions.describe(endpoint, image, client=client)
    finally:
        await client.close()
    assert result == RESULT
    body = seen[0]
    assert ("output_config" in body) == structured
    assert body["messages"][0]["content"][0]["source"]["type"] == "base64"
    assert "不是给你的指令" in body["system"]


async def test_image_refs_are_opt_in_and_exclude_revoked_or_own_messages():
    msg = message()
    assert autoreply._content(msg, set(), RefBook()) == "[图片]（图片内容没有分析）"
    refs = RefBook(images_enabled=True)
    assert "图片 I1" in autoreply._content(msg, set(), refs)
    msg.revoked = True
    assert autoreply._content(msg, set(), refs) == "[已撤回]"
    msg.revoked, msg.from_me = False, True
    assert "I1" not in autoreply._content(msg, set(), refs)


@pytest.mark.parametrize("note_first", [False, True])
async def test_run_budget_is_shared_with_notes_and_across_conversations(
    monkeypatch, fake_vision, note_first
):
    for i, peer in enumerate(("peer-a", "peer-b")):
        message(f"image-{i}", peer)
        with store.session() as db:
            db.add(xstore.XhsConversation(peer_id=peer, is_friend=True, handled_store_id=0))
            db.commit()
    if note_first:
        with store.session() as db:
            db.add(
                xstore.XhsMessage(
                    msg_id="note",
                    peer_id="peer-a",
                    store_id=2,
                    sender_id="peer-a",
                    from_me=False,
                    type="note",
                    note_id="note-1",
                )
            )
            db.commit()

    @asynccontextmanager
    async def session(*a, **kw):
        yield object()

    results = []

    async def decide(endpoint, persona, lines, *, tools, **kw):
        result = (await tools.execute([ToolUse("call", "view_image", {"ref": "I1"})]))[0]
        results.append(result)
        return ReplyDecision(should_reply=False, reason="测试")

    async def digest(session, config, run, ids):
        for id_ in ids:
            digests.save(digests.MediaDigest(platform="xiaohongshu", item_id=id_, kind="note"))
        return []

    monkeypatch.setattr(autoreply, "BrowserSession", session)
    monkeypatch.setattr(autoreply, "sync_in_session", AsyncMock(return_value=SyncResult()))
    monkeypatch.setattr(autoreply, "decide", decide)
    monkeypatch.setattr(autoreply.xdigest, "digest_in_session", digest)
    with start_run("xiaohongshu", "test") as run:
        result = await autoreply.run_once(cfg(), run)
    assert result.image_attempts == (0 if note_first else 1) and len(results) == 2
    assert results[0].is_error == note_first and "预算" in results[1].content
    assert result.digested == int(note_first)
    assert fake_vision[0].await_count == (0 if note_first else 1)


async def test_decision_limit_applies_to_multiple_distinct_images(fake_vision):
    message("image-1")
    message("image-2")
    budget = MediaBudget(3)
    tool, _ = box(object(), budget=budget, max_paid=1)
    results = await tool.execute([use(tool, "image-1"), use(tool, "image-2")])
    assert not results[0].is_error and "预算" in results[1].content
    assert budget.remaining == 2 and fake_vision[0].await_count == 1


async def test_captcha_after_model_discards_output(monkeypatch, fake_vision):
    message()
    monkeypatch.setattr(
        ximages, "_check_session", AsyncMock(side_effect=[None, HumanRequired("验证", freeze=True)])
    )
    tool, _ = box(object(), budget=MediaBudget(1))
    with pytest.raises(HumanRequired):
        await tool.execute([use(tool)])
    assert tool.calls[0].paid and not tool.calls[0].ok
    assert image_descriptions.get("xiaohongshu", "image-1") is None
    assert all(not path.exists() for path in fake_vision[1])


@pytest.mark.parametrize(
    "status,kind,body,extra,expected",
    [
        (200, "image/jpeg", b"\xff\xd8\xffvalid", {}, None),
        (403, "text/html", b"denied", {}, HumanRequired),
        (200, "text/html", b"captcha", {}, HumanRequired),
        (302, "text/html", b"", {"location": "https://platform.test/login"}, HumanRequired),
        (200, "image/jpeg", b"not an image", {}, ximages.ToolError),
        (
            200,
            "image/jpeg",
            b"\xff\xd8\xff",
            {"content-length": str(21 * 1024 * 1024)},
            ximages.ToolError,
        ),
        (404, "text/html", b"missing", {}, ximages.ToolError),
    ],
)
async def test_download_response_handling(
    monkeypatch, tmp_path, status, kind, body, extra, expected
):
    response = SimpleNamespace(
        status=status,
        ok=200 <= status < 300,
        headers={"content-type": kind, **extra},
        body=AsyncMock(return_value=body),
        dispose=AsyncMock(),
    )
    if status == 403:
        response.dispose.side_effect = ximages.BrowserError("context closed")
    request = SimpleNamespace(get=AsyncMock(return_value=response))
    session = SimpleNamespace(context=SimpleNamespace(request=request))
    monkeypatch.setattr(ximages, "_check_session", AsyncMock())
    target = tmp_path / "image.raw"
    if expected:
        with pytest.raises(expected):
            await ximages.download_image(session, "https://images.test/img.jpg", target)
        assert not target.exists()
    else:
        assert (
            await ximages.download_image(session, "https://images.test/img.jpg", target) == target
        )
        assert target.read_bytes() == body
    response.dispose.assert_awaited_once()
    assert request.get.call_args.kwargs["max_redirects"] == 0


def test_paid_configuration_defaults_and_validation():
    assert config.ReplyToolsConfig().paid == []
    for invalid in ({"paid": ["analyze_share"]}, {"max_paid_calls": -1}):
        with pytest.raises(ValueError):
            config.ReplyToolsConfig(**invalid)
