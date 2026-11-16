"""Moomoo 订单快照 → 状态机事件（docs/status-mapping.md；vendor § Trading Definitions › Order Status）。

纯函数，不读写存储。输入「本地订单 + 券商快照」，输出事件序列：先补成交，再处理状态。
映射不了的状态返回 unmapped=True，由 OMS 写事件并报警（不静默忽略）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from mooapp.broker.port import BrokerOrder
from mooapp.oms.state import Event, EventType, Order, OrdStatus

S, E = OrdStatus, EventType

# vendor § Trading Definitions › Order Status（第 26023 行起）的全部取值
VENDOR_STATUSES: frozenset[str] = frozenset(
    {
        "NONE",
        "WAITING_SUBMIT",
        "SUBMITTING",
        "SUBMITTED",
        "FILLED_PART",
        "FILLED_ALL",
        "CANCELLED_PART",
        "CANCELLED_ALL",
        "FAILED",
        "DISABLED",
        "DELETED",
    }
)
# 文档写明的含义不足以映射；DISABLED / DELETED 等探针 P9 实测后再定（docs/status-mapping.md 第 1、10、11 行）
UNMAPPED_STATUSES: frozenset[str] = frozenset({"NONE", "DISABLED", "DELETED"})
# 券商已收到 / 交易所处理中：不产生事件（第 2、3 行）
NO_EVENT_STATUSES: frozenset[str] = frozenset({"WAITING_SUBMIT", "SUBMITTING"})
_CANCELLED = frozenset({"CANCELLED_PART", "CANCELLED_ALL"})
_FILLED = frozenset({"FILLED_PART", "FILLED_ALL"})

_PRICE_Q = Decimal("0.000001")  # 反推的成交价保留 6 位小数，与 broker/convert.py FillSynthesizer 一致

# 本地已是终态、快照重复报告同一结局：不再发事件（否则 CANCELED + UNSOLICITED_CANCEL 会被判为非法而误报）
_SAME_ENDING: frozenset[tuple[OrdStatus, EventType]] = frozenset(
    {(S.CANCELED, E.CANCEL_ACK), (S.CANCELED, E.UNSOLICITED_CANCEL), (S.REJECTED, E.EXCH_REJECT)}
)


@dataclass
class MappingResult:
    events: list[Event] = field(default_factory=list)
    unmapped: bool = False
    deal_poll_needed: bool = False  # REAL：快照的累计成交量领先于本地，需要立即轮询成交记录


def synthesized_fill(order: Order, snap: BrokerOrder) -> Event | None:
    """SIMULATE：按本地 cum_qty 算增量（不用 broker 层 FillSynthesizer 的数量，它重启后从 0 起算）。"""
    delta = snap.dealt_qty - order.cum_qty
    if delta <= 0:
        return None
    price = (snap.dealt_avg_price * snap.dealt_qty - order.avg_px * order.cum_qty) / delta
    return Event(E.FILL, exec_id=f"{snap.broker_order_id}:{snap.dealt_qty}", qty=delta, price=price.quantize(_PRICE_Q))


def map_snapshot(order: Order, snap: BrokerOrder, fills_from_deals: bool = False) -> MappingResult:
    r = MappingResult()
    raw = snap.raw_status
    ahead = snap.dealt_qty > order.cum_qty

    # 1. 先补成交
    status_after_fill = order.status
    if ahead:
        if fills_from_deals:
            r.deal_poll_needed = True
        else:
            f = synthesized_fill(order, snap)
            assert f is not None
            r.events.append(f)
            status_after_fill = S.PARTIALLY_FILLED  # 足以判断下面是否需要隐含确认
    caught_up = not ahead or not fills_from_deals

    # 2. 再处理状态
    if raw in UNMAPPED_STATUSES or raw not in VENDOR_STATUSES:
        r.unmapped = True
        return r
    if raw in NO_EVENT_STATUSES or raw in _FILLED:
        return r  # 成交态由数量决定（第 5、6 行）
    if raw == "SUBMITTED":
        t = Event(E.ACK)
    elif raw == "FAILED":
        t = Event(E.EXCH_REJECT, reason=snap.last_err_msg or "FAILED")
    elif raw in _CANCELLED:
        if not caught_up:
            return r  # REAL：成交还没补齐，先不发终态
        kind = E.CANCEL_ACK if order.status is S.PENDING_CANCEL else E.UNSOLICITED_CANCEL
        t = Event(kind, reason=raw)
        if status_after_fill in (S.PENDING_NEW, S.UNKNOWN):
            # 没看到确认就已撤销（推送丢失，轮询直接看到终态）：撤销即证明曾被接受，先补确认（T13/T18）
            r.events.append(Event(E.ACK))
    else:  # pragma: no cover - VENDOR_STATUSES 已全部覆盖
        r.unmapped = True
        return r
    if (order.status, t.type) in _SAME_ENDING:
        return r
    r.events.append(t)
    return r
