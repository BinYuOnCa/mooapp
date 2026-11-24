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


def test_outbox_enqueue_due_sent_failed(store):
    """M5 决定 2：outbox 持久化；到期才取；发出后不再取；失败记次数、错误和下次时间。"""
    with store.transaction():
        a = store.enqueue_email("fill", "成交", "正文", ["me@example.com"], T)
        b = store.enqueue_email("alert", "报警", "正文", ["me@example.com"], T + timedelta(seconds=1))
    due = store.due_emails(T + timedelta(seconds=1))
    assert [e.id for e in due] == [a, b]
    assert due[0].recipients == ["me@example.com"] and due[0].attempts == 0 and due[0].status == "pending"
    with store.transaction():
        store.mark_email_failed(a, "SMTP 认证失败", T + timedelta(minutes=1), T)
        store.mark_email_sent(b, T)
    assert store.due_emails(T + timedelta(seconds=30)) == []
    later = store.due_emails(T + timedelta(minutes=1))
    assert [e.id for e in later] == [a] and later[0].attempts == 1 and later[0].last_error == "SMTP 认证失败"


def test_fills_until_ordered_by_ts(store):
    """M7：P&L 需要某时刻之前的全部本地成交（期初持仓与成本）。"""
    o = mk_order()
    with store.transaction():
        store.insert_order(o)
        store.insert_fill_once(replace(fl(o.client_order_id, "late", 1), ts=T + timedelta(hours=2)))
        store.insert_fill_once(replace(fl(o.client_order_id, "early", 2), ts=T))
        store.insert_fill_once(replace(fl(o.client_order_id, "after", 3), ts=T + timedelta(days=1)))
    got = store.fills_until(T + timedelta(hours=3))
    assert [f.fill_id for f in got] == ["early", "late"]


def test_eod_reports_append_and_read(store):
    """M7 决定 4：eod_reports 只追加；同一交易日可多次运行。"""
    from datetime import date

    from mooapp.oms.store import EodRecord

    d = date(2026, 11, 13)
    r1 = EodRecord(d, T, "SIMULATE", True, {"realized": "0.50"}, [], [{"symbol": "US.SPY", "realized": "0.50"}])
    r2 = replace(r1, generated_at=T + timedelta(minutes=5), ok=False, breaks=[{"kind": "fill_qty_mismatch"}])
    with store.transaction():
        store.append_eod_report(r1)
        store.append_eod_report(r2)
        store.append_eod_report(replace(r1, trade_date=date(2026, 11, 12)))
    got = store.eod_reports(d)
    assert [g.ok for g in got] == [True, False]
    assert got[0].rows == [{"symbol": "US.SPY", "realized": "0.50"}] and got[1].breaks == [{"kind": "fill_qty_mismatch"}]
    assert got[0].summary == {"realized": "0.50"} and got[0].env == "SIMULATE"


def test_approvals_roundtrip(store):
    """M8：approvals（提议、状态、过期、结果）；按状态与创建时间查询。"""
    from mooapp.oms.store import ApprovalRecord

    a = ApprovalRecord("ap1", "order", {"symbol": "US.SPY", "qty": 1}, "理由", "agent:mcp", "pending", T, T + timedelta(minutes=30), None, None)
    b = replace(a, id="ap2", kind="rule", payload={"yaml": "id: x"}, created_at=T + timedelta(minutes=1))
    with store.transaction():
        store.insert_approval(a)
        store.insert_approval(b)
    assert store.get_approval("ap1") == a and store.get_approval("zzz") is None
    with store.transaction():
        store.update_approval("ap1", "approved", T + timedelta(minutes=2), {"allowed": True})
    got = store.get_approval("ap1")
    assert got.status == "approved" and got.result == {"allowed": True} and got.decided_at == T + timedelta(minutes=2)
    assert [x.id for x in store.list_approvals()] == ["ap1", "ap2"]
    assert [x.id for x in store.list_approvals(status="pending")] == ["ap2"]
    assert [x.id for x in store.list_approvals(since=T + timedelta(seconds=30))] == ["ap2"]


def test_agent_calls_append_only_list(store):
    with store.transaction():
        store.append_agent_call("get_positions", {}, {"rows": []}, "agent:mcp", True, T)
        store.append_agent_call("propose_order", {"qty": 1}, {"error": "x"}, "agent:mcp", False, T + timedelta(seconds=1))
    calls = store.agent_calls()
    assert [(c.tool, c.ok) for c in calls] == [("get_positions", True), ("propose_order", False)]
    assert calls[1].args == {"qty": 1} and calls[1].result == {"error": "x"} and calls[0].caller == "agent:mcp"


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


# ------------------------------------------------------------------ M4：audit、当日下单次数、commands、单实例锁、待撤标记


def audit_rec(**kw):
    from mooapp.oms.store import AuditRecord

    base = dict(
        ts=T, action="place", source="cli", actor="cli:test", client_order_id=None, symbol="US.SPY", side="BUY", qty=1,
        price=Decimal("10.00"), allowed=False, rule="whitelist", reason="不在白名单", details={"k": 1},
    )  # fmt: skip
    base.update(kw)
    return AuditRecord(**base)


def test_audit_roundtrip(store):
    with store.transaction():
        store.append_audit(audit_rec())
        store.append_audit(audit_rec(allowed=True, rule=None, reason="ok"))
    recs = store.audit_records()
    assert [r.allowed for r in recs] == [False, True]
    assert recs[0].price == Decimal("10.00") and recs[0].details == {"k": 1}


def test_count_submitted_between(store):
    a, b, c = mk_order(), mk_order(), mk_order()
    with store.transaction():
        for o in (a, b, c):
            store.insert_order(o)
        store.append_event(ev(a.client_order_id, ts=T))
        store.append_event(ev(a.client_order_id, event_type="RESUBMIT", from_status="UNKNOWN", ts=T + timedelta(seconds=5)))
        store.append_event(ev(b.client_order_id, ts=T + timedelta(hours=1)))
        store.append_event(ev(c.client_order_id, ts=T + timedelta(days=1)))  # 次日
        store.append_event(ev(c.client_order_id, event_type="SUBMIT", outcome="rejected", ts=T))  # 未生效的不算
    assert store.count_submitted_between(T - timedelta(hours=1), T + timedelta(hours=2)) == 2


def test_commands_queue(store):
    with store.transaction():
        i1 = store.enqueue_command("place", {"symbol": "US.SPY"}, "cli", T)
        i2 = store.enqueue_command("halt", {"reason": "x"}, "cli", T)
    c = store.claim_next_command(T)
    assert c is not None and c.id == i1 and c.kind == "place" and c.status == "running"
    with store.transaction():
        store.finish_command(i1, "done", {"allowed": True}, T)
    got = store.get_command(i1)
    assert got.status == "done" and got.result == {"allowed": True}
    c2 = store.claim_next_command(T)
    assert c2 is not None and c2.id == i2
    assert store.claim_next_command(T) is None


def test_fail_running_commands_only_touches_running(store):
    """M4 审查 #7：启动时把遗留的 running 命令标为 error；pending 和已结束的不动。"""
    with store.transaction():
        i1 = store.enqueue_command("halt", {}, "cli", T)
        i2 = store.enqueue_command("halt", {}, "cli", T)
        i3 = store.enqueue_command("halt", {}, "cli", T)
    store.claim_next_command(T)
    with store.transaction():
        store.finish_command(i1, "done", {"halted": True}, T)
    store.claim_next_command(T)  # i2 → running
    with store.transaction():
        assert store.fail_running_commands({"error": "重启"}, T) == 1
    assert store.get_command(i1).status == "done"
    got = store.get_command(i2)
    assert got.status == "error" and got.result == {"error": "重启"} and got.finished_at is not None
    assert store.get_command(i3).status == "pending"


def test_runner_lock_exclusive_within_store(store):
    assert store.try_runner_lock() is True
    store.release_runner_lock()
    assert store.try_runner_lock() is True
    store.release_runner_lock()


def test_cancel_pending_roundtrip(store):
    o = mk_order(cancel_pending=True)
    with store.transaction():
        store.insert_order(o)
    assert store.get_order(o.client_order_id).cancel_pending is True


@pytest.mark.pg
@pytest.mark.parametrize("stmt", ["UPDATE {s}.audit SET ts = ts", "DELETE FROM {s}.audit", "TRUNCATE {s}.audit"])
def test_audit_is_append_only(pg_schema, stmt):
    import psycopg

    from mooapp.oms.pg_store import PostgresStore

    dsn, schema = pg_schema
    st = PostgresStore(dsn, schema=schema, other_schema="real")
    with st.transaction():
        st.append_audit(audit_rec())
    st.close()
    with psycopg.connect(dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.Error, match="只追加"):
            conn.execute(stmt.format(s=schema))


@pytest.mark.pg
def test_runner_lock_exclusive_across_connections(pg_schema):
    from mooapp.oms.pg_store import PostgresStore

    dsn, schema = pg_schema
    a = PostgresStore(dsn, schema=schema, other_schema="real")
    b = PostgresStore(dsn, schema=schema, other_schema="real")
    try:
        assert a.try_runner_lock() is True
        assert b.try_runner_lock() is False  # 第二个常驻进程拿不到（SPEC 3.4 单实例锁）
        a.release_runner_lock()
        assert b.try_runner_lock() is True
    finally:
        a.close()
        b.close()


@pytest.mark.pg
def test_claim_skips_rows_locked_by_other_connection(pg_schema):
    from mooapp.oms.pg_store import PostgresStore

    dsn, schema = pg_schema
    a = PostgresStore(dsn, schema=schema, other_schema="real")
    b = PostgresStore(dsn, schema=schema, other_schema="real")
    try:
        with a.transaction():
            i1 = a.enqueue_command("halt", {}, "cli", T)
            i2 = a.enqueue_command("resume", {}, "cli", T)
        c1 = a.claim_next_command(T)
        c2 = b.claim_next_command(T)
        assert {c1.id, c2.id} == {i1, i2}
    finally:
        a.close()
        b.close()


# ------------------------------------------------------------------ M6b：rules、rule_orders、rule_events（只追加）、active_rule_for


def rule_rec(rule_id="r1", symbol="US.SPY", status="PENDING"):
    from mooapp.oms.store import RuleRecord

    return RuleRecord(rule_id, symbol, "id: r1\n", status, "e" * 32, {"bought": 0}, "cli:test", T, T)


def test_rules_roundtrip_and_active_lookup(store):
    with store.transaction():
        store.insert_rule(rule_rec())
    r = store.get_rule("r1")
    assert r is not None and r.state == {"bought": 0} and r.entry_cid == "e" * 32
    assert store.active_rule_for("US.SPY") == "r1"
    with store.transaction():
        store.update_rule("r1", "ARMED", {"bought": 10, "stop_price": "95.00"}, T + timedelta(seconds=1))
    assert store.get_rule("r1").status == "ARMED" and store.get_rule("r1").state["stop_price"] == "95.00"
    assert store.active_rule_for("US.SPY") == "r1"
    with store.transaction():
        store.update_rule("r1", "CLOSED", {"bought": 10}, T + timedelta(seconds=2))
    assert store.active_rule_for("US.SPY") is None
    assert [x.id for x in store.list_rules(active_only=False)] == ["r1"]
    assert store.list_rules(active_only=True) == []


def test_rule_duplicate_id_rejected(store):
    with store.transaction():
        store.insert_rule(rule_rec())
    with pytest.raises(StoreError):
        with store.transaction():
            store.insert_rule(rule_rec())


def test_rule_orders_mapping(store):
    with store.transaction():
        store.insert_rule(rule_rec())
        store.link_rule_order("a" * 32, "r1", "entry", T)
        store.link_rule_order("b" * 32, "r1", "sell", T)
    assert store.rule_order("a" * 32) == ("r1", "entry")
    assert store.rule_order("c" * 32) is None
    assert store.rule_orders("r1") == [("a" * 32, "entry"), ("b" * 32, "sell")]


def test_rule_events_append(store):
    from mooapp.oms.store import RuleEventRecord

    with store.transaction():
        store.insert_rule(rule_rec())
        store.append_rule_event(RuleEventRecord("r1", T, "frozen", "止损价 95.00", {"x": 1}))
    (e,) = store.rule_events("r1")
    assert (e.kind, e.message, e.details) == ("frozen", "止损价 95.00", {"x": 1})


@pytest.mark.pg
@pytest.mark.parametrize("stmt", ["UPDATE {s}.rule_events SET ts = ts", "DELETE FROM {s}.rule_events", "TRUNCATE {s}.rule_events"])
def test_rule_events_append_only(pg_schema, stmt):
    import psycopg

    from mooapp.oms.pg_store import PostgresStore
    from mooapp.oms.store import RuleEventRecord

    dsn, schema = pg_schema
    st = PostgresStore(dsn, schema=schema, other_schema="real")
    with st.transaction():
        st.insert_rule(rule_rec())
        st.append_rule_event(RuleEventRecord("r1", T, "frozen", "x", {}))
    st.close()
    with psycopg.connect(dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.Error, match="只追加"):
            conn.execute(stmt.format(s=schema))
