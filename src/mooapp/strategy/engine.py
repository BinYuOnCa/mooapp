"""规则引擎（M6b）：把纯逻辑的 RuleMachine 接到闸门、OMS 的成交事件、行情推送和 halt 上。

线程（M6b 审查修复 #1、#8）：
- 规则状态只在 EventPump 的事件线程上读写。行情推送（SDK 线程）、OrderChanged、HaltChanged、OmsReady 只排队。
- 事件线程**不调用闸门、不做网络调用**：下单、撤单、时段查询、快照、退订都交给动作执行器（threaded_actions=True 时是专门的线程；
  否则作为事件队列里的任务，供同步测试使用），结果再排回事件线程。闸门在自己的锁里会等事件线程处理 OMS 操作，
  如果事件线程反过来等闸门锁，就会死锁。
- add / cancel 由调用方线程（命令线程）调用闸门；准备和收尾经 pump.call 在事件线程上做。
写路径：入场单、卖单、撤单都经 RiskGate（来源 RULE；卖单带 protective，ADR-0021；Intent 带 rule_id，ADR-0020）。
成交按订单累计量算增量，并记下每张单已计入的数量和金额（存进 rules.state.orders），重复事件、恢复补喂都不会重复计。
默认做法（M6b 计划）：只在常规时段判断触发；停机重启后从 rules 表恢复；rule cancel 停止监控。
"""

from __future__ import annotations

import copy
import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from mooapp.broker.port import MarketDataPort, OrderRequest, Quote, new_client_order_id
from mooapp.clock import Clock
from mooapp.common import OrderKind, Side, Source
from mooapp.oms.events import Alert, EventBus, OmsReady, OrderChanged
from mooapp.oms.oms import OMS
from mooapp.oms.pump import EventPump
from mooapp.oms.state import TERMINAL, Order, OrdStatus
from mooapp.oms.store import RULE_ACTIVE, RuleEventRecord, RuleRecord, Store
from mooapp.risk.gate import Decision, HaltChanged, Intent, RiskGate
from mooapp.risk.rules import REGULAR_HOURS_STATES
from mooapp.strategy.machine import (
    ACTIVE_STATUSES,
    Action,
    CancelEntry,
    PlaceProtectiveSell,
    RuleMachine,
    RuleNote,
    RuleStatus,
)
from mooapp.strategy.spec import RuleSpec, RuleSpecError, load_rule

log = logging.getLogger(__name__)
MARKET_TZ = ZoneInfo("America/New_York")
_PRICE_Q = Decimal("0.000001")
_ALERT_NOTES = frozenset({"error", "unprotected_fill"})
_SELL_ENDED = frozenset({OrdStatus.REJECTED, OrdStatus.CANCELED, OrdStatus.EXPIRED})
_SESSION_BOUNDS = (time(9, 30), time(16, 0))  # 跨过这两个时刻（美东）时段缓存立即失效（审查 #7）
MARKET_STATE_FAILURE_TTL = 5.0  # 秒：时段查询失败也缓存，避免每笔推送都查
MIN_SUBSCRIBED_SECONDS = 60.0  # vendor § Subscribe and Unsubscribe：订阅后至少 1 分钟才能退订
FALLBACK_ALERT_INTERVAL = 60.0  # 秒：快照兜底失败时，同一标的的报警间隔（审查 #9）
_STOP = object()


@dataclass(frozen=True)
class AddResult:
    allowed: bool
    rule_id: str | None
    reason: str
    entry_cid: str | None = None


class _Actions:
    """动作执行器：在事件线程之外调用闸门和券商。threaded=False 时作为事件队列任务执行（同步测试）。"""

    def __init__(self, pump: EventPump, bus: EventBus, threaded: bool):
        self._pump = pump
        self._bus = bus
        self._threaded = threaded
        self._q: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None

    def submit(self, fn: Callable[[], None]) -> None:
        if self._threaded:
            self._q.put(fn)
        else:
            self._pump.submit_task(fn)

    def start(self) -> None:
        if self._threaded and self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="mooapp-rule-actions", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        self._q.put(_STOP)
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise RuntimeError(f"规则动作线程 {timeout:g} 秒内未停止")
        self._thread = None

    def _loop(self) -> None:
        while True:
            fn = self._q.get()
            if fn is _STOP:
                return
            try:
                fn()
            except Exception as e:  # 每个动作自己接住业务异常；这里兜住代码缺陷：记日志、报警，继续下一个
                log.exception("规则动作出错")
                self._bus.publish(Alert("rule_action_error", f"{type(e).__name__}：{e}"))


@dataclass
class _MarketState:
    state: str
    mono: float
    segment: tuple[object, int]  # (美东日期, 时段序号)：跨过 9:30 / 16:00 即不同
    ttl: float


class RuleEngine:
    def __init__(
        self,
        gate: RiskGate,
        oms: OMS,
        store: Store,
        md: MarketDataPort,
        bus: EventBus,
        clock: Clock,
        pump: EventPump,
        poll_fallback_seconds: float = 5.0,
        market_state_ttl: float = 30.0,
        max_sell_attempts: int = 3,
        threaded_actions: bool = False,
    ):
        self._gate = gate
        self._oms = oms
        self._store = store
        self._md = md
        self._bus = bus
        self._clock = clock
        self._pump = pump
        self.poll_fallback_seconds = poll_fallback_seconds
        self.market_state_ttl = market_state_ttl
        self.max_sell_attempts = max_sell_attempts
        self._actions = _Actions(pump, bus, threaded_actions)
        self._machines: dict[str, RuleMachine] = {}
        self._counted: dict[str, dict[str, dict[str, Any]]] = {}  # rule_id → cid → {"qty", "cost", "ended"}
        self._persisted: dict[str, dict[str, Any]] = {}
        self._subscribed: dict[str, float] = {}  # symbol → 订阅时刻（monotonic）
        self._last_quote_at: dict[str, float] = {}
        self._last_price: dict[str, Decimal] = {}
        self._market: dict[str, _MarketState] = {}
        self._market_refreshing: set[str] = set()
        self._pending_quote: dict[str, Quote] = {}
        self._fallback_inflight: set[str] = set()
        self._fallback_alert_at: dict[str, float] = {}
        self._deferred: list[tuple[str, Action]] = []
        self._paused = gate.halted
        bus.subscribe(OrderChanged, lambda e: pump.submit_task(lambda o=e.order: self._on_order_changed(o)))
        bus.subscribe(HaltChanged, lambda e: pump.submit_task(lambda h=e.halted: self._on_halt(h)))
        bus.subscribe(OmsReady, lambda e: pump.submit_task(self._flush_deferred))

    def start(self) -> None:
        self._actions.start()

    def stop(self) -> None:
        self._actions.stop()

    # ------------------------------------------------------------------ 查询

    def rule(self, rule_id: str) -> RuleRecord:
        r = self._store.get_rule(rule_id)
        if r is None:
            raise KeyError(rule_id)
        return r

    def status(self, rule_id: str) -> RuleStatus:
        m = self._machines.get(rule_id)
        return m.status if m is not None else RuleStatus(self.rule(rule_id).status)

    # ------------------------------------------------------------------ 命令（调用方线程；准备与收尾在事件线程）

    def add(self, yaml_text: str, actor: str) -> AddResult:
        prep = self._pump.call(lambda: self._add_prepare(yaml_text, actor))
        if isinstance(prep, AddResult):
            return prep
        spec, req = prep
        intent = Intent(Source.RULE, "place", request=req, actor=f"rule:{spec.id}", rule_id=spec.id)
        d, err = self._call_gate(intent)
        return self._pump.call(lambda: self._add_finish(spec, req, d, err))

    def cancel(self, rule_id: str, actor: str) -> dict[str, Any]:
        res, entry_cid = self._pump.call(lambda: self._cancel_prepare(rule_id, actor))
        if entry_cid is not None:
            d, err = self._call_gate(Intent(Source.RULE, "cancel", client_order_id=entry_cid, actor=f"rule:{rule_id}", rule_id=rule_id))
            self._pump.call(lambda: self._cancel_entry_result(rule_id, d, err))
        return res

    def recover(self) -> None:
        """从 rules 表恢复活动规则（默认做法 2）。应在 OMS 启动恢复之前调用：之后 OMS 产生的 OrderChanged 会补喂停机期间的成交。
        OMS 就绪（OmsReady）之前产生的下单、撤单动作先排队（审查 #5）。"""
        self._pump.call(self._recover)

    def poll_stale_quotes(self) -> None:
        """常驻进程每秒调用一次：推送中断的标的用快照补；不再需要的订阅退订。只排队。"""
        self._pump.submit_task(self._poll_stale)

    # ------------------------------------------------------------------ 内部：命令

    def _call_gate(self, intent: Intent) -> tuple[Decision | None, str | None]:
        """在非事件线程上调用闸门。异常不吞：转成原因文本交回事件线程处理（审查 #3），并记日志。"""
        try:
            return self._gate.submit(intent), None
        except Exception as e:
            log.exception("规则引擎调用闸门出错")
            return None, f"{type(e).__name__}：{e}"

    def _add_prepare(self, yaml_text: str, actor: str) -> AddResult | tuple[RuleSpec, OrderRequest]:
        try:
            spec = load_rule(yaml_text)
        except RuleSpecError as e:
            return AddResult(False, None, f"规则不合法：{e}")
        if self._store.get_rule(spec.id) is not None:
            return AddResult(False, spec.id, f"规则 id {spec.id} 已存在")
        active = self._store.active_rule_for(spec.symbol)
        if active is not None:
            return AddResult(False, spec.id, f"{spec.symbol} 已有活动规则 {active}（ADR-0020）")
        now = self._clock.now()
        entry_cid = new_client_order_id()
        m = RuleMachine(spec, self.max_sell_attempts)
        if self._paused:
            m.pause()
        self._machines[spec.id] = m
        self._counted[spec.id] = {}
        with self._store.transaction():
            self._store.insert_rule(RuleRecord(spec.id, spec.symbol, yaml_text, m.status.value, entry_cid, self._state(spec.id), actor, now, now))
            self._store.link_rule_order(entry_cid, spec.id, "entry", now)
            self._store.append_rule_event(RuleEventRecord(spec.id, now, "added", f"{actor} 添加规则", {"entry_cid": entry_cid}))
        self._persisted[spec.id] = copy.deepcopy(self._state(spec.id))
        return spec, OrderRequest(entry_cid, spec.symbol, Side.BUY, spec.entry.qty, OrderKind.LIMIT, spec.entry.limit_price)

    def _add_finish(self, spec: RuleSpec, req: OrderRequest, d: Decision | None, err: str | None) -> AddResult:
        m = self._machines[spec.id]
        if err is not None or d is None or not d.allowed:
            reason = f"入场单出错：{err}" if err is not None else f"闸门拒绝入场单（{d.rule}）：{d.reason}"  # type: ignore[union-attr]
            self._event(spec.id, "entry_rejected", reason)
            self._run(spec.id, m.on_entry_terminal())  # 一股未成交 → VOID，不留活动规则
            return AddResult(False, spec.id, reason, req.client_order_id)
        self._subscribe(spec.symbol)
        self._persist(spec.id, force=True)
        return AddResult(True, spec.id, "ok", req.client_order_id)

    def _cancel_prepare(self, rule_id: str, actor: str) -> tuple[dict[str, Any], str | None]:
        rec = self.rule(rule_id)  # 不存在则 KeyError
        m = self._machines.get(rule_id)
        if m is None:
            if rec.status not in RULE_ACTIVE:
                return {"rule_id": rule_id, "status": rec.status, "unprotected_qty": 0}, None
            # 库里是活动的，但没有加载（例如加载失败、或本进程没 recover）：直接在库里标为 CANCELED（审查 #10）
            held = int(rec.state.get("bought", 0)) - int(rec.state.get("sold", 0))
            with self._store.transaction():
                self._store.update_rule(rule_id, RuleStatus.CANCELED.value, {**rec.state, "cancelled_unloaded": True}, self._clock.now())
            self._event(rule_id, "cancelled", f"{actor} 取消未加载的规则" + (f"；持仓 {held} 股不再受保护" if held > 0 else ""))
            return {"rule_id": rule_id, "status": RuleStatus.CANCELED.value, "unprotected_qty": max(held, 0)}, self._live_entry(rec)
        if m.status not in ACTIVE_STATUSES:
            return {"rule_id": rule_id, "status": m.status.value, "unprotected_qty": 0}, None
        self._event(rule_id, "cancel_requested", f"{actor} 取消规则")
        acts = m.cancel()
        need_cancel = any(isinstance(a, CancelEntry) for a in acts)
        self._run(rule_id, [a for a in acts if not isinstance(a, CancelEntry)])
        return {"rule_id": rule_id, "status": m.status.value, "unprotected_qty": m.held}, self._live_entry(rec) if need_cancel else None

    def _live_entry(self, rec: RuleRecord) -> str | None:
        o = self._oms.get_or_none(rec.entry_cid)
        return rec.entry_cid if o is not None and o.status not in TERMINAL else None

    def _recover(self) -> None:
        self._paused = self._gate.halted
        for rec in self._store.list_rules(active_only=True):
            try:
                spec = load_rule(rec.spec_yaml)
            except RuleSpecError as e:
                # 审查 #10：不能静默跳过（会一直占着 ADR-0020 的名额）：标为 ERROR 并报警
                with self._store.transaction():
                    self._store.update_rule(rec.id, RuleStatus.ERROR.value, {**rec.state, "load_error": str(e)}, self._clock.now())
                self._event(rec.id, "error", f"恢复时规则加载失败，已标为 ERROR：{e}")
                self._bus.publish(Alert("rule_error", f"规则 {rec.id} 恢复失败，已标为 ERROR：{e}"))
                continue
            state = dict(rec.state)
            counted = state.pop("orders", {}) or {}
            m = RuleMachine.from_state(spec, state, self.max_sell_attempts)
            m.pause() if self._paused else m.resume()  # 审查 #4：按闸门当前状态，不沿用快照里的 paused
            self._machines[rec.id] = m
            self._counted[rec.id] = {k: dict(v) for k, v in counted.items()}
            self._persisted[rec.id] = copy.deepcopy(self._state(rec.id))
            self._event(rec.id, "recovered", f"从数据库恢复（{m.status.value}）")
            self._subscribe(spec.symbol)
            for cid, _role in self._store.rule_orders(rec.id):  # 先按本地已有的订单状态对齐
                o = self._oms.get_or_none(cid)
                if o is not None:
                    self._on_order_changed(o)

    # ------------------------------------------------------------------ 内部：事件（事件线程）

    def _on_halt(self, halted: bool) -> None:
        self._paused = halted
        for m in self._machines.values():
            m.pause() if halted else m.resume()

    def _on_order_changed(self, o: Order) -> None:
        link = self._store.rule_order(o.client_order_id)
        if link is None:
            return
        rule_id, role = link
        m = self._machines.get(rule_id)
        if m is None:
            return
        counted = self._counted.setdefault(rule_id, {}).setdefault(o.client_order_id, {"qty": 0, "cost": "0", "ended": False})
        acts: list[Action] = []
        delta = o.cum_qty - int(counted["qty"])
        if delta > 0:
            total = o.avg_px * o.cum_qty
            price = ((total - Decimal(counted["cost"])) / delta).quantize(_PRICE_Q)
            counted["qty"], counted["cost"] = o.cum_qty, str(total)
            if role == "entry":
                acts += m.on_entry_fill(delta, price, last=self._last_price.get(m.spec.symbol))
            else:
                acts += m.on_sell_filled(delta, price)
        if role == "entry" and o.status in TERMINAL and not m.entry_done:
            acts += m.on_entry_terminal()
        ended = counted.get("ended") or counted.get("rejected")
        if role == "sell" and o.status in _SELL_ENDED and not ended:
            counted["ended"] = True
            if o.cum_qty < o.qty:
                # 审查 #2：被拒、被撤（例如 halt）、过期，剩余没卖出的都按卖单失败处理，下一笔行情重试
                reason = f"卖单 {o.status.value}，{o.qty - o.cum_qty} 股未成交：{o.reason or ''}"
                self._event(rule_id, "sell_rejected", reason)
                acts += m.on_sell_rejected(reason)
        self._run(rule_id, acts)

    def _on_quote(self, q: Quote) -> None:
        sym = q.symbol
        self._last_quote_at[sym] = self._clock.monotonic()
        machines = [(rid, m) for rid, m in self._machines.items() if m.spec.symbol == sym and m.status in ACTIVE_STATUSES]
        if not machines:
            return
        rth = self._regular_hours(sym)
        if rth is None:
            self._pending_quote[sym] = q  # 时段查询中：查完后用这笔行情再判断一次
            return
        if not rth:
            return  # 默认做法 1：非常规时段忽略
        self._last_price[sym] = q.last
        for rule_id, m in machines:
            acts: list[Action] = []
            if q.open is not None:
                acts += m.on_open(q.open)
            acts += m.on_tick(q.last)
            self._run(rule_id, acts)

    def _poll_stale(self) -> None:
        now = self._clock.monotonic()
        for sym in sorted(self._subscribed):
            active = any(m.spec.symbol == sym and m.status in ACTIVE_STATUSES for m in self._machines.values())
            if not active:
                if now - self._subscribed[sym] >= MIN_SUBSCRIBED_SECONDS:
                    del self._subscribed[sym]  # 审查 #6：不再需要的订阅退订
                    self._actions.submit(lambda s=sym: self._do_unsubscribe(s))
                continue
            last = self._last_quote_at.get(sym)
            if (last is not None and now - last <= self.poll_fallback_seconds) or sym in self._fallback_inflight:
                continue
            self._fallback_inflight.add(sym)
            self._actions.submit(lambda s=sym: self._do_snapshot(s))

    def _flush_deferred(self) -> None:
        pending, self._deferred = self._deferred, []
        for rule_id, a in pending:
            self._event(rule_id, "deferred_run", "OMS 已就绪，执行恢复期间排队的动作")
            self._dispatch(rule_id, a)
            self._persist(rule_id)

    # ------------------------------------------------------------------ 内部：执行动作（事件线程决定，执行器执行）

    def _run(self, rule_id: str, acts: list[Action]) -> None:
        for a in acts:
            if isinstance(a, RuleNote):
                self._event(rule_id, a.kind, a.message)
                if a.kind in _ALERT_NOTES:
                    self._bus.publish(Alert(f"rule_{a.kind}", f"规则 {rule_id}：{a.message}"))
            else:
                self._dispatch(rule_id, a)
        self._persist(rule_id)

    def _dispatch(self, rule_id: str, a: Action) -> None:
        if not self._oms.ready:
            self._deferred.append((rule_id, a))  # 审查 #5：OMS 就绪前闸门一律拒绝（not_ready），不能算作失败
            self._event(rule_id, "deferred", f"OMS 尚未就绪，动作排队：{type(a).__name__}")
            return
        if isinstance(a, CancelEntry):
            entry_cid = self.rule(rule_id).entry_cid
            self._actions.submit(lambda: self._do_cancel_entry(rule_id, entry_cid))
        elif isinstance(a, PlaceProtectiveSell):
            cid = new_client_order_id()
            with self._store.transaction():
                self._store.link_rule_order(cid, rule_id, "sell", self._clock.now())
            self._actions.submit(lambda: self._do_sell(rule_id, cid, a))

    # 执行器一侧（不碰规则状态，结果排回事件线程）

    def _do_sell(self, rule_id: str, cid: str, a: PlaceProtectiveSell) -> None:
        req = OrderRequest(cid, a.symbol, Side.SELL, a.qty, OrderKind.MARKET, None)
        d, err = self._call_gate(Intent(Source.RULE, "place", request=req, actor=f"rule:{rule_id}", protective=True, rule_id=rule_id))
        self._pump.submit_task(lambda: self._sell_result(rule_id, cid, a, d, err))

    def _do_cancel_entry(self, rule_id: str, entry_cid: str) -> None:
        o = self._oms.get_or_none(entry_cid)
        if o is None or o.status in TERMINAL:
            return
        d, err = self._call_gate(Intent(Source.RULE, "cancel", client_order_id=entry_cid, actor=f"rule:{rule_id}", rule_id=rule_id))
        self._pump.submit_task(lambda: self._cancel_entry_result(rule_id, d, err))

    def _do_snapshot(self, sym: str) -> None:
        try:
            quotes, err = self._md.snapshot([sym]), None  # vendor § Get Market Snapshot
        except Exception as e:  # 记下原文交回事件线程（限频报警），不吞
            quotes, err = [], f"{type(e).__name__}：{e}"
        self._pump.submit_task(lambda: self._snapshot_result(sym, quotes, err))

    def _do_market_state(self, sym: str) -> None:
        try:
            state, err = self._md.market_state(sym), None  # vendor § Get Market Status of Securities
        except Exception as e:
            state, err = None, f"{type(e).__name__}：{e}"
        self._pump.submit_task(lambda: self._market_state_result(sym, state, err))

    def _do_unsubscribe(self, sym: str) -> None:
        try:
            self._md.unsubscribe_quotes([sym])  # type: ignore[attr-defined]
        except Exception as e:
            log.exception("退订 %s 失败", sym)
            self._bus.publish(Alert("quote_unsubscribe_failed", f"退订 {sym} 失败：{type(e).__name__}：{e}"))

    # 结果（事件线程）

    def _sell_result(self, rule_id: str, cid: str, a: PlaceProtectiveSell, d: Decision | None, err: str | None) -> None:
        detail = {"client_order_id": cid, "side": a.side, "qty": a.qty, "attempt": a.attempt, "observed": str(a.observed_price)}
        if err is None and d is not None and d.allowed:
            self._event(rule_id, "sell_sent", f"市价卖 {a.qty} 股（第 {a.attempt} 次）", detail)
            return
        reason = f"卖单出错：{err}" if err is not None else f"闸门拒绝卖单（{d.rule}）：{d.reason}"  # type: ignore[union-attr]
        self._event(rule_id, "sell_rejected", reason, detail)
        m = self._machines.get(rule_id)
        if m is not None:
            self._run(rule_id, m.on_sell_rejected(reason))

    def _cancel_entry_result(self, rule_id: str, d: Decision | None, err: str | None) -> None:
        if err is not None:
            self._event(rule_id, "cancel_entry_failed", f"撤入场单余量出错：{err}")
            self._bus.publish(Alert("rule_cancel_entry_failed", f"规则 {rule_id} 撤入场单余量出错：{err}"))
        elif d is not None:
            self._event(rule_id, "cancel_entry", "已请求撤入场单余量" if d.allowed else f"撤入场单余量被拒（{d.rule}）：{d.reason}")

    def _snapshot_result(self, sym: str, quotes: list[Quote], err: str | None) -> None:
        self._fallback_inflight.discard(sym)
        if err is not None:
            now = self._clock.monotonic()
            last = self._fallback_alert_at.get(sym)
            if last is None or now - last >= FALLBACK_ALERT_INTERVAL:  # 审查 #9：限频报警
                self._fallback_alert_at[sym] = now
                self._bus.publish(Alert("quote_fallback_failed", f"{sym} 推送中断，快照也取不到：{err}"))
            log.warning("行情兜底取 %s 快照失败：%s", sym, err)
            return
        for q in quotes:
            self._on_quote(q)

    def _market_state_result(self, sym: str, state: str | None, err: str | None) -> None:
        self._market_refreshing.discard(sym)
        if err is not None:
            log.warning("取 %s 时段失败，按非常规时段处理：%s", sym, err)
        self._market[sym] = _MarketState(
            state if state is not None else f"UNAVAILABLE（{err}）",
            self._clock.monotonic(),
            self._segment(),
            self.market_state_ttl if err is None else MARKET_STATE_FAILURE_TTL,
        )
        q = self._pending_quote.pop(sym, None)
        if q is not None:
            self._on_quote(q)

    # ------------------------------------------------------------------ 内部：状态、订阅、时段

    def _event(self, rule_id: str, kind: str, message: str, details: dict[str, Any] | None = None) -> None:
        with self._store.transaction():
            self._store.append_rule_event(RuleEventRecord(rule_id, self._clock.now(), kind, message, details or {}))

    def _state(self, rule_id: str) -> dict[str, Any]:
        st = self._machines[rule_id].to_state()
        st["orders"] = self._counted.get(rule_id, {})
        return st

    def _persist(self, rule_id: str, force: bool = False) -> None:
        st = copy.deepcopy(self._state(rule_id))  # 深拷贝：之后原地修改的计数不能影响「上次写库的内容」
        old = self._persisted.get(rule_id)
        # 只差最新价时不写库（每笔行情都写太频繁）；其余变化都写
        if not force and old is not None and {**st, "last_observed": None} == {**old, "last_observed": None}:
            return
        m = self._machines[rule_id]
        with self._store.transaction():
            self._store.update_rule(rule_id, m.status.value, st, self._clock.now())
        self._persisted[rule_id] = st

    def _subscribe(self, symbol: str) -> None:
        if symbol in self._subscribed:
            return  # 审查 #6：同一标的只订阅一次
        self._md.subscribe_quotes([symbol], lambda q: self._pump.submit_task(lambda q=q: self._on_quote(q)))  # type: ignore[attr-defined]
        self._subscribed[symbol] = self._clock.monotonic()

    def _segment(self) -> tuple[object, int]:
        et = self._clock.now().astimezone(MARKET_TZ)
        t = et.time()
        return (et.date(), sum(1 for b in _SESSION_BOUNDS if t >= b))

    def _regular_hours(self, symbol: str) -> bool | None:
        """True / False；None 表示正在查询（结果回来后会用 pending 的行情再判断一次）。"""
        cached = self._market.get(symbol)
        if cached is not None and self._clock.monotonic() - cached.mono <= cached.ttl and cached.segment == self._segment():
            return cached.state in REGULAR_HOURS_STATES
        if symbol not in self._market_refreshing:
            self._market_refreshing.add(symbol)
            self._actions.submit(lambda s=symbol: self._do_market_state(s))
        return None


__all__ = ["AddResult", "RuleEngine"]
