"""M6b 规则引擎（strategy/engine.py）：把纯逻辑的状态机接到闸门、OMS 的成交事件、行情推送和 halt 上。

手算基准同 M6a：入场买 10 股 @100.21，开盘价 99.99 → 止损 95.00，止盈 105.22。
FakeBroker：push_quote 推行情；step() 确认；fill() 成交。harness 的事件通道是同步模式，测试显式 drain()。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from oms_harness import make_system

from mooapp.broker.port import OrderRejected
from mooapp.common import OrderKind, Side
from mooapp.oms.events import OrderChanged
from mooapp.risk.gate import Decision
from mooapp.strategy.engine import RuleEngine
from mooapp.strategy.machine import RuleStatus

D = Decimal
SYMBOL = "US.SPY"
RULE = """\
id: spy-oco-1
symbol: US.SPY
entry: {side: BUY, qty: 10, limit_price: "100.21"}
stop: "min(open, avg_cost) * 0.95"
take: "max(open, avg_cost) * 1.05"
"""


def setup(**kw):
    sys = make_system(**kw)
    sys.market_open(last=D("100.21"))
    eng = RuleEngine(gate=sys.gate, oms=sys.oms, store=sys.store, md=sys.broker, bus=sys.bus, clock=sys.clock, pump=sys.pump,
                     poll_fallback_seconds=5.0)  # fmt: skip
    return sys, eng


def push(sys, last, open_="99.99"):
    sys.broker.push_quote(SYMBOL, last=D(last), open=D(open_) if open_ else None)
    sys.pump.drain()


def entry_order(sys, eng, rule_id="spy-oco-1"):
    return sys.oms.get(eng.rule(rule_id).entry_cid)


def fill_entry(sys, eng, qty, price="100.21"):
    o = entry_order(sys, eng)
    if o.status.value == "PENDING_NEW":
        sys.broker.step()
    sys.broker.fill(o.broker_order_id, qty, D(price))
    sys.pump.drain()


def sell_orders(sys):
    return [c[0] for c in sys.broker.calls("place_order") if c[0].side is Side.SELL]


def armed(**kw):
    sys, eng = setup(**kw)
    r = eng.add(RULE, actor="cli:test")
    assert r.allowed, r.reason
    push(sys, "100.21")
    fill_entry(sys, eng, 10)
    assert eng.status("spy-oco-1") is RuleStatus.ARMED
    return sys, eng


# ------------------------------------------------------------------ add


def test_add_places_entry_through_gate_as_rule():
    sys, eng = setup()
    r = eng.add(RULE, actor="cli:test")
    assert r.allowed and r.rule_id == "spy-oco-1"
    (req,) = [c[0] for c in sys.broker.calls("place_order")]
    assert (req.side, req.qty, req.kind, req.limit_price) == (Side.BUY, 10, OrderKind.LIMIT, D("100.21"))
    audit = sys.store.audit_records()[-1]
    assert (audit.source, audit.actor, audit.allowed) == ("rule", "rule:spy-oco-1", True)
    assert eng.status("spy-oco-1") is RuleStatus.PENDING
    assert sys.store.active_rule_for(SYMBOL) == "spy-oco-1"
    assert (SYMBOL,) in [tuple(c[0]) for c in sys.broker.calls("subscribe_quotes")]


def test_second_rule_on_same_symbol_rejected():
    """ADR-0020：每个标的同时最多一条活动规则。"""
    sys, eng = setup()
    eng.add(RULE, actor="cli:test")
    r = eng.add(RULE.replace("spy-oco-1", "spy-oco-2"), actor="cli:test")
    assert not r.allowed and "ADR-0020" in r.reason
    assert len(sys.broker.calls("place_order")) == 1


def test_entry_rejected_by_gate_voids_rule():
    sys, eng = setup()
    sys.gate.halt("测试", actor="cli:test")
    r = eng.add(RULE, actor="cli:test")
    assert not r.allowed and r.rule_id == "spy-oco-1"
    assert eng.status("spy-oco-1") is RuleStatus.VOID
    assert sys.store.active_rule_for(SYMBOL) is None


def test_invalid_yaml_rejected_without_order():
    sys, eng = setup()
    r = eng.add(RULE.replace("min(open, avg_cost) * 0.95", "__import__('os')"), actor="cli:test")
    assert not r.allowed and "stop" in r.reason
    assert sys.broker.calls("place_order") == []


# ------------------------------------------------------------------ 完整流程：成交 → 冻结 → 触发 → 卖出 → 结束


def test_full_flow_stop_then_close():
    sys, eng = armed()
    rule = eng.rule("spy-oco-1")
    assert (rule.state["stop_price"], rule.state["take_price"]) == ("95.00", "105.22")
    push(sys, "95.01")
    assert sell_orders(sys) == []
    push(sys, "95.00")
    (sell,) = sell_orders(sys)
    assert (sell.qty, sell.kind) == (10, OrderKind.MARKET)
    audit = [a for a in sys.store.audit_records() if a.side == "SELL"][-1]
    assert audit.details.get("protective") is True and audit.source == "rule"
    assert eng.status("spy-oco-1") is RuleStatus.TRIGGERED
    o = sys.oms.get(sell.client_order_id)
    sys.broker.step()
    sys.broker.fill(o.broker_order_id, 10, D("95.00"))
    sys.pump.drain()
    assert eng.status("spy-oco-1") is RuleStatus.CLOSED
    assert sys.store.active_rule_for(SYMBOL) is None
    push(sys, "106.00")
    assert len(sell_orders(sys)) == 1  # OCO：止盈作废


def test_partial_entry_trigger_cancels_remainder():
    sys, eng = setup()
    eng.add(RULE, actor="cli:test")
    push(sys, "100.21")
    fill_entry(sys, eng, 4)
    push(sys, "95.00")
    entry = entry_order(sys, eng)
    assert (entry.broker_order_id,) in sys.broker.calls("cancel_order")
    (sell,) = sell_orders(sys)
    assert sell.qty == 4


def test_waits_for_open_price_from_quotes():
    sys, eng = setup()
    eng.add(RULE, actor="cli:test")
    fill_entry(sys, eng, 10)  # 还没收到带开盘价的行情
    assert eng.status("spy-oco-1") is RuleStatus.PENDING
    push(sys, "100.21", open_="99.99")
    assert eng.status("spy-oco-1") is RuleStatus.ARMED


def test_sell_rejected_by_gate_retried_on_next_quote(monkeypatch):
    sys, eng = armed()
    real = sys.gate.submit
    calls = {"n": 0}

    def flaky(intent):
        if intent.request is not None and intent.request.side is Side.SELL and calls["n"] == 0:
            calls["n"] += 1
            return Decision(False, "test_reject", "测试拒单")
        return real(intent)

    monkeypatch.setattr(sys.gate, "submit", flaky)
    push(sys, "95.00")
    assert sell_orders(sys) == []
    push(sys, "94.90")
    (sell,) = sell_orders(sys)
    assert sell.qty == 10
    kinds = [e.kind for e in sys.store.rule_events("spy-oco-1")]
    assert "sell_rejected" in kinds


def test_sell_rejected_by_broker_retried(monkeypatch):
    sys, eng = armed()
    real = sys.broker.place_order
    state = {"rejected": False}

    def reject_first_sell(req):
        if req.side is Side.SELL and not state["rejected"]:
            state["rejected"] = True
            sys.broker._record("place_order", req)  # 记一次调用，便于断言
            raise OrderRejected("place_order 失败：测试")
        return real(req)

    monkeypatch.setattr(sys.broker, "place_order", reject_first_sell)
    push(sys, "95.00")
    push(sys, "94.90")
    assert len(sell_orders(sys)) == 2


def test_halt_pauses_and_resume_rechecks():
    sys, eng = armed()
    sys.gate.halt("测试", actor="cli:test")
    sys.pump.drain()
    push(sys, "90.00")
    assert sell_orders(sys) == []
    sys.gate.resume(actor="cli:test")
    sys.pump.drain()
    push(sys, "94.00")
    assert len(sell_orders(sys)) == 1


def test_outside_regular_hours_ignored_then_gap_triggers_at_open():
    """默认做法 1：非常规时段忽略行情；开盘后第一笔行情已越过止损，立即触发。"""
    sys, eng = armed()
    sys.broker.set_market_state(SYMBOL, "AFTER_HOURS_BEGIN")
    sys.clock.advance(31)  # 时段缓存 30 秒
    push(sys, "90.00")
    assert sell_orders(sys) == []
    sys.broker.set_market_state(SYMBOL, "AFTERNOON")
    sys.clock.advance(31)
    sys.market_open(last=D("92.00"))  # 让闸门看到新鲜行情
    push(sys, "92.00")
    (sell,) = sell_orders(sys)
    assert sell.qty == 10


def test_stale_feed_falls_back_to_snapshot():
    sys, eng = armed()
    sys.broker.set_quote(SYMBOL, last=D("94.50"), bid=D("94.50"), ask=D("94.50"))  # 只改快照，不推送
    sys.clock.advance(6)
    eng.poll_stale_quotes()
    sys.pump.drain()
    assert len(sell_orders(sys)) == 1


def test_no_fallback_when_feed_fresh():
    sys, eng = armed()
    n = len(sys.broker.calls("snapshot"))
    sys.clock.advance(1)
    eng.poll_stale_quotes()
    sys.pump.drain()
    assert len(sys.broker.calls("snapshot")) == n  # 推送新鲜：不取快照


# ------------------------------------------------------------------ rule cancel（默认做法 3）


def test_cancel_rule_with_partial_position_warns():
    sys, eng = setup()
    eng.add(RULE, actor="cli:test")
    push(sys, "100.21")
    fill_entry(sys, eng, 4)
    r = eng.cancel("spy-oco-1", actor="cli:test")
    assert r["status"] == "CANCELED" and r["unprotected_qty"] == 4
    assert (entry_order(sys, eng).broker_order_id,) in sys.broker.calls("cancel_order")
    push(sys, "90.00")
    assert sell_orders(sys) == []
    assert sys.store.active_rule_for(SYMBOL) is None


def test_cancel_unknown_rule():
    sys, eng = setup()
    with pytest.raises(KeyError):
        eng.cancel("nope", actor="cli:test")


# ------------------------------------------------------------------ OMS 的 OrderChanged 事件


def test_oms_publishes_order_changed_with_fills():
    sys, _ = setup()
    got = []
    sys.bus.subscribe(OrderChanged, got.append)
    cid = sys.place(qty=5, price="100.21").order.client_order_id
    sys.broker.step()
    sys.broker.fill(sys.oms.get(cid).broker_order_id, 5, D("100.21"))
    sys.pump.drain()
    last = [e for e in got if e.order.client_order_id == cid][-1]
    assert last.order.cum_qty == 5 and last.order.status.value == "FILLED"


def test_events_from_other_orders_ignored():
    sys, eng = armed()
    n_events = len(sys.store.rule_events("spy-oco-1"))
    cid = sys.place(qty=1, price="100.21").order.client_order_id  # 命令行的单，不属于规则
    sys.broker.step()
    sys.broker.fill(sys.oms.get(cid).broker_order_id, 1, D("100.21"))
    sys.pump.drain()
    assert eng.status("spy-oco-1") is RuleStatus.ARMED
    assert eng.rule("spy-oco-1").state["bought"] == 10
    assert len(sys.store.rule_events("spy-oco-1")) == n_events
