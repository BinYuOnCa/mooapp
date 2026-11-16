"""三方对账（PROGRESS「M7 计划」）：① 本地成交与净持仓 ② 券商订单 dealt_qty / 均价 ③ 券商持仓。

只核算本程序的订单（ADR-0019）：券商订单的 client_order_id 在本地库里才算本程序的；
remark 带 moo: 但本地没有的（例如契约测试直接下的单）、以及程序外持仓，只列出，不算 BREAK。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from mooapp.broker.port import BrokerOrder, Position
from mooapp.oms.store import FillRecord

PRICE_TOLERANCE = Decimal("0.001")  # 均价比较容差：券商 dealt_avg_price 最多 3 位小数（P10）


@dataclass(frozen=True)
class ReconBreak:
    kind: str  # fill_qty_mismatch / fill_price_mismatch / position_mismatch / local_anomaly
    symbol: str | None
    client_order_id: str | None
    details: dict[str, Any]


@dataclass
class ReconResult:
    breaks: list[ReconBreak] = field(default_factory=list)
    unknown_orders: list[str] = field(default_factory=list)  # broker_order_id
    external_qty: dict[str, int] = field(default_factory=dict)


def reconcile(
    day_fills: list[FillRecord],
    own_cids: set[str],
    day_orders: list[BrokerOrder],
    own_net: dict[str, int],
    positions: list[Position],
) -> ReconResult:
    """day_fills：本地当日成交；own_cids：本地库里的全部 client_order_id；day_orders：券商当日订单；
    own_net：本程序全部成交的净持仓（运行时刻）；positions：券商持仓（刷新查询，运行时刻）。"""
    res = ReconResult()
    local: dict[str, list[FillRecord]] = {}
    for f in day_fills:
        local.setdefault(f.client_order_id, []).append(f)

    broker: dict[str, BrokerOrder] = {}
    for o in day_orders:
        if o.client_order_id is None:
            continue  # 外部订单（remark 不是 moo:）：恢复时已报警，这里不重复
        if o.client_order_id not in own_cids:
            res.unknown_orders.append(o.broker_order_id)
            continue
        broker[o.client_order_id] = o

    for cid in sorted(set(local) | set(broker)):
        fills = local.get(cid, [])
        o = broker.get(cid)
        lq = sum(f.qty for f in fills)
        bq = o.dealt_qty if o is not None else 0
        symbol = o.symbol if o is not None else fills[0].symbol
        if lq != bq:
            res.breaks.append(ReconBreak("fill_qty_mismatch", symbol, cid, {
                "local_qty": lq, "broker_qty": bq, "broker_order_id": None if o is None else o.broker_order_id,
                "local_fill_ids": [f.fill_id for f in fills],
            }))  # fmt: skip
            continue
        if lq == 0:
            continue
        local_avg = sum(f.price * f.qty for f in fills) / lq
        assert o is not None
        if abs(local_avg - o.dealt_avg_price) > PRICE_TOLERANCE:
            res.breaks.append(ReconBreak("fill_price_mismatch", symbol, cid, {
                "local_avg": str(local_avg), "broker_avg": str(o.dealt_avg_price), "qty": lq, "broker_order_id": o.broker_order_id,
            }))  # fmt: skip

    held = {p.symbol: p.qty for p in positions}
    for symbol in sorted(set(own_net) | set(held)):
        mine = own_net.get(symbol, 0)
        theirs = held.get(symbol, 0)
        if theirs < mine:
            res.breaks.append(ReconBreak("position_mismatch", symbol, None, {"own_net": mine, "broker_qty": theirs}))
        elif theirs > mine:
            res.external_qty[symbol] = theirs - mine  # ADR-0019：程序外部分，只报告
    return res
