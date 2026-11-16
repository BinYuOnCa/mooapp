"""ADR-0031（修复计划 1）：FakeBroker 的 can_sell_qty 照 P10d 扣已确认的卖单；OMS 给出未确认卖单剩余量；闸门不再重复扣。"""

from __future__ import annotations

from decimal import Decimal

from oms_harness import make_system

from mooapp.common import Side
from mooapp.oms.state import OrdStatus

S = OrdStatus


def test_fake_can_sell_excludes_acked_open_sells():
    """P10d：持仓 1、挂 1 股卖单（已确认）后 qty=1、can_sell_qty=0。"""
    sys = make_system()
    sys.broker.set_position("US.SPY", 10, Decimal("10"))
    sys.place(qty=3, side=Side.SELL)
    p = sys.oms.broker_position("US.SPY")
    assert (p.qty, p.can_sell_qty) == (10, 10)  # 还没确认（SUBMITTING）：券商未扣
    sys.broker.step()
    sys.pump.drain()
    p = sys.oms.broker_position("US.SPY")
    assert (p.qty, p.can_sell_qty) == (10, 7)


def test_open_leaves_summary_splits_unacked_sells():
    sys = make_system(attach=False)
    sys.broker.set_position("US.SPY", 10, Decimal("10"))
    sys.place(qty=2, side=Side.SELL)  # 不推送：本地停在 PENDING_NEW
    s = sys.oms.open_leaves_summary("US.SPY")
    assert (s.buy, s.sell, s.sell_unacked) == (0, 2, 2)
    sys.broker.step()
    sys.poller.poll_once()
    sys.pump.drain()
    s = sys.oms.open_leaves_summary("US.SPY")
    assert (s.buy, s.sell, s.sell_unacked) == (0, 2, 0)


def test_gate_allows_selling_rest_after_acked_sell():
    """修复前：持仓 10、已确认卖单 3（can_sell 7），再卖 7 被算成 7 − 3 = 4 而错误拒绝。"""
    sys = make_system()
    sys.broker.set_position("US.SPY", 10, Decimal("10"))
    assert sys.place(qty=3, side=Side.SELL).allowed
    sys.broker.step()
    sys.pump.drain()
    assert sys.place(qty=7, side=Side.SELL).allowed
    d = sys.place(qty=1, side=Side.SELL)
    assert not d.allowed and d.rule == "can_sell_qty"


def test_gate_still_counts_unacked_sell():
    sys = make_system()
    sys.broker.set_position("US.SPY", 10, Decimal("10"))
    assert sys.place(qty=6, side=Side.SELL).allowed  # 未确认：券商 can_sell 仍是 10
    d = sys.place(qty=5, side=Side.SELL)  # min(10 − 6, 10 − 6) = 4 < 5
    assert not d.allowed and d.rule == "can_sell_qty"
    assert sys.place(qty=4, side=Side.SELL).allowed
