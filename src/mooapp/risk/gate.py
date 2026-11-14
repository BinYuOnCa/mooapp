"""RiskGate：下单、撤单、halt、resume 的唯一入口（SPEC 5.5；手册 M4；M4 计划）。

下单：bad_intent → REAL 拒绝（确认点 4：实盘仍关闭）→ OMS 未完成启动恢复 → halt → 收集事实 → risk/rules.py 的规则。
  收集事实时库不可用 → store_unavailable；券商查询出错 → broker_unavailable（ADR-0025：读不到当日数据就不下新单）。
  行情、时段查询出错不直接拒：交给规则（quote_stale / regular_hours），拒绝理由里带出错原文。
撤单：只检查「订单属于本程序且处于活动状态」，不受限额和 halt 限制（ADR-0016）；未确认的订单按 ADR-0026 挂「待撤」标记。
halt：状态写 kv（ADR-0022）；撤销本程序全部活动订单；不碰外部订单（ADR-0019）；REAL 时上锁（ADR-0015）；发 HaltChanged。
每次判定写 audit；拒单发 RiskRejected；当日亏损触线且 halt_on_daily_loss 时自动 halt（ADR-0017）。
审计或 halt 状态写不进库时：撤单、halt 照常，报警（ADR-0025）；下单则拒绝。
检查与发单在同一把锁内（SPEC 3.4），避免两个入口同时通过「当日次数」检查。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from mooapp.broker.port import BrokerError, MarketDataPort, OrderRequest
from mooapp.clock import Clock
from mooapp.common import Env, MooappError, Source
from mooapp.config.models import RiskLimits
from mooapp.oms.events import Alert, EventBus, OmsReady
from mooapp.oms.executor import CANCEL_ACCEPTED, CANCELLABLE, Executor
from mooapp.oms.oms import OMS, UNACKED
from mooapp.oms.state import Order
from mooapp.oms.store import Store, StoreUnavailable
from mooapp.oms.ticket import GateTicket, _issue_ticket
from mooapp.risk.audit import audit_for
from mooapp.risk.rules import OrderContext, check_order

log = logging.getLogger(__name__)
HALT_KEY = "halt"


@dataclass(frozen=True)
class Intent:
    source: Source
    action: Literal["place", "cancel"]  # 不支持 modify（Q4）
    request: OrderRequest | None = None  # place 时必填
    client_order_id: str | None = None  # cancel 时必填
    actor: str = ""
    protective: bool = False  # ADR-0021
    rule_id: str | None = None  # M6b：规则引擎发出的单所属的规则


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule: str | None  # 命中的规则名
    reason: str
    order: Order | None = None
    ticket: GateTicket | None = None


@dataclass(frozen=True)
class RiskRejected:
    """拒单事件（M5 发邮件）。"""

    rule: str
    reason: str
    action: str
    source: str
    actor: str
    client_order_id: str | None
    symbol: str | None


@dataclass(frozen=True)
class HaltChanged:
    """halt / resume 事件（M6 规则引擎据此暂停，ADR-0021）。"""

    halted: bool
    reason: str
    actor: str


@dataclass(frozen=True)
class HaltState:
    reason: str
    actor: str
    since: datetime


@dataclass
class HaltReport:
    cancel_sent: list[str] = field(default_factory=list)  # 已向券商发出撤单
    deferred: list[str] = field(default_factory=list)  # 未确认，挂了「待撤」标记（ADR-0026）
    failed: dict[str, str] = field(default_factory=dict)  # 撤单失败：cid → 原因


class RiskGate:
    def __init__(
        self,
        env: Env,
        limits: RiskLimits,
        oms: OMS,
        executor: Executor,
        md: MarketDataPort,
        store: Store,
        bus: EventBus,
        clock: Clock,
        stale_after_seconds: float = 30.0,
    ):
        self._env = env
        self._limits = limits
        self._oms = oms
        self._executor = executor
        self._md = md
        self._store = store
        self._bus = bus
        self._clock = clock
        self.stale_after_seconds = stale_after_seconds
        self._lock = threading.RLock()  # 自动 halt 在 submit 里调用 halt，可重入
        self._halt = self._load_halt()  # ADR-0022：跨重启保留
        # M4 审查修复 #4：库不可用时的 halt 只在 journal 里；OMS 启动恢复回放 journal 后再读一次
        bus.subscribe(OmsReady, lambda e: self._reload_halt())

    # ------------------------------------------------------------------ halt 状态

    def _load_halt(self) -> HaltState | None:
        raw = self._store.get_kv(HALT_KEY)
        if raw is None:
            return None
        d = json.loads(raw)
        if not d.get("halted"):
            return None
        return HaltState(d["reason"], d["actor"], datetime.fromisoformat(d["since"]))

    def _reload_halt(self) -> None:
        with self._lock:
            persisted = self._load_halt()
            if persisted is not None and self._halt is None:
                self._halt = persisted
                log.warning("恢复后发现已持久化的 halt（%s），闸门进入 halt", persisted.reason)
                self._bus.publish(HaltChanged(True, persisted.reason, persisted.actor))

    @property
    def halted(self) -> bool:
        return self._halt is not None

    def halt_state(self) -> HaltState | None:
        return self._halt

    def _persist_halt(self, halted: bool, reason: str, actor: str, since: datetime) -> None:
        value = json.dumps({"halted": halted, "reason": reason, "actor": actor, "since": since.isoformat()}, ensure_ascii=False)
        try:
            with self._store.transaction():
                self._store.set_kv(HALT_KEY, value)
        except StoreUnavailable as e:
            self._bus.publish(Alert("halt_not_persisted", f"halt 状态未能写入数据库（已写 journal 或丢失）：{e}"))

    def _audit(self, **kw: object) -> None:
        """写审计；库不可用时报警但不阻断（撤单、halt 用）。下单路径另行处理。"""
        try:
            with self._store.transaction():
                self._store.append_audit(audit_for(ts=self._clock.now(), **kw))  # type: ignore[arg-type]
        except StoreUnavailable as e:
            self._bus.publish(Alert("audit_unavailable", f"审计未能写入数据库：{e}", details={k: str(v) for k, v in kw.items()}))

    # ------------------------------------------------------------------ 入口

    def submit(self, intent: Intent) -> Decision:
        with self._lock:
            if intent.action == "place":
                return self._place(intent)
            if intent.action == "cancel":
                return self._cancel(intent)
            return self._reject("bad_intent", f"不支持的动作 {intent.action!r}", intent)

    def _reject(self, rule: str, reason: str, intent: Intent, halt: bool = False) -> Decision:
        req = intent.request
        cid = req.client_order_id if req else intent.client_order_id
        log.info("闸门拒绝 %s（%s，%s）：%s", intent.action, intent.source.value, rule, reason)
        self._audit(
            action=str(intent.action), source=intent.source.value, actor=intent.actor, allowed=False, rule=rule, reason=reason,
            client_order_id=cid, symbol=req.symbol if req else None, side=req.side.value if req else None,
            qty=req.qty if req else None, price=req.limit_price if req else None, details={"protective": intent.protective},
        )  # fmt: skip
        self._bus.publish(RiskRejected(rule, reason, str(intent.action), intent.source.value, intent.actor, cid, req.symbol if req else None))
        if halt and not self.halted:
            self._bus.publish(Alert("daily_loss_halt", f"当日亏损触线，自动 halt：{reason}"))
            self.halt(f"当日亏损触线：{reason}", actor="system:daily_loss", source=Source.SYSTEM)
        return Decision(False, rule, reason)

    # ------------------------------------------------------------------ 下单

    def _place(self, intent: Intent) -> Decision:
        req = intent.request
        if req is None:
            return self._reject("bad_intent", "下单缺少 request", intent)
        if self._env is not Env.SIMULATE:
            return self._reject("real_disabled", "REAL 尚未开放：M4 模拟盘验收通过后另行计划（M4 确认点 4）", intent)
        if not self._oms.ready:
            return self._reject("not_ready", "OMS 尚未完成启动恢复", intent)
        if self._halt is not None:
            return self._reject("halt", f"已 halt（{self._halt.reason}），拒绝新单", intent)

        ctx_or_reject = self._collect(intent, req)
        if isinstance(ctx_or_reject, Decision):
            return ctx_or_reject
        v = check_order(ctx_or_reject, self._limits, self.stale_after_seconds)
        if not v.ok:
            assert v.rule is not None
            return self._reject(v.rule, v.reason, intent, halt=v.halt)

        try:
            with self._store.transaction():
                self._store.append_audit(
                    audit_for(
                        ts=self._clock.now(), action="place", source=intent.source.value, actor=intent.actor, allowed=True,
                        rule=None, reason="ok", client_order_id=req.client_order_id, symbol=req.symbol, side=req.side.value,
                        qty=req.qty, price=req.limit_price, details={"protective": intent.protective},
                    )  # fmt: skip
                )
        except StoreUnavailable as e:
            # 这条「通过」审计可能已写进 journal，回放后会出现；在拒绝记录里注明它作废
            return self._reject("store_unavailable", f"审计写不进数据库，不下新单（ADR-0025；上一条 allowed 审计记录作废）：{e}", intent)
        ticket = _issue_ticket("place", req.client_order_id)
        order = self._executor.place(ticket, req, intent.source)
        return Decision(True, None, "ok", order, ticket)

    def _collect(self, intent: Intent, req: OrderRequest) -> OrderContext | Decision:
        # 行情与时段：出错不直接拒，交给规则（理由里带原文）
        try:
            market_state = self._md.market_state(req.symbol)  # vendor § Get Market Status of Securities
        except BrokerError as e:
            market_state = f"UNAVAILABLE（{e}）"
        try:
            quotes = self._md.snapshot([req.symbol])  # vendor § Get Market Snapshot
            quote = quotes[0] if quotes else None
        except BrokerError as e:
            log.warning("取 %s 行情失败：%s", req.symbol, e)
            quote = None
        # 库里的事实：读不到就拒（ADR-0025）
        try:
            sent = self._oms.orders_sent_today()
            buy_leaves, sell_leaves = self._oms.open_leaves_both(req.symbol)  # 只扫一次订单表
            rule_id = self._store.active_rule_for(req.symbol)
        except StoreUnavailable as e:
            return self._reject("store_unavailable", f"读不到当日次数或挂单（ADR-0025），拒绝新单：{e}", intent)
        # 券商的事实：读不到就拒
        try:
            pos = self._oms.broker_position(req.symbol)
            pnl = self._oms.daily_pnl()
        except BrokerError as e:
            return self._reject("broker_unavailable", f"读不到券商持仓或当日盈亏，拒绝新单：{e}", intent)
        return OrderContext(
            req=req,
            source=intent.source,
            protective=intent.protective,
            market_state=market_state,
            quote=quote,
            now=self._clock.now(),
            position_qty=pos.qty if pos else 0,
            can_sell_qty=pos.can_sell_qty if pos else 0,
            open_buy_leaves=buy_leaves,
            open_sell_leaves=sell_leaves,
            orders_sent_today=sent,
            daily_pnl=pnl,
            active_rule=rule_id,
            rule_id=intent.rule_id,
        )

    # ------------------------------------------------------------------ 撤单（ADR-0016、0026）

    def _cancel(self, intent: Intent) -> Decision:
        cid = intent.client_order_id
        if cid is None:
            return self._reject("bad_intent", "撤单缺少 client_order_id", intent)
        try:
            o = self._oms.get_or_none(cid)
        except StoreUnavailable as e:
            return self._cancel_degraded(intent, cid, e)
        if o is None or o.status not in CANCEL_ACCEPTED:
            return self._reject("not_own_active_order", f"{cid} 不是本程序的活动订单", intent)
        reason = "ok" if o.status in CANCELLABLE else "订单尚未确认，确认后自动撤单（ADR-0026）"
        self._audit(
            action="cancel", source=intent.source.value, actor=intent.actor, allowed=True, rule=None, reason=reason,
            client_order_id=cid, symbol=o.symbol, side=o.side.value, qty=o.qty, price=o.price,
        )  # fmt: skip
        ticket = _issue_ticket("cancel", cid)
        order = self._executor.cancel(ticket, cid)
        return Decision(True, None, reason, order, ticket)

    def _broker_own_open(self) -> dict[str, str]:
        """库不可用时，从券商查本程序（remark 带 client_order_id）的未完成订单：cid → broker_order_id。"""
        return {o.client_order_id: o.broker_order_id for o in self._oms.broker.query_orders(open_only=True) if o.client_order_id}

    def _cancel_degraded(self, intent: Intent, cid: str, err: Exception) -> Decision:
        """ADR-0025：库不可用时撤单照常。归属用券商端的 remark 判断。"""
        own = self._broker_own_open()
        if cid not in own:
            return self._reject("not_own_active_order", f"库不可用，券商端也没有 {cid} 的未完成订单", intent)
        self._bus.publish(Alert("cancel_degraded", f"库不可用（{err}），按券商端订单撤 {cid}", cid))
        self._audit(action="cancel", source=intent.source.value, actor=intent.actor, allowed=True, rule=None,
                    reason="库不可用，按券商端订单撤单（ADR-0025）", client_order_id=cid)  # fmt: skip
        ticket = _issue_ticket("cancel", cid)
        order = self._executor.cancel(ticket, cid, broker_order_id=own[cid])
        return Decision(True, None, "库不可用，已直接撤券商端订单（ADR-0025）", order, ticket)

    # ------------------------------------------------------------------ halt / resume

    def halt(self, reason: str, actor: str, source: Source = Source.CLI) -> HaltReport:
        with self._lock:
            now = self._clock.now()
            if self._halt is None:
                self._halt = HaltState(reason, actor, now)
            h = self._halt
            self._persist_halt(True, h.reason, h.actor, h.since)  # 第二次 halt 不覆盖第一次的原因（只在上次没写进库时补写）
            self._audit(action="halt", source=source.value, actor=actor, allowed=True, rule=None, reason=reason)
            self._bus.publish(HaltChanged(True, reason, actor))
            log.warning("HALT by %s：%s", actor, reason)
            report = HaltReport()
            try:
                targets = [(o.client_order_id, None, o.status in CANCELLABLE)
                           for o in self._oms.open_orders()
                           if o.status in CANCELLABLE or (o.status in UNACKED and not o.cancel_pending)]  # fmt: skip
            except StoreUnavailable as e:
                # ADR-0025 / 审查 #3：库不可用时 halt 照常，挂单改从券商查（只撤 remark 带 client_order_id 的本程序订单）
                self._bus.publish(Alert("halt_degraded", f"库不可用（{e}），halt 改按券商端挂单撤单"))
                targets = [(cid, bid, True) for cid, bid in self._broker_own_open().items()]
            for cid, bid, now_cancel in targets:
                try:
                    self._executor.cancel(_issue_ticket("cancel", cid), cid, broker_order_id=bid)
                except MooappError as e:  # 审查 #1：单张单状态已变（成交、已确认）或券商拒绝都只记失败，继续撤下一张
                    report.failed[cid] = f"{type(e).__name__}：{e}"
                    continue
                (report.cancel_sent if now_cancel else report.deferred).append(cid)
            if report.failed:
                self._bus.publish(Alert("halt_cancel_failed", f"halt 时 {len(report.failed)} 张单撤单失败：{report.failed}"))
            if self._env is Env.REAL:
                try:
                    self._oms.broker.lock()  # ADR-0015：halt 时重新上锁
                except BrokerError as e:
                    self._bus.publish(Alert("lock_failed", f"halt 时重新上锁失败：{e}"))
            return report

    def resume(self, actor: str, source: Source = Source.CLI) -> None:
        """只能由 ops 在交互确认（TTY + 确认码，ops/humanlock.confirm_interactively）之后调用（ADR-0022）。"""
        with self._lock:
            now = self._clock.now()
            self._halt = None
            self._persist_halt(False, "resume", actor, now)
            self._audit(action="resume", source=source.value, actor=actor, allowed=True, rule=None, reason="resume")
            self._bus.publish(HaltChanged(False, "resume", actor))
            log.warning("RESUME by %s", actor)
