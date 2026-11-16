"""M4 闸门（risk/gate.py）：收集上下文、调用规则、审计、RiskRejected 事件、亏损触线自动 halt、库不可用时的行为。

规则本身的边界在 test_risk_rules.py；这里验证闸门把正确的事实交给规则，以及判定之后的副作用。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from oms_harness import LOOSE_LIMITS, make_system

from mooapp.broker.port import OrderRequest, new_client_order_id
from mooapp.common import Env, OrderKind, Side, Source
from mooapp.oms.store import StoreUnavailable
from mooapp.risk.gate import Intent, RiskRejected


def limits(**kw):
    return LOOSE_LIMITS.model_copy(update=kw)


def events(sys, kind):
    got = []
    sys.bus.subscribe(kind, got.append)
    return got


def test_every_decision_is_audited():
    sys = make_system()
    ok = sys.place(qty=1)
    rej = sys.gate.submit(
        Intent(Source.CLI, "place", OrderRequest(new_client_order_id(), "US.AAPL", Side.BUY, 1, OrderKind.LIMIT, Decimal("10")), actor="cli:test")
    )
    recs = sys.store.audit_records()
    assert [r.allowed for r in recs] == [True, False]
    assert recs[0].client_order_id == ok.order.client_order_id and recs[0].rule is None
    assert recs[1].rule == "whitelist" and recs[1].actor == "cli:test" and recs[1].symbol == "US.AAPL"
    assert not rej.allowed


def test_rejection_publishes_risk_rejected():
    sys = make_system(limits=limits(max_order_qty=5))
    got = events(sys, RiskRejected)
    d = sys.place(qty=6)
    assert not d.allowed and d.rule == "max_order_qty"
    assert len(got) == 1 and got[0].rule == "max_order_qty"


def test_rejected_order_not_created_and_not_counted():
    sys = make_system(limits=limits(max_order_qty=5))
    sys.place(qty=6)
    assert sys.oms.open_orders() == []
    assert sys.oms.orders_sent_today() == 0
    assert sys.broker.calls("place_order") == []


def test_real_disabled_in_m4():
    sys = make_system(env=Env.REAL)
    d = sys.place(qty=1)
    assert not d.allowed and d.rule == "real_disabled"
    assert sys.broker.calls("place_order") == []


def test_quote_staleness_uses_clock():
    sys = make_system(stale_after_seconds=30.0)
    sys.clock.advance(31)
    d = sys.place(qty=1)
    assert not d.allowed and d.rule == "quote_stale"
    sys.market_open()  # 刷新报价
    assert sys.place(qty=1).allowed


def test_market_closed_rejected():
    sys = make_system()
    sys.broker.set_market_state("US.SPY", "AFTER_HOURS_BEGIN")
    d = sys.place(qty=1)
    assert not d.allowed and d.rule == "regular_hours"


def test_position_cap_counts_open_buy_orders():
    sys = make_system(limits=limits(max_position_qty=25))
    sys.broker.set_position("US.SPY", 5, Decimal("10"))
    assert sys.place(qty=10).allowed  # 5 + 0 + 10
    assert sys.place(qty=10).allowed  # 5 + 10 + 10
    d = sys.place(qty=1)  # 5 + 20 + 1 = 26
    assert not d.allowed and d.rule == "max_position_qty"


def test_sell_counts_open_sell_orders():
    sys = make_system()
    sys.broker.set_position("US.SPY", 10, Decimal("10"))
    assert sys.place(qty=6, side=Side.SELL).allowed
    d = sys.place(qty=5, side=Side.SELL)  # 10 − 6 = 4 < 5
    assert not d.allowed and d.rule == "can_sell_qty"


def test_daily_order_count_from_oms():
    sys = make_system(limits=limits(max_daily_orders=2))
    assert sys.place(qty=1).allowed
    assert sys.place(qty=1).allowed
    d = sys.place(qty=1)
    assert not d.allowed and d.rule == "max_daily_orders"


def test_daily_loss_hit_auto_halts():
    sys = make_system(limits=limits(max_daily_loss=Decimal("20")))
    got = events(sys, RiskRejected)
    sys.broker.set_today_pnl(Decimal("-20"))
    d = sys.place(qty=1)
    assert not d.allowed and d.rule == "max_daily_loss"
    assert sys.gate.halted
    assert "daily_loss_halt" in sys.alert_kinds()
    assert got and got[0].rule == "max_daily_loss"
    d2 = sys.place(qty=1)
    assert d2.rule == "halt"


def test_daily_loss_without_auto_halt():
    sys = make_system(limits=limits(max_daily_loss=Decimal("20"), halt_on_daily_loss=False))
    sys.broker.set_today_pnl(Decimal("-25"))
    assert sys.place(qty=1).rule == "max_daily_loss"
    assert not sys.gate.halted


def test_daily_pnl_unavailable_rejects_buy_allows_sell():
    sys = make_system()
    sys.broker.set_today_pnl(None)
    sys.broker.set_position("US.SPY", 5, Decimal("10"))
    assert sys.place(qty=1).rule == "max_daily_loss"
    assert sys.place(qty=1, side=Side.SELL).allowed


def test_store_unavailable_rejects_new_orders(monkeypatch):
    sys = make_system()

    def down(*a, **k):
        raise StoreUnavailable("数据库不可用（测试）")

    monkeypatch.setattr(sys.store, "count_submitted_between", down)
    d = sys.place(qty=1)
    assert not d.allowed and d.rule == "store_unavailable"
    assert sys.broker.calls("place_order") == []


def test_store_unavailable_does_not_block_cancel(monkeypatch):
    sys = make_system()
    cid = sys.place(qty=1).order.client_order_id
    sys.broker.step()
    sys.pump.drain()

    def down(*a, **k):
        raise StoreUnavailable("数据库不可用（测试）")

    monkeypatch.setattr(sys.store, "count_submitted_between", down)
    monkeypatch.setattr(sys.store, "append_audit", down)
    d = sys.cancel(cid)
    assert d.allowed
    assert "audit_unavailable" in sys.alert_kinds()


def test_cancel_not_limited_by_halt_or_limits():
    sys = make_system()
    cid = sys.place(qty=1).order.client_order_id
    sys.broker.step()
    sys.pump.drain()
    sys.broker.set_market_state("US.SPY", "CLOSED")
    sys.broker.set_today_pnl(Decimal("-100000000"))
    assert sys.cancel(cid).allowed


def test_limits_object_not_modified():
    lim = limits(max_order_qty=5)
    sys = make_system(limits=lim)
    for q in (1, 5, 6):
        sys.place(qty=q)
    assert lim == limits(max_order_qty=5)


def test_resolver_clock_not_breaking_staleness_default():
    """M3 的测试会让 Resolver 推进 FakeClock；harness 默认放宽过期阈值，这里确认放宽只在 harness。"""
    sys = make_system()
    assert sys.gate.stale_after_seconds == 3600.0
    sys.clock.advance(timedelta(minutes=5).total_seconds())
    assert sys.place(qty=1).allowed


@pytest.mark.parametrize("bad", [Intent(Source.CLI, "place"), Intent(Source.CLI, "cancel"), Intent(Source.CLI, "modify")])  # type: ignore[arg-type]
def test_bad_intents_rejected_and_audited(bad):
    sys = make_system()
    d = sys.gate.submit(bad)
    assert not d.allowed and d.rule == "bad_intent"
    assert sys.store.audit_records()[-1].rule == "bad_intent"


def test_active_rule_check_skips_the_rule_own_entry():
    """M6b：ADR-0020 的检查不拦规则自己的入场单（Intent 带 rule_id）。"""
    from mooapp.oms.store import RuleRecord

    sys = make_system()
    t = sys.clock.now()
    with sys.store.transaction():
        sys.store.insert_rule(RuleRecord("r1", "US.SPY", "id: r1\n", "PENDING", "e" * 32, {}, "cli", t, t))
    req = OrderRequest(new_client_order_id(), "US.SPY", Side.BUY, 1, OrderKind.LIMIT, Decimal("10"))
    assert sys.gate.submit(Intent(Source.RULE, "place", request=req, actor="rule:r1", rule_id="r1")).allowed
    req2 = OrderRequest(new_client_order_id(), "US.SPY", Side.BUY, 1, OrderKind.LIMIT, Decimal("10"))
    d = sys.gate.submit(Intent(Source.RULE, "place", request=req2, actor="rule:r2", rule_id="r2"))
    assert not d.allowed and d.rule == "active_rule"
