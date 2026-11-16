"""RiskGate 骨架（M3 计划第 5 节；SPEC 5.5 的子集）。下单、撤单的唯一入口。

M3 只检查（任一不通过即拒）：
- 下单：REAL 一律拒绝（REAL 在 M4）→ OMS 未完成启动恢复 → halt（只在内存，M4 持久化，ADR-0022）→ 白名单
- 撤单：订单属于本程序且处于活动状态（ADR-0016：不受 halt 限制）
通过后签发 GateTicket 交给 Executor。
不读、不改风控限额：单笔数量与金额、限价偏离、持仓上限、当日次数与亏损等规则、审计日志都在 M4 实现。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Literal

from mooapp.broker.port import OrderRequest
from mooapp.common import Env, Source
from mooapp.oms.executor import CANCELLABLE, Executor
from mooapp.oms.oms import OMS
from mooapp.oms.state import Order
from mooapp.oms.ticket import GateTicket, _issue_ticket

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Intent:
    source: Source
    action: Literal["place", "cancel"]  # 不支持 modify（Q4）
    request: OrderRequest | None = None  # place 时必填
    client_order_id: str | None = None  # cancel 时必填
    actor: str = ""
    protective: bool = False  # ADR-0021；M4 实现


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule: str | None  # 命中的规则名
    reason: str
    order: Order | None = None
    ticket: GateTicket | None = None


class RiskGate:
    def __init__(self, env: Env, whitelist: frozenset[str], oms: OMS, executor: Executor):
        self._env = env
        self._whitelist = frozenset(whitelist)
        self._oms = oms
        self._executor = executor
        self._halted = False
        self._lock = threading.Lock()  # 检查与发单在同一把锁内（SPEC 3.4）

    @property
    def halted(self) -> bool:
        return self._halted

    def halt(self, reason: str, actor: str) -> None:
        """M3：只拒绝新单。撤销全部挂单、暂停规则引擎、上锁、持久化在 M4（ADR-0022）。"""
        log.warning("HALT by %s：%s", actor, reason)
        self._halted = True

    def resume(self, actor: str) -> None:
        log.warning("RESUME by %s", actor)
        self._halted = False

    def submit(self, intent: Intent) -> Decision:
        with self._lock:
            if intent.action == "place":
                return self._place(intent)
            if intent.action == "cancel":
                return self._cancel(intent)
            return self._reject("bad_intent", f"不支持的动作 {intent.action!r}", intent)

    def _reject(self, rule: str, reason: str, intent: Intent) -> Decision:
        log.info("闸门拒绝 %s（%s，%s）：%s", intent.action, intent.source.value, rule, reason)
        return Decision(False, rule, reason)

    def _place(self, intent: Intent) -> Decision:
        req = intent.request
        if req is None:
            return self._reject("bad_intent", "下单缺少 request", intent)
        if self._env is not Env.SIMULATE:
            return self._reject("real_not_in_m3", "REAL 下单在 M4 经人锁与限额锁实现", intent)
        if not self._oms.ready:
            return self._reject("not_ready", "OMS 尚未完成启动恢复", intent)
        if self._halted:
            return self._reject("halt", "已 halt，拒绝新单", intent)
        if req.symbol not in self._whitelist:
            return self._reject("whitelist", f"{req.symbol} 不在白名单", intent)
        ticket = _issue_ticket("place", req.client_order_id)
        order = self._executor.place(ticket, req, intent.source)
        return Decision(True, None, "ok", order, ticket)

    def _cancel(self, intent: Intent) -> Decision:
        cid = intent.client_order_id
        if cid is None:
            return self._reject("bad_intent", "撤单缺少 client_order_id", intent)
        o = self._oms.get_or_none(cid)
        if o is None or o.status not in CANCELLABLE:
            return self._reject("not_own_active_order", f"{cid} 不是本程序的活动订单", intent)
        ticket = _issue_ticket("cancel", cid)
        order = self._executor.cancel(ticket, cid)
        return Decision(True, None, "ok", order, ticket)
