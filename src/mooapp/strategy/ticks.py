"""按最小变动单位取整（ADR-0009：止损向上、止盈向下，都选触发更早的方向）。

最小变动单位复用 broker/port.py 的 tick_size（vendor FAQ Q14 › Rules on Price Spread：美股 ≥ $1 为 0.01，< $1 为 0.0001）。
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal

from mooapp.broker.port import tick_size

Direction = Literal["up", "down"]


def round_tick(price: Decimal, direction: Direction) -> Decimal:
    if direction not in ("up", "down"):
        raise ValueError(f"取整方向只能是 up 或 down，收到 {direction!r}")
    if not price.is_finite() or price <= 0:
        raise ValueError(f"价格必须为正数，收到 {price}")
    tick = tick_size(price)
    steps = (price / tick).to_integral_value(rounding=ROUND_CEILING if direction == "up" else ROUND_FLOOR)
    return (steps * tick).quantize(tick)
