"""Store 协议与内存实现（SPEC 5.4；ADR-0024：代码只依赖协议，测试用 MemoryStore，PostgresStore 见 pg_store.py）。

表：orders、order_events（只追加）、fills（按 fill_id 去重）、recon_events（只追加）、kv、journal_applied。
写操作可以在 transaction() 里成组提交；不在事务里时每个写操作自成一个事务。
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
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
    def set_kv(self, key: str, value: str) -> None: ...
    # journal（ADR-0025）
    def mark_journal_applied(self, entry_id: str) -> None: ...
    def is_journal_applied(self, entry_id: str) -> bool: ...
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


class MemoryStore:
    """仅测试用。事务：最外层开始时整份复制，出错时还原。读写都复制对象，调用者改不到库里的数据。"""

    def __init__(self) -> None:
        self._s = _State()
        self._depth = 0
        self._backup: _State | None = None

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

    def get_kv(self, key: str) -> str | None:
        return self._s.kv.get(key)

    def set_kv(self, key: str, value: str) -> None:
        self._s.kv[key] = value

    # journal

    def mark_journal_applied(self, entry_id: str) -> None:
        self._s.journal.add(entry_id)

    def is_journal_applied(self, entry_id: str) -> bool:
        return entry_id in self._s.journal
