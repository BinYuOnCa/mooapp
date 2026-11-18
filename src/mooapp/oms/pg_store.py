"""PostgresStore（ADR-0024；SPEC 5.4）。

- 一个实例只对应一个 schema（search_path 固定），不做跨环境查询
- 连接后断言 current_schema() 等于指定 schema，且当前角色对另一个 schema（存在时）没有 USAGE（Q20）
- 单连接；一把可重入锁保证一个事务从头到尾只属于一个线程（常驻进程里多个线程共用本实例）
- 约束错误 → StoreError；连接类错误 → StoreUnavailable；都保留原异常链，不吞
- DSN 含密码，不写日志（SPEC 5.4 dsn_for）
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from mooapp.common import OrderKind, Side, Source
from mooapp.oms.state import Order, OrdStatus
from mooapp.oms.store import (
    COMMAND_FINAL,
    AuditRecord,
    AgentCallRecord,
    ApprovalRecord,
    CommandRecord,
    EventRecord,
    FillRecord,
    ReconRecord,
    RuleEventRecord,
    RuleRecord,
    StoreError,
    StoreUnavailable,
    check_event,
)

T = TypeVar("T")
_APPROVAL_COLS = "id, kind, payload, reason, caller, status, created_at, expires_at, decided_at, result"

_ORDER_COLS = (
    "client_order_id, broker_order_id, source, symbol, side, kind, qty, limit_price, cl_ord_id, status, base_status,"
    " cum_qty, avg_fill_price, pending_qty, pending_price, pending_cl_ord_id, orig_cl_ord_ids, attempts, reason,"
    " raw_status, raw_dealt_qty, cancel_pending, created_at, updated_at"
)
_N_ORDER_COLS = 24
_AUDIT_COLS = "ts, action, source, actor, client_order_id, symbol, side, qty, price, allowed, rule, reason, details"
_COMMAND_COLS = "id, created_at, kind, payload, actor, status, result, claimed_at, finished_at"
_RULE_COLS = "id, symbol, spec_yaml, status, entry_cid, state, actor, created_at, updated_at"
_RULE_ACTIVE_SQL = "status IN ('PENDING', 'ARMED', 'TRIGGERED')"
_EVENT_COLS = "client_order_id, event_type, origin, outcome, from_status, to_status, exec_id, qty, price, raw_status, reason, payload, ts"
_FILL_COLS = "fill_id, client_order_id, broker_order_id, symbol, side, qty, price, ts, synthetic, origin"


def _order_params(o: Order) -> tuple[Any, ...]:
    return (
        o.client_order_id, o.broker_order_id, o.source.value, o.symbol, o.side.value, o.kind.value, o.qty, o.price,
        o.cl_ord_id, o.status.value, o.base_status.value if o.base_status else None, o.cum_qty, o.avg_px,
        o.pending_qty, o.pending_price, o.pending_cl_ord_id, list(o.orig_cl_ord_ids), o.attempts, o.reason,
        o.raw_status, o.raw_dealt_qty, o.cancel_pending, o.created_at, o.updated_at,
    )  # fmt: skip


def _order_from(r: dict[str, Any]) -> Order:
    return Order(
        client_order_id=r["client_order_id"],
        broker_order_id=r["broker_order_id"],
        source=Source(r["source"]),
        symbol=r["symbol"],
        side=Side(r["side"]),
        kind=OrderKind(r["kind"]),
        qty=r["qty"],
        price=r["limit_price"],
        cl_ord_id=r["cl_ord_id"],
        status=OrdStatus(r["status"]),
        base_status=OrdStatus(r["base_status"]) if r["base_status"] else None,
        cum_qty=r["cum_qty"],
        avg_px=r["avg_fill_price"],
        pending_qty=r["pending_qty"],
        pending_price=r["pending_price"],
        pending_cl_ord_id=r["pending_cl_ord_id"],
        orig_cl_ord_ids=list(r["orig_cl_ord_ids"]),
        attempts=r["attempts"],
        reason=r["reason"],
        raw_status=r["raw_status"],
        raw_dealt_qty=r["raw_dealt_qty"],
        cancel_pending=r["cancel_pending"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def _fill_from(r: dict[str, Any]) -> FillRecord:
    return FillRecord(
        r["fill_id"], r["client_order_id"], r["broker_order_id"], r["symbol"], Side(r["side"]), r["qty"], r["price"],
        r["ts"], r["synthetic"], r["origin"],
    )  # fmt: skip


class PostgresStore:
    def __init__(self, dsn: str, schema: str, other_schema: str | None):
        self.schema = schema
        try:
            self._conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        except psycopg.OperationalError as e:
            raise StoreUnavailable(f"连不上 PostgreSQL：{e.__class__.__name__}") from e
        try:
            self._conn.execute("SET TIME ZONE 'UTC'")
            self._conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            current = self._conn.execute("SELECT current_schema() AS s").fetchone()["s"]
            if current != schema:
                raise StoreError(f"schema {schema!r} 不存在或无权使用（current_schema() = {current!r}）")
            if other_schema is not None:
                row = self._conn.execute(
                    "SELECT to_regnamespace(%s) IS NOT NULL AS present,"
                    " CASE WHEN to_regnamespace(%s) IS NULL THEN false ELSE has_schema_privilege(%s, 'USAGE') END AS usage",
                    (other_schema, other_schema, other_schema),
                ).fetchone()
                if row["usage"]:
                    raise StoreError(f"当前角色对 schema {other_schema!r} 有 USAGE 权限，环境隔离失效（Q20）")
        except BaseException:
            self._conn.close()
            raise
        self._depth = 0
        # 常驻进程里命令线程、事件线程、调用方会共用这个连接：一把可重入锁让一个事务从头到尾只属于一个线程
        self._mutex = threading.RLock()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ 执行与错误映射

    def _run(self, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
        try:
            with self._mutex:
                if self._depth:
                    return fn(self._conn)
                with self._conn.transaction():
                    return fn(self._conn)
        except psycopg.OperationalError as e:
            raise StoreUnavailable(f"数据库不可用：{e}") from e
        except psycopg.Error as e:
            raise StoreError(f"{e.__class__.__name__}：{e}") from e

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            with self._mutex, self._conn.transaction():
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
        except psycopg.OperationalError as e:
            raise StoreUnavailable(f"数据库不可用：{e}") from e
        except psycopg.Error as e:
            raise StoreError(f"{e.__class__.__name__}：{e}") from e

    # ------------------------------------------------------------------ orders

    def insert_order(self, o: Order) -> None:
        q = f"INSERT INTO orders ({_ORDER_COLS}) VALUES ({', '.join(['%s'] * _N_ORDER_COLS)})"
        self._run(lambda c: c.execute(q, _order_params(o)))

    def update_order(self, o: Order) -> None:
        cols = [c.strip() for c in _ORDER_COLS.split(",")][1:]
        q = f"UPDATE orders SET {', '.join(f'{c} = %s' for c in cols)} WHERE client_order_id = %s"
        params = _order_params(o)

        def go(c: psycopg.Connection[Any]) -> None:
            cur = c.execute(q, (*params[1:], params[0]))
            if cur.rowcount != 1:
                raise StoreError(f"orders 没有 {o.client_order_id}")

        self._run(go)

    def get_order(self, client_order_id: str) -> Order | None:
        r = self._run(lambda c: c.execute(f"SELECT {_ORDER_COLS} FROM orders WHERE client_order_id = %s", (client_order_id,)).fetchone())
        return _order_from(r) if r else None

    def find_by_broker_id(self, broker_order_id: str) -> Order | None:
        r = self._run(lambda c: c.execute(f"SELECT {_ORDER_COLS} FROM orders WHERE broker_order_id = %s", (broker_order_id,)).fetchone())
        return _order_from(r) if r else None

    def list_orders(self, open_only: bool = False) -> list[Order]:
        where = "WHERE status NOT IN ('FILLED', 'CANCELED', 'EXPIRED', 'REJECTED')" if open_only else ""
        rows = self._run(lambda c: c.execute(f"SELECT {_ORDER_COLS} FROM orders {where} ORDER BY created_at, client_order_id").fetchall())
        return [_order_from(r) for r in rows]

    # ------------------------------------------------------------------ order_events

    def append_event(self, e: EventRecord) -> None:
        check_event(e)
        q = f"INSERT INTO order_events ({_EVENT_COLS}) VALUES ({', '.join(['%s'] * 13)})"
        p = (e.client_order_id, e.event_type, e.origin, e.outcome, e.from_status, e.to_status, e.exec_id, e.qty, e.price,
             e.raw_status, e.reason, Jsonb(e.payload), e.ts)  # fmt: skip
        self._run(lambda c: c.execute(q, p))

    def events_for(self, client_order_id: str) -> list[EventRecord]:
        rows = self._run(
            lambda c: c.execute(f"SELECT {_EVENT_COLS} FROM order_events WHERE client_order_id = %s ORDER BY id", (client_order_id,)).fetchall()
        )
        return [EventRecord(**r) for r in rows]

    # ------------------------------------------------------------------ fills

    def insert_fill_once(self, f: FillRecord) -> bool:
        q = f"INSERT INTO fills ({_FILL_COLS}) VALUES ({', '.join(['%s'] * 10)}) ON CONFLICT (fill_id) DO NOTHING"
        p = (f.fill_id, f.client_order_id, f.broker_order_id, f.symbol, f.side.value, f.qty, f.price, f.ts, f.synthetic, f.origin)
        return self._run(lambda c: c.execute(q, p).rowcount == 1)

    def get_fill(self, fill_id: str) -> FillRecord | None:
        r = self._run(lambda c: c.execute(f"SELECT {_FILL_COLS} FROM fills WHERE fill_id = %s", (fill_id,)).fetchone())
        return _fill_from(r) if r else None

    def fills_for(self, client_order_id: str) -> list[FillRecord]:
        rows = self._run(
            lambda c: c.execute(f"SELECT {_FILL_COLS} FROM fills WHERE client_order_id = %s ORDER BY ts, fill_id", (client_order_id,)).fetchall()
        )
        return [_fill_from(r) for r in rows]

    def net_fill_qty_by_symbol(self) -> dict[str, int]:
        rows = self._run(
            lambda c: c.execute(
                "SELECT symbol, sum(CASE WHEN side = 'BUY' THEN qty ELSE -qty END)::bigint AS net FROM fills GROUP BY symbol"
            ).fetchall()
        )
        return {r["symbol"]: int(r["net"]) for r in rows}

    # ------------------------------------------------------------------ recon_events

    def append_recon(self, kind: str, details: dict[str, Any], client_order_id: str | None, ts: Any) -> None:
        self._run(
            lambda c: c.execute(
                "INSERT INTO recon_events (kind, client_order_id, details, ts) VALUES (%s, %s, %s, %s)",
                (kind, client_order_id, Jsonb(details), ts),
            )
        )

    def recon_events(self) -> list[ReconRecord]:
        rows = self._run(lambda c: c.execute("SELECT kind, details, client_order_id, ts FROM recon_events ORDER BY id").fetchall())
        return [ReconRecord(r["kind"], r["details"], r["client_order_id"], r["ts"]) for r in rows]

    # ------------------------------------------------------------------ M8：approvals、agent_calls

    def insert_approval(self, a: ApprovalRecord) -> None:
        self._run(
            lambda c: c.execute(
                f"INSERT INTO approvals ({_APPROVAL_COLS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (a.id, a.kind, Jsonb(a.payload), a.reason, a.caller, a.status, a.created_at, a.expires_at, a.decided_at,
                 None if a.result is None else Jsonb(a.result)),
            )
        )  # fmt: skip

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        r = self._run(lambda c: c.execute(f"SELECT {_APPROVAL_COLS} FROM approvals WHERE id = %s", (approval_id,)).fetchone())
        return ApprovalRecord(**r) if r else None

    def update_approval(self, approval_id: str, status: str, decided_at: Any, result: dict[str, Any] | None) -> None:
        def go(c: psycopg.Connection[Any]) -> None:
            cur = c.execute(
                "UPDATE approvals SET status = %s, decided_at = %s, result = %s WHERE id = %s",
                (status, decided_at, None if result is None else Jsonb(result), approval_id),
            )
            if cur.rowcount != 1:
                raise StoreError(f"approvals 没有 {approval_id}")

        self._run(go)

    def list_approvals(self, status: str | None = None, since: Any = None) -> list[ApprovalRecord]:
        q = (f"SELECT {_APPROVAL_COLS} FROM approvals WHERE (%s::text IS NULL OR status = %s) "
             "AND (%s::timestamptz IS NULL OR created_at >= %s) ORDER BY created_at, id")  # fmt: skip
        rows = self._run(lambda c: c.execute(q, (status, status, since, since)).fetchall())
        return [ApprovalRecord(**r) for r in rows]

    def append_agent_call(self, tool: str, args: dict[str, Any], result: dict[str, Any], caller: str, ok: bool, ts: Any) -> None:
        self._run(
            lambda c: c.execute(
                "INSERT INTO agent_calls (tool, args, result, caller, ok, ts) VALUES (%s, %s, %s, %s, %s, %s)",
                (tool, Jsonb(args), Jsonb(result), caller, ok, ts),
            )
        )

    def agent_calls(self) -> list[AgentCallRecord]:
        rows = self._run(lambda c: c.execute("SELECT tool, args, result, caller, ok, ts FROM agent_calls ORDER BY id").fetchall())
        return [AgentCallRecord(**r) for r in rows]

    # ------------------------------------------------------------------ kv

    def get_kv(self, key: str) -> str | None:
        r = self._run(lambda c: c.execute("SELECT value FROM kv WHERE key = %s", (key,)).fetchone())
        return r["value"] if r else None

    def set_kv(self, key: str, value: str) -> None:
        self._run(
            lambda c: c.execute(
                "INSERT INTO kv (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (key, value),
            )
        )

    # ------------------------------------------------------------------ journal

    def mark_journal_applied(self, entry_id: str) -> None:
        self._run(lambda c: c.execute("INSERT INTO journal_applied (entry_id) VALUES (%s)", (entry_id,)))

    def is_journal_applied(self, entry_id: str) -> bool:
        return self._run(lambda c: c.execute("SELECT 1 FROM journal_applied WHERE entry_id = %s", (entry_id,)).fetchone()) is not None

    # ------------------------------------------------------------------ M4：audit（只追加）

    def append_audit(self, a: AuditRecord) -> None:
        q = f"INSERT INTO audit ({_AUDIT_COLS}) VALUES ({', '.join(['%s'] * 13)})"
        p = (a.ts, a.action, a.source, a.actor, a.client_order_id, a.symbol, a.side, a.qty, a.price, a.allowed, a.rule, a.reason,
             Jsonb(a.details))  # fmt: skip
        self._run(lambda c: c.execute(q, p))

    def audit_records(self) -> list[AuditRecord]:
        rows = self._run(lambda c: c.execute(f"SELECT {_AUDIT_COLS} FROM audit ORDER BY id").fetchall())
        return [AuditRecord(**r) for r in rows]

    # ------------------------------------------------------------------ M4：当日下单次数（ADR-0018）

    def count_submitted_between(self, start: Any, end: Any) -> int:
        r = self._run(
            lambda c: c.execute(
                "SELECT count(DISTINCT client_order_id) AS n FROM order_events"
                " WHERE event_type = 'SUBMIT' AND outcome = 'applied' AND ts >= %s AND ts < %s",
                (start, end),
            ).fetchone()
        )
        return int(r["n"])

    # ------------------------------------------------------------------ M4：commands（SPEC 3.4）

    def enqueue_command(self, kind: str, payload: dict[str, Any], actor: str, ts: Any) -> int:
        r = self._run(
            lambda c: c.execute(
                "INSERT INTO commands (created_at, kind, payload, actor) VALUES (%s, %s, %s, %s) RETURNING id",
                (ts, kind, Jsonb(payload), actor),
            ).fetchone()
        )
        return int(r["id"])

    def claim_next_command(self, ts: Any) -> CommandRecord | None:
        # FOR UPDATE SKIP LOCKED：多个连接同时取命令时互不重复
        r = self._run(
            lambda c: c.execute(
                f"UPDATE commands SET status = 'running', claimed_at = %s WHERE id = ("
                " SELECT id FROM commands WHERE status = 'pending' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
                f") RETURNING {_COMMAND_COLS}",
                (ts,),
            ).fetchone()
        )
        return CommandRecord(**r) if r else None

    def finish_command(self, command_id: int, status: str, result: dict[str, Any], ts: Any) -> None:
        if status not in COMMAND_FINAL:
            raise StoreError(f"commands.status 只能结束为 {sorted(COMMAND_FINAL)}，收到 {status!r}")

        def go(c: psycopg.Connection[Any]) -> None:
            cur = c.execute(
                "UPDATE commands SET status = %s, result = %s, finished_at = %s WHERE id = %s",
                (status, Jsonb(result), ts, command_id),
            )
            if cur.rowcount != 1:
                raise StoreError(f"commands 没有 {command_id}")

        self._run(go)

    def get_command(self, command_id: int) -> CommandRecord | None:
        r = self._run(lambda c: c.execute(f"SELECT {_COMMAND_COLS} FROM commands WHERE id = %s", (command_id,)).fetchone())
        return CommandRecord(**r) if r else None

    def fail_running_commands(self, result: dict[str, Any], ts: Any) -> int:
        """M4 审查 #7：常驻进程启动时（已持有单实例锁），把上一个进程遗留的 running 命令标为 error。"""

        def go(c: psycopg.Connection[Any]) -> int:
            cur = c.execute(
                "UPDATE commands SET status = 'error', result = %s, finished_at = %s WHERE status = 'running'",
                (Jsonb(result), ts),
            )
            return int(cur.rowcount)

        return self._run(go)

    # ------------------------------------------------------------------ M4：常驻进程单实例锁（SPEC 3.4：advisory lock，按 schema 区分）

    def _lock_key(self) -> str:
        return f"mooapp_runner:{self.schema}"

    def try_runner_lock(self) -> bool:
        r = self._run(lambda c: c.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (self._lock_key(),)).fetchone())
        return bool(r["ok"])

    def release_runner_lock(self) -> None:
        r = self._run(lambda c: c.execute("SELECT pg_advisory_unlock(hashtext(%s)) AS ok", (self._lock_key(),)).fetchone())
        if not r["ok"]:
            raise StoreError("释放单实例锁失败：当前连接没有持有它")

    # ------------------------------------------------------------------ M6b：规则

    def active_rule_for(self, symbol: str) -> str | None:
        """ADR-0020：该标的的活动规则 id（rules_one_active_per_symbol 唯一索引兜底）。"""
        r = self._run(lambda c: c.execute(f"SELECT id FROM rules WHERE symbol = %s AND {_RULE_ACTIVE_SQL}", (symbol,)).fetchone())
        return r["id"] if r else None

    def insert_rule(self, r: RuleRecord) -> None:
        q = f"INSERT INTO rules ({_RULE_COLS}) VALUES ({', '.join(['%s'] * 9)})"
        p = (r.id, r.symbol, r.spec_yaml, r.status, r.entry_cid, Jsonb(r.state), r.actor, r.created_at, r.updated_at)
        self._run(lambda c: c.execute(q, p))

    def update_rule(self, rule_id: str, status: str, state: dict[str, Any], ts: Any) -> None:
        def go(c: psycopg.Connection[Any]) -> None:
            cur = c.execute("UPDATE rules SET status = %s, state = %s, updated_at = %s WHERE id = %s", (status, Jsonb(state), ts, rule_id))
            if cur.rowcount != 1:
                raise StoreError(f"rules 没有 {rule_id}")

        self._run(go)

    def get_rule(self, rule_id: str) -> RuleRecord | None:
        r = self._run(lambda c: c.execute(f"SELECT {_RULE_COLS} FROM rules WHERE id = %s", (rule_id,)).fetchone())
        return RuleRecord(**r) if r else None

    def list_rules(self, active_only: bool = False) -> list[RuleRecord]:
        where = f"WHERE {_RULE_ACTIVE_SQL}" if active_only else ""
        rows = self._run(lambda c: c.execute(f"SELECT {_RULE_COLS} FROM rules {where} ORDER BY created_at, id").fetchall())
        return [RuleRecord(**r) for r in rows]

    def link_rule_order(self, client_order_id: str, rule_id: str, role: str, ts: Any) -> None:
        self._run(
            lambda c: c.execute(
                "INSERT INTO rule_orders (client_order_id, rule_id, role, created_at) VALUES (%s, %s, %s, %s)",
                (client_order_id, rule_id, role, ts),
            )
        )

    def rule_order(self, client_order_id: str) -> tuple[str, str] | None:
        r = self._run(lambda c: c.execute("SELECT rule_id, role FROM rule_orders WHERE client_order_id = %s", (client_order_id,)).fetchone())
        return (r["rule_id"], r["role"]) if r else None

    def rule_orders(self, rule_id: str) -> list[tuple[str, str]]:
        rows = self._run(
            lambda c: c.execute(
                "SELECT client_order_id, role FROM rule_orders WHERE rule_id = %s ORDER BY created_at, client_order_id", (rule_id,)
            ).fetchall()
        )
        return [(r["client_order_id"], r["role"]) for r in rows]

    def append_rule_event(self, e: RuleEventRecord) -> None:
        self._run(
            lambda c: c.execute(
                "INSERT INTO rule_events (rule_id, ts, kind, message, details) VALUES (%s, %s, %s, %s, %s)",
                (e.rule_id, e.ts, e.kind, e.message, Jsonb(e.details)),
            )
        )

    def rule_events(self, rule_id: str) -> list[RuleEventRecord]:
        rows = self._run(
            lambda c: c.execute("SELECT rule_id, ts, kind, message, details FROM rule_events WHERE rule_id = %s ORDER BY id", (rule_id,)).fetchall()
        )
        return [RuleEventRecord(**r) for r in rows]
