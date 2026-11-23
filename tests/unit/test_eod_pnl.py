"""M7 验收 1：构造数据的 P&L 与手算一致到分（ADR-0010 平均成本法；PROGRESS「M7 计划」决定 6：内部 Decimal 不舍入，输出时 ROUND_HALF_UP 到 0.01）。

每个用例的手算写在注释里。交易日 D = 2026-11-17（美东），D 之前的成交只用来建立期初持仓与成本。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mooapp.common import Side
from mooapp.eod.pnl import compute_pnl, money
from mooapp.oms.store import FillRecord

D = Decimal
DAY_START = datetime(2026, 11, 17, 5, 0, tzinfo=UTC)  # 美东 00:00
DAY_END = DAY_START + timedelta(days=1)
BEFORE = DAY_START - timedelta(hours=10)  # 前一天
DURING = DAY_START + timedelta(hours=15)  # 美东 10:00
AFTER = DAY_END + timedelta(hours=15)  # 第二天

_n = 0


def fill(side: Side, qty: int, price: str, ts: datetime = DURING, symbol: str = "US.SPY") -> FillRecord:
    global _n
    _n += 1
    return FillRecord(f"o{_n}:{qty}", f"c{_n}", f"o{_n}", symbol, side, qty, D(price), ts + timedelta(seconds=_n), True, "push")


B, S = Side.BUY, Side.SELL


def run(fills, closes):
    return compute_pnl(fills, DAY_START, DAY_END, closes)


# ------------------------------------------------------------------ money()


@pytest.mark.parametrize(
    "raw, out",
    [("0.005", "0.01"), ("-0.005", "-0.01"), ("2.675", "2.68"), ("0.0049", "0.00"), ("-0.0033333", "0.00"), ("75", "75.00")],
)
def test_money_rounds_half_up_to_cents(raw, out):
    assert money(D(raw)) == D(out)
    assert str(money(D(raw))) == out  # 不出现 -0.00


# ------------------------------------------------------------------ 平均成本法


def test_carry_in_add_then_partial_sell():
    """期初 10 股 @100；D 日买 10 @110 → 20 股，成本 2100，均价 105；卖 5 @120 → 已实现 5 × (120 − 105) = 75.00；
    剩 15 股，成本 1575（均价 105）；收盘 112 → 未实现 15 × (112 − 105) = 105.00。"""
    rows = run([fill(B, 10, "100", BEFORE), fill(B, 10, "110"), fill(S, 5, "120")], {"US.SPY": D("112")})
    r = rows["US.SPY"]
    assert (r.qty, r.buy_qty, r.sell_qty) == (15, 10, 5)
    assert r.avg_cost == D("105")
    assert money(r.realized) == D("75.00")
    assert money(r.unrealized) == D("105.00")
    assert r.anomalies == []


def test_cents_level_average():
    """买 3 @10.00、3 @10.01 → 均价 10.005；卖 4 @10.02 → 已实现 4 × 0.015 = 0.06；
    剩 2 股 @10.005；收盘 10.00 → 未实现 2 × (−0.005) = −0.01。"""
    r = run([fill(B, 3, "10.00"), fill(B, 3, "10.01"), fill(S, 4, "10.02")], {"US.SPY": D("10.00")})["US.SPY"]
    assert r.avg_cost == D("10.005")
    assert money(r.realized) == D("0.06")
    assert money(r.unrealized) == D("-0.01")


def test_non_terminating_average_not_rounded_inside():
    """买 1 @10、1 @10、1 @10.01 → 成本 30.01，均价 10.00333…；卖 2 @10.02 → 已实现 2 × (10.02 − 10.00333…) = 0.03333… → 0.03；
    剩 1 股，成本 30.01 − 2 × 10.00333… = 10.00333…；收盘 10.00 → 未实现 −0.00333… → 0.00。
    若中间把均价先舍到 10.00，已实现会变成 0.04：用来确认内部不舍入。"""
    r = run([fill(B, 1, "10"), fill(B, 1, "10"), fill(B, 1, "10.01"), fill(S, 2, "10.02")], {"US.SPY": D("10.00")})["US.SPY"]
    assert money(r.realized) == D("0.03")
    assert money(r.unrealized) == D("0.00")
    assert r.qty == 1


def test_sell_out_flat():
    """买 2 @740.81，D 日卖光 2 @741.01 → 已实现 0.40；持仓 0，无未实现，均价为 None。"""
    r = run([fill(B, 2, "740.81"), fill(S, 2, "741.01")], {"US.SPY": D("739.96")})["US.SPY"]
    assert r.qty == 0 and r.avg_cost is None
    assert money(r.realized) == D("0.40")
    assert money(r.unrealized) == D("0.00")


def test_realized_only_counts_sells_on_trade_date():
    """D 前：买 10 @100、卖 4 @90（D 前的亏损 −40 不计入 D）；D 日：卖 2 @110 → 已实现 2 × 10 = 20.00；
    D 后的卖出不计入；期末 4 股 @100，收盘 101 → 未实现 4.00。"""
    fills = [fill(B, 10, "100", BEFORE), fill(S, 4, "90", BEFORE), fill(S, 2, "110"), fill(S, 1, "130", AFTER)]
    r = run(fills, {"US.SPY": D("101")})["US.SPY"]
    assert money(r.realized) == D("20.00")
    assert (r.qty, r.sell_qty, r.buy_qty) == (4, 2, 0)
    assert money(r.unrealized) == D("4.00")


def test_symbols_independent_and_sorted():
    rows = run([fill(B, 1, "10", symbol="US.QQQ"), fill(B, 2, "20"), fill(S, 1, "21")], {"US.SPY": D("20"), "US.QQQ": D("11")})
    assert list(rows) == ["US.QQQ", "US.SPY"]
    assert money(rows["US.QQQ"].unrealized) == D("1.00")
    assert money(rows["US.SPY"].realized) == D("1.00")


def test_order_of_fills_by_timestamp_not_input_order():
    later = fill(S, 1, "12", DURING + timedelta(hours=2))
    earlier = fill(B, 1, "10", DURING)
    r = run([later, earlier], {"US.SPY": D("11")})["US.SPY"]  # 输入顺序是先卖后买；按 ts 处理：先买后卖
    assert money(r.realized) == D("2.00") and r.anomalies == []  # 1 × (12 − 10)


def test_local_oversell_is_anomaly_not_short():
    """ADR-0002 不做空：本地成交出现卖超（数据错误），记 anomaly，持仓按 0 截断，不产生负成本。"""
    r = run([fill(B, 1, "10"), fill(S, 2, "11")], {"US.SPY": D("10")})["US.SPY"]
    assert r.qty == 0
    assert r.anomalies and "卖超" in r.anomalies[0]


def test_missing_close_price_for_open_position_is_anomaly():
    r = run([fill(B, 1, "10")], {})["US.SPY"]
    assert r.close is None and r.unrealized is None
    assert any("收盘价" in a for a in r.anomalies)


def test_no_fills_no_rows():
    assert run([], {"US.SPY": D("1")}) == {}
