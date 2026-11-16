"""OMS 给闸门的只读查询（SPEC 5.4；ADR-0017、0018；M4 确认点 3、默认做法 4）。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from oms_harness import make_system

from mooapp.broker.port import BrokerUnavailable, OrderRejected
from mooapp.clock import FakeClock
from mooapp.common import Env, Side


def test_orders_sent_today_counts_new_orders_including_broker_rejects():
    sys = make_system()
    sys.place(qty=1)
    sys.broker.fail_next_place(OrderRejected("place_order 失败：测试"), accepted=False)
    sys.place(qty=1)
    assert sys.oms.orders_sent_today() == 2  # ADR-0018：被券商拒绝的也算


def test_resubmit_not_counted_twice():
    sys = make_system()
    sys.broker.fail_next_place(BrokerUnavailable("超时"), accepted=False)
    sys.place(qty=1)
    assert len(sys.broker.calls("place_order")) == 2
    assert sys.oms.orders_sent_today() == 1


def test_trading_day_boundary_is_new_york():
    # 2026-11-13 23:30 美东 = 2026-11-14 04:30Z
    clock = FakeClock(datetime(2026, 11, 14, 4, 30, tzinfo=UTC))
    sys = make_system(clock=clock)
    sys.place(qty=1)
    assert sys.oms.orders_sent_today() == 1
    clock.advance(60 * 60)  # 美东 00:30，11-14
    assert sys.oms.orders_sent_today() == 0


def test_open_leaves_by_symbol_and_side():
    sys = make_system()
    sys.broker.set_position("US.SPY", 100, Decimal("10"))
    a = sys.place(qty=10).order.client_order_id
    sys.place(qty=7, side=Side.SELL)
    sys.place(qty=3, symbol="US.QQQ")
    sys.broker.step()
    sys.broker.fill(sys.oms.get(a).broker_order_id, 4, Decimal("10"))
    sys.pump.drain()
    assert sys.oms.open_leaves("US.SPY", Side.BUY) == 6
    assert sys.oms.open_leaves("US.SPY", Side.SELL) == 7
    assert sys.oms.open_leaves("US.QQQ", Side.BUY) == 3
    assert sys.oms.open_leaves("US.TD", Side.BUY) == 0


def test_broker_position_lookup():
    sys = make_system()
    sys.broker.set_position("US.SPY", 12, Decimal("10"))
    p = sys.oms.broker_position("US.SPY")
    assert (p.qty, p.can_sell_qty) == (12, 12)
    assert sys.oms.broker_position("US.QQQ") is None


def test_daily_pnl_from_broker():
    sys = make_system()
    sys.broker.set_today_pnl(Decimal("-12.5"))
    assert sys.oms.daily_pnl() == Decimal("-12.5")
    sys.broker.set_today_pnl(None)
    assert sys.oms.daily_pnl() is None


def test_daily_pnl_real_subtracts_fees(monkeypatch):
    sys = make_system(env=Env.REAL)
    sys.broker.set_today_pnl(Decimal("10"))
    monkeypatch.setattr(sys.oms, "fees_today", lambda: Decimal("1.25"))
    assert sys.oms.daily_pnl() == Decimal("8.75")
