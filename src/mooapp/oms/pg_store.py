"""PostgresStore（ADR-0024；SPEC 5.4）。

- 一个实例只对应一个 schema（search_path 固定），不做跨环境查询
- 连接后断言 current_schema() 等于指定 schema，且当前角色对另一个 schema（存在时）没有 USAGE（Q20）
- 单连接；psycopg 连接本身是线程安全的，OMS 用自己的锁把读写串行化（3.4 节：单消费线程）
- 约束错误 → StoreError；连接类错误 → StoreUnavailable；都保留原异常链，不吞
- DSN 含密码，不写日志（SPEC 5.4 dsn_for）
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from mooapp.common import OrderKind, Side, Source
from mooapp.oms.state import Order, OrdStatus
from mooapp.oms.store import EventRecord, FillRecord, ReconRecord, StoreError, StoreUnavailable, check_event

T = TypeVar("T")

_ORDER_COLS = (
    "client_order_id, broker_order_id, source, symbol, side, kind, qty, limit_price, cl_ord_id, status, base_status,"
    " cum_qty, avg_fill_price, pending_qty, pending_price, pending_cl_ord_id, orig_cl_ord_ids, attempts, reason,"
    " raw_status, raw_dealt_qty, created_at, updated_at"
)
_EVENT_COLS = "client_order_id, event_type, origin, outcome, from_status, to_status, exec_id, qty, price, raw_status, reason, payload, ts"
_FILL_COLS = "fill_id, client_order_id, broker_order_id, symbol, side, qty, price, ts, synthetic, origin"


def _order_params(o: Order) -> tuple[Any, ...]:
    return (
        o.client_order_id, o.broker_order_id, o.source.value, o.symbol, o.side.value, o.kind.value, o.qty, o.price,
        o.cl_ord_id, o.status.value, o.base_status.value if o.base_status else None, o.cum_qty, o.avg_px,
        o.pending_qty, o.pending_price, o.pending_cl_ord_id, list(o.orig_cl_ord_ids), o.attempts, o.reason,
        o.raw_status, o.raw_dealt_qty, o.created_at, o.updated_at,
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

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ 执行与错误映射

    def _run(self, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
        try:
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
            with self._conn.transaction():
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
        q = f"INSERT INTO orders ({_ORDER_COLS}) VALUES ({', '.join(['%s'] * 23)})"
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
