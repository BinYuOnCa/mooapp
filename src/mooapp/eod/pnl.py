"""收盘 P&L（ADR-0010：平均成本法，未实现用当日常规时段收盘价）。纯计算，不碰券商、不碰库。

PROGRESS「M7 计划」决定 6：内部全程 Decimal 不舍入，只在报告输出时用 money() ROUND_HALF_UP 到 0.01。
只做多（ADR-0002）：本地成交出现卖超时记 anomaly、持仓截到 0，不产生负成本（对账会因此报 BREAK）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from mooapp.common import Side
from mooapp.oms.store import FillRecord

CENT = Decimal("0.01")
ZERO = Decimal(0)


def money(x: Decimal) -> Decimal:
    """报告口径：ROUND_HALF_UP 到分；结果为 0 时去掉负号（不出现 -0.00）。"""
    q = x.quantize(CENT, rounding=ROUND_HALF_UP)
    return q.copy_abs() if q == 0 else q


@dataclass
class PnlRow:
    symbol: str
    qty: int = 0  # 交易日结束时本程序的持仓
    cost: Decimal = ZERO  # 期末持仓的总成本（平均成本法）
    buy_qty: int = 0  # 交易日内
    sell_qty: int = 0
    realized: Decimal = ZERO  # 交易日内卖出的已实现盈亏
    close: Decimal | None = None
    unrealized: Decimal | None = None  # 有持仓但没有收盘价时为 None
    fees: Decimal = ZERO  # 模拟盘没有订单费用（P7），由调用方填写
    anomalies: list[str] = field(default_factory=list)

    @property
    def avg_cost(self) -> Decimal | None:
        return self.cost / self.qty if self.qty else None


def compute_pnl(fills: list[FillRecord], day_start: datetime, day_end: datetime, closes: dict[str, Decimal]) -> dict[str, PnlRow]:
    """day_end 之前的全部成交按 (ts, fill_id) 依次处理；[day_start, day_end) 内的计入当日买卖与已实现。返回按标的排序的结果。"""
    rows: dict[str, PnlRow] = {}
    for f in sorted((f for f in fills if f.ts < day_end), key=lambda f: (f.ts, f.fill_id)):
        r = rows.setdefault(f.symbol, PnlRow(f.symbol))
        today = f.ts >= day_start
        if f.side is Side.BUY:
            r.qty += f.qty
            r.cost += f.price * f.qty
            if today:
                r.buy_qty += f.qty
            continue
        sold = f.qty
        if sold > r.qty:
            r.anomalies.append(f"本地成交卖超：{f.fill_id} 卖 {f.qty}，持仓只有 {r.qty}（ADR-0002 不做空）")
            sold = r.qty
        avg = r.cost / r.qty if r.qty else ZERO
        if today:
            r.realized += (f.price - avg) * sold
            r.sell_qty += f.qty
        r.cost -= avg * sold
        r.qty -= sold
        if r.qty == 0:
            r.cost = ZERO
    for r in rows.values():
        r.close = closes.get(r.symbol)
        if r.qty == 0:
            r.unrealized = ZERO
        elif r.close is None:
            r.anomalies.append(f"{r.symbol} 有持仓 {r.qty} 但没有收盘价，无法计算未实现盈亏")
        else:
            r.unrealized = (r.close - r.cost / r.qty) * r.qty
    return dict(sorted(rows.items()))
