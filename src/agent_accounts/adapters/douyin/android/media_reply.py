"""单作品媒体理解与回复；历史入口显式确认，不把历史作品冒充新消息。"""

from agent_accounts.adapters.douyin.android import autoreply as auto
from agent_accounts.adapters.douyin.android import shares, understanding
from agent_accounts.adapters.douyin.android.session import AndroidError
from agent_accounts.core import config, guard, store
from agent_accounts.core.errors import HumanRequired


def request_key(expected, card_hash):
    from agent_accounts.adapters.douyin.android.messaging import AndroidSend

    key = "media_" + shares.fingerprint(expected + ":" + card_hash)
    with store.session() as db:
        if db.get(AndroidSend, key):
            raise AndroidError("该会话的同文分享已尝试发送，不自动重复付费或重发")
    return key


async def prepare(
    s, run, cfg, expected, self_name, card_hash, *, check_active, expected_snapshot=None
):
    check_active()
    evidence = shares.inspect_share(s, expected, self_name, card_hash)
    shares.bottom(s, expected)
    baseline = auto.read_messages(s.source(), expected, self_name)
    if expected_snapshot is not None and not auto.same_tail(expected_snapshot, baseline):
        raise HumanRequired("打开作品期间收到新消息，未调用理解模型")

    def check():
        check_active()
        if not auto.same_tail(baseline, auto.read_messages(s.source(), expected, self_name)):
            raise HumanRequired("媒体理解期间聊天发生变化，停止本次回复")

    try:
        summary = await understanding.analyze(cfg, run, evidence, check_active=check)
    except (AndroidError, HumanRequired):
        raise
    except Exception as exc:
        raise AndroidError(
            f"媒体处理失败（{type(exc).__name__}），未生成回复且不自动重试"
        ) from None
    check()
    return evidence, summary, baseline


async def reply_share(
    s,
    run,
    cfg,
    expected,
    self_name,
    card_hash,
    *,
    confirmed=False,
    execute=False,
    generate=False,
    load_config=None,
    decider=None,
):
    if not confirmed:
        raise AndroidError("需明确确认当前互关私聊以及针对选定历史分享进行回复")
    loader = load_config or config.load
    mode = auto.permitted(cfg, cfg, execute)
    if execute and (not generate or mode != "on" or not cfg.douyin.android.allow_send):
        raise AndroidError("真实媒体回复需要生成许可与安卓发送开关")
    if card_hash not in {x["card_hash"] for x in shares.list_shares(s, expected, self_name)}:
        raise AndroidError("选定卡片不在当前已核对的对方分享中")
    pre = guard.check(cfg.guard, auto.guard_input(cfg, expected, mode))
    if not pre.ok:
        return {"status": "blocked", "reasons": pre.reasons}
    if not generate:
        return {"status": "dry_run", "model_called": False, "submitted": False}
    request_id = request_key(expected, card_hash)

    def check_active():
        fresh = loader()
        if fresh != cfg:
            raise HumanRequired("媒体回复配置变化，请重新确认")
        auto.permitted(fresh, cfg, execute)

    evidence, summary, baseline = await prepare(
        s, run, cfg, expected, self_name, card_hash, check_active=check_active
    )
    run.audit(
        "android.media.reply.bound",
        card_hash=card_hash,
        kind=evidence.kind,
        historical=True,
        request_id=request_id,
    )
    return await auto.respond(
        s,
        run,
        cfg,
        cfg,
        expected,
        self_name,
        baseline,
        [],
        mode,
        loader,
        decider or auto.decide_once,
        execute,
        summaries={card_hash: summary},
        historical_summary=summary,
        request_id=request_id,
    )
