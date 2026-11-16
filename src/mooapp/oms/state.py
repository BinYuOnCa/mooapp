"""订单状态机（M3 移植自 docs/reference/order_state.py，FIX 语义；矩阵见 docs/order-transitions.md）。

与原文的差异：
- 订单字段按 SPEC 5.4：client_order_id（主键，写入 remark）、broker_order_id、symbol、side、kind、source、时间戳；
  原文的 order_id 即 client_order_id，cl_ord_id 默认等于它（改单链保留，v1 不改单，Q4）。
- 枚举改为 StrEnum。
- M3 新增事件 RESUBMIT 与转换 T55：UNKNOWN → PENDING_NEW。守卫「券商端确认没有这张单」由上游（executor 的 Resolver）判定；
  attempts < MAX_ATTEMPTS 由本模块校验。每次进入 PENDING_NEW，attempts 加 1。
- 新增 raw_status / raw_dealt_qty：最近一次处理过的券商快照，用于跳过内容没变的重复快照（docs/status-mapping.md）。
- seen_exec_ids、anomalies、history 只在内存里（不参与相等比较、不落库）：成交去重以 fills 表的 fill_id 为准。

其余语义（数量单独记账、挂起层 base_status、IGNORED 竞态、apply 原子性与不变量）与原文一致。
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from mooapp.common import MooappError, OrderKind, Side, Source

MAX_ATTEMPTS = 3  # T55：同一 client_order_id 最多发出 3 次（首发 + 2 次重发）


# ---------------------------------------------------------------- 状态与事件


class OrdStatus(StrEnum):
    CREATED = "CREATED"  # OMS 已建单，未过风控
    WAITING_SUBMIT = "WAITING_SUBMIT"  # 非交易时段提交，开盘再送（v1 不用：闸门判断时段）
    PENDING_TRIGGER = "PENDING_TRIGGER"  # 止损类，等行情触发（v1 不用：ADR-0006 本地监控）
    PENDING_NEW = "PENDING_NEW"  # 已发券商，待确认
    UNKNOWN = "UNKNOWN"  # 确认超时或断线，结果未知，需查询
    NEW = "NEW"  # 交易所已接受，挂单中
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"  # 撤单请求在途（Moomoo 没有此状态，只在本地）
    PENDING_REPLACE = "PENDING_REPLACE"  # 改单请求在途（v1 不改单）
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class EventType(StrEnum):
    # OMS 内部
    RISK_REJECT = "RISK_REJECT"
    SUBMIT = "SUBMIT"
    QUEUE_OFF_HOURS = "QUEUE_OFF_HOURS"
    PARK_FOR_TRIGGER = "PARK_FOR_TRIGGER"
    SESSION_OPEN = "SESSION_OPEN"
    TRIGGER_HIT = "TRIGGER_HIT"
    ACK_TIMEOUT = "ACK_TIMEOUT"
    STATUS_RESOLVED = "STATUS_RESOLVED"
    RESUBMIT = "RESUBMIT"  # M3 新增（T55）
    # 用户请求
    CANCEL_REQ = "CANCEL_REQ"
    REPLACE_REQ = "REPLACE_REQ"
    # 交易所回报
    ACK = "ACK"
    EXCH_REJECT = "EXCH_REJECT"
    FILL = "FILL"
    CANCEL_ACK = "CANCEL_ACK"
    CANCEL_REJECT = "CANCEL_REJECT"
    REPLACE_ACK = "REPLACE_ACK"
    REPLACE_REJECT = "REPLACE_REJECT"
    EXPIRE = "EXPIRE"
    UNSOLICITED_CANCEL = "UNSOLICITED_CANCEL"


S, E = OrdStatus, EventType

TERMINAL: frozenset[OrdStatus] = frozenset({S.FILLED, S.CANCELED, S.EXPIRED, S.REJECTED})
WORKING: frozenset[OrdStatus] = frozenset({S.NEW, S.PARTIALLY_FILLED})
PENDING_REQ: frozenset[OrdStatus] = frozenset({S.PENDING_CANCEL, S.PENDING_REPLACE})
LIVE: frozenset[OrdStatus] = WORKING | PENDING_REQ
BROKER_HELD: frozenset[OrdStatus] = frozenset({S.WAITING_SUBMIT, S.PENDING_TRIGGER})
POST_ACCEPT: frozenset[OrdStatus] = LIVE | frozenset({S.FILLED, S.CANCELED, S.EXPIRED})


# ---------------------------------------------------------------- 转换规格

TRANSITIONS: dict[tuple[OrdStatus, EventType], OrdStatus] = {
    (S.CREATED, E.RISK_REJECT): S.REJECTED,
    (S.CREATED, E.SUBMIT): S.PENDING_NEW,
    (S.CREATED, E.QUEUE_OFF_HOURS): S.WAITING_SUBMIT,
    (S.CREATED, E.PARK_FOR_TRIGGER): S.PENDING_TRIGGER,
    (S.WAITING_SUBMIT, E.SESSION_OPEN): S.PENDING_NEW,
    (S.WAITING_SUBMIT, E.RISK_REJECT): S.REJECTED,
    (S.WAITING_SUBMIT, E.CANCEL_REQ): S.CANCELED,
    (S.WAITING_SUBMIT, E.EXPIRE): S.EXPIRED,
    (S.PENDING_TRIGGER, E.TRIGGER_HIT): S.PENDING_NEW,
    (S.PENDING_TRIGGER, E.RISK_REJECT): S.REJECTED,
    (S.PENDING_TRIGGER, E.CANCEL_REQ): S.CANCELED,
    (S.PENDING_TRIGGER, E.EXPIRE): S.EXPIRED,
    # 等确认（未确认前不接受撤单）
    (S.PENDING_NEW, E.ACK): S.NEW,
    (S.PENDING_NEW, E.EXCH_REJECT): S.REJECTED,
    (S.PENDING_NEW, E.ACK_TIMEOUT): S.UNKNOWN,
    # 未知：等回报或查询；确认券商端没有才重发（T55）
    (S.UNKNOWN, E.ACK): S.NEW,
    (S.UNKNOWN, E.EXCH_REJECT): S.REJECTED,
    (S.UNKNOWN, E.RESUBMIT): S.PENDING_NEW,
    # 活动态发起撤 / 改
    (S.NEW, E.CANCEL_REQ): S.PENDING_CANCEL,
    (S.PARTIALLY_FILLED, E.CANCEL_REQ): S.PENDING_CANCEL,
    (S.NEW, E.REPLACE_REQ): S.PENDING_REPLACE,
    (S.PARTIALLY_FILLED, E.REPLACE_REQ): S.PENDING_REPLACE,
    (S.PENDING_CANCEL, E.CANCEL_ACK): S.CANCELED,
}
TRANSITIONS.update({(s, E.EXPIRE): S.EXPIRED for s in LIVE})
TRANSITIONS.update({(s, E.UNSOLICITED_CANCEL): S.CANCELED for s in LIVE})

DYNAMIC_EVENTS: dict[EventType, frozenset[OrdStatus]] = {
    E.FILL: frozenset({S.PENDING_NEW, S.UNKNOWN}) | LIVE,
    E.REPLACE_ACK: frozenset({S.PENDING_REPLACE}),
    E.CANCEL_REJECT: frozenset({S.PENDING_CANCEL}),
    E.REPLACE_REJECT: frozenset({S.PENDING_REPLACE}),
    E.STATUS_RESOLVED: frozenset({S.UNKNOWN}),
}

LATE_FILL_STATES: frozenset[OrdStatus] = frozenset({S.CANCELED, S.EXPIRED})

IGNORED: frozenset[tuple[OrdStatus, EventType]] = frozenset(
    {(s, E.CANCEL_REJECT) for s in TERMINAL}
    | {(s, E.REPLACE_REJECT) for s in TERMINAL}
    | {(s, E.ACK) for s in POST_ACCEPT}
)

RESOLVABLE: frozenset[OrdStatus] = frozenset({S.NEW, S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.REJECTED})


def allowed_events(status: OrdStatus) -> frozenset[EventType]:
    """该状态下不会被判为非法的事件（含会被静默忽略的）。"""
    evs: set[EventType] = {e for (s, e) in TRANSITIONS if s is status}
    evs |= {e for e, starts in DYNAMIC_EVENTS.items() if status in starts}
    evs |= {e for (s, e) in IGNORED if s is status}
    if status in LATE_FILL_STATES:
        evs.add(E.FILL)
    return frozenset(evs)


# ---------------------------------------------------------------- 数据结构


class OrderError(MooappError):
    pass


class InvalidTransition(OrderError):
    """该状态不接受该事件。"""


class BadRequest(OrderError):
    """事件本身不合法：缺字段、改单数量不大于已成交、对账结论与已记账成交矛盾、重发次数用完等。"""


class Overfill(OrderError):
    """成交量超过剩余量。"""


class InvariantError(OrderError):
    """转换后不变量被破坏（代码缺陷）。"""


@dataclass(frozen=True)
class Event:
    type: EventType
    exec_id: str | None = None  # 成交去重键；FILL 必填
    qty: int | None = None  # FILL：成交量；REPLACE_REQ：新的订单总量
    price: Decimal | None = None
    cl_ord_id: str | None = None  # REPLACE_REQ：新 ClOrdID
    resolved_status: OrdStatus | None = None  # STATUS_RESOLVED
    reason: str | None = None


@dataclass
class Order:
    client_order_id: str
    symbol: str
    side: Side
    kind: OrderKind
    qty: int
    price: Decimal | None = None  # None 表示市价
    source: Source = Source.SYSTEM
    broker_order_id: str | None = None
    cl_ord_id: str = ""
    status: OrdStatus = S.CREATED
    cum_qty: int = 0
    avg_px: Decimal = Decimal(0)
    base_status: OrdStatus | None = None
    pending_qty: int | None = None
    pending_price: Decimal | None = None
    pending_cl_ord_id: str | None = None
    orig_cl_ord_ids: list[str] = field(default_factory=list)
    attempts: int = 0
    reason: str | None = None
    raw_status: str | None = None
    raw_dealt_qty: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    seen_exec_ids: set[str] = field(default_factory=set, compare=False, repr=False)
    anomalies: list[str] = field(default_factory=list, compare=False, repr=False)
    history: list[tuple[OrdStatus, EventType, OrdStatus]] = field(default_factory=list, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError("qty must be positive")
        if not self.cl_ord_id:
            self.cl_ord_id = self.client_order_id

    @property
    def leaves_qty(self) -> int:
        return 0 if self.status in TERMINAL else self.qty - self.cum_qty

    @property
    def is_open(self) -> bool:
        return self.status not in TERMINAL


# ---------------------------------------------------------------- 事件处理


def apply(order: Order, ev: Event) -> bool:
    """应用一个事件。True：订单有变化；False：被幂等或竞态规则忽略。出错抛 OrderError 子类，订单保持原样。"""
    if ev.exec_id is not None and ev.exec_id in order.seen_exec_ids:
        return False
    before = order.status
    if ev.type not in allowed_events(before):
        raise InvalidTransition(f"{before.value} 不接受 {ev.type.value}")
    if (before, ev.type) in IGNORED:
        _mark_seen(order, ev)
        return False

    trial = copy.deepcopy(order)
    _HANDLERS.get(ev.type, _static)(trial, ev)
    _mark_seen(trial, ev)
    trial.history.append((before, ev.type, trial.status))
    check_invariants(trial)
    order.__dict__.update(trial.__dict__)
    return True


def _mark_seen(o: Order, ev: Event) -> None:
    if ev.exec_id is not None:
        o.seen_exec_ids.add(ev.exec_id)


def _clear_pending(o: Order) -> None:
    o.base_status = None
    o.pending_qty = o.pending_price = o.pending_cl_ord_id = None


def _status_by_qty(o: Order) -> OrdStatus:
    if o.cum_qty == o.qty:
        return S.FILLED
    return S.PARTIALLY_FILLED if o.cum_qty > 0 else S.NEW


def _static(o: Order, ev: Event) -> None:
    target = TRANSITIONS[(o.status, ev.type)]
    if target in PENDING_REQ:
        o.base_status = o.status
    elif o.status in PENDING_REQ:
        _clear_pending(o)
    if ev.reason:
        o.reason = ev.reason
    if target is S.PENDING_NEW:
        o.attempts += 1
    o.status = target


def _resubmit(o: Order, ev: Event) -> None:
    if o.attempts >= MAX_ATTEMPTS:
        raise BadRequest(f"已发出 {o.attempts} 次，达到上限 {MAX_ATTEMPTS}，不再重发")
    _static(o, ev)


def _replace_req(o: Order, ev: Event) -> None:
    if ev.qty is None or not ev.cl_ord_id:
        raise BadRequest("改单需要新数量和新 ClOrdID")
    new_price = ev.price if ev.price is not None else o.price
    if ev.qty <= o.cum_qty:
        raise BadRequest(f"新数量 {ev.qty} 必须大于已成交 {o.cum_qty}")
    if ev.qty == o.qty and new_price == o.price:
        raise BadRequest("改单内容没有变化")
    o.pending_qty, o.pending_price, o.pending_cl_ord_id = ev.qty, new_price, ev.cl_ord_id
    _static(o, ev)


def _replace_ack(o: Order, ev: Event) -> None:
    assert o.pending_qty is not None and o.pending_cl_ord_id is not None
    if o.pending_qty < o.cum_qty:
        raise BadRequest(f"改单确认的新数量 {o.pending_qty} 低于已成交 {o.cum_qty}：回报矛盾，需人工对账")
    o.orig_cl_ord_ids.append(o.cl_ord_id)
    o.qty, o.price, o.cl_ord_id = o.pending_qty, o.pending_price, o.pending_cl_ord_id
    _clear_pending(o)
    o.status = _status_by_qty(o)


def _back_to_base(o: Order, ev: Event) -> None:
    assert o.base_status is not None
    o.status = o.base_status
    _clear_pending(o)
    if ev.reason:
        o.reason = ev.reason


def _fill(o: Order, ev: Event) -> None:
    if ev.exec_id is None:
        raise BadRequest("成交回报必须带 exec_id")
    if ev.qty is None or ev.qty <= 0 or ev.price is None:
        raise BadRequest("成交回报需要正的数量和价格")
    room = o.qty - o.cum_qty
    if ev.qty > room:
        raise Overfill(f"成交 {ev.qty} 超过剩余 {room}")
    o.avg_px = (o.avg_px * o.cum_qty + ev.price * ev.qty) / (o.cum_qty + ev.qty)
    o.cum_qty += ev.qty

    if o.status in LATE_FILL_STATES:
        o.anomalies.append(f"LATE_FILL {ev.exec_id} qty={ev.qty} after {o.status.value}")
        return
    if o.status in PENDING_REQ:
        if o.cum_qty == o.qty:
            _clear_pending(o)
            o.status = S.FILLED
        else:
            o.base_status = S.PARTIALLY_FILLED
        return
    o.status = _status_by_qty(o)


def _resolve(o: Order, ev: Event) -> None:
    r = ev.resolved_status
    if r not in RESOLVABLE:
        raise BadRequest(f"对账结论必须是 {sorted(s.value for s in RESOLVABLE)} 之一")
    if r in (S.NEW, S.PARTIALLY_FILLED, S.FILLED) and r is not _status_by_qty(o):
        raise BadRequest("对账结论与已记账成交不一致：先把缺失的成交作为 FILL 补进来")
    if ev.reason:
        o.reason = ev.reason
    o.status = r


_HANDLERS: dict[EventType, Callable[[Order, Event], None]] = {
    E.FILL: _fill,
    E.RESUBMIT: _resubmit,
    E.REPLACE_REQ: _replace_req,
    E.REPLACE_ACK: _replace_ack,
    E.CANCEL_REJECT: _back_to_base,
    E.REPLACE_REJECT: _back_to_base,
    E.STATUS_RESOLVED: _resolve,
}


# ---------------------------------------------------------------- 不变量

_NO_FILL_STATES: frozenset[OrdStatus] = frozenset({S.CREATED, S.PENDING_NEW, S.UNKNOWN, S.NEW, S.REJECTED}) | BROKER_HELD


def check_invariants(o: Order) -> None:
    errs: list[str] = []
    s = o.status
    if not 0 <= o.cum_qty <= o.qty:
        errs.append(f"cum_qty {o.cum_qty} 超出 [0, {o.qty}]")
    if s is S.FILLED and o.cum_qty != o.qty:
        errs.append("FILLED 但 cum_qty != qty")
    if s is S.PARTIALLY_FILLED and not 0 < o.cum_qty < o.qty:
        errs.append("PARTIALLY_FILLED 需要 0 < cum_qty < qty")
    if s in _NO_FILL_STATES and o.cum_qty:
        errs.append(f"{s.value} 不应有成交")
    if (s in PENDING_REQ) != (o.base_status is not None):
        errs.append("base_status 只在撤/改在途时存在")
    if o.base_status is not None:
        expected = S.PARTIALLY_FILLED if o.cum_qty else S.NEW
        if o.base_status is not expected:
            errs.append(f"base_status {o.base_status.value} 与成交量不符")
    if (s is S.PENDING_REPLACE) != (o.pending_qty is not None):
        errs.append("pending_qty 只在改单在途时存在")
    if o.cum_qty == 0 and o.avg_px != 0:
        errs.append("无成交但 avg_px 非零")
    if not 0 <= o.attempts <= MAX_ATTEMPTS:
        errs.append(f"attempts {o.attempts} 超出 [0, {MAX_ATTEMPTS}]")
    if errs:
        raise InvariantError(f"{o.client_order_id}: " + "; ".join(errs))


# ---------------------------------------------------------------- 对外展示


def display_status(o: Order) -> str:
    """派生对外展示的组合态（命名参照 Moomoo OrderStatus；EXPIRED → DISABLED 未经实测，见 P9）。"""
    s, part = o.status, o.cum_qty > 0
    if s is S.CREATED:
        return "UNSUBMITTED"
    if s in BROKER_HELD:
        return "WAITING_SUBMIT"
    if s in (S.PENDING_NEW, S.UNKNOWN):
        return "SUBMITTING"
    if s is S.PENDING_CANCEL:
        return "CANCELLING_PART" if part else "CANCELLING_ALL"
    if s is S.PENDING_REPLACE:
        assert o.base_status is not None
        s = o.base_status
    return {
        S.NEW: "SUBMITTED",
        S.PARTIALLY_FILLED: "FILLED_PART",
        S.FILLED: "FILLED_ALL",
        S.CANCELED: "CANCELLED_PART" if part else "CANCELLED_ALL",
        S.EXPIRED: "DISABLED",
        S.REJECTED: "FAILED",
    }[s]
