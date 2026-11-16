"""OMS（SPEC 5.4；M3 计划第 6、7 节）。

- 单一事件入口：推送、轮询、本地动作都经 EventPump 进入这里（ingest_snapshot / ingest_fill / handle / handle_local），
  每个事件写 order_events，并记录来源 origin（push / poll / local）
- 不缓存订单：每次从 Store 读出、应用、在一个事务里写回订单、事件和成交
- 来自券商的回报违反状态机：写 outcome=rejected 的事件并报警，订单不变；本地动作违反状态机：原样抛出
- 映射不了的状态：写 outcome=unmapped 的事件并报警，订单状态不变（docs/status-mapping.md）
- 启动恢复 recover()：完成之前 ready=False，闸门拒绝新指令
- 一把 RLock 串行化全部读写：正常只有 EventPump 的消费线程写入（3.4 节），闸门在调用者线程里只读
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from mooapp.broker.port import BrokerError, BrokerFill, BrokerOrder, BrokerPort, NotSupported, OrderRequest, Position
from mooapp.clock import Clock
from mooapp.common import Env, MooappError, Side, Source
from mooapp.oms.events import Alert, CancelDue, DealPollRequested, EventBus, OmsReady, OrderChanged
from mooapp.oms.journal import Journal, ReplayReport, replay_journal
from mooapp.oms.mapping import map_snapshot
from mooapp.oms.state import TERMINAL, Event, EventType, Order, OrderError, OrdStatus, apply
from mooapp.oms.store import EventRecord, FillRecord, Store, StoreError

if TYPE_CHECKING:
    from mooapp.oms.executor import Executor

log = logging.getLogger(__name__)
HISTORY_MARGIN = timedelta(days=1)  # 修复计划 2：历史查询窗口前后各放宽 1 天（vendor 未写明时区）
S, E = OrdStatus, EventType
MARKET_TZ = ZoneInfo("America/New_York")  # ADR-0011：交易日按美东划分
UNACKED = frozenset({S.PENDING_NEW, S.UNKNOWN})  # ADR-0026：可以挂「待撤」标记的状态


class OrderNotFound(MooappError, KeyError):
    pass


@dataclass(frozen=True)
class OmsEvent:
    """SPEC 5.4：通用事件入口的参数（本地或测试构造）。"""

    type: EventType
    client_order_id: str
    origin: str  # push / poll / local
    exec_id: str | None = None
    qty: int | None = None
    price: Decimal | None = None
    reason: str | None = None
    resolved_status: OrdStatus | None = None


@dataclass(frozen=True)
class Mismatch:
    kind: str
    client_order_id: str | None
    details: dict[str, Any]


@dataclass
class RecoveryReport:
    started_at: datetime
    finished_at: datetime | None = None
    orders_checked: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)
    deals_source: str = ""  # "deals"（实盘成交记录）/ "snapshot"（模拟盘：订单快照的 dealt_qty，P4）
    journal: ReplayReport | None = None


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def snapshot_details(s: BrokerOrder) -> dict[str, Any]:
    return {
        "broker_order_id": s.broker_order_id,
        "client_order_id": s.client_order_id,
        "symbol": s.symbol,
        "side": s.side.value,
        "kind": s.kind.value if s.kind else None,
        "qty": s.qty,
        "price": _jsonable(s.price),
        "raw_status": s.raw_status,
        "dealt_qty": s.dealt_qty,
        "dealt_avg_price": _jsonable(s.dealt_avg_price),
        "last_err_msg": s.last_err_msg,
        "updated_at": _jsonable(s.updated_at),
        "remark": s.remark,
    }


class OMS:
    def __init__(
        self,
        store: Store,
        broker: BrokerPort,
        bus: EventBus,
        clock: Clock,
        env: Env,
        journal: Journal | None = None,
    ):
        self.store = store
        self.broker = broker
        self.bus = bus
        self.clock = clock
        self.env = env
        self.journal = journal
        self.ready = False
        self._fills_from_deals = env is Env.REAL  # docs/status-mapping.md「REAL 的成交」
        self._executor: Executor | None = None
        self._lock = threading.RLock()
        self._reported: set[tuple[str, str]] = set()  # 外部订单等只报一次
        self._recovery: RecoveryReport | None = None

    def set_executor(self, executor: Executor) -> None:
        self._executor = executor

    # ------------------------------------------------------------------ 只读查询

    def get(self, client_order_id: str) -> Order:
        o = self.get_or_none(client_order_id)
        if o is None:
            raise OrderNotFound(f"没有订单 {client_order_id}")
        return o

    def get_or_none(self, client_order_id: str) -> Order | None:
        with self._lock:
            return self.store.get_order(client_order_id)

    def open_orders(self) -> list[Order]:
        with self._lock:
            return self.store.list_orders(open_only=True)

    # ------------------------------------------------------------------ 给闸门的只读查询（M4）

    def trading_day_bounds(self) -> tuple[datetime, datetime]:
        """当前美东交易日 [00:00, 次日 00:00) 的 UTC 起止（ADR-0011、0017、0018）。"""
        today = self.clock.now().astimezone(MARKET_TZ).date()
        start = datetime.combine(today, time(0), MARKET_TZ)
        return start, start + timedelta(days=1)

    def orders_sent_today(self) -> int:
        """ADR-0018：当日（美东）首次 SUBMIT 的订单数，含被券商拒绝的；T55 重发不重复计；闸门拒绝的根本没有建单。"""
        start, end = self.trading_day_bounds()
        with self._lock:
            return self.store.count_submitted_between(start, end)

    def open_leaves(self, symbol: str, side: Side) -> int:
        """本程序未结束订单的剩余量（M4 确认点 3：持仓上限与可卖数量都算挂单）。"""
        with self._lock:
            return sum(o.qty - o.cum_qty for o in self.store.list_orders(open_only=True) if o.symbol == symbol and o.side is side)

    def open_leaves_both(self, symbol: str) -> tuple[int, int]:
        """（买, 卖）两边的未成交剩余量，只扫一次订单表（M4 审查修复）。"""
        buy = sell = 0
        with self._lock:
            for o in self.store.list_orders(open_only=True):
                if o.symbol == symbol:
                    if o.side is Side.BUY:
                        buy += o.qty - o.cum_qty
                    else:
                        sell += o.qty - o.cum_qty
        return buy, sell

    def broker_position(self, symbol: str) -> Position | None:
        """券商总持仓（ADR-0019：持仓上限按总持仓算，含程序外部分）。vendor § Get Positions。"""
        for p in self.broker.query_positions():
            if p.symbol == symbol:
                return p
        return None

    def fees_today(self) -> Decimal:
        """当日订单费用（vendor § Get Order Fee；模拟盘不支持，P7）。只在 REAL 的 daily_pnl 里用。"""
        start, _ = self.trading_day_bounds()
        with self._lock:
            ids = [o.broker_order_id for o in self.store.list_orders() if o.broker_order_id and o.created_at and o.created_at >= start]
        if not ids:
            return Decimal(0)
        return sum(self.broker.query_fees(ids).values(), Decimal(0))

    def daily_pnl(self) -> Decimal | None:
        """ADR-0017 / M4 确认点 2：券商 today_pl_val 之和（含 qty=0 的行）；实盘再减去当日费用。None = 不可用。"""
        gross = self.broker.query_today_pnl()
        if gross is None:
            return None
        return gross - self.fees_today() if self.env is Env.REAL else gross

    # ------------------------------------------------------------------ ADR-0026：确认之前的撤单

    def prepare_cancel(self, client_order_id: str) -> tuple[str, Order | None]:
        """撤单前的判断与「待撤」标记一步完成（M4 审查修复 #2：避免判断之后确认推送才到的竞态）。

        返回 ("deferred", o)：未确认，已挂待撤标记（ADR-0026）；("send", o)：可以立即撤；("inactive", o)：不可撤；("missing", None)。
        """
        with self._lock:
            o = self.store.get_order(client_order_id)
            if o is None:
                return "missing", None
            if o.status in UNACKED:
                self.mark_cancel_pending(client_order_id)
                return "deferred", self.store.get_order(client_order_id)
            if o.status in (S.NEW, S.PARTIALLY_FILLED) and o.broker_order_id is not None:
                return "send", o
            return "inactive", o

    def mark_cancel_pending(self, client_order_id: str) -> bool:
        """给 PENDING_NEW / UNKNOWN 的订单挂「待撤」标记。已挂过返回 False（不重复记录）。"""
        with self._lock:
            o = self.get(client_order_id)
            if o.status not in UNACKED:
                raise OrderError(f"{client_order_id} 状态 {o.status.value}，不能挂待撤标记（只用于未确认的订单）")
            if o.cancel_pending:
                return False
            o.cancel_pending = True
            now = self.clock.now()
            o.updated_at = now
            rec = EventRecord(
                o.client_order_id, "CANCEL_DEFERRED", "local", "applied", o.status.value, o.status.value,
                None, None, None, o.raw_status, "ADR-0026：订单尚未确认，确认后自动撤单", {}, now,
            )  # fmt: skip
            with self.store.transaction():
                self.store.update_order(o)
                self.store.append_event(rec)
            return True

    # ------------------------------------------------------------------ 本地动作

    def create_order(self, req: OrderRequest, source: Source) -> Order:
        """建单（CREATED）。同一 client_order_id 已存在时返回已有订单（幂等）。"""
        with self._lock:
            existing = self.store.get_order(req.client_order_id)
            if existing is not None:
                return existing
            now = self.clock.now()
            o = Order(
                client_order_id=req.client_order_id,
                symbol=req.symbol,
                side=req.side,
                kind=req.kind,
                qty=req.qty,
                price=req.limit_price,
                source=source,
                created_at=now,
                updated_at=now,
            )
            with self.store.transaction():
                self.store.insert_order(o)
            return o

    def handle_local(self, client_order_id: str, ev: Event) -> Order:
        with self._lock:
            return self._process(self.get(client_order_id), [ev], "local", broker_report=False)

    def handle(self, e: OmsEvent) -> Order:
        ev = Event(e.type, exec_id=e.exec_id, qty=e.qty, price=e.price, reason=e.reason, resolved_status=e.resolved_status)
        with self._lock:
            return self._process(self.get(e.client_order_id), [ev], e.origin, broker_report=e.origin != "local")

    # ------------------------------------------------------------------ 券商回报

    def ingest_snapshot(self, snap: BrokerOrder, origin: str) -> Order | None:
        with self._lock:
            o = self._find(snap.client_order_id, snap.broker_order_id)
            if o is None:
                self._report_stranger(snap)
                return None
            linked = False
            if o.broker_order_id is None:
                o.broker_order_id = snap.broker_order_id
                linked = True
            elif o.broker_order_id != snap.broker_order_id:
                self._issue(
                    "unexpected_report",
                    o.client_order_id,
                    {"reason": "同一 client_order_id 出现两个券商订单", "local": o.broker_order_id, **snapshot_details(snap)},
                    key=("dup_broker_id", snap.broker_order_id),
                )
                return o
            if not linked and (snap.raw_status, snap.dealt_qty) == (o.raw_status, o.raw_dealt_qty):
                return o  # 内容没变的重复快照（含每次轮询）：不重复记录
            res = map_snapshot(o, snap, fills_from_deals=self._fills_from_deals)
            o = self._process(
                o, res.events, origin, broker_report=True, snap=snap, unmapped=res.unmapped, fill_ts=snap.updated_at, synthetic=True
            )
        if res.deal_poll_needed:
            self.bus.publish(DealPollRequested(o.client_order_id))
        return o

    def ingest_fill(self, fill: BrokerFill, origin: str) -> Order | None:
        if fill.synthetic and not self._fills_from_deals:
            # 模拟盘的合成成交由 OMS 按本地 cum_qty 从快照算（docs/status-mapping.md 第 1 步）；这里不重复记账
            log.debug("忽略 broker 层合成成交 %s（由快照增量计算）", fill.fill_id)
            return None
        with self._lock:
            o = self._find(fill.client_order_id, fill.broker_order_id)
            if o is None:
                self._issue(
                    "external_order" if fill.client_order_id is None else "unknown_client_order_id",
                    fill.client_order_id,
                    {"fill_id": fill.fill_id, "broker_order_id": fill.broker_order_id, "symbol": fill.symbol, "qty": fill.qty},
                    key=("fill_stranger", fill.broker_order_id),
                )
                return None
            if o.broker_order_id is None:
                o.broker_order_id = fill.broker_order_id
            ev = Event(E.FILL, exec_id=fill.fill_id, qty=fill.qty, price=fill.price)
            return self._process(o, [ev], origin, broker_report=True, fill_ts=fill.ts, synthetic=fill.synthetic, force_write=True)

    # ------------------------------------------------------------------ 内部

    def _find(self, client_order_id: str | None, broker_order_id: str) -> Order | None:
        if client_order_id is not None:
            return self.store.get_order(client_order_id)
        return self.store.find_by_broker_id(broker_order_id)

    def _report_stranger(self, snap: BrokerOrder) -> None:
        if snap.client_order_id is None:
            # ADR-0019：程序外的订单只告警，不撤不改
            self._issue("external_order", None, snapshot_details(snap), key=("external", snap.broker_order_id), mismatch=False)
        else:
            self._issue(
                "unknown_client_order_id",
                snap.client_order_id,
                snapshot_details(snap),
                key=("unknown_cid", snap.broker_order_id),
            )

    def _issue(
        self,
        kind: str,
        client_order_id: str | None,
        details: dict[str, Any],
        key: tuple[str, str] | None = None,
        alert: bool = True,
        mismatch: bool = True,
    ) -> None:
        """写 recon_events，并按需报警、计入恢复报告。key 相同的只处理一次（进程内）。"""
        if key is not None:
            if key in self._reported:
                return
            self._reported.add(key)
        details = {k: _jsonable(v) for k, v in details.items()}
        with self.store.transaction():
            self.store.append_recon(kind, details, client_order_id, self.clock.now())
        if mismatch and self._recovery is not None:
            self._recovery.mismatches.append(Mismatch(kind, client_order_id, details))
        if alert:
            self.bus.publish(Alert(kind, f"{kind}：{details}", client_order_id, details))

    def _process(
        self,
        o: Order,
        events: list[Event],
        origin: str,
        broker_report: bool,
        snap: BrokerOrder | None = None,
        unmapped: bool = False,
        fill_ts: datetime | None = None,
        synthetic: bool = False,
        force_write: bool = False,
    ) -> Order:
        now = self.clock.now()
        records: list[EventRecord] = []
        fills: list[FillRecord] = []
        alerts: list[Alert] = []
        raw = snap.raw_status if snap else None
        payload = snapshot_details(snap) if snap else {}

        def rec(ev_type: str, outcome: str, frm: OrdStatus | None, to: OrdStatus | None, ev: Event | None = None, reason: str | None = None) -> EventRecord:
            return EventRecord(
                client_order_id=o.client_order_id,
                event_type=ev_type,
                origin=origin,
                outcome=outcome,
                from_status=frm.value if frm else None,
                to_status=to.value if to else None,
                exec_id=ev.exec_id if ev else None,
                qty=ev.qty if ev else None,
                price=ev.price if ev else None,
                raw_status=raw,
                reason=reason if reason is not None else (ev.reason if ev else None),
                payload=payload,
                ts=now,
            )

        for ev in events:
            before = o.status
            if ev.type is E.FILL and ev.exec_id is not None and self.store.get_fill(ev.exec_id) is not None:
                records.append(rec(ev.type.value, "ignored", before, before, ev, "成交 id 已记账"))
                continue
            try:
                changed = apply(o, ev)
            except OrderError as err:
                if not broker_report:
                    raise
                records.append(rec(ev.type.value, "rejected", before, before, ev, f"{type(err).__name__}：{err}"))
                alerts.append(Alert("unexpected_report", f"{ev.type.value} 在 {before.value}：{err}", o.client_order_id, payload))
                break
            records.append(rec(ev.type.value, "applied" if changed else "ignored", before, o.status, ev))
            if changed and ev.type is E.CANCEL_REQ:
                o.cancel_pending = False  # ADR-0026：撤单已发出
            if changed and ev.type is E.FILL:
                assert ev.exec_id is not None and ev.qty is not None and ev.price is not None and o.broker_order_id
                fills.append(
                    FillRecord(ev.exec_id, o.client_order_id, o.broker_order_id, o.symbol, o.side, ev.qty, ev.price,
                               fill_ts or now, synthetic, origin)  # fmt: skip
                )
            if changed and o.anomalies:
                alerts.append(Alert("late_fill", o.anomalies[-1], o.client_order_id))
                o.anomalies.clear()

        notices: list[object] = []
        if o.cancel_pending:
            if o.status in TERMINAL:
                o.cancel_pending = False
                records.append(rec("CANCEL_DEFERRED_CLEARED", "applied", o.status, o.status, reason=f"订单在确认前已结束（{o.status.value}），不再撤单"))
            elif o.status in (S.NEW, S.PARTIALLY_FILLED):
                notices.append(CancelDue(o.client_order_id))  # Executor 收到后排队发出撤单（不在这里阻塞）

        if snap is not None:
            if unmapped:
                records.append(rec("SNAPSHOT", "unmapped", o.status, o.status, reason=f"映射不了的状态 {raw}"))
                alerts.append(Alert("unmapped_status", f"Moomoo 状态 {raw} 映射不了，订单状态不变", o.client_order_id, payload))
            elif not records:
                records.append(rec("SNAPSHOT", "ignored", o.status, o.status))
            o.raw_status, o.raw_dealt_qty = snap.raw_status, snap.dealt_qty

        if records or force_write or snap is not None:
            o.updated_at = now
            with self.store.transaction():
                self.store.update_order(o)
                for f in fills:
                    self.store.insert_fill_once(f)
                for r in records:
                    self.store.append_event(r)
        for a in alerts:
            self.bus.publish(a)
        for n in notices:
            self.bus.publish(n)
        if any(r.outcome == "applied" for r in records):
            self.bus.publish(OrderChanged(replace(o)))  # M6b：规则引擎订阅
        return o

    # ------------------------------------------------------------------ 启动恢复

    def recover(self) -> RecoveryReport:
        """券商未完成订单、当日成交、持仓 → 与本地对齐；不一致写 recon_events 并报警；完成后才 ready。

        Resolver（可能重发）在锁外调用：它经 EventPump 排队执行，持锁等待会和消费线程互相等死。
        """
        self.ready = False
        report = RecoveryReport(started_at=self.clock.now())
        to_resolve: list[str] = []
        with self._lock:
            self._recovery = report
            try:
                if self.journal is not None:
                    report.journal = replay_journal(self.journal, getattr(self.store, "inner", self.store))
                    if report.journal.failed:
                        raise StoreError(f"journal 回放失败 {report.journal.failed} 条，未就绪（ADR-0025）")
                local_open = self.store.list_orders(open_only=True)
                report.orders_checked = len(local_open)
                # vendor § Get open Orders：未完成 + 24 小时内的订单；refresh_cache=True 不用 OpenD 缓存
                snaps = self.broker.query_orders(open_only=False, refresh=True)
                seen_cids = {s.client_order_id for s in snaps if s.client_order_id}
                for s in snaps:
                    self.ingest_snapshot(s, "poll")
                try:
                    deals = self.broker.query_deals()  # vendor § Get Today's Deals：只支持实盘（P4）
                    report.deals_source = "deals"
                except NotSupported:
                    deals = []
                    report.deals_source = "snapshot"
                for d in deals:
                    self.ingest_fill(d, "poll")
                missing = [o for o in local_open if o.client_order_id not in seen_cids and o.status is not S.CREATED]
                if missing:
                    seen_cids |= self._recover_from_history(missing)  # 修复计划 2：前一天的单当日列表查不到
                for o in local_open:
                    if o.client_order_id in seen_cids:
                        continue
                    cur = self.store.get_order(o.client_order_id)
                    assert cur is not None
                    details = {"status": cur.status.value, "broker_order_id": cur.broker_order_id, "symbol": cur.symbol}
                    if cur.status is S.CREATED:
                        # 建单后、SUBMIT 之前被杀：从未发出
                        self.handle_local(cur.client_order_id, Event(E.RISK_REJECT, reason="启动恢复：建单后未发出"))
                    elif cur.status is S.PENDING_NEW:
                        self._issue("missing_at_broker", cur.client_order_id, details)
                        self.handle_local(cur.client_order_id, Event(E.ACK_TIMEOUT, reason="启动恢复：券商查不到"))
                        to_resolve.append(cur.client_order_id)
                    elif cur.status is S.UNKNOWN:
                        to_resolve.append(cur.client_order_id)
                    else:
                        self._issue("missing_at_broker", cur.client_order_id, details)
            except BaseException:
                self._recovery = None
                raise

        try:
            for cid in to_resolve:
                if self._executor is None:
                    self._issue("resolve_failed", cid, {"reason": "没有 Executor，无法确认"})
                else:
                    self._executor.resolve_unknown(cid)
            with self._lock:
                self._check_positions()
        finally:
            self._recovery = None
        report.finished_at = self.clock.now()
        with self._lock:
            # M4 审查修复 #8：确认已到、但待撤的撤单还没发出进程就退出了 → 重启后补发
            due = [o.client_order_id for o in self.store.list_orders(open_only=True)
                   if o.cancel_pending and o.status in (S.NEW, S.PARTIALLY_FILLED)]  # fmt: skip
        for cid in due:
            self.bus.publish(CancelDue(cid))
        self.ready = True
        self.bus.publish(OmsReady())
        log.info("启动恢复完成：检查 %d 张本地未完成订单，差异 %d 条", report.orders_checked, len(report.mismatches))
        return report

    def _recover_from_history(self, missing: list[Order]) -> set[str]:
        """修复计划 2（learnings「P9 第二次 --check」）：当日列表（order_list_query）查不到的本地未完成订单，
        查一次历史订单（vendor § Get Historical Orders），命中的按快照处理（DAY 过期 CANCELLED_ALL → CANCELED，成交照常合成）。

        vendor 没写清 start / end 的时区：窗口前后各放宽 HISTORY_MARGIN，再按 client_order_id / broker_order_id 精确匹配；
        其余历史订单不处理（不报 unknown / external）。查询出错只报警，返回空集，调用方照旧按「券商查不到」处理。
        """
        start = min(o.created_at for o in missing) - HISTORY_MARGIN
        end = self.clock.now() + HISTORY_MARGIN
        try:
            history = self.broker.query_order_history(start, end)
        except BrokerError as e:
            log.error("启动恢复查历史订单失败：%s", e)
            self._issue("history_query_failed", None, {"error": str(e), "orders": [o.client_order_id for o in missing]})
            return set()
        by_cid = {o.client_order_id for o in missing}
        by_bid = {o.broker_order_id: o.client_order_id for o in missing if o.broker_order_id}
        found: set[str] = set()
        for snap in history:
            cid = snap.client_order_id if snap.client_order_id in by_cid else by_bid.get(snap.broker_order_id)
            if cid is None or cid in found:
                continue
            self.ingest_snapshot(snap, "poll")
            found.add(cid)
        if found:
            log.info("启动恢复：%d 张当日列表查不到的订单在历史订单中找到并已更新", len(found))
        return found

    def _check_positions(self) -> None:
        net = self.store.net_fill_qty_by_symbol()
        broker = {p.symbol: p.qty for p in self.broker.query_positions()}  # vendor § Get Positions
        for sym in sorted(set(net) | set(broker)):
            local, held = net.get(sym, 0), broker.get(sym, 0)
            if local > held:
                self._issue("position_mismatch", None, {"symbol": sym, "local_net_fills": local, "broker_qty": held})
            elif held > local:
                # ADR-0019：多出来的是程序外持仓，只记录
                self._issue(
                    "external_position", None, {"symbol": sym, "local_net_fills": local, "broker_qty": held}, alert=False, mismatch=False
                )


__all__ = ["OMS", "Mismatch", "OmsEvent", "OrderNotFound", "RecoveryReport"]
