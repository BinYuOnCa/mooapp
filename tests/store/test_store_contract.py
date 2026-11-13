"""Store 契约（SPEC 第 10 节 tests/store/；M3 计划第 3 节）。同一组用例跑两种实现：

- memory（默认）：MemoryStore
- pg（-m pg）：PostgresStore，连本机测试库 mooapp_test（经 unix socket，角色 mooapp_sim，密码在 config/db.local.toml），
  每个用例建临时 schema、跑全部迁移、结束删除。Claude 在 sandbox 里可以运行。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from mooapp.common import OrderKind, Side, Source
from mooapp.oms.state import Order, OrdStatus
from mooapp.oms.store import EventRecord, FillRecord, MemoryStore, StoreError

S = OrdStatus
REPO = Path(__file__).resolve().parents[2]
T = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)


def pg_dsn() -> str:
    from mooapp.common import Env
    from mooapp.config.loader import dsn_for, load_db_cfg, load_db_local

    db = load_db_cfg(REPO / "config" / "db.toml").model_copy(update={"dbname": "mooapp_test"})
    return dsn_for(Env.SIMULATE, db, load_db_local(REPO / "config" / "db.local.toml"), None)


@pytest.fixture
def pg_schema():
    import psycopg

    from mooapp.oms.migrate import migrate

    dsn = pg_dsn()
    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            applied = migrate(conn, schema, REPO / "db")
            assert "0001" in applied
        yield dsn, schema
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture(params=["memory", pytest.param("pg", marks=pytest.mark.pg)])
def store(request):
    if request.param == "memory":
        yield MemoryStore()
        return
    from mooapp.oms.pg_store import PostgresStore

    dsn, schema = request.getfixturevalue("pg_schema")
    st = PostgresStore(dsn, schema=schema, other_schema="real")
    try:
        yield st
    finally:
        st.close()


def mk_order(cid: str | None = None, **kw) -> Order:
    o = Order(
        client_order_id=cid or uuid.uuid4().hex,
        symbol="US.SPY",
        side=Side.BUY,
        kind=OrderKind.LIMIT,
        qty=100,
        price=Decimal("677.47"),
        source=Source.CLI,
        created_at=T,
        updated_at=T,
    )
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def ev(cid: str, **kw) -> EventRecord:
    base = dict(
        client_order_id=cid,
        event_type="SUBMIT",
        origin="local",
        outcome="applied",
        from_status="CREATED",
        to_status="PENDING_NEW",
        exec_id=None,
        qty=None,
        price=None,
        raw_status=None,
        reason=None,
        payload={"k": "v"},
        ts=T,
    )
    base.update(kw)
    return EventRecord(**base)


def fl(cid: str, fid: str = "900001:40", qty: int = 40, side: Side = Side.BUY, symbol: str = "US.SPY") -> FillRecord:
    return FillRecord(
        fill_id=fid,
        client_order_id=cid,
        broker_order_id="900001",
        symbol=symbol,
        side=side,
        qty=qty,
        price=Decimal("759.179"),
        ts=T,
        synthetic=True,
        origin="push",
    )


def test_order_roundtrip(store):
    o = mk_order(broker_order_id="900001")
    with store.transaction():
        store.insert_order(o)
    got = store.get_order(o.client_order_id)
    assert got == o
    assert got.price == Decimal("677.47") and got.created_at == T


def test_get_missing_is_none(store):
    assert store.get_order("0" * 32) is None


def test_update_and_lookup_by_broker_id(store):
    o = mk_order()
    with store.transaction():
        store.insert_order(o)
    o2 = replace(o, broker_order_id="900002", status=S.NEW, attempts=1, updated_at=T + timedelta(seconds=1))
    with store.transaction():
        store.update_order(o2)
    assert store.get_order(o.client_order_id) == o2
    assert store.find_by_broker_id("900002") == o2
    assert store.find_by_broker_id("nope") is None


def test_list_open_orders(store):
    a, b = mk_order(status=S.NEW), mk_order(status=S.FILLED, cum_qty=100, avg_px=Decimal("10"))
    with store.transaction():
        store.insert_order(a)
        store.insert_order(b)
    assert [o.client_order_id for o in store.list_orders(open_only=True)] == [a.client_order_id]
    assert len(store.list_orders(open_only=False)) == 2


def test_duplicate_order_rejected(store):
    o = mk_order()
    with store.transaction():
        store.insert_order(o)
    with pytest.raises(StoreError):
        with store.transaction():
            store.insert_order(o)


def test_events_append_in_order(store):
    o = mk_order()
    with store.transaction():
        store.insert_order(o)
        store.append_event(ev(o.client_order_id))
        store.append_event(ev(o.client_order_id, event_type="ACK", origin="push", from_status="PENDING_NEW", to_status="NEW"))
    recs = store.events_for(o.client_order_id)
    assert [r.event_type for r in recs] == ["SUBMIT", "ACK"]
    assert [r.origin for r in recs] == ["local", "push"]
    assert recs[0].payload == {"k": "v"}


def test_event_origin_validated(store):
    o = mk_order()
    with store.transaction():
        store.insert_order(o)
    with pytest.raises((StoreError, ValueError)):
        with store.transaction():
            store.append_event(ev(o.client_order_id, origin="email"))


def test_fill_dedup(store):
    o = mk_order()
    with store.transaction():
        store.insert_order(o)
        assert store.insert_fill_once(fl(o.client_order_id)) is True
    with store.transaction():
        assert store.insert_fill_once(fl(o.client_order_id)) is False
    fills = store.fills_for(o.client_order_id)
    assert len(fills) == 1 and fills[0].price == Decimal("759.179")


def test_net_fill_qty_by_symbol(store):
    o = mk_order()
    with store.transaction():
        store.insert_order(o)
        store.insert_fill_once(fl(o.client_order_id, "a", 100))
        store.insert_fill_once(fl(o.client_order_id, "b", 30, side=Side.SELL))
        store.insert_fill_once(fl(o.client_order_id, "c", 5, symbol="US.QQQ"))
    assert store.net_fill_qty_by_symbol() == {"US.SPY": 70, "US.QQQ": 5}


def test_recon_events(store):
    with store.transaction():
        store.append_recon("external_order", {"broker_order_id": "555"}, None, T)
    recs = store.recon_events()
    assert len(recs) == 1 and recs[0].kind == "external_order" and recs[0].details == {"broker_order_id": "555"}


def test_kv(store):
    assert store.get_kv("halt") is None
    with store.transaction():
        store.set_kv("halt", "1")
    with store.transaction():
        store.set_kv("halt", "0")
    assert store.get_kv("halt") == "0"


def test_transaction_rolls_back(store):
    o = mk_order()
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.insert_order(o)
            store.append_event(ev(o.client_order_id))
            raise RuntimeError("boom")
    assert store.get_order(o.client_order_id) is None
    assert store.events_for(o.client_order_id) == []


def test_journal_applied_marker(store):
    assert store.is_journal_applied("e1") is False
    with store.transaction():
        store.mark_journal_applied("e1")
    assert store.is_journal_applied("e1") is True


# ------------------------------------------------------------------ 只对 PostgreSQL


@pytest.mark.pg
@pytest.mark.parametrize("table", ["order_events", "recon_events"])
@pytest.mark.parametrize("stmt", ["UPDATE {s}.{t} SET ts = ts", "DELETE FROM {s}.{t}", "TRUNCATE {s}.{t}"])
def test_append_only_tables(pg_schema, table, stmt):
    import psycopg

    from mooapp.oms.pg_store import PostgresStore

    dsn, schema = pg_schema
    st = PostgresStore(dsn, schema=schema, other_schema="real")
    o = mk_order()
    with st.transaction():
        st.insert_order(o)
        st.append_event(ev(o.client_order_id))
        st.append_recon("x", {}, o.client_order_id, T)
    st.close()
    with psycopg.connect(dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.Error, match="只追加"):
            conn.execute(stmt.format(s=schema, t=table))


@pytest.mark.pg
def test_cum_qty_check_constraint(pg_schema):
    from mooapp.oms.pg_store import PostgresStore

    dsn, schema = pg_schema
    st = PostgresStore(dsn, schema=schema, other_schema="real")
    try:
        with pytest.raises(StoreError):
            with st.transaction():
                st.insert_order(mk_order(cum_qty=101))
    finally:
        st.close()


@pytest.mark.pg
def test_schema_assertion(pg_schema):
    from mooapp.oms.pg_store import PostgresStore

    dsn, _ = pg_schema
    with pytest.raises(StoreError):
        PostgresStore(dsn, schema="t_does_not_exist", other_schema="real")


@pytest.mark.pg
def test_migrate_is_idempotent(pg_schema):
    import psycopg

    from mooapp.oms.migrate import migrate

    dsn, schema = pg_schema
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert migrate(conn, schema, REPO / "db") == []
        n = conn.execute(f'SELECT count(*) FROM "{schema}".schema_migrations').fetchone()[0]
    assert n == len(list((REPO / "db" / "migrations").glob("*.sql")))
