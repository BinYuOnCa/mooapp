"""
mini-OMS 订单状态机（FIX 语义）

设计要点
- 状态只描述"订单走到哪一步"，数量（qty / cum_qty）单独记账；leaves_qty 由两者派生，
  不单独存储，所以 CumQty + LeavesQty = OrderQty 不可能被写坏。
  对外展示的组合态（如"部分成交后已撤"）由状态 + 数量派生，见 display_status()。
- TRANSITIONS：目标状态与数量无关的转换，(起始状态, 事件) -> 目标状态。
- DYNAMIC_EVENTS：目标状态取决于数量或挂起前状态的事件（成交、改单确认、撤/改被拒、对账结论）。
- IGNORED：按正常竞态处理、静默忽略的 (状态, 事件)，例如成交之后才到的撤单拒绝。
- 撤单中 / 改单中 是叠加在活动态上的"挂起层"：base_status 记住挂起前的状态，
  请求被拒时回到它；挂起期间到达的成交照常记账，并把 base_status 推进到部分成交。
- 同一时刻最多一个在途的撤/改请求。
- 交易所回报带 exec_id，重复的直接忽略（幂等）。
- apply() 是原子的：出错时抛异常，订单保持原样；成功后检查不变量。

不在本模块：成交冲正（trade bust / correct）、资金冻结、OCO/OTO 联动。
它们分别属于账务持仓层和父子单编排层。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Callable, Dict, FrozenSet, List, Optional, Set, Tuple


# ---------------------------------------------------------------- 状态与事件

class OrdStatus(str, Enum):
    CREATED = "CREATED"                    # OMS 已建单，未过风控
    WAITING_SUBMIT = "WAITING_SUBMIT"      # 非交易时段提交，券商侧持有，开盘再送
    PENDING_TRIGGER = "PENDING_TRIGGER"    # 止损 / 跟踪止损，券商侧持有，等行情触发
    PENDING_NEW = "PENDING_NEW"            # 已发交易所，待确认
    UNKNOWN = "UNKNOWN"                    # 确认超时或断线，结果未知，需对账
    NEW = "NEW"                            # 交易所已接受，挂单中
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"      # 撤单请求在途
    PENDING_REPLACE = "PENDING_REPLACE"    # 改单请求在途
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class EventType(str, Enum):
    # OMS 内部
    RISK_REJECT = "RISK_REJECT"            # 风控不通过（下单时，或券商侧持有单放行前复核）
    SUBMIT = "SUBMIT"                      # 风控通过，发 NewOrderSingle (35=D)
    QUEUE_OFF_HOURS = "QUEUE_OFF_HOURS"    # 非交易时段，进入待开盘队列
    PARK_FOR_TRIGGER = "PARK_FOR_TRIGGER"  # 止损类订单，进入待触发
    SESSION_OPEN = "SESSION_OPEN"          # 时段切换，放行待开盘订单
    TRIGGER_HIT = "TRIGGER_HIT"            # 行情满足触发价
    ACK_TIMEOUT = "ACK_TIMEOUT"            # 等确认超时 / 会话断开
    STATUS_RESOLVED = "STATUS_RESOLVED"    # 对账得出结论（OrderStatusRequest 35=H / drop copy）
    # 用户请求
    CANCEL_REQ = "CANCEL_REQ"              # OrderCancelRequest (35=F)
    REPLACE_REQ = "REPLACE_REQ"            # OrderCancelReplaceRequest (35=G)
    # 交易所回报
    ACK = "ACK"                            # ExecType=0 New
    EXCH_REJECT = "EXCH_REJECT"            # ExecType=8 Rejected
    FILL = "FILL"                          # ExecType=F Trade
    CANCEL_ACK = "CANCEL_ACK"              # ExecType=4 Canceled（应答撤单请求）
    CANCEL_REJECT = "CANCEL_REJECT"        # 35=9, CxlRejResponseTo=1
    REPLACE_ACK = "REPLACE_ACK"            # ExecType=5 Replaced
    REPLACE_REJECT = "REPLACE_REJECT"      # 35=9, CxlRejResponseTo=2
    EXPIRE = "EXPIRE"                      # ExecType=C Expired（DAY 收盘、GTD 到期）
    UNSOLICITED_CANCEL = "UNSOLICITED_CANCEL"  # 非用户发起的撤销：IOC 余量、熔断、kill switch


S, E = OrdStatus, EventType

TERMINAL: FrozenSet[OrdStatus] = frozenset({S.FILLED, S.CANCELED, S.EXPIRED, S.REJECTED})
WORKING: FrozenSet[OrdStatus] = frozenset({S.NEW, S.PARTIALLY_FILLED})        # 在交易所簿上
PENDING_REQ: FrozenSet[OrdStatus] = frozenset({S.PENDING_CANCEL, S.PENDING_REPLACE})
LIVE: FrozenSet[OrdStatus] = WORKING | PENDING_REQ
BROKER_HELD: FrozenSet[OrdStatus] = frozenset({S.WAITING_SUBMIT, S.PENDING_TRIGGER})
POST_ACCEPT: FrozenSet[OrdStatus] = LIVE | frozenset({S.FILLED, S.CANCELED, S.EXPIRED})


# ---------------------------------------------------------------- 转换规格

TRANSITIONS: Dict[Tuple[OrdStatus, EventType], OrdStatus] = {
    # 下单
    (S.CREATED, E.RISK_REJECT): S.REJECTED,
    (S.CREATED, E.SUBMIT): S.PENDING_NEW,
    (S.CREATED, E.QUEUE_OFF_HOURS): S.WAITING_SUBMIT,
    (S.CREATED, E.PARK_FOR_TRIGGER): S.PENDING_TRIGGER,
    # 券商侧持有：放行前复核风控；还没到交易所，撤单在本地即完成
    (S.WAITING_SUBMIT, E.SESSION_OPEN): S.PENDING_NEW,
    (S.WAITING_SUBMIT, E.RISK_REJECT): S.REJECTED,
    (S.WAITING_SUBMIT, E.CANCEL_REQ): S.CANCELED,
    (S.WAITING_SUBMIT, E.EXPIRE): S.EXPIRED,
    (S.PENDING_TRIGGER, E.TRIGGER_HIT): S.PENDING_NEW,
    (S.PENDING_TRIGGER, E.RISK_REJECT): S.REJECTED,
    (S.PENDING_TRIGGER, E.CANCEL_REQ): S.CANCELED,
    (S.PENDING_TRIGGER, E.EXPIRE): S.EXPIRED,
    # 等确认（本项目约定：未确认前不接受撤单）
    (S.PENDING_NEW, E.ACK): S.NEW,
    (S.PENDING_NEW, E.EXCH_REJECT): S.REJECTED,
    (S.PENDING_NEW, E.ACK_TIMEOUT): S.UNKNOWN,
    # 未知：只能等回报或对账，绝不重发
    (S.UNKNOWN, E.ACK): S.NEW,
    (S.UNKNOWN, E.EXCH_REJECT): S.REJECTED,
    # 活动态发起撤 / 改
    (S.NEW, E.CANCEL_REQ): S.PENDING_CANCEL,
    (S.PARTIALLY_FILLED, E.CANCEL_REQ): S.PENDING_CANCEL,
    (S.NEW, E.REPLACE_REQ): S.PENDING_REPLACE,
    (S.PARTIALLY_FILLED, E.REPLACE_REQ): S.PENDING_REPLACE,
    (S.PENDING_CANCEL, E.CANCEL_ACK): S.CANCELED,
}
# 在交易所簿上的订单（含撤/改在途）都可能被到期或被动撤销
TRANSITIONS.update({(s, E.EXPIRE): S.EXPIRED for s in LIVE})
TRANSITIONS.update({(s, E.UNSOLICITED_CANCEL): S.CANCELED for s in LIVE})

# 目标状态取决于数量或 base_status 的事件 -> 允许的起始状态
DYNAMIC_EVENTS: Dict[EventType, FrozenSet[OrdStatus]] = {
    # 吃单可能先收到成交再收到确认；未知态收到成交即证明已被接受
    E.FILL: frozenset({S.PENDING_NEW, S.UNKNOWN}) | LIVE,
    E.REPLACE_ACK: frozenset({S.PENDING_REPLACE}),
    E.CANCEL_REJECT: frozenset({S.PENDING_CANCEL}),
    E.REPLACE_REJECT: frozenset({S.PENDING_REPLACE}),
    E.STATUS_RESOLVED: frozenset({S.UNKNOWN}),
}

# 已撤 / 已过期之后才到的成交：照记数量并标异常，状态不变（成交优先于状态）
LATE_FILL_STATES: FrozenSet[OrdStatus] = frozenset({S.CANCELED, S.EXPIRED})

# 正常竞态，静默忽略
IGNORED: FrozenSet[Tuple[OrdStatus, EventType]] = frozenset(
    {(s, E.CANCEL_REJECT) for s in TERMINAL}      # 成交 / 过期之后才到的"撤单太晚"
    | {(s, E.REPLACE_REJECT) for s in TERMINAL}
    | {(s, E.ACK) for s in POST_ACCEPT}           # 晚于成交到达的确认
)

# 对账可给出的结论
RESOLVABLE: FrozenSet[OrdStatus] = frozenset(
    {S.NEW, S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.REJECTED})


def allowed_events(status: OrdStatus) -> FrozenSet[EventType]:
    """该状态下不会被判为非法的事件（含会被静默忽略的）。"""
    evs: Set[EventType] = {e for (s, e) in TRANSITIONS if s is status}
    evs |= {e for e, starts in DYNAMIC_EVENTS.items() if status in starts}
    evs |= {e for (s, e) in IGNORED if s is status}
    if status in LATE_FILL_STATES:
        evs.add(E.FILL)
    return frozenset(evs)


# ---------------------------------------------------------------- 数据结构

class OrderError(Exception):
    pass


class InvalidTransition(OrderError):
    """该状态不接受该事件。"""


class BadRequest(OrderError):
    """事件本身不合法：缺字段、改单数量不大于已成交、对账结论与已记账成交矛盾等。"""


class Overfill(OrderError):
    """成交量超过剩余量。"""


class InvariantError(OrderError):
    """转换后不变量被破坏（代码缺陷）。"""


@dataclass(frozen=True)
class Event:
    type: EventType
    exec_id: Optional[str] = None          # 交易所回报唯一 ID，用于去重；FILL 必填
    qty: Optional[int] = None              # FILL：成交量；REPLACE_REQ：新的订单总量
    price: Optional[Decimal] = None        # FILL：成交价；REPLACE_REQ：新价格（None 表示不改价）
    cl_ord_id: Optional[str] = None        # REPLACE_REQ：新 ClOrdID
    resolved_status: Optional[OrdStatus] = None  # STATUS_RESOLVED：对账结论
    reason: Optional[str] = None


@dataclass
class Order:
    order_id: str
    qty: int
    price: Optional[Decimal] = None        # None 表示市价
    cl_ord_id: str = ""
    status: OrdStatus = S.CREATED
    cum_qty: int = 0
    avg_px: Decimal = Decimal(0)
    base_status: Optional[OrdStatus] = None        # 撤/改在途时，挂起前的活动态
    pending_qty: Optional[int] = None              # 改单在途时的新数量
    pending_price: Optional[Decimal] = None
    pending_cl_ord_id: Optional[str] = None
    orig_cl_ord_ids: List[str] = field(default_factory=list)  # 改单链
    reason: Optional[str] = None
    seen_exec_ids: Set[str] = field(default_factory=set)
    anomalies: List[str] = field(default_factory=list)
    history: List[Tuple[OrdStatus, EventType, OrdStatus]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError("qty must be positive")
        if not self.cl_ord_id:
            self.cl_ord_id = f"{self.order_id}-0"

    @property
    def leaves_qty(self) -> int:
        return 0 if self.status in TERMINAL else self.qty - self.cum_qty


# ---------------------------------------------------------------- 事件处理

def apply(order: Order, ev: Event) -> bool:
    """应用一个事件。

    返回 True：订单有变化；False：被幂等或竞态规则忽略。
    出错时抛 OrderError 的子类，订单保持原样。
    """
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
    o.status = target


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
    # 改单在途期间的成交按原数量记账，可能把 cum_qty 推到新数量以上
    if o.pending_qty < o.cum_qty:
        raise BadRequest(
            f"改单确认的新数量 {o.pending_qty} 低于已成交 {o.cum_qty}：回报矛盾，需人工对账")
    o.orig_cl_ord_ids.append(o.cl_ord_id)
    o.qty, o.price, o.cl_ord_id = o.pending_qty, o.pending_price, o.pending_cl_ord_id
    _clear_pending(o)
    o.status = _status_by_qty(o)


def _back_to_base(o: Order, ev: Event) -> None:
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
        if o.cum_qty == o.qty:          # 请求还在途，订单已全部成交；稍后的拒绝会被忽略
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


_HANDLERS: Dict[EventType, Callable[[Order, Event], None]] = {
    E.FILL: _fill,
    E.REPLACE_REQ: _replace_req,
    E.REPLACE_ACK: _replace_ack,
    E.CANCEL_REJECT: _back_to_base,
    E.REPLACE_REJECT: _back_to_base,
    E.STATUS_RESOLVED: _resolve,
}


# ---------------------------------------------------------------- 不变量

_NO_FILL_STATES: FrozenSet[OrdStatus] = frozenset(
    {S.CREATED, S.PENDING_NEW, S.UNKNOWN, S.NEW, S.REJECTED}) | BROKER_HELD


def check_invariants(o: Order) -> None:
    errs: List[str] = []
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
    if errs:
        raise InvariantError(f"{o.order_id}: " + "; ".join(errs))


# ---------------------------------------------------------------- 对外展示

def display_status(o: Order) -> str:
    """派生对外展示的组合态。

    命名参照 Futu OpenAPI 的 OrderStatus。EXPIRED -> DISABLED、REJECTED -> FAILED
    是本项目的约定，未对照 Futu 的实际行为。
    """
    s, part = o.status, o.cum_qty > 0
    if s is S.CREATED:
        return "UNSUBMITTED"
    if s in BROKER_HELD:
        return "WAITING_SUBMIT"
    if s in (S.PENDING_NEW, S.UNKNOWN):
        return "SUBMITTING"
    if s is S.PENDING_CANCEL:
        return "CANCELLING_PART" if part else "CANCELLING_ALL"
    if s is S.PENDING_REPLACE:          # 改单在途期间按原状态展示
        s = o.base_status
    return {
        S.NEW: "SUBMITTED",
        S.PARTIALLY_FILLED: "FILLED_PART",
        S.FILLED: "FILLED_ALL",
        S.CANCELED: "CANCELLED_PART" if part else "CANCELLED_ALL",
        S.EXPIRED: "DISABLED",
        S.REJECTED: "FAILED",
    }[s]


def transition_table_markdown() -> str:
    """把规格打印成 Markdown 矩阵：行=状态，列=事件。"""
    head = "| 状态 \\ 事件 | " + " | ".join(e.value for e in E) + " |"
    sep = "|---" * (len(E) + 1) + "|"
    rows = [head, sep]
    for s in S:
        cells = []
        for e in E:
            if (s, e) in TRANSITIONS:
                cells.append(TRANSITIONS[(s, e)].value)
            elif (s, e) in IGNORED:
                cells.append("忽略")
            elif s in LATE_FILL_STATES and e is E.FILL:
                cells.append("记账+异常")
            elif s in DYNAMIC_EVENTS.get(e, ()):
                cells.append("按数量")
            else:
                cells.append("")
        rows.append(f"| {s.value} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


if __name__ == "__main__":
    print(transition_table_markdown())
