"""SDK 行 → 数据类（broker/convert.py）。纯函数，不 import SDK，sandbox 里可测。

样例行照抄 2026-11-13 的探针录制（learnings「M2 券商适配层：探针结论」），只删了与转换无关的字段：
- recordings/probes/probe_m2_20261113T143130Z.jsonl
- recordings/probes/probe_m2_partial_fill_20261113T145538Z.jsonl
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from mooapp.broker.convert import (
    FillSynthesizer,
    cash_from_row,
    order_from_row,
    parse_et_time,
    position_from_row,
    quote_from_row,
    today_pnl_by_symbol_from_rows,
)
from mooapp.broker.port import TERMINAL_STATUSES, BrokerError, BrokerOrder
from mooapp.common import OrderKind, Side

CID = "0123456789abcdef0123456789abcdef"

# order_list_query 的一行（订单 840514，撤单后）；remark 换成本程序格式
ORDER_ROW = {
    "code": "US.SPY",
    "stock_name": "SPDR S&P 500 ETF",
    "order_market": "US",
    "trd_side": "BUY",
    "order_type": "NORMAL",
    "order_status": "CANCELLED_ALL",
    "order_id": "840514",
    "qty": 1.0,
    "price": 683.61,
    "create_time": "2026-11-13 09:31:31",
    "updated_time": "2026-11-13 09:31:31",
    "dealt_qty": 0.0,
    "dealt_avg_price": 0.0,
    "last_err_msg": "",
    "remark": "moo:" + CID,
    "time_in_force": "DAY",
    "fill_outside_rth": "N/A",
    "session": "RTH",
    "aux_price": "N/A",
    "currency": "USD",
}

# 订单推送的一行（订单 840685，市价卖 1000 股成交）；推送多了 trd_env、trd_market，没有 order_market
PUSH_ROW = {
    "trd_env": "SIMULATE",
    "code": "US.TD",
    "stock_name": "The Toronto-Dominion Bank",
    "dealt_avg_price": 123.965,
    "dealt_qty": 1000.0,
    "qty": 1000.0,
    "order_id": "840685",
    "order_type": "MARKET",
    "price": 0.0,
    "order_status": "FILLED_ALL",
    "create_time": "2026-11-13 09:55:48",
    "updated_time": "2026-11-13 09:55:57",
    "trd_side": "SELL",
    "last_err_msg": "",
    "trd_market": "US",
    "remark": "probe:m2:sell_back:145548",
    "time_in_force": "DAY",
    "session": "RTH",
    "currency": "USD",
}

POSITION_ROW = {
    "code": "US.SPY",
    "position_market": "US",
    "qty": 1.0,
    "can_sell_qty": 1.0,
    "cost_price": 759.67,
    "cost_price_valid": True,
    "average_cost": 759.67,
    "diluted_cost": 759.67,
    "currency": "USD",
}


# ------------------------------------------------------------------ 时间（P3：美东本地时间，精确到秒）


def test_parse_et_time_winter():
    assert parse_et_time("2026-11-13 09:31:31") == datetime(2026, 11, 13, 14, 31, 31, tzinfo=UTC)


def test_parse_et_time_summer_dst():
    # 夏令时 UTC−4（zoneinfo 处理；未实测，见 learnings P3「影响」）
    assert parse_et_time("2026-07-01 09:30:00") == datetime(2026, 7, 1, 13, 30, tzinfo=UTC)


def test_parse_et_time_accepts_millis():
    # vendor § Get open Orders：start/end 允许 .MS；返回值若带毫秒也要能解析
    assert parse_et_time("2026-11-13 09:31:31.250") == datetime(2026, 11, 13, 14, 31, 31, 250000, tzinfo=UTC)


@pytest.mark.parametrize("text", ["", "N/A", "2026/11/13 09:31:31", "garbage"])
def test_parse_et_time_rejects(text):
    with pytest.raises(BrokerError):
        parse_et_time(text)


# ------------------------------------------------------------------ 订单


def test_order_from_query_row():
    o = order_from_row(ORDER_ROW)
    assert isinstance(o, BrokerOrder)
    assert o.broker_order_id == "840514"
    assert o.client_order_id == CID
    assert o.symbol == "US.SPY"
    assert o.side is Side.BUY
    assert o.kind is OrderKind.LIMIT
    assert o.qty == 1 and isinstance(o.qty, int)
    assert o.price == Decimal("683.61")
    assert o.raw_status == "CANCELLED_ALL"
    assert o.dealt_qty == 0 and isinstance(o.dealt_qty, int)
    assert o.dealt_avg_price == Decimal("0")
    assert o.created_at == datetime(2026, 11, 13, 14, 31, 31, tzinfo=UTC)
    assert o.updated_at == datetime(2026, 11, 13, 14, 31, 31, tzinfo=UTC)
    assert o.remark == "moo:" + CID
    assert o.is_terminal


def test_order_from_push_row_market_and_external():
    o = order_from_row(PUSH_ROW)
    assert o.kind is OrderKind.MARKET
    assert o.price is None  # 市价单回报 price=0.0（learnings M2 其他发现）→ None
    assert o.side is Side.SELL
    assert o.dealt_qty == 1000
    assert o.dealt_avg_price == Decimal("123.965")  # 经 str 转换，不带二进制浮点尾数
    assert o.client_order_id is None  # probe: 开头 → 外部订单（ADR-0019）
    assert o.updated_at == datetime(2026, 11, 13, 14, 55, 57, tzinfo=UTC)


def test_order_unknown_type_is_none_kind():
    # 外部订单可能是 STOP 等 v1 不支持的类型（M2 计划第 8 点）
    o = order_from_row({**ORDER_ROW, "order_type": "STOP", "aux_price": 700.0})
    assert o.kind is None


def test_order_fractional_qty_rejected():
    # 美股整股（SPEC 5 节）；出现零股说明假设不成立，报错而不是截断
    with pytest.raises(BrokerError):
        order_from_row({**ORDER_ROW, "qty": 1.5})


def test_order_missing_field_raises_broker_error():
    row = dict(ORDER_ROW)
    del row["order_status"]
    with pytest.raises(BrokerError):
        order_from_row(row)


def test_terminal_statuses():
    # vendor § Trading Definitions › Order Status
    assert TERMINAL_STATUSES == {"FILLED_ALL", "CANCELLED_PART", "CANCELLED_ALL", "FAILED", "DISABLED", "DELETED"}
    assert not order_from_row({**ORDER_ROW, "order_status": "SUBMITTED"}).is_terminal


# ------------------------------------------------------------------ 持仓、资金、行情


def test_position_from_row():
    p = position_from_row(POSITION_ROW)
    assert p is not None
    assert p.symbol == "US.SPY" and p.qty == 1 and p.can_sell_qty == 1
    assert p.avg_cost == Decimal("759.67")


def test_position_zero_qty_is_none():
    # 卖光后 position_list_query 仍返回 qty=0 的行（learnings M2 其他发现）
    assert position_from_row({**POSITION_ROW, "qty": 0.0, "can_sell_qty": 0.0, "average_cost": 0.0}) is None


def test_today_pnl_by_symbol_includes_zero_qty_rows():
    # 卖光后 qty=0 的行仍带 today_pl_val（learnings P10a、P10b）；eod 的 broker_today_pl 参考列要用它
    rows = [
        {**POSITION_ROW, "qty": 0.0, "can_sell_qty": 0.0, "today_pl_val": 0.09},
        {**POSITION_ROW, "code": "US.QQQ", "today_pl_val": -1.5},
    ]
    assert today_pnl_by_symbol_from_rows(rows) == {"US.SPY": Decimal("0.09"), "US.QQQ": Decimal("-1.5")}


@pytest.mark.parametrize("bad", ["N/A", None])
def test_today_pnl_by_symbol_invalid_is_none_not_zero(bad):
    assert today_pnl_by_symbol_from_rows([{**POSITION_ROW, "today_pl_val": bad}]) == {"US.SPY": None}
    assert today_pnl_by_symbol_from_rows([POSITION_ROW]) == {"US.SPY": None}  # 字段缺失


@pytest.mark.parametrize("bad", ["N/A", None])
def test_position_avg_cost_missing_is_none(bad):
    # vendor § Get Positions：average_cost 对证券模拟账户「无效」；实测有值，但仍要容忍缺失
    p = position_from_row({**POSITION_ROW, "average_cost": bad})
    assert p is not None and p.avg_cost is None


def test_cash_from_row():
    c = cash_from_row({"currency": "N/A", "cash": 1000000.0, "power": 2000000.0, "us_cash": 1000000.0})
    assert c.currency == "USD"  # 单市场账户 currency 为 N/A；查询时已指定 USD
    assert c.cash == Decimal("1000000.0")
    assert c.buying_power == Decimal("2000000.0")


def test_quote_from_row():
    q = quote_from_row({"code": "US.TD", "update_time": "2026-11-13 09:55:08", "last_price": 124.06, "open_price": 124.045})
    assert q.symbol == "US.TD"
    assert q.last == Decimal("124.06")
    assert q.open == Decimal("124.045")
    assert q.ts == datetime(2026, 11, 13, 14, 55, 8, tzinfo=UTC)


def test_quote_bid_ask():
    q = quote_from_row(
        {"code": "US.TD", "update_time": "2026-11-13 09:55:08", "last_price": 124.06, "open_price": 124.045, "ask_price": 124.02, "bid_price": 123.93}
    )
    assert (q.bid, q.ask) == (Decimal("123.93"), Decimal("124.02"))
    q2 = quote_from_row({"code": "US.TD", "update_time": "2026-11-13 09:55:08", "last_price": 124.06, "open_price": 124.045})
    assert (q2.bid, q2.ask) == (None, None)


def test_quote_open_zero_is_none():
    q = quote_from_row({"code": "US.TD", "update_time": "2026-11-13 09:00:00", "last_price": 124.06, "open_price": 0.0})
    assert q.open is None


# ------------------------------------------------------------------ 合成成交（M2 计划第 5 点；SPEC Q2；P4）


def snap(cum: int, avg: str, status: str = "FILLED_PART", oid: str = "840682") -> BrokerOrder:
    return order_from_row(
        {**ORDER_ROW, "order_id": oid, "qty": 1000.0, "order_status": status, "dealt_qty": float(cum), "dealt_avg_price": float(avg)}
    )


def test_synth_whole_fill():
    s = FillSynthesizer()
    assert s.observe(snap(0, "0", "SUBMITTED")) is None
    f = s.observe(snap(1000, "123.965", "FILLED_ALL"))
    assert f is not None
    assert f.fill_id == "840682:1000"
    assert f.broker_order_id == "840682"
    assert f.client_order_id == CID
    assert f.symbol == "US.SPY" and f.side is Side.BUY
    assert f.qty == 1000
    assert f.price == Decimal("123.965")
    assert f.synthetic is True


def test_synth_partial_fills_back_out_price():
    s = FillSynthesizer()
    f1 = s.observe(snap(400, "124.00"))
    f2 = s.observe(snap(1000, "124.03", "FILLED_ALL"))
    assert (f1.qty, f1.price, f1.fill_id) == (400, Decimal("124.00"), "840682:400")
    # (124.03×1000 − 124.00×400) ÷ 600 = 124.05
    assert (f2.qty, f2.price, f2.fill_id) == (600, Decimal("124.05"), "840682:1000")


def test_synth_duplicate_and_stale_push_ignored():
    s = FillSynthesizer()
    assert s.observe(snap(400, "124.00")) is not None
    assert s.observe(snap(400, "124.00")) is None  # 重复推送
    assert s.observe(snap(0, "0", "SUBMITTED")) is None  # 乱序的旧推送：不回退
    f = s.observe(snap(1000, "124.03", "FILLED_ALL"))
    assert f.qty == 600  # 基准仍是 400，没被旧推送重置


def test_synth_tracks_orders_independently():
    s = FillSynthesizer()
    a = s.observe(snap(1, "759.67", "FILLED_ALL", oid="A"))
    b = s.observe(snap(1, "759.79", "FILLED_ALL", oid="B"))
    assert a.fill_id == "A:1" and b.fill_id == "B:1"


def test_synth_ts_is_order_updated_at():
    s = FillSynthesizer()
    f = s.observe(snap(1000, "123.965", "FILLED_ALL"))
    assert f.ts == datetime(2026, 11, 13, 14, 31, 31, tzinfo=UTC)
