"""M3 测试共用：把 FakeBroker、MemoryStore、OMS、EventPump（同步模式）、Poller、Executor、闸门骨架接在一起。

tests/ 目录因为有 conftest.py 而在 sys.path 上（与 `from conftest import write` 同理），各测试 `from oms_harness import ...`。
同步模式下推送只进队列，测试显式调用 `sys.pump.drain()` 处理，便于构造重复、乱序、丢失。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from mooapp.broker.fake import FakeBroker
from mooapp.broker.port import BrokerOrder, OrderRequest, new_client_order_id
from mooapp.clock import FakeClock
from mooapp.common import Env, OrderKind, Side, Source
from mooapp.oms.events import Alert, EventBus
from mooapp.oms.executor import Executor
from mooapp.oms.oms import OMS
from mooapp.oms.poller import Poller
from mooapp.oms.pump import EventPump
from mooapp.oms.store import MemoryStore, Store
from mooapp.risk.gate import Decision, Intent, RiskGate

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
WHITELIST = frozenset({"US.SPY", "US.QQQ", "US.TD"})
T0 = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)  # 美东 10:00


@dataclass
class System:
    env: Env
    clock: FakeClock
    broker: FakeBroker
    store: Store
    bus: EventBus
    oms: OMS
    pump: EventPump
    poller: Poller
    executor: Executor
    gate: RiskGate
    alerts: list[Alert] = field(default_factory=list)

    def place(
        self,
        qty: int = 100,
        price: str | None = "10.00",
        side: Side = Side.BUY,
        symbol: str = "US.SPY",
        cid: str | None = None,
        source: Source = Source.CLI,
    ) -> Decision:
        kind = OrderKind.LIMIT if price is not None else OrderKind.MARKET
        req = OrderRequest(
            client_order_id=cid or new_client_order_id(),
            symbol=symbol,
            side=side,
            qty=qty,
            kind=kind,
            limit_price=Decimal(price) if price is not None else None,
        )
        return self.gate.submit(Intent(source=source, action="place", request=req, actor="test"))

    def cancel(self, cid: str) -> Decision:
        return self.gate.submit(Intent(source=Source.CLI, action="cancel", client_order_id=cid, actor="test"))

    def alert_kinds(self) -> list[str]:
        return [a.kind for a in self.alerts]


def make_system(
    env: Env = Env.SIMULATE,
    store: Store | None = None,
    broker: FakeBroker | None = None,
    clock: FakeClock | None = None,
    attach: bool = True,
    recover: bool = True,
) -> System:
    clock = clock or FakeClock(T0)
    broker = broker or FakeBroker(env, clock)
    store = store if store is not None else MemoryStore()
    bus = EventBus()
    alerts: list[Alert] = []
    bus.subscribe(Alert, alerts.append)
    oms = OMS(store=store, broker=broker, bus=bus, clock=clock, env=env)
    pump = EventPump(oms, threaded=False)
    if attach:
        pump.attach(broker)
    poller = Poller(broker=broker, pump=pump, env=env, bus=bus, interval=5.0)
    executor = Executor(oms=oms, broker=broker, pump=pump, clock=clock, bus=bus, resolver_grace=3.0)
    oms.set_executor(executor)  # 启动恢复时，本地 PENDING_NEW 而券商端没有的订单交给 Resolver
    gate = RiskGate(env=env, whitelist=WHITELIST, oms=oms, executor=executor)
    sys = System(env, clock, broker, store, bus, oms, pump, poller, executor, gate, alerts)
    if recover:
        oms.recover()
    return sys


def load_push_rows(name: str) -> list[dict[str, Any]]:
    """PushRecorder 格式的 jsonl：跳过 meta 行，返回每条推送的原始 row。"""
    rows = []
    with (FIXTURES / name).open(encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("meta"):
                continue
            assert rec["kind"] == "order", rec
            rows.append(rec["row"])
    return rows


def last_snapshot_by_order(snaps: list[BrokerOrder]) -> dict[str, BrokerOrder]:
    out: dict[str, BrokerOrder] = {}
    for s in snaps:
        out[s.broker_order_id] = s
    return out
