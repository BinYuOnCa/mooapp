"""Executor：唯一调用 BrokerPort 写方法的模块（SPEC 2.3、5.4；M3 计划第 4 节）。

公开方法：
- place(ticket, req, source)、cancel(ticket, cid)：必须带闸门签发的 GateTicket（oms/ticket.py）
- resolve_unknown(cid)：只处理已经过闸门、处于 UNKNOWN 的订单（超时后、启动恢复时），不新建订单

幂等（同一 client_order_id 只有 1 张券商单）：
1. cid 已存在且不是 CREATED：直接返回已有订单，不再发单
2. 先写库（CREATED → SUBMIT → PENDING_NEW），再调用 place_order；remark = encode_remark(cid)
3. place_order 抛 OrderRejected → EXCH_REJECT；其他 BrokerError（超时、断线、限流等，结果不明）→ ACK_TIMEOUT → UNKNOWN，
   交给 Resolver：等宽限期，用 refresh_cache=True 按 remark 查询；查到就当轮询快照送进 OMS；
   连续 confirm_queries 次查不到且 attempts < 3 才按 T55 用同一 cid 重发；用完次数 → STATUS_RESOLVED(REJECTED) 并报警
"""

from __future__ import annotations

import logging

from mooapp.broker.port import BrokerError, BrokerPort, OrderRejected, OrderRequest
from mooapp.clock import Clock
from mooapp.common import MooappError, OrderKind, Source
from mooapp.oms.events import Alert, CancelDue, EventBus
from mooapp.oms.oms import OMS, UNACKED
from mooapp.oms.pump import EventPump
from mooapp.oms.state import MAX_ATTEMPTS, TERMINAL, Event, EventType, Order, OrdStatus
from mooapp.oms.ticket import TicketBook

log = logging.getLogger(__name__)
S, E = OrdStatus, EventType
CANCELLABLE = frozenset({S.NEW, S.PARTIALLY_FILLED})  # 可以立即撤
CANCEL_ACCEPTED = CANCELLABLE | UNACKED  # 闸门受理的撤单：未确认的按 ADR-0026 挂「待撤」标记


class OrderNotActive(MooappError):
    """撤单时订单不在活动状态（或不属于本程序）。"""


def _request_of(o: Order) -> OrderRequest:
    return OrderRequest(
        client_order_id=o.client_order_id,
        symbol=o.symbol,
        side=o.side,
        qty=o.qty,
        kind=o.kind,
        limit_price=o.price if o.kind is OrderKind.LIMIT else None,
    )


class Executor:
    def __init__(
        self,
        oms: OMS,
        broker: BrokerPort,
        pump: EventPump,
        clock: Clock,
        bus: EventBus,
        resolver_grace: float = 3.0,
        confirm_queries: int = 2,
    ):
        self.oms = oms
        self.broker = broker
        self.pump = pump
        self.clock = clock
        self.bus = bus
        self.resolver_grace = resolver_grace
        self.confirm_queries = confirm_queries
        self._tickets = TicketBook()
        # ADR-0026：确认后自动撤单。OMS 持锁时发布 CancelDue，这里只排队，由事件线程执行
        bus.subscribe(CancelDue, lambda e: self.pump.submit_task(lambda cid=e.client_order_id: self._send_deferred_cancel(cid)))

    # ------------------------------------------------------------------ 下单

    def place(self, ticket: object, req: OrderRequest, source: Source) -> Order:
        self._tickets.consume(ticket, "place", req.client_order_id)
        cid = req.client_order_id
        existing = self.pump.call(lambda: self.oms.get_or_none(cid))
        if existing is not None and existing.status is not S.CREATED:
            log.info("client_order_id %s 已存在（%s），不再发单", cid, existing.status.value)
            return existing
        self.pump.call(lambda: self.oms.create_order(req, source))
        self.pump.call(lambda: self.oms.handle_local(cid, Event(E.SUBMIT)))  # 写库之后才发单
        return self._send(_request_of(self.pump.call(lambda: self.oms.get(cid))))

    def _send(self, req: OrderRequest) -> Order:
        cid = req.client_order_id
        try:
            snap = self.broker.place_order(req)  # vendor § Place Orders（trd_env、acc_id、remark 由 broker 层填）
        except OrderRejected as e:
            return self.pump.call(lambda: self.oms.handle_local(cid, Event(E.EXCH_REJECT, reason=str(e))))
        except BrokerError as e:
            log.warning("place_order 结果不明（%s）：%s", type(e).__name__, e)
            self.pump.call(lambda: self.oms.handle_local(cid, Event(E.ACK_TIMEOUT, reason=f"{type(e).__name__}：{e}")))
            self.bus.publish(Alert("ack_timeout", f"place_order 结果不明：{e}", cid))
            return self.resolve_unknown(cid)
        self.pump.call(lambda: self.oms.ingest_snapshot(snap, "local"))
        return self.pump.call(lambda: self.oms.get(cid))

    # ------------------------------------------------------------------ 结果不明的订单

    def resolve_unknown(self, cid: str) -> Order:
        o = self.pump.call(lambda: self.oms.get(cid))
        if o.status is not S.UNKNOWN:
            return o
        for i in range(self.confirm_queries):
            self.clock.sleep(self.resolver_grace)
            o = self.pump.call(lambda: self.oms.get(cid))
            if o.status is not S.UNKNOWN:
                return o  # 等待期间推送已到
            # vendor § Get open Orders：refresh_cache=True 绕过 OpenD 缓存；按 remark（client_order_id）查找
            mine = [s for s in self.broker.query_orders(open_only=False, refresh=True) if s.client_order_id == cid]
            if mine:
                for s in mine:
                    self.pump.call(lambda s=s: self.oms.ingest_snapshot(s, "poll"))
                log.info("订单 %s 在券商端已存在（第 %d 次查询），不重发", cid, i + 1)
                return self.pump.call(lambda: self.oms.get(cid))
        if o.attempts < MAX_ATTEMPTS:
            reason = f"连续 {self.confirm_queries} 次查询确认券商端没有该 remark，第 {o.attempts + 1} 次发出（T55）"
            self.bus.publish(Alert("resubmit", reason, cid))
            self.pump.call(lambda: self.oms.handle_local(cid, Event(E.RESUBMIT, reason=reason)))
            return self._send(_request_of(o))
        reason = f"已发出 {o.attempts} 次，券商端仍查不到，判为未成功（T23）"
        o = self.pump.call(lambda: self.oms.handle_local(cid, Event(E.STATUS_RESOLVED, resolved_status=S.REJECTED, reason=reason)))
        self.bus.publish(Alert("resolve_failed", reason, cid))
        return o

    # ------------------------------------------------------------------ 撤单

    def cancel(self, ticket: object, client_order_id: str) -> Order:
        self._tickets.consume(ticket, "cancel", client_order_id)
        cid = client_order_id
        o = self.pump.call(lambda: self.oms.get_or_none(cid))
        if o is not None and o.status in UNACKED:
            # ADR-0026：订单还没确认，先挂「待撤」标记；确认后由 _send_deferred_cancel 发出
            self.pump.call(lambda: self.oms.mark_cancel_pending(cid))
            return self.pump.call(lambda: self.oms.get(cid))
        if o is None or o.status not in CANCELLABLE or o.broker_order_id is None:
            raise OrderNotActive(f"订单 {cid} 不在可撤状态（{o.status.value if o else '不存在'}）")
        return self._send_cancel(o)

    def _send_deferred_cancel(self, cid: str) -> None:
        """ADR-0026：在事件线程里执行。撤单失败不抛给事件线程（那里没有调用者），而是报警。"""
        o = self.pump.call(lambda: self.oms.get(cid))
        if not o.cancel_pending or o.status in TERMINAL:
            return  # 已经发过，或订单已结束
        if o.status not in CANCELLABLE or o.broker_order_id is None:
            return  # 还没确认，等下一次 CancelDue
        try:
            self._send_cancel(o)
        except BrokerError as e:
            self.bus.publish(Alert("deferred_cancel_failed", f"确认后自动撤单失败：{e}", cid))

    def _send_cancel(self, o: Order) -> Order:
        cid = o.client_order_id
        assert o.broker_order_id is not None
        self.pump.call(lambda: self.oms.handle_local(cid, Event(E.CANCEL_REQ)))
        try:
            # vendor § Modify or Cancel Orders：返回成功 ≠ 已撤销（异步，learnings M1），等推送或轮询看到 CANCELLED_*
            self.broker.cancel_order(o.broker_order_id)
        except BrokerError as e:
            self.pump.call(lambda: self.oms.handle_local(cid, Event(E.CANCEL_REJECT, reason=f"{type(e).__name__}：{e}")))
            raise
        return self.pump.call(lambda: self.oms.get(cid))
