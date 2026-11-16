"""SPEC 8 节 M4 验收 1：每条规则都有「刚好不超限通过、超限被拒」两个测试。

规则是纯函数（risk/rules.py），输入一个 OrderContext（闸门在调用前收集好的事实），输出 Verdict。
基准上下文能通过全部规则；每个用例只改一个量。限额用测试自己的值，不读 config/settings.toml。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mooapp.broker.port import OrderRequest, Quote
from mooapp.common import OrderKind, Side, Source
from mooapp.config.models import RiskLimits
from mooapp.risk.rules import RULE_ORDER, OrderContext, check_order

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)  # 周一美东 10:00
CID = "c" * 32
STALE = 30.0

LIMITS = RiskLimits(
    whitelist=("US.SPY", "US.QQQ"),
    max_order_qty=100,
    max_order_value=Decimal("5000"),
    max_position_qty=200,
    max_daily_orders=50,
    max_daily_loss=Decimal("500"),
    halt_on_daily_loss=True,
    max_limit_deviation_pct=Decimal("5"),
    regular_hours_only=True,
)


def req(side=Side.BUY, qty=10, price="100.00", symbol="US.SPY", kind=OrderKind.LIMIT) -> OrderRequest:
    return OrderRequest(CID, symbol, side, qty, kind, Decimal(price) if kind is OrderKind.LIMIT else None)


def quote(last="100.00", age=1.0, symbol="US.SPY") -> Quote:
    return Quote(symbol=symbol, last=Decimal(last), open=None, ts=NOW - timedelta(seconds=age))


BASE = OrderContext(
    req=req(),
    source=Source.CLI,
    protective=False,
    market_state="AFTERNOON",
    quote=quote(),
    now=NOW,
    position_qty=0,
    can_sell_qty=0,
    open_buy_leaves=0,
    open_sell_leaves=0,
    orders_sent_today=0,
    daily_pnl=Decimal("0"),
    active_rule=None,
)


def check(ctx: OrderContext, limits: RiskLimits = LIMITS):
    return check_order(ctx, limits, stale_after_seconds=STALE)


def ok(ctx, limits=LIMITS):
    v = check(ctx, limits)
    assert v.ok, f"应通过，却被 {v.rule} 拒绝：{v.reason}"


def rejected(ctx, rule, limits=LIMITS):
    v = check(ctx, limits)
    assert not v.ok, "应被拒绝，却通过了"
    assert v.rule == rule, f"应由 {rule} 拒绝，实际 {v.rule}：{v.reason}"
    assert v.reason


def test_baseline_passes():
    ok(BASE)


def test_rule_order_matches_spec():
    """SPEC 5.5 的顺序（加上 M4 计划补充的 protective_misuse、quote_stale、active_rule、daily_loss_unknown）。"""
    assert RULE_ORDER == (
        "protective_misuse",
        "whitelist",
        "regular_hours",
        "quote_stale",
        "max_order_qty",
        "max_order_value",
        "max_limit_deviation_pct",
        "max_position_qty",
        "can_sell_qty",
        "active_rule",
        "max_daily_orders",
        "max_daily_loss",
    )


# ------------------------------------------------------------------ 白名单


def test_whitelist_listed_passes():
    ok(replace(BASE, req=req(symbol="US.QQQ"), quote=quote(symbol="US.QQQ")))


def test_whitelist_unlisted_rejected():
    rejected(replace(BASE, req=req(symbol="US.AAPL"), quote=quote(symbol="US.AAPL")), "whitelist")


# ------------------------------------------------------------------ 常规时段（vendor § Market State：AFTERNOON = 美股常规时段）


def test_regular_hours_afternoon_passes():
    ok(replace(BASE, market_state="AFTERNOON"))


@pytest.mark.parametrize("state", ["AFTER_HOURS_BEGIN", "PRE_MARKET_BEGIN", "CLOSED", "OVERNIGHT", "MORNING"])
def test_regular_hours_other_states_rejected(state):
    rejected(replace(BASE, market_state=state), "regular_hours")


def test_regular_hours_rule_can_be_disabled_by_limits():
    ok(replace(BASE, market_state="CLOSED"), replace_limits(regular_hours_only=False))


def replace_limits(**kw) -> RiskLimits:
    return LIMITS.model_copy(update=kw)


# ------------------------------------------------------------------ 行情是否过期（M4 默认做法 3）


def test_quote_exactly_stale_after_passes():
    ok(replace(BASE, quote=quote(age=STALE)))


def test_quote_older_than_stale_after_rejected():
    rejected(replace(BASE, quote=quote(age=STALE + 0.001)), "quote_stale")


def test_missing_quote_rejected():
    rejected(replace(BASE, quote=None), "quote_stale")


# ------------------------------------------------------------------ 单笔数量


def test_qty_at_limit_passes():
    ok(replace(BASE, req=req(qty=100, price="10.00"), quote=quote("10.00")))


def test_qty_over_limit_rejected():
    rejected(replace(BASE, req=req(qty=101, price="10.00"), quote=quote("10.00")), "max_order_qty")


# ------------------------------------------------------------------ 单笔金额（限价 × 数量；市价单用最新价）


def test_limit_value_at_limit_passes():
    ok(replace(BASE, req=req(qty=100, price="50.00"), quote=quote("50.00")))


def test_limit_value_over_limit_rejected():
    rejected(replace(BASE, req=req(qty=100, price="50.01"), quote=quote("50.01")), "max_order_value")


def test_market_value_at_limit_passes():
    ok(replace(BASE, req=req(qty=100, kind=OrderKind.MARKET), quote=quote("50.00")))


def test_market_value_over_limit_rejected():
    rejected(replace(BASE, req=req(qty=100, kind=OrderKind.MARKET), quote=quote("50.01")), "max_order_value")


# ------------------------------------------------------------------ 限价偏离最新价


def test_deviation_up_at_limit_passes():
    ok(replace(BASE, req=req(price="105.00")))


def test_deviation_up_over_limit_rejected():
    rejected(replace(BASE, req=req(price="105.01")), "max_limit_deviation_pct")


def test_deviation_down_at_limit_passes():
    ok(replace(BASE, req=req(price="95.00")))


def test_deviation_down_over_limit_rejected():
    rejected(replace(BASE, req=req(price="94.99")), "max_limit_deviation_pct")


def test_market_order_has_no_deviation_check():
    ok(replace(BASE, req=req(kind=OrderKind.MARKET), quote=quote("100.00")))


# ------------------------------------------------------------------ 单票持仓上限（买；含未成交买单，确认点 3）


def test_position_at_limit_passes():
    ok(replace(BASE, position_qty=150, open_buy_leaves=40))  # 150 + 40 + 10 = 200


def test_position_over_limit_rejected():
    rejected(replace(BASE, position_qty=150, open_buy_leaves=41), "max_position_qty")


def test_position_limit_does_not_apply_to_sells():
    ok(replace(BASE, req=req(side=Side.SELL), position_qty=500, can_sell_qty=500))


# ------------------------------------------------------------------ 可卖数量（卖；含未成交卖单；ADR-0002 不做空）


def test_sell_exactly_sellable_passes():
    ok(replace(BASE, req=req(side=Side.SELL), position_qty=15, can_sell_qty=15, open_sell_leaves=5))  # 15 − 5 = 10


def test_sell_more_than_sellable_rejected():
    rejected(replace(BASE, req=req(side=Side.SELL), position_qty=15, can_sell_qty=15, open_sell_leaves=6), "can_sell_qty")


# ------------------------------------------------------------------ 当日下单次数（ADR-0018）


def test_daily_orders_last_allowed_passes():
    ok(replace(BASE, orders_sent_today=49))


def test_daily_orders_at_limit_rejected():
    rejected(replace(BASE, orders_sent_today=50), "max_daily_orders")


# ------------------------------------------------------------------ 当日亏损（ADR-0017；确认点 2）


def test_daily_loss_just_under_passes():
    ok(replace(BASE, daily_pnl=Decimal("-499.99")))


def test_daily_loss_reached_rejected():
    rejected(replace(BASE, daily_pnl=Decimal("-500")), "max_daily_loss")


def test_daily_pnl_unknown_rejects_buy():
    rejected(replace(BASE, daily_pnl=None), "max_daily_loss")
    v = check(replace(BASE, daily_pnl=None))
    assert "today_pl_val" in v.reason


def test_daily_pnl_unknown_allows_sell():
    ok(replace(BASE, req=req(side=Side.SELL), position_qty=10, can_sell_qty=10, daily_pnl=None))


def test_daily_loss_verdict_requests_halt():
    v = check(replace(BASE, daily_pnl=Decimal("-500")))
    assert v.halt is True
    v2 = check(replace(BASE, daily_pnl=Decimal("-500")), replace_limits(halt_on_daily_loss=False))
    assert v2.halt is False and not v2.ok


# ------------------------------------------------------------------ 保护性卖单（ADR-0021）


def protective_sell(**kw) -> OrderContext:
    base = replace(
        BASE, req=req(side=Side.SELL, qty=60, price="100.00"), source=Source.RULE, protective=True, position_qty=60, can_sell_qty=60
    )
    return replace(base, **kw)


def test_protective_sell_skips_order_value():
    ok(protective_sell())  # 60 × 100 = 6000 > 5000


def test_protective_sell_skips_daily_order_count():
    ok(protective_sell(orders_sent_today=50))


def test_protective_sell_still_checks_sellable():
    rejected(protective_sell(can_sell_qty=59), "can_sell_qty")


def test_protective_flag_from_cli_rejected():
    rejected(protective_sell(source=Source.CLI), "protective_misuse")


def test_protective_flag_from_agent_rejected():
    rejected(protective_sell(source=Source.AGENT), "protective_misuse")


def test_protective_flag_on_buy_rejected():
    rejected(replace(BASE, source=Source.RULE, protective=True), "protective_misuse")


# ------------------------------------------------------------------ 同一标的已有活动规则（ADR-0020）


@pytest.mark.parametrize("source", [Source.RULE, Source.AGENT])
def test_active_rule_blocks_rule_and_agent_buys(source):
    rejected(replace(BASE, source=source, active_rule="r1"), "active_rule")


def test_active_rule_does_not_block_cli_buy():
    ok(replace(BASE, source=Source.CLI, active_rule="r1"))


def test_active_rule_does_not_block_sells():
    ok(replace(BASE, req=req(side=Side.SELL), source=Source.AGENT, active_rule="r1", position_qty=10, can_sell_qty=10))


def test_no_active_rule_rule_buy_passes():
    ok(replace(BASE, source=Source.RULE))


# ------------------------------------------------------------------ 先命中的规则先报


def test_first_failing_rule_reported():
    ctx = replace(BASE, req=req(symbol="US.AAPL", qty=1000), quote=quote(symbol="US.AAPL"), orders_sent_today=999)
    rejected(ctx, "whitelist")
