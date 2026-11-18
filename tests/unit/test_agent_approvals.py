"""M8 审批队列（agent/approvals.py）与常驻进程处理 approve / reject（ops/commands.py）。PROGRESS「M8 计划」决定 2、3、4、7。

- 提议只写 approvals，不碰闸门、不碰券商
- 同时最多 5 个待批准、每天最多 20 个；30 分钟或当天收盘（美东 16:00）先到即过期
- approve 命令必须带 ADR-0034 签名（approve|id|ts|nonce，[resume] key），nonce 只能用一次；伪造、过期、重放都拒绝
- 批准后由常驻进程 gate.submit(source=AGENT)；REAL 下闸门照样拦（M4 起 REAL 关闭）
- auto_approve_simulate：只在 SIMULATE 下由常驻进程在进程内批准
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from oms_harness import make_system

from mooapp.agent.approvals import MAX_PENDING, MAX_PER_DAY, ApprovalError, ApprovalQueue
from mooapp.common import Env, Source
from mooapp.ops.approve_auth import approve_payload
from mooapp.ops.commands import CommandClient, CommandProcessor

KEY = bytes.fromhex("44" * 32)
ORDER = {"symbol": "US.SPY", "side": "BUY", "qty": 1, "limit_price": "10.00"}


def setup(env=Env.SIMULATE, **kw):
    sys = make_system(env=env, **kw)
    q = ApprovalQueue(sys.store, sys.clock)
    proc = CommandProcessor(gate=sys.gate, store=sys.store, clock=sys.clock, bus=sys.bus, resume_key=KEY)
    client = CommandClient(store=sys.store, clock=sys.clock, timeout=10.0, poll_interval=0.5)
    proc.heartbeat()
    return sys, q, proc, client


def approve(sys, proc, client, approval_id, key=KEY, ts=None, nonce=None):
    c = client.enqueue("approve", approve_payload(key, approval_id, ts or sys.clock.now(), nonce=nonce), actor="cli:test")
    proc.process_one()
    return sys.store.get_command(c)


# ------------------------------------------------------------------ 提议


def test_propose_writes_approval_only():
    sys, q, proc, client = setup()
    a = q.propose("order", ORDER, "均线金叉，试 1 股", caller="agent:mcp")
    assert a.status == "pending" and a.kind == "order" and a.payload == ORDER
    assert sys.store.get_approval(a.id).reason == "均线金叉，试 1 股"
    assert sys.broker.calls("place_order") == []
    assert sys.store.audit_records() == []  # 没经过闸门
    assert proc.process_one() is False  # 命令表里也没有东西


def test_expiry_30_minutes_or_close_whichever_first():
    sys, q, *_ = setup()  # harness：美东 10:00
    assert q.propose("order", ORDER, "r", "agent").expires_at == sys.clock.now() + timedelta(minutes=30)
    sys.clock.advance((datetime(2026, 11, 13, 20, 45, tzinfo=UTC) - sys.clock.now()).total_seconds())  # 美东 15:45
    assert q.propose("order", ORDER, "r", "agent").expires_at == datetime(2026, 11, 13, 21, 0, tzinfo=UTC)  # 16:00


def test_max_pending():
    sys, q, *_ = setup()
    for _ in range(MAX_PENDING):
        q.propose("order", ORDER, "r", "agent")
    with pytest.raises(ApprovalError, match="待批准"):
        q.propose("order", ORDER, "r", "agent")


def test_expired_do_not_count_as_pending():
    sys, q, *_ = setup()
    for _ in range(MAX_PENDING):
        q.propose("order", ORDER, "r", "agent")
    sys.clock.advance(31 * 60)
    q.propose("order", ORDER, "r", "agent")


def test_max_per_day():
    sys, q, *_ = setup()
    for i in range(MAX_PER_DAY):
        a = q.propose("order", ORDER, "r", "agent")
        with sys.store.transaction():
            sys.store.update_approval(a.id, "rejected", sys.clock.now(), {"reason": "测试"})
    with pytest.raises(ApprovalError, match="每天"):
        q.propose("order", ORDER, "r", "agent")


@pytest.mark.parametrize(
    "bad",
    [
        {**ORDER, "side": "SHORT"},
        {**ORDER, "qty": 0},
        {**ORDER, "qty": "1"},
        {**ORDER, "symbol": "SPY; DROP TABLE"},
        {**ORDER, "limit_price": "abc"},
        {"symbol": "US.SPY", "side": "BUY"},
    ],
)
def test_bad_order_payload_rejected(bad):
    sys, q, *_ = setup()
    with pytest.raises(ApprovalError):
        q.propose("order", bad, "r", "agent")


def test_reason_length_capped():
    sys, q, *_ = setup()
    with pytest.raises(ApprovalError, match="理由"):
        q.propose("order", ORDER, "x" * 501, "agent")


# ------------------------------------------------------------------ 批准（ADR-0034）


def test_signed_approve_submits_with_agent_source():
    sys, q, proc, client = setup()
    a = q.propose("order", ORDER, "r", "agent:mcp")
    got = approve(sys, proc, client, a.id)
    assert got.status == "done" and got.result["approved"] is True and got.result["allowed"] is True
    assert len(sys.broker.calls("place_order")) == 1
    rec = sys.store.audit_records()[-1]
    assert rec.source == Source.AGENT.value and "agent:mcp" in rec.actor
    after = sys.store.get_approval(a.id)
    assert after.status == "approved" and after.result["client_order_id"] == got.result["client_order_id"]


@pytest.mark.parametrize("variant", ["unsigned", "wrong_key", "too_old", "other_id"])
def test_forged_approve_rejected(variant):
    sys, q, proc, client = setup()
    a = q.propose("order", ORDER, "r", "agent")
    if variant == "unsigned":
        c = client.enqueue("approve", {"approval_id": a.id}, actor="agent")
        proc.process_one()
        got = sys.store.get_command(c)
    elif variant == "wrong_key":
        got = approve(sys, proc, client, a.id, key=bytes.fromhex("55" * 32))
    elif variant == "too_old":
        got = approve(sys, proc, client, a.id, ts=sys.clock.now() - timedelta(seconds=61))
    else:
        b = q.propose("order", ORDER, "r", "agent")
        payload = {**approve_payload(KEY, b.id, sys.clock.now()), "approval_id": a.id}  # 签的是 b，改成 a
        c = client.enqueue("approve", payload, actor="agent")
        proc.process_one()
        got = sys.store.get_command(c)
    assert got.status == "error"
    assert sys.broker.calls("place_order") == []
    assert sys.store.get_approval(a.id).status == "pending"
    assert "approve_rejected" in sys.alert_kinds()


def test_resume_signature_cannot_approve():
    """前缀不同（resume| vs approve|），resume 的签名不能拿来批准。"""
    from mooapp.ops.resume_auth import resume_payload

    sys, q, proc, client = setup()
    a = q.propose("order", ORDER, "r", "agent")
    c = client.enqueue("approve", {**resume_payload(KEY, sys.clock.now(), nonce="n"), "approval_id": a.id}, actor="agent")
    proc.process_one()
    assert sys.store.get_command(c).status == "error" and sys.broker.calls("place_order") == []


def test_approve_nonce_single_use():
    sys, q, proc, client = setup()
    a = q.propose("order", ORDER, "r", "agent")
    b = q.propose("order", ORDER, "r", "agent")
    assert approve(sys, proc, client, a.id, nonce="same").status == "done"
    assert approve(sys, proc, client, b.id, nonce="same").status == "error"


def test_expired_approval_cannot_be_approved():
    sys, q, proc, client = setup()
    a = q.propose("order", ORDER, "r", "agent")
    sys.clock.advance(31 * 60)
    got = approve(sys, proc, client, a.id)
    assert got.result["approved"] is False and "过期" in got.result["reason"]
    assert sys.store.get_approval(a.id).status == "expired"
    assert sys.broker.calls("place_order") == []


def test_approve_twice_second_is_noop():
    sys, q, proc, client = setup()
    a = q.propose("order", ORDER, "r", "agent")
    approve(sys, proc, client, a.id)
    got = approve(sys, proc, client, a.id)
    assert got.result["approved"] is False and len(sys.broker.calls("place_order")) == 1


def test_gate_still_checks_after_approval():
    sys, q, proc, client = setup()
    a = q.propose("order", {**ORDER, "symbol": "US.MSFT"}, "r", "agent")  # 不在白名单
    got = approve(sys, proc, client, a.id)
    assert got.result["approved"] is True and got.result["allowed"] is False and got.result["rule"] == "whitelist"
    assert sys.broker.calls("place_order") == []


def test_reject_needs_no_signature():
    sys, q, proc, client = setup()
    a = q.propose("order", ORDER, "r", "agent")
    c = client.enqueue("reject", {"approval_id": a.id, "reason": "不看好"}, actor="cli:test")
    proc.process_one()
    assert sys.store.get_command(c).status == "done"
    after = sys.store.get_approval(a.id)
    assert after.status == "rejected" and after.result["reason"] == "不看好"


def test_rule_proposal_approved_goes_to_rule_engine():
    from mooapp.strategy.engine import RuleEngine

    sys = make_system()
    eng = RuleEngine(gate=sys.gate, oms=sys.oms, store=sys.store, md=sys.broker, bus=sys.bus, clock=sys.clock, pump=sys.pump)
    q = ApprovalQueue(sys.store, sys.clock)
    proc = CommandProcessor(gate=sys.gate, store=sys.store, clock=sys.clock, bus=sys.bus, engine=eng, resume_key=KEY)
    client = CommandClient(store=sys.store, clock=sys.clock, timeout=10.0, poll_interval=0.5)
    proc.heartbeat()
    yaml_text = 'id: agent-1\nsymbol: US.SPY\nentry: {side: BUY, qty: 1, limit_price: "10.00"}\nstop: "avg_cost - 0.05"\ntake: "avg_cost + 0.05"\n'
    a = q.propose("rule", {"yaml": yaml_text}, "r", "agent")
    got = approve(sys, proc, client, a.id)
    assert got.result["approved"] is True and got.result["rule_id"] == "agent-1"
    assert len(sys.broker.calls("place_order")) == 1


# ------------------------------------------------------------------ 验收 2 的 FakeBroker 版：REAL 下未批准不发出


def test_real_proposal_never_reaches_broker_without_approval():
    sys, q, proc, client = setup(env=Env.REAL)
    for _ in range(3):
        q.propose("order", ORDER, "r", "agent")
    while proc.process_one():
        pass
    proc.auto_approve_pending(env=Env.REAL, enabled=True)  # 即使开关打开，REAL 也不自动批准
    assert sys.broker.calls("place_order") == []
    assert all(a.status == "pending" for a in sys.store.list_approvals())


def test_real_approved_still_blocked_by_gate_while_real_disabled():
    sys, q, proc, client = setup(env=Env.REAL)
    a = q.propose("order", ORDER, "r", "agent")
    got = approve(sys, proc, client, a.id)
    assert got.result["allowed"] is False and got.result["rule"] == "real_disabled"
    assert sys.broker.calls("place_order") == []


# ------------------------------------------------------------------ auto_approve_simulate（Q9）


def test_auto_approve_only_when_enabled_and_simulate():
    sys, q, proc, client = setup()
    a = q.propose("order", ORDER, "r", "agent")
    assert proc.auto_approve_pending(env=Env.SIMULATE, enabled=False) == 0
    assert sys.store.get_approval(a.id).status == "pending"
    assert proc.auto_approve_pending(env=Env.SIMULATE, enabled=True) == 1
    assert sys.store.get_approval(a.id).status == "approved"
    assert len(sys.broker.calls("place_order")) == 1


def test_auto_approve_skips_expired():
    sys, q, proc, client = setup()
    q.propose("order", ORDER, "r", "agent")
    sys.clock.advance(31 * 60)
    assert proc.auto_approve_pending(env=Env.SIMULATE, enabled=True) == 0
    assert sys.broker.calls("place_order") == []


def test_quantity_decimal_price_kept_exact():
    sys, q, proc, client = setup()
    a = q.propose("order", {**ORDER, "limit_price": "10.01"}, "r", "agent")
    approve(sys, proc, client, a.id)
    assert sys.broker.calls("place_order")[0][0].limit_price == Decimal("10.01")
