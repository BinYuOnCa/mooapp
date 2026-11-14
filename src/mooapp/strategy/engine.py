"""规则引擎（M6b）：把纯逻辑的 RuleMachine 接到闸门、OMS 的成交事件、行情推送和 halt 上。

线程：全部状态只在 EventPump 的事件线程上读写。行情推送（SDK 线程）、OrderChanged、HaltChanged 只排队；
add / cancel / recover 经 pump.call 在事件线程上执行。
写路径：入场单、卖单、撤单都经 RiskGate（来源 RULE；卖单带 protective，ADR-0021；Intent 带 rule_id，ADR-0020）。
成交按订单累计量算增量，并记下每张单已计入的数量和金额（存进 rules.state.orders），重复事件、恢复补喂都不会重复计。
默认做法（M6b 计划）：只在常规时段判断触发（market_state 缓存 30 秒）；停机重启后从 rules 表恢复；rule cancel 停止监控。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from mooapp.broker.port import BrokerError, MarketDataPort, OrderRequest, Quote, new_client_order_id
from mooapp.clock import Clock
from mooapp.common import OrderKind, Side, Source
from mooapp.oms.events import Alert, EventBus, OrderChanged
from mooapp.oms.oms import OMS
from mooapp.oms.pump import EventPump
from mooapp.oms.state import TERMINAL, Order, OrdStatus
from mooapp.oms.store import RuleEventRecord, RuleRecord, Store
from mooapp.risk.gate import HaltChanged, Intent, RiskGate
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
from mooapp.strategy.spec import RuleSpecError, load_rule

log = logging.getLogger(__name__)
_PRICE_Q = Decimal("0.000001")
_ALERT_NOTES = frozenset({"error", "unprotected_fill"})


@dataclass(frozen=True)
class AddResult:
    allowed: bool
    rule_id: str | None
    reason: str
    entry_cid: str | None = None


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
        self._machines: dict[str, RuleMachine] = {}
        self._counted: dict[str, dict[str, dict[str, Any]]] = {}  # rule_id → cid → {"qty", "cost", "rejected"}
        self._persisted: dict[str, dict[str, Any]] = {}
        self._subscribed: set[str] = set()
        self._last_quote_at: dict[str, float] = {}
        self._last_price: dict[str, Decimal] = {}
        self._state_cache: dict[str, tuple[str, float]] = {}
        self._paused = gate.halted
        bus.subscribe(OrderChanged, lambda e: pump.submit_task(lambda o=e.order: self._on_order_changed(o)))
        bus.subscribe(HaltChanged, lambda e: pump.submit_task(lambda h=e.halted: self._on_halt(h)))

    # ------------------------------------------------------------------ 查询

    def rule(self, rule_id: str) -> RuleRecord:
        r = self._store.get_rule(rule_id)
        if r is None:
            raise KeyError(rule_id)
        return r

    def status(self, rule_id: str) -> RuleStatus:
        m = self._machines.get(rule_id)
        return m.status if m is not None else RuleStatus(self.rule(rule_id).status)

    # ------------------------------------------------------------------ 命令（在事件线程上执行）

    def add(self, yaml_text: str, actor: str) -> AddResult:
        return self._pump.call(lambda: self._add(yaml_text, actor))

    def cancel(self, rule_id: str, actor: str) -> dict[str, Any]:
        return self._pump.call(lambda: self._cancel(rule_id, actor))

    def recover(self) -> None:
        """从 rules 表恢复活动规则（默认做法 2）。应在 OMS 启动恢复之前调用：之后 OMS 产生的 OrderChanged 会补喂停机期间的成交。"""
        self._pump.call(self._recover)

    def poll_stale_quotes(self) -> None:
        """行情兜底：某个标的超过 poll_fallback_seconds 没有推送，就取一次快照（常驻进程每秒调用一次）。"""
        self._pump.submit_task(self._poll_stale)

    # ------------------------------------------------------------------ 内部：命令

    def _add(self, yaml_text: str, actor: str) -> AddResult:
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
        self._persisted[spec.id] = self._state(spec.id)
        req = OrderRequest(entry_cid, spec.symbol, Side.BUY, spec.entry.qty, OrderKind.LIMIT, spec.entry.limit_price)
        d = self._gate.submit(Intent(Source.RULE, "place", request=req, actor=f"rule:{spec.id}", rule_id=spec.id))
        if not d.allowed:
            reason = f"闸门拒绝入场单（{d.rule}）：{d.reason}"
            self._event(spec.id, "entry_rejected", reason)
            self._run(spec.id, m.on_entry_terminal())
            return AddResult(False, spec.id, reason, entry_cid)
        self._subscribe(spec.symbol)
        self._persist(spec.id, force=True)
        return AddResult(True, spec.id, "ok", entry_cid)

    def _cancel(self, rule_id: str, actor: str) -> dict[str, Any]:
        rec = self.rule(rule_id)  # 不存在则 KeyError
        m = self._machines.get(rule_id)
        if m is None or m.status not in ACTIVE_STATUSES:
            return {"rule_id": rule_id, "status": rec.status, "unprotected_qty": 0}
        self._event(rule_id, "cancel_requested", f"{actor} 取消规则")
        self._run(rule_id, m.cancel())
        return {"rule_id": rule_id, "status": m.status.value, "unprotected_qty": m.held}

    def _recover(self) -> None:
        self._paused = self._gate.halted
        for rec in self._store.list_rules(active_only=True):
            try:
                spec = load_rule(rec.spec_yaml)
            except RuleSpecError as e:
                self._bus.publish(Alert("rule_error", f"规则 {rec.id} 恢复失败：{e}"))
                continue
            state = dict(rec.state)
            counted = state.pop("orders", {}) or {}
            m = RuleMachine.from_state(spec, state, self.max_sell_attempts)
            if self._paused:
                m.pause()
            self._machines[rec.id] = m
            self._counted[rec.id] = {k: dict(v) for k, v in counted.items()}
            self._persisted[rec.id] = self._state(rec.id)
            self._event(rec.id, "recovered", f"从数据库恢复（{m.status.value}）")
            self._subscribe(spec.symbol)
            for cid, _role in self._store.rule_orders(rec.id):  # 先按本地已有的订单状态对齐
                o = self._oms.get_or_none(cid)
                if o is not None:
                    self._on_order_changed(o)

    # ------------------------------------------------------------------ 内部：事件

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
        counted = self._counted.setdefault(rule_id, {}).setdefault(o.client_order_id, {"qty": 0, "cost": "0", "rejected": False})
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
        if role == "sell" and o.status is OrdStatus.REJECTED and not counted["rejected"]:
            counted["rejected"] = True
            reason = f"券商拒绝卖单：{o.reason or ''}"
            self._event(rule_id, "sell_rejected", reason)
            acts += m.on_sell_rejected(reason)
        self._run(rule_id, acts)

    def _on_quote(self, q: Quote) -> None:
        sym = q.symbol
        self._last_quote_at[sym] = self._clock.monotonic()
        machines = [(rid, m) for rid, m in self._machines.items() if m.spec.symbol == sym and m.status in ACTIVE_STATUSES]
        if not machines or not self._regular_hours(sym):
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
            if not any(m.spec.symbol == sym and m.status in ACTIVE_STATUSES for m in self._machines.values()):
                continue
            last = self._last_quote_at.get(sym)
            if last is not None and now - last <= self.poll_fallback_seconds:
                continue
            try:
                quotes = self._md.snapshot([sym])  # vendor § Get Market Snapshot
            except BrokerError as e:
                log.warning("行情兜底取 %s 快照失败：%s", sym, e)
                self._bus.publish(Alert("quote_fallback_failed", f"{sym} 推送中断，快照也取不到：{e}"))
                continue
            for q in quotes:
                self._on_quote(q)

    # ------------------------------------------------------------------ 内部：执行动作、持久化

    def _run(self, rule_id: str, acts: list[Action]) -> None:
        m = self._machines[rule_id]
        for a in acts:
            if isinstance(a, RuleNote):
                self._event(rule_id, a.kind, a.message)
                if a.kind in _ALERT_NOTES:
                    self._bus.publish(Alert(f"rule_{a.kind}", f"规则 {rule_id}：{a.message}"))
            elif isinstance(a, CancelEntry):
                self._cancel_entry(rule_id)
            elif isinstance(a, PlaceProtectiveSell):
                self._place_sell(rule_id, m, a)
        self._persist(rule_id)

    def _cancel_entry(self, rule_id: str) -> None:
        rec = self.rule(rule_id)
        o = self._oms.get_or_none(rec.entry_cid)
        if o is None or o.status in TERMINAL:
            return
        try:
            d = self._gate.submit(Intent(Source.RULE, "cancel", client_order_id=rec.entry_cid, actor=f"rule:{rule_id}", rule_id=rule_id))
        except BrokerError as e:
            self._event(rule_id, "cancel_entry_failed", f"撤入场单余量失败：{e}")
            self._bus.publish(Alert("rule_cancel_entry_failed", f"规则 {rule_id} 撤入场单余量失败：{e}"))
            return
        self._event(rule_id, "cancel_entry", "已请求撤入场单余量" if d.allowed else f"撤入场单余量被拒（{d.rule}）：{d.reason}")

    def _place_sell(self, rule_id: str, m: RuleMachine, a: PlaceProtectiveSell) -> None:
        cid = new_client_order_id()
        now = self._clock.now()
        with self._store.transaction():
            self._store.link_rule_order(cid, rule_id, "sell", now)
        req = OrderRequest(cid, a.symbol, Side.SELL, a.qty, OrderKind.MARKET, None)
        d = self._gate.submit(Intent(Source.RULE, "place", request=req, actor=f"rule:{rule_id}", protective=True, rule_id=rule_id))
        detail = {"client_order_id": cid, "side": a.side, "qty": a.qty, "attempt": a.attempt, "observed": str(a.observed_price)}
        if not d.allowed:
            reason = f"闸门拒绝卖单（{d.rule}）：{d.reason}"
            self._event(rule_id, "sell_rejected", reason, detail)
            self._run(rule_id, m.on_sell_rejected(reason))
            return
        self._event(rule_id, "sell_sent", f"市价卖 {a.qty} 股（第 {a.attempt} 次）", detail)

    def _event(self, rule_id: str, kind: str, message: str, details: dict[str, Any] | None = None) -> None:
        with self._store.transaction():
            self._store.append_rule_event(RuleEventRecord(rule_id, self._clock.now(), kind, message, details or {}))

    def _state(self, rule_id: str) -> dict[str, Any]:
        st = self._machines[rule_id].to_state()
        st["orders"] = self._counted.get(rule_id, {})
        return st

    def _persist(self, rule_id: str, force: bool = False) -> None:
        st = self._state(rule_id)
        old = self._persisted.get(rule_id)
        # 只差最新价时不写库（每笔行情都写太频繁）；其余变化都写
        if not force and old is not None and {**st, "last_observed": None} == {**old, "last_observed": None}:
            return
        m = self._machines[rule_id]
        with self._store.transaction():
            self._store.update_rule(rule_id, m.status.value, st, self._clock.now())
        self._persisted[rule_id] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in st.items()}

    def _subscribe(self, symbol: str) -> None:
        self._md.subscribe_quotes([symbol], lambda q: self._pump.submit_task(lambda q=q: self._on_quote(q)))  # type: ignore[attr-defined]
        self._subscribed.add(symbol)

    def _regular_hours(self, symbol: str) -> bool:
        cached = self._state_cache.get(symbol)
        now = self._clock.monotonic()
        if cached is None or now - cached[1] > self.market_state_ttl:
            try:
                state = self._md.market_state(symbol)  # vendor § Get Market Status of Securities
            except BrokerError as e:
                log.warning("取 %s 时段失败，按非常规时段处理：%s", symbol, e)
                return False
            cached = (state, now)
            self._state_cache[symbol] = cached
        return cached[0] in REGULAR_HOURS_STATES
