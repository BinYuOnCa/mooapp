"""本地 journal（ADR-0025；M3 计划第 3 节）：写库前先写 journal，提交后归档；库不可用时留在 pending，恢复后按 entry_id 幂等回放。"""

from __future__ import annotations

import os
import stat
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from mooapp.common import Env, OrderKind, Side, Source
from mooapp.oms.journal import Journal, JournaledStore, replay_journal
from mooapp.oms.state import Order
from mooapp.oms.store import EventRecord, FillRecord, MemoryStore, StoreUnavailable

T = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)


class FlakyStore(MemoryStore):
    """down=True 时任何写操作都抛 StoreUnavailable（模拟库不可用）。"""

    def __init__(self) -> None:
        super().__init__()
        self.down = False

    def _guard(self) -> None:
        if self.down:
            raise StoreUnavailable("数据库不可用（测试）")

    def insert_order(self, o):
        self._guard()
        return super().insert_order(o)

    def append_event(self, e):
        self._guard()
        return super().append_event(e)

    def insert_fill_once(self, f):
        self._guard()
        return super().insert_fill_once(f)


def order() -> Order:
    return Order(
        client_order_id=uuid.uuid4().hex,
        symbol="US.SPY",
        side=Side.BUY,
        kind=OrderKind.LIMIT,
        qty=10,
        price=Decimal("10.01"),
        source=Source.CLI,
        created_at=T,
        updated_at=T,
    )


def event(cid: str) -> EventRecord:
    return EventRecord(cid, "SUBMIT", "local", "applied", "CREATED", "PENDING_NEW", None, None, None, None, None, {"a": 1}, T)


def fill(cid: str) -> FillRecord:
    return FillRecord("900001:10", cid, "900001", "US.SPY", Side.BUY, 10, Decimal("10.01"), T, True, "push")


def write_all(st, o):
    with st.transaction():
        st.insert_order(o)
        st.append_event(event(o.client_order_id))
        st.insert_fill_once(fill(o.client_order_id))


def test_commit_archives_entry(tmp_path):
    j = Journal(tmp_path, Env.SIMULATE)
    inner = MemoryStore()
    st = JournaledStore(inner, j, clock_now=lambda: T)
    o = order()
    write_all(st, o)
    assert j.pending() == []
    archived = list((tmp_path / "SIMULATE" / "archive" / "2026-11-13").glob("*.json"))
    assert len(archived) == 1
    assert inner.get_order(o.client_order_id) == o


def test_reads_pass_through(tmp_path):
    inner = MemoryStore()
    st = JournaledStore(inner, Journal(tmp_path, Env.SIMULATE), clock_now=lambda: T)
    o = order()
    write_all(st, o)
    assert st.get_order(o.client_order_id) == o
    assert len(st.fills_for(o.client_order_id)) == 1


def test_db_down_leaves_pending_with_mode_600(tmp_path):
    j = Journal(tmp_path, Env.SIMULATE)
    inner = FlakyStore()
    inner.down = True
    st = JournaledStore(inner, j, clock_now=lambda: T)
    o = order()
    with pytest.raises(StoreUnavailable):
        write_all(st, o)
    pend = j.pending()
    assert len(pend) == 1
    path = tmp_path / "SIMULATE" / "pending" / f"{pend[0].entry_id}.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert inner.get_order(o.client_order_id) is None


def test_replay_after_recovery_is_idempotent(tmp_path):
    j = Journal(tmp_path, Env.SIMULATE)
    inner = FlakyStore()
    inner.down = True
    st = JournaledStore(inner, j, clock_now=lambda: T)
    o = order()
    with pytest.raises(StoreUnavailable):
        write_all(st, o)
    inner.down = False
    r1 = replay_journal(j, inner)
    assert r1.applied == 1 and r1.failed == 0
    assert inner.get_order(o.client_order_id) == o
    assert len(inner.events_for(o.client_order_id)) == 1
    assert len(inner.fills_for(o.client_order_id)) == 1
    assert j.pending() == []
    r2 = replay_journal(j, inner)
    assert r2.applied == 0


def test_replay_skips_entry_already_applied(tmp_path):
    """库已提交、但归档前进程被杀：pending 里还有 entry，回放时按 entry_id 跳过、只归档。"""
    j = Journal(tmp_path, Env.SIMULATE)
    inner = MemoryStore()
    o = order()
    entry = j.new_entry([("insert_order", o)], now=T)
    j.write(entry)
    with inner.transaction():
        inner.insert_order(o)
        inner.mark_journal_applied(entry.entry_id)
    r = replay_journal(j, inner)
    assert r.applied == 0 and r.skipped == 1
    assert j.pending() == []


def test_replay_failure_keeps_pending(tmp_path):
    j = Journal(tmp_path, Env.SIMULATE)
    inner = FlakyStore()
    inner.down = True
    st = JournaledStore(inner, j, clock_now=lambda: T)
    with pytest.raises(StoreUnavailable):
        write_all(st, order())
    r = replay_journal(j, inner)  # 库仍不可用
    assert r.failed == 1
    assert len(j.pending()) == 1


def test_pending_in_write_order(tmp_path):
    j = Journal(tmp_path, Env.SIMULATE)
    ids = []
    for _ in range(5):
        e = j.new_entry([("set_kv", "k", "v")], now=T)
        j.write(e)
        ids.append(e.entry_id)
    assert [e.entry_id for e in j.pending()] == ids
