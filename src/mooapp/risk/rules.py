"""风控规则（SPEC 5.5；手册 M4；M4 计划设计要点）。纯函数：输入闸门收集好的事实，输出判定。

顺序（任一不通过即拒，先命中的先报）：
protective_misuse → whitelist → regular_hours → quote_stale → max_order_qty → max_order_value → max_limit_deviation_pct
→ max_position_qty（买）→ can_sell_qty（卖）→ active_rule → max_daily_orders → max_daily_loss

边界约定：「不超过上限」通过，「超过」拒绝；当日亏损「触及」上限即拒（亏损额 ≥ 上限）。
halt 不在这里：它是闸门自己的状态，最先检查。撤单不经过这些规则（ADR-0016）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from mooapp.broker.port import OrderRequest, Quote
from mooapp.common import OrderKind, Side, Source
from mooapp.config.models import RiskLimits

# vendor § Trading Definitions › Market State：AFTERNOON = "Regular trading hours for U.S stock market"
REGULAR_HOURS_STATES = frozenset({"AFTERNOON"})


@dataclass(frozen=True)
class OrderContext:
    """闸门在调用规则前收集的事实（只读快照）。"""

    req: OrderRequest
    source: Source
    protective: bool
    market_state: str
    quote: Quote | None  # 最新快照；拿不到为 None
    now: datetime
    position_qty: int  # 券商总持仓（ADR-0019）
    can_sell_qty: int
    open_buy_leaves: int  # 本程序未结束买单的剩余量（确认点 3）
    open_sell_leaves: int
    orders_sent_today: int  # ADR-0018
    daily_pnl: Decimal | None  # ADR-0017；None = today_pl_val 不可用
    active_rule: str | None  # ADR-0020；M6 之前总是 None


@dataclass(frozen=True)
class Verdict:
    ok: bool
    rule: str | None
    reason: str
    halt: bool = False  # 当日亏损触线且 halt_on_daily_loss：闸门应自动 halt（ADR-0017）


PASS = Verdict(True, None, "ok")
Rule = Callable[[OrderContext, RiskLimits, float], Verdict | None]  # None = 本条通过


def _reject(rule: str, reason: str, halt: bool = False) -> Verdict:
    return Verdict(False, rule, reason, halt)


def _is_protective(ctx: OrderContext) -> bool:
    """ADR-0021：只有规则引擎的卖单带 protective 才被承认。"""
    return ctx.protective and ctx.source is Source.RULE and ctx.req.side is Side.SELL


def _ref_price(ctx: OrderContext) -> Decimal | None:
    """单笔金额的参考价：限价单用限价；市价单用最新价（SPEC 5.5）。"""
    if ctx.req.kind is OrderKind.LIMIT:
        return ctx.req.limit_price
    return ctx.quote.last if ctx.quote is not None else None


# ------------------------------------------------------------------ 各条规则


def protective_misuse(ctx: OrderContext, lim: RiskLimits, stale: float) -> Verdict | None:
    if ctx.protective and not _is_protective(ctx):
        return _reject("protective_misuse", f"protective 标记只承认规则引擎的卖单（ADR-0021），收到 {ctx.source.value} 的 {ctx.req.side.value}")
    return None


def whitelist(ctx: OrderContext, lim: RiskLimits, stale: float) -> Verdict | None:
    if ctx.req.symbol not in lim.whitelist:
        return _reject("whitelist", f"{ctx.req.symbol} 不在白名单")
    return None


def regular_hours(ctx: OrderContext, lim: RiskLimits, stale: float) -> Verdict | None:
    if lim.regular_hours_only and ctx.market_state not in REGULAR_HOURS_STATES:
        return _reject("regular_hours", f"当前时段 {ctx.market_state}，只允许常规时段（ADR-0003）")
    return None


def quote_stale(ctx: OrderContext, lim: RiskLimits, stale: float) -> Verdict | None:
    if ctx.quote is None:
        return _reject("quote_stale", f"拿不到 {ctx.req.symbol} 的最新行情")
    age = (ctx.now - ctx.quote.ts).total_seconds()
    if age > stale:
        return _reject("quote_stale", f"行情已 {age:.1f} 秒未更新（上限 {stale:g} 秒）")
    return None


def max_order_qty(ctx: OrderContext, lim: RiskLimits, stale: float) -> Verdict | None:
    if ctx.req.qty > lim.max_order_qty:
        return _reject("max_order_qty", f"数量 {ctx.req.qty} 超过单笔上限 {lim.max_order_qty}")
    return None


def max_order_value(ctx: OrderContext, lim: RiskLimits, stale: float) -> Verdict | None:
    if _is_protective(ctx):
        return None  # ADR-0021
    price = _ref_price(ctx)
    if price is None:
        return _reject("max_order_value", "市价单没有最新价，无法计算金额")
    value = price * ctx.req.qty
    if value > lim.max_order_value:
        return _reject("max_order_value", f"金额 {value} 超过单笔上限 {lim.max_order_value}")
    return None


def max_limit_deviation_pct(ctx: OrderContext, lim: RiskLimits, stale: float) -> Verdict | None:
    if ctx.req.kind is not OrderKind.LIMIT or ctx.quote is None or ctx.quote.last <= 0:
        return None  # 市价单不查；没有行情的情况已由 quote_stale 拒绝
    assert ctx.req.limit_price is not None
    pct = abs(ctx.req.limit_price - ctx.quote.last) / ctx.quote.last * 100
    if pct > lim.max_limit_deviation_pct:
        return _reject(
            "max_limit_deviation_pct",
            f"限价 {ctx.req.limit_price} 偏离最新价 {ctx.quote.last} {pct.quantize(Decimal('0.01'))}%，上限 {lim.max_limit_deviation_pct}%",
        )
    return None


def max_position_qty(ctx: OrderContext, lim: RiskLimits, stale: float) -> Verdict | None:
    if ctx.req.side is not Side.BUY:
        return None
    after = ctx.position_qty + ctx.open_buy_leaves + ctx.req.qty
    if after > lim.max_position_qty:
        return _reject(
            "max_position_qty",
            f"持仓 {ctx.position_qty} + 未成交买单 {ctx.open_buy_leaves} + 本单 {ctx.req.qty} = {after}，超过上限 {lim.max_position_qty}",
        )
    return None


def can_sell_qty(ctx: OrderContext, lim: RiskLimits, stale: float) -> Verdict | None:
    if ctx.req.side is not Side.SELL:
        return None
    room = ctx.can_sell_qty - ctx.open_sell_leaves
    if ctx.req.qty > room:
        return _reject(
            "can_sell_qty",
            f"可卖 {ctx.can_sell_qty} − 未成交卖单 {ctx.open_sell_leaves} = {room}，不够卖 {ctx.req.qty}（ADR-0002 不做空）",
        )
    return None


def active_rule(ctx: OrderContext, lim: RiskLimits, stale: float) -> Verdict | None:
    if ctx.active_rule is not None and ctx.req.side is Side.BUY and ctx.source in (Source.RULE, Source.AGENT):
        return _reject("active_rule", f"{ctx.req.symbol} 已有活动规则 {ctx.active_rule}（ADR-0020）")
    return None


def max_daily_orders(ctx: OrderContext, lim: RiskLimits, stale: float) -> Verdict | None:
    if _is_protective(ctx):
        return None  # ADR-0021
    if ctx.orders_sent_today + 1 > lim.max_daily_orders:
        return _reject("max_daily_orders", f"今日已发出 {ctx.orders_sent_today} 张新单，达到上限 {lim.max_daily_orders}")
    return None


def max_daily_loss(ctx: OrderContext, lim: RiskLimits, stale: float) -> Verdict | None:
    if ctx.daily_pnl is None:
        if ctx.req.side is Side.BUY:
            return _reject("max_daily_loss", "当日盈亏不可用（持仓接口 today_pl_val 无效，见探针 P10），不确定时不加仓")
        return None
    if -ctx.daily_pnl >= lim.max_daily_loss:
        return _reject(
            "max_daily_loss", f"当日盈亏 {ctx.daily_pnl}，已触及亏损上限 {lim.max_daily_loss}", halt=lim.halt_on_daily_loss
        )
    return None


RULES: tuple[tuple[str, Rule], ...] = (
    ("protective_misuse", protective_misuse),
    ("whitelist", whitelist),
    ("regular_hours", regular_hours),
    ("quote_stale", quote_stale),
    ("max_order_qty", max_order_qty),
    ("max_order_value", max_order_value),
    ("max_limit_deviation_pct", max_limit_deviation_pct),
    ("max_position_qty", max_position_qty),
    ("can_sell_qty", can_sell_qty),
    ("active_rule", active_rule),
    ("max_daily_orders", max_daily_orders),
    ("max_daily_loss", max_daily_loss),
)
RULE_ORDER: tuple[str, ...] = tuple(name for name, _ in RULES)


def check_order(ctx: OrderContext, limits: RiskLimits, stale_after_seconds: float) -> Verdict:
    for _name, rule in RULES:
        v = rule(ctx, limits, stale_after_seconds)
        if v is not None:
            return v
    return PASS
