"""M6 规则状态机（纯逻辑：输入事件，输出动作）；SPEC 8 节 M6 验收 1 的 5 条价格路径。

手算基准（M6a 计划，请你核对）：入场买 10 股 @100.21，开盘价 99.99，首次成交价 100.21
- 止损价 = min(99.99, 100.21) × 0.95 = 94.9905 → 向上取整（ADR-0009）= 95.00
- 止盈价 = max(99.99, 100.21) × 1.05 = 105.2205 → 向下取整 = 105.22
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from mooapp.strategy.machine import CancelEntry, PlaceProtectiveSell, RuleMachine, RuleNote, RuleStatus
from mooapp.strategy.spec import load_rule

D = Decimal
SPEC_TEXT = """\
id: spy-oco-1
symbol: US.SPY
entry: {side: BUY, qty: 10, limit_price: "100.21"}
stop: "min(open, avg_cost) * 0.95"
take: "max(open, avg_cost) * 1.05"
"""
STOP, TAKE = D("95.00"), D("105.22")


def spec(stop="min(open, avg_cost) * 0.95", take="max(open, avg_cost) * 1.05"):
    return load_rule(SPEC_TEXT.replace("min(open, avg_cost) * 0.95", stop).replace("max(open, avg_cost) * 1.05", take))


def sells(actions):
    return [a for a in actions if isinstance(a, PlaceProtectiveSell)]


def notes(actions, kind):
    return [a for a in actions if isinstance(a, RuleNote) and a.kind == kind]


def armed(qty=10, price="100.21") -> RuleMachine:
    m = RuleMachine(spec())
    m.on_open(D("99.99"))
    m.on_entry_fill(qty, D(price))
    if qty == 10:
        m.on_entry_terminal()  # 入场单全部成交
    assert m.status is RuleStatus.ARMED
    return m


def complete_sell(m, acts, price=None):
    """模拟卖单成交（市价，按看到的价格）。"""
    (s,) = sells(acts)
    return m.on_sell_filled(s.qty, price or s.observed_price)


# ------------------------------------------------------------------ 冻结价格（ADR-0005、0009、0027 第 5 条）


def test_prices_frozen_at_first_fill_with_rounding():
    m = armed()
    assert (m.stop_price, m.take_price) == (STOP, TAKE)


def test_waits_for_open_price_before_freezing():
    m = RuleMachine(spec())
    acts = m.on_entry_fill(10, D("100.21"))
    assert m.status is RuleStatus.PENDING and m.stop_price is None
    assert notes(acts, "awaiting_open")
    assert m.on_tick(D("50")) == []  # 价格未冻结，不触发
    m.on_open(D("99.99"))
    assert m.status is RuleStatus.ARMED and m.stop_price == STOP


def test_unreasonable_prices_go_to_error():
    m = RuleMachine(spec(stop="avg_cost * 2"))  # 止损价高于止盈价
    m.on_open(D("99.99"))
    acts = m.on_entry_fill(10, D("100.21"))
    assert m.status is RuleStatus.ERROR
    assert notes(acts, "error")


def test_immediate_trigger_if_already_beyond_at_freeze():
    """ADR-0007：设规则时价格已越过触发价则立即触发。"""
    m = RuleMachine(spec())
    m.on_open(D("99.99"))
    acts = m.on_entry_fill(10, D("100.21"), last=D("94.00"))
    (s,) = sells(acts)
    assert (s.side, s.qty, s.observed_price) == ("stop", 10, D("94.00"))


# ------------------------------------------------------------------ 验收 1：5 条价格路径


def test_path1_stop_first():
    m = armed()
    assert m.on_tick(D("99.00")) == []
    assert m.on_tick(D("95.01")) == []
    acts = m.on_tick(D("95.00"))
    (s,) = sells(acts)
    assert (s.side, s.qty, s.trigger_price, s.observed_price) == ("stop", 10, STOP, D("95.00"))
    assert m.status is RuleStatus.TRIGGERED
    assert m.on_tick(D("106.00")) == []  # OCO：止盈已作废
    complete_sell(m, acts)
    assert m.status is RuleStatus.CLOSED and m.held == 0


def test_path2_take_first():
    m = armed()
    assert m.on_tick(D("103.00")) == []
    assert m.on_tick(D("105.21")) == []
    acts = m.on_tick(D("105.22"))
    (s,) = sells(acts)
    assert (s.side, s.qty, s.trigger_price) == ("take", 10, TAKE)
    assert m.on_tick(D("90.00")) == []  # 止损已作废
    complete_sell(m, acts)
    assert m.status is RuleStatus.CLOSED


def test_path3_gap_through_stop():
    m = armed()
    assert m.on_tick(D("98.00")) == []
    acts = m.on_tick(D("92.50"))
    (s,) = sells(acts)
    assert (s.side, s.qty, s.observed_price) == ("stop", 10, D("92.50"))
    assert s.trigger_price - s.observed_price == D("2.50")  # 滑点


def test_path4_both_sides_in_same_bar_counts_as_take():
    """ADR-0027 第 2 条（你的决定）：同一时段两边都碰到，按止盈算。"""
    m = armed()
    acts = m.on_bar(low=D("94.00"), high=D("106.00"))
    (s,) = sells(acts)
    assert (s.side, s.qty) == ("take", 10)
    assert notes(acts, "bar_both_sides")


def test_bar_touching_only_stop():
    m = armed()
    (s,) = sells(m.on_bar(low=D("94.50"), high=D("100.00")))
    assert s.side == "stop"


def test_path5a_partial_fills_then_trigger_prices_not_recomputed():
    m = RuleMachine(spec())
    m.on_open(D("99.99"))
    m.on_entry_fill(4, D("100.21"))
    assert (m.stop_price, m.take_price) == (STOP, TAKE)
    m.on_entry_fill(6, D("99.80"))  # 均价变为 99.964，但不重算（ADR-0005）
    m.on_entry_terminal()
    assert (m.stop_price, m.take_price, m.held) == (STOP, TAKE, 10)
    (s,) = sells(m.on_tick(D("94.80")))
    assert (s.side, s.qty) == ("stop", 10)


def test_path5b_trigger_while_entry_partially_filled():
    """ADR-0027 第 3 条：先撤入场单余量，再卖已成交的 4 股；撤单生效前又成交 2 股，补卖 2 股。"""
    m = armed(qty=4)
    acts = m.on_tick(D("95.00"))
    assert any(isinstance(a, CancelEntry) for a in acts)
    (s,) = sells(acts)
    assert (s.side, s.qty) == ("stop", 4)
    late = m.on_entry_fill(2, D("100.21"))  # 撤单生效前又成交
    (s2,) = sells(late)
    assert (s2.side, s2.qty) == ("stop", 2)
    m.on_sell_filled(4, D("95.00"))
    m.on_sell_filled(2, D("94.90"))
    assert m.status is RuleStatus.TRIGGERED  # 入场单还没结束
    m.on_entry_terminal()  # 撤单生效
    assert m.status is RuleStatus.CLOSED and (m.bought, m.sold) == (6, 6)


# ------------------------------------------------------------------ 卖单被拒（ADR-0027 第 4 条）


def test_rejected_sell_retried_on_next_tick_if_still_beyond():
    m = armed()
    m.on_tick(D("95.00"))
    assert m.on_sell_rejected("测试拒单") == []
    (s,) = sells(m.on_tick(D("94.90")))
    assert (s.side, s.attempt) == ("stop", 2)


def test_three_failures_go_to_error():
    m = armed()
    m.on_tick(D("95.00"))
    m.on_sell_rejected("1")
    m.on_tick(D("94.90"))
    m.on_sell_rejected("2")
    m.on_tick(D("94.80"))
    acts = m.on_sell_rejected("3")
    assert m.status is RuleStatus.ERROR
    assert notes(acts, "error")
    assert m.on_tick(D("90.00")) == []


def test_price_back_inside_rearms_both_sides():
    m = armed()
    m.on_tick(D("95.00"))
    m.on_sell_rejected("测试拒单")
    assert m.on_tick(D("100.00")) == []
    assert m.status is RuleStatus.ARMED
    (s,) = sells(m.on_tick(D("105.22")))  # 另一边照样有效
    assert s.side == "take" and s.attempt == 2  # 失败次数累计


# ------------------------------------------------------------------ 入场单结束（ADR-0023）、halt（ADR-0021）


def test_entry_expired_without_fill_voids_rule():
    m = RuleMachine(spec())
    m.on_open(D("99.99"))
    acts = m.on_entry_terminal()
    assert m.status is RuleStatus.VOID and notes(acts, "void")


def test_entry_expired_after_partial_fill_keeps_protecting_filled_qty():
    m = armed(qty=3)
    m.on_entry_terminal()
    assert m.status is RuleStatus.ARMED
    acts = m.on_tick(D("95.00"))
    (s,) = sells(acts)
    assert s.qty == 3
    assert not any(isinstance(a, CancelEntry) for a in acts)  # 入场单已结束，不用撤


def test_paused_ignores_quotes_and_resume_rechecks():
    m = armed()
    m.pause()
    assert m.on_tick(D("90.00")) == []
    m.resume()
    (s,) = sells(m.on_tick(D("94.00")))
    assert s.side == "stop"


def test_no_cancel_entry_when_entry_done():
    m = armed()
    acts = m.on_tick(D("95.00"))
    assert not any(isinstance(a, CancelEntry) for a in acts)


@pytest.mark.parametrize("method", ["on_tick", "on_bar"])
def test_quotes_before_fill_do_nothing(method):
    m = RuleMachine(spec())
    m.on_open(D("99.99"))
    args = (D("10"),) if method == "on_tick" else (D("10"), D("200"))
    assert getattr(m, method)(*args) == []
    assert m.status is RuleStatus.PENDING
