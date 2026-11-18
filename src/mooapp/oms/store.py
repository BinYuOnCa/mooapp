"""Store 协议与内存实现（SPEC 5.4；ADR-0024：代码只依赖协议，测试用 MemoryStore，PostgresStore 见 pg_store.py）。

表：orders、order_events（只追加）、fills（按 fill_id 去重）、recon_events（只追加）、kv、journal_applied。
写操作可以在 transaction() 里成组提交；不在事务里时每个写操作自成一个事务。
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from mooapp.common import MooappError, Side
from mooapp.oms.state import Order

ORIGINS = frozenset({"push", "poll", "local"})
OUTCOMES = frozenset({"applied", "ignored", "rejected", "unmapped"})


class StoreError(MooappError):
    """违反约束（重复主键、CHECK）或其他存储错误。"""


class StoreUnavailable(StoreError):
    """数据库不可用（连不上、断线）。ADR-0025：此时撤单与 halt 照常，事件只写 journal。"""


@dataclass(frozen=True)
class EventRecord:
    """order_events 的一行。event_type 是 EventType 的值，或 SNAPSHOT（没有产生状态事件的券商快照）。"""

    client_order_id: str
    event_type: str
    origin: str  # push / poll / local
    outcome: str  # applied / ignored / rejected / unmapped
    from_status: str | None
    to_status: str | None
    exec_id: str | None
    qty: int | None
    price: Decimal | None
    raw_status: str | None
    reason: str | None
    payload: dict[str, Any]
    ts: datetime


@dataclass(frozen=True)
class FillRecord:
    fill_id: str  # 实盘 deal_id；模拟盘 "order_id:累计成交量"
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: Side
    qty: int
    price: Decimal
    ts: datetime
    synthetic: bool
    origin: str


@dataclass(frozen=True)
class ReconRecord:
    kind: str
    details: dict[str, Any]
    client_order_id: str | None
    ts: datetime


@dataclass(frozen=True)
class AuditRecord:
    """audit 表的一行（只追加）：每次闸门判定、halt、resume（M4）。"""

    ts: datetime
    action: str  # place / cancel / halt / resume
    source: str  # Source 的值
    actor: str
    client_order_id: str | None
    symbol: str | None
    side: str | None
    qty: int | None
    price: Decimal | None
    allowed: bool
    rule: str | None
    reason: str
    details: dict[str, Any]


@dataclass(frozen=True)
class ApprovalRecord:
    """approvals 的一行（M8）：Agent 的提议。status：pending → approved / rejected / expired。"""

    id: str
    kind: str  # order / rule
    payload: dict[str, Any]
    reason: str  # Agent 给的理由（外部文本，只当数据）
    caller: str
    status: str
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    result: dict[str, Any] | None


@dataclass(frozen=True)
class AgentCallRecord:
    """agent_calls 的一行（M8，只追加）：每次 MCP 工具调用的输入、输出、时间、调用方。"""

    tool: str
    args: dict[str, Any]
    result: dict[str, Any]
    caller: str
    ok: bool
    ts: datetime


@dataclass(frozen=True)
class CommandRecord:
    """commands 表的一行（SPEC 3.4）。status：pending → running → done / error。"""

    id: int
    created_at: datetime
    kind: str
    payload: dict[str, Any]
    actor: str
    status: str
    result: dict[str, Any] | None
    claimed_at: datetime | None
    finished_at: datetime | None


COMMAND_FINAL = frozenset({"done", "error"})
RULE_ACTIVE = frozenset({"PENDING", "ARMED", "TRIGGERED"})  # 与 strategy/machine.py ACTIVE_STATUSES 一致


@dataclass(frozen=True)
class RuleRecord:
    """rules 表的一行（M6b）。state 是状态机快照（JSON）。"""

    id: str
    symbol: str
    spec_yaml: str
    status: str
    entry_cid: str
    state: dict[str, Any]
    actor: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RuleEventRecord:
    rule_id: str
    ts: datetime
    kind: str
    message: str
    details: dict[str, Any]


def check_event(e: EventRecord) -> None:
    if e.origin not in ORIGINS:
        raise StoreError(f"order_events.origin 必须是 {sorted(ORIGINS)} 之一，收到 {e.origin!r}")
    if e.outcome not in OUTCOMES:
        raise StoreError(f"order_events.outcome 必须是 {sorted(OUTCOMES)} 之一，收到 {e.outcome!r}")


class Store(Protocol):
    # orders
    def insert_order(self, o: Order) -> None: ...
    def update_order(self, o: Order) -> None: ...
    def get_order(self, client_order_id: str) -> Order | None: ...
    def find_by_broker_id(self, broker_order_id: str) -> Order | None: ...
    def list_orders(self, open_only: bool = False) -> list[Order]: ...
    # order_events（只追加）
    def append_event(self, e: EventRecord) -> None: ...
    def events_for(self, client_order_id: str) -> list[EventRecord]: ...
    # fills
    def insert_fill_once(self, f: FillRecord) -> bool: ...
    def get_fill(self, fill_id: str) -> FillRecord | None: ...
    def fills_for(self, client_order_id: str) -> list[FillRecord]: ...
    def net_fill_qty_by_symbol(self) -> dict[str, int]: ...
    # recon_events（只追加）
    def append_recon(self, kind: str, details: dict[str, Any], client_order_id: str | None, ts: datetime) -> None: ...
    def recon_events(self) -> list[ReconRecord]: ...
    # kv
    def get_kv(self, key: str) -> str | None: ...
    # M8：approvals、agent_calls（只追加）
    def insert_approval(self, a: ApprovalRecord) -> None: ...
    def get_approval(self, approval_id: str) -> ApprovalRecord | None: ...
    def update_approval(self, approval_id: str, status: str, decided_at: datetime, result: dict[str, Any] | None) -> None: ...
    def list_approvals(self, status: str | None = None, since: datetime | None = None) -> list[ApprovalRecord]: ...
    def append_agent_call(self, tool: str, args: dict[str, Any], result: dict[str, Any], caller: str, ok: bool, ts: datetime) -> None: ...
    def agent_calls(self) -> list[AgentCallRecord]: ...
    def set_kv(self, key: str, value: str) -> None: ...
    # journal（ADR-0025）
    def mark_journal_applied(self, entry_id: str) -> None: ...
    def is_journal_applied(self, entry_id: str) -> bool: ...
    # M4：audit（只追加）、当日下单次数、commands、常驻进程单实例锁
    def append_audit(self, a: AuditRecord) -> None: ...
    def audit_records(self) -> list[AuditRecord]: ...
    def count_submitted_between(self, start: datetime, end: datetime) -> int: ...
    def enqueue_command(self, kind: str, payload: dict[str, Any], actor: str, ts: datetime) -> int: ...
    def claim_next_command(self, ts: datetime) -> CommandRecord | None: ...
    def finish_command(self, command_id: int, status: str, result: dict[str, Any], ts: datetime) -> None: ...
    def get_command(self, command_id: int) -> CommandRecord | None: ...
    def fail_running_commands(self, result: dict[str, Any], ts: datetime) -> int: ...  # M4 审查 #7：启动时清理遗留的 running
    def try_runner_lock(self) -> bool: ...
    def release_runner_lock(self) -> None: ...
    def active_rule_for(self, symbol: str) -> str | None: ...  # ADR-0020
    # M6b：规则
    def insert_rule(self, r: RuleRecord) -> None: ...
    def update_rule(self, rule_id: str, status: str, state: dict[str, Any], ts: datetime) -> None: ...
    def get_rule(self, rule_id: str) -> RuleRecord | None: ...
    def list_rules(self, active_only: bool = False) -> list[RuleRecord]: ...
    def link_rule_order(self, client_order_id: str, rule_id: str, role: str, ts: datetime) -> None: ...
    def rule_order(self, client_order_id: str) -> tuple[str, str] | None: ...
    def rule_orders(self, rule_id: str) -> list[tuple[str, str]]: ...
    def append_rule_event(self, e: RuleEventRecord) -> None: ...
    def rule_events(self, rule_id: str) -> list[RuleEventRecord]: ...
    # 事务
    def transaction(self) -> Any: ...


@dataclass
class _State:
    orders: dict[str, Order] = field(default_factory=dict)
    events: list[EventRecord] = field(default_factory=list)
    fills: dict[str, FillRecord] = field(default_factory=dict)
    recon: list[ReconRecord] = field(default_factory=list)
    kv: dict[str, str] = field(default_factory=dict)
    journal: set[str] = field(default_factory=set)
    audit: list[AuditRecord] = field(default_factory=list)
    commands: dict[int, CommandRecord] = field(default_factory=dict)
    rules: dict[str, RuleRecord] = field(default_factory=dict)
    rule_orders: dict[str, tuple[str, str, datetime]] = field(default_factory=dict)
    rule_events: list[RuleEventRecord] = field(default_factory=list)
    approvals: dict[str, ApprovalRecord] = field(default_factory=dict)
    agent_calls: list[AgentCallRecord] = field(default_factory=list)


class MemoryStore:
    """仅测试用。事务：最外层开始时整份复制，出错时还原。读写都复制对象，调用者改不到库里的数据。"""

    def __init__(self) -> None:
        self._s = _State()
        self._depth = 0
        self._backup: _State | None = None
        self._runner_locked = False  # 进程内单实例锁（PostgresStore 用 advisory lock）

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._depth == 0:
            self._backup = copy.deepcopy(self._s)
        self._depth += 1
        try:
            yield
        except BaseException:
            if self._depth == 1:
                assert self._backup is not None
                self._s = self._backup
            raise
        finally:
            self._depth -= 1
            if self._depth == 0:
                self._backup = None

    # orders

    def insert_order(self, o: Order) -> None:
        if o.client_order_id in self._s.orders:
            raise StoreError(f"orders 主键重复：{o.client_order_id}")
        if o.broker_order_id is not None and self.find_by_broker_id(o.broker_order_id) is not None:
            raise StoreError(f"orders.broker_order_id 重复：{o.broker_order_id}")
        self._s.orders[o.client_order_id] = copy.deepcopy(o)

    def update_order(self, o: Order) -> None:
        if o.client_order_id not in self._s.orders:
            raise StoreError(f"orders 没有 {o.client_order_id}")
        other = self.find_by_broker_id(o.broker_order_id) if o.broker_order_id else None
        if other is not None and other.client_order_id != o.client_order_id:
            raise StoreError(f"orders.broker_order_id 重复：{o.broker_order_id}")
        self._s.orders[o.client_order_id] = copy.deepcopy(o)

    def get_order(self, client_order_id: str) -> Order | None:
        o = self._s.orders.get(client_order_id)
        return copy.deepcopy(o) if o is not None else None

    def find_by_broker_id(self, broker_order_id: str) -> Order | None:
        for o in self._s.orders.values():
            if o.broker_order_id == broker_order_id:
                return copy.deepcopy(o)
        return None

    def list_orders(self, open_only: bool = False) -> list[Order]:
        return [copy.deepcopy(o) for o in self._s.orders.values() if not open_only or o.is_open]

    # order_events

    def append_event(self, e: EventRecord) -> None:
        check_event(e)
        if e.client_order_id not in self._s.orders:
            raise StoreError(f"order_events 外键：orders 没有 {e.client_order_id}")
        self._s.events.append(copy.deepcopy(e))

    def events_for(self, client_order_id: str) -> list[EventRecord]:
        return [copy.deepcopy(e) for e in self._s.events if e.client_order_id == client_order_id]

    # fills

    def insert_fill_once(self, f: FillRecord) -> bool:
        if f.fill_id in self._s.fills:
            return False
        if f.origin not in ORIGINS:
            raise StoreError(f"fills.origin 非法：{f.origin!r}")
        self._s.fills[f.fill_id] = f
        return True

    def get_fill(self, fill_id: str) -> FillRecord | None:
        return self._s.fills.get(fill_id)

    def fills_for(self, client_order_id: str) -> list[FillRecord]:
        return [f for f in self._s.fills.values() if f.client_order_id == client_order_id]

    def net_fill_qty_by_symbol(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self._s.fills.values():
            out[f.symbol] = out.get(f.symbol, 0) + (f.qty if f.side is Side.BUY else -f.qty)
        return out

    # recon_events

    def append_recon(self, kind: str, details: dict[str, Any], client_order_id: str | None, ts: datetime) -> None:
        self._s.recon.append(ReconRecord(kind, copy.deepcopy(details), client_order_id, ts))

    def recon_events(self) -> list[ReconRecord]:
        return list(self._s.recon)

    # kv

    # M8：approvals、agent_calls

    def insert_approval(self, a: ApprovalRecord) -> None:
        if a.id in self._s.approvals:
            raise StoreError(f"approvals 已有 {a.id}")
        self._s.approvals[a.id] = copy.deepcopy(a)

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        a = self._s.approvals.get(approval_id)
        return copy.deepcopy(a) if a is not None else None

    def update_approval(self, approval_id: str, status: str, decided_at: datetime, result: dict[str, Any] | None) -> None:
        a = self._s.approvals.get(approval_id)
        if a is None:
            raise StoreError(f"approvals 没有 {approval_id}")
        self._s.approvals[approval_id] = replace(a, status=status, decided_at=decided_at, result=copy.deepcopy(result))

    def list_approvals(self, status: str | None = None, since: datetime | None = None) -> list[ApprovalRecord]:
        out = [a for a in self._s.approvals.values() if (status is None or a.status == status) and (since is None or a.created_at >= since)]
        return [copy.deepcopy(a) for a in sorted(out, key=lambda a: (a.created_at, a.id))]

    def append_agent_call(self, tool: str, args: dict[str, Any], result: dict[str, Any], caller: str, ok: bool, ts: datetime) -> None:
        self._s.agent_calls.append(AgentCallRecord(tool, copy.deepcopy(args), copy.deepcopy(result), caller, ok, ts))

    def agent_calls(self) -> list[AgentCallRecord]:
        return copy.deepcopy(self._s.agent_calls)

    def get_kv(self, key: str) -> str | None:
        return self._s.kv.get(key)

    def set_kv(self, key: str, value: str) -> None:
        self._s.kv[key] = value

    # journal

    def mark_journal_applied(self, entry_id: str) -> None:
        self._s.journal.add(entry_id)

    def is_journal_applied(self, entry_id: str) -> bool:
        return entry_id in self._s.journal

    # M4：audit

    def append_audit(self, a: AuditRecord) -> None:
        self._s.audit.append(copy.deepcopy(a))

    def audit_records(self) -> list[AuditRecord]:
        return list(self._s.audit)

    # M4：当日下单次数（ADR-0018：已发往券商的新单；RESUBMIT 不重复计；只算生效的 SUBMIT）

    def count_submitted_between(self, start: datetime, end: datetime) -> int:
        return len(
            {e.client_order_id for e in self._s.events if e.event_type == "SUBMIT" and e.outcome == "applied" and start <= e.ts < end}
        )

    # M4：commands（SPEC 3.4）

    def enqueue_command(self, kind: str, payload: dict[str, Any], actor: str, ts: datetime) -> int:
        cid = max(self._s.commands, default=0) + 1
        self._s.commands[cid] = CommandRecord(cid, ts, kind, copy.deepcopy(payload), actor, "pending", None, None, None)
        return cid

    def claim_next_command(self, ts: datetime) -> CommandRecord | None:
        pending = sorted(i for i, c in self._s.commands.items() if c.status == "pending")
        if not pending:
            return None
        c = replace(self._s.commands[pending[0]], status="running", claimed_at=ts)
        self._s.commands[c.id] = c
        return copy.deepcopy(c)

    def finish_command(self, command_id: int, status: str, result: dict[str, Any], ts: datetime) -> None:
        if status not in COMMAND_FINAL:
            raise StoreError(f"commands.status 只能结束为 {sorted(COMMAND_FINAL)}，收到 {status!r}")
        c = self._s.commands.get(command_id)
        if c is None:
            raise StoreError(f"commands 没有 {command_id}")
        self._s.commands[command_id] = replace(c, status=status, result=copy.deepcopy(result), finished_at=ts)

    def get_command(self, command_id: int) -> CommandRecord | None:
        c = self._s.commands.get(command_id)
        return copy.deepcopy(c) if c is not None else None

    def fail_running_commands(self, result: dict[str, Any], ts: datetime) -> int:
        ids = [i for i, c in self._s.commands.items() if c.status == "running"]
        for i in ids:
            self._s.commands[i] = replace(self._s.commands[i], status="error", result=copy.deepcopy(result), finished_at=ts)
        return len(ids)

    # M4：常驻进程单实例锁

    def try_runner_lock(self) -> bool:
        if self._runner_locked:
            return False
        self._runner_locked = True
        return True

    def release_runner_lock(self) -> None:
        self._runner_locked = False

    # ------------------------------------------------------------------ M6b：规则

    def active_rule_for(self, symbol: str) -> str | None:
        """ADR-0020：该标的的活动规则 id。"""
        for r in self._s.rules.values():
            if r.symbol == symbol and r.status in RULE_ACTIVE:
                return r.id
        return None

    def insert_rule(self, r: RuleRecord) -> None:
        if r.id in self._s.rules:
            raise StoreError(f"rules 主键重复：{r.id}")
        if r.status in RULE_ACTIVE and self.active_rule_for(r.symbol) is not None:
            raise StoreError(f"{r.symbol} 已有活动规则（ADR-0020）")
        self._s.rules[r.id] = copy.deepcopy(r)

    def update_rule(self, rule_id: str, status: str, state: dict[str, Any], ts: datetime) -> None:
        r = self._s.rules.get(rule_id)
        if r is None:
            raise StoreError(f"rules 没有 {rule_id}")
        self._s.rules[rule_id] = replace(r, status=status, state=copy.deepcopy(state), updated_at=ts)

    def get_rule(self, rule_id: str) -> RuleRecord | None:
        r = self._s.rules.get(rule_id)
        return copy.deepcopy(r) if r is not None else None

    def list_rules(self, active_only: bool = False) -> list[RuleRecord]:
        rs = sorted(self._s.rules.values(), key=lambda r: (r.created_at, r.id))
        return [copy.deepcopy(r) for r in rs if not active_only or r.status in RULE_ACTIVE]

    def link_rule_order(self, client_order_id: str, rule_id: str, role: str, ts: datetime) -> None:
        if rule_id not in self._s.rules:
            raise StoreError(f"rule_orders 外键：rules 没有 {rule_id}")
        if role not in ("entry", "sell"):
            raise StoreError(f"rule_orders.role 非法：{role!r}")
        self._s.rule_orders[client_order_id] = (rule_id, role, ts)

    def rule_order(self, client_order_id: str) -> tuple[str, str] | None:
        v = self._s.rule_orders.get(client_order_id)
        return (v[0], v[1]) if v is not None else None

    def rule_orders(self, rule_id: str) -> list[tuple[str, str]]:
        items = [(cid, v) for cid, v in self._s.rule_orders.items() if v[0] == rule_id]
        return [(cid, v[1]) for cid, v in sorted(items, key=lambda x: x[1][2])]

    def append_rule_event(self, e: RuleEventRecord) -> None:
        if e.rule_id not in self._s.rules:
            raise StoreError(f"rule_events 外键：rules 没有 {e.rule_id}")
        self._s.rule_events.append(copy.deepcopy(e))

    def rule_events(self, rule_id: str) -> list[RuleEventRecord]:
        return [copy.deepcopy(e) for e in self._s.rule_events if e.rule_id == rule_id]
