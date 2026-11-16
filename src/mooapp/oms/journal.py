"""本地 journal（ADR-0025）：每个写库事务提交前，先把它的写操作写成本地文件。

- var/journal/<env>/pending/<entry_id>.json：写完 fsync，权限 600；提交成功后移到 archive/YYYY-MM-DD/
- 库不可用（StoreUnavailable）时：事务里其余写操作照样记进 journal，事务结束时写出 entry 再抛 StoreUnavailable
  （调用者决定后续：撤单、halt 照常，新单拒绝 —— M4）
- replay_journal：启动和库恢复时按写入顺序回放 pending；按 entry_id 幂等（journal_applied 表与写操作在同一事务里提交）
- journal 与日志同等对待：不进 Git，不含 DSN、密码等凭据；账号字段 Store 里本来就没有
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from mooapp.common import Env, OrderKind, Side, Source
from mooapp.oms.state import Order, OrdStatus
from mooapp.oms.store import AuditRecord, EventRecord, FillRecord, RuleEventRecord, RuleRecord, Store, StoreError, StoreUnavailable

log = logging.getLogger(__name__)

_WRITE_OPS = frozenset(
    {
        "insert_order", "update_order", "append_event", "insert_fill_once", "append_recon", "set_kv", "append_audit", "finish_command",
        "insert_rule", "update_rule", "link_rule_order", "append_rule_event",
    }  # fmt: skip
)
_ORDER_SKIP = frozenset({"seen_exec_ids", "anomalies", "history"})
_ORDER_ENUMS: dict[str, type] = {"side": Side, "kind": OrderKind, "source": Source, "status": OrdStatus, "base_status": OrdStatus}
_seq = itertools.count()


# ------------------------------------------------------------------ 编解码


def _enc(v: Any) -> Any:
    if isinstance(v, Order):
        return {"__order__": {f.name: _enc(getattr(v, f.name)) for f in fields(v) if f.name not in _ORDER_SKIP}}
    if isinstance(v, EventRecord):
        return {"__event__": {f.name: _enc(getattr(v, f.name)) for f in fields(v)}}
    if isinstance(v, FillRecord):
        return {"__fill__": {f.name: _enc(getattr(v, f.name)) for f in fields(v)}}
    if isinstance(v, AuditRecord):
        return {"__audit__": {f.name: _enc(getattr(v, f.name)) for f in fields(v)}}
    if isinstance(v, RuleRecord):
        return {"__rule__": {f.name: _enc(getattr(v, f.name)) for f in fields(v)}}
    if isinstance(v, RuleEventRecord):
        return {"__rule_event__": {f.name: _enc(getattr(v, f.name)) for f in fields(v)}}
    if isinstance(v, Decimal):
        return {"__dec__": str(v)}
    if isinstance(v, datetime):
        return {"__dt__": v.isoformat()}
    if isinstance(v, (Side, OrderKind, Source, OrdStatus)):
        return v.value
    if isinstance(v, dict):
        return {k: _enc(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_enc(x) for x in v]
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    raise TypeError(f"journal 无法编码 {type(v).__name__}")


def _dec(v: Any) -> Any:
    if isinstance(v, dict):
        if "__order__" in v:
            d = {k: _dec(x) for k, x in v["__order__"].items()}
            for k, enum in _ORDER_ENUMS.items():
                if d.get(k) is not None:
                    d[k] = enum(d[k])
            return Order(**d)
        if "__event__" in v:
            return EventRecord(**{k: _dec(x) for k, x in v["__event__"].items()})
        if "__fill__" in v:
            d = {k: _dec(x) for k, x in v["__fill__"].items()}
            d["side"] = Side(d["side"])
            return FillRecord(**d)
        if "__rule__" in v:
            return RuleRecord(**{k: _dec(x) for k, x in v["__rule__"].items()})
        if "__rule_event__" in v:
            return RuleEventRecord(**{k: _dec(x) for k, x in v["__rule_event__"].items()})
        if "__audit__" in v:
            return AuditRecord(**{k: _dec(x) for k, x in v["__audit__"].items()})
        if "__dec__" in v:
            return Decimal(v["__dec__"])
        if "__dt__" in v:
            return datetime.fromisoformat(v["__dt__"])
        return {k: _dec(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_dec(x) for x in v]
    return v


# ------------------------------------------------------------------ Journal


@dataclass(frozen=True)
class JournalEntry:
    entry_id: str
    created_at: datetime
    ops: list[tuple[Any, ...]]  # (方法名, *参数)


class Journal:
    def __init__(self, root: Path, env: Env):
        self.base = root / env.value
        self.pending_dir = self.base / "pending"
        self.archive_dir = self.base / "archive"
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def new_entry(self, ops: list[tuple[Any, ...]], now: datetime) -> JournalEntry:
        # 文件名按写入顺序排序：纳秒时间 + 进程内序号；再加随机后缀防止多进程碰撞
        entry_id = f"{time.time_ns():020d}{next(_seq):06d}-{uuid.uuid4().hex[:8]}"
        return JournalEntry(entry_id, now.astimezone(UTC), list(ops))

    def write(self, entry: JournalEntry) -> Path:
        path = self.pending_dir / f"{entry.entry_id}.json"
        body = json.dumps(
            {"entry_id": entry.entry_id, "created_at": entry.created_at.isoformat(), "ops": _enc(entry.ops)},
            ensure_ascii=False,
        ).encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_dir(self.pending_dir)
        return path

    def pending(self) -> list[JournalEntry]:
        out = []
        for p in sorted(self.pending_dir.glob("*.json")):
            raw = json.loads(p.read_text(encoding="utf-8"))
            ops = [tuple(op) for op in _dec(raw["ops"])]
            out.append(JournalEntry(raw["entry_id"], datetime.fromisoformat(raw["created_at"]), ops))
        return out

    def archive(self, entry: JournalEntry) -> None:
        day = self.archive_dir / entry.created_at.astimezone(UTC).strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        os.replace(self.pending_dir / f"{entry.entry_id}.json", day / f"{entry.entry_id}.json")
        _fsync_dir(self.pending_dir)


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ------------------------------------------------------------------ JournaledStore


class JournaledStore:
    """包一层 Store：读直接转发；写在事务里先执行到内层，同时记进 entry；提交前写 journal。"""

    def __init__(self, inner: Store, journal: Journal, clock_now: Callable[[], datetime]):
        self.inner = inner
        self.journal = journal
        self._now = clock_now
        self._ops: list[tuple[Any, ...]] | None = None
        self._degraded: StoreUnavailable | None = None

    def __getattr__(self, name: str) -> Any:
        if name in _WRITE_OPS:
            return lambda *args: self._write(name, *args)
        return getattr(self.inner, name)  # 读操作

    def _write(self, name: str, *args: Any) -> Any:
        if self._ops is None:
            with self.transaction():
                return self._write(name, *args)
        self._ops.append((name, *args))
        if self._degraded is not None:
            return True if name == "insert_fill_once" else None
        try:
            return getattr(self.inner, name)(*args)
        except StoreUnavailable as e:
            log.error("数据库不可用，本事务其余写操作只记 journal：%s", e)
            self._degraded = e
            return True if name == "insert_fill_once" else None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._ops is not None:  # 嵌套：并入外层
            yield
            return
        self._ops, self._degraded = [], None
        entry: JournalEntry | None = None
        try:
            try:
                with self.inner.transaction():
                    yield
                    if self._degraded is not None:
                        raise self._degraded  # 让内层回滚已执行的部分；entry 在下面写出
                    if self._ops:
                        entry = self.journal.new_entry(self._ops, self._now())
                        self.journal.write(entry)
                        self.inner.mark_journal_applied(entry.entry_id)
            except StoreUnavailable:
                if entry is None and self._ops:
                    self.journal.write(self.journal.new_entry(self._ops, self._now()))
                raise
            if entry is not None:
                self.journal.archive(entry)
        finally:
            self._ops, self._degraded = None, None


# ------------------------------------------------------------------ 回放


@dataclass
class ReplayReport:
    applied: int = 0
    skipped: int = 0
    failed: int = 0


def replay_journal(journal: Journal, store: Store) -> ReplayReport:
    """按写入顺序回放 pending。store 应是内层 Store（不要传 JournaledStore，否则会再写一次 journal）。"""
    r = ReplayReport()
    for entry in journal.pending():
        try:
            if store.is_journal_applied(entry.entry_id):
                journal.archive(entry)
                r.skipped += 1
                continue
            with store.transaction():
                for name, *args in entry.ops:
                    if name not in _WRITE_OPS:
                        raise StoreError(f"journal {entry.entry_id} 含未知操作 {name!r}")
                    getattr(store, name)(*args)
                store.mark_journal_applied(entry.entry_id)
        except StoreError as e:
            # 停在第一条失败的 entry：后面的不能先于它写入（按写入顺序）
            log.error("journal %s 回放失败，它和之后的 entry 都保留在 pending：%s", entry.entry_id, e)
            r.failed += 1
            break
        journal.archive(entry)
        r.applied += 1
    return r
