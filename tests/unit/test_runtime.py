"""常驻进程生命周期（ops/run.py 的 Runtime）：用 FakeBroker + MemoryStore + 真实时钟组装，不连 OpenD 和数据库。

build_runtime 本身要连 OpenD 和 PostgreSQL，由 M4 验收 3 在模拟盘上覆盖。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from oms_harness import LOOSE_LIMITS

from mooapp.broker.fake import FakeBroker
from mooapp.clock import SystemClock
from mooapp.common import Env
from mooapp.oms.events import EventBus
from mooapp.oms.executor import Executor
from mooapp.oms.oms import OMS
from mooapp.oms.poller import Poller
from mooapp.oms.pump import EventPump
from mooapp.oms.store import MemoryStore
from mooapp.ops.commands import CommandClient, CommandProcessor
from mooapp.ops.run import RealDisabled, Runtime, RunnerAlreadyRunning, build_runtime
from mooapp.risk.gate import RiskGate


def runtime(store=None) -> Runtime:
    clock = SystemClock()
    broker = FakeBroker(Env.SIMULATE, clock)
    for s in LOOSE_LIMITS.whitelist:
        broker.set_market_state(s, "AFTERNOON")
        broker.set_quote(s, last=Decimal("10.00"))
    store = store or MemoryStore()
    bus = EventBus()
    oms = OMS(store=store, broker=broker, bus=bus, clock=clock, env=Env.SIMULATE)
    pump = EventPump(oms, threaded=True)
    pump.attach(broker)
    ex = Executor(oms=oms, broker=broker, pump=pump, clock=clock, bus=bus, resolver_grace=0.01)
    oms.set_executor(ex)
    poller = Poller(broker=broker, pump=pump, env=Env.SIMULATE, bus=bus, interval=0.2)
    gate = RiskGate(env=Env.SIMULATE, limits=LOOSE_LIMITS, oms=oms, executor=ex, md=broker, store=store, bus=bus, clock=clock)
    proc = CommandProcessor(gate=gate, store=store, clock=clock, bus=bus)
    return Runtime(Env.SIMULATE, clock, broker, broker, store, bus, oms, pump, poller, ex, gate, proc)


def test_start_process_commands_and_stop():
    rt = runtime()
    rt.start()
    try:
        assert rt.oms.ready
        client = CommandClient(store=rt.store, clock=rt.clock, timeout=5.0, poll_interval=0.05)
        assert client.runner_alive()
        placed = client.submit("place", client.place_payload("US.SPY", "BUY", 1, Decimal("10.00")), actor="test")
        assert placed["allowed"] is True
        r = client.submit("halt", {"reason": "测试"}, actor="test")
        # FakeBroker 不会自己确认（没有 step），订单停在 PENDING_NEW：halt 挂「待撤」标记（ADR-0026）
        assert r["halted"] is True and r["deferred"] == [placed["client_order_id"]] and r["cancel_sent"] == []
    finally:
        rt.stop()
    assert rt.broker.calls("close")


def test_single_instance_lock():
    store = MemoryStore()
    rt1 = runtime(store)
    rt1.start()
    try:
        rt2 = runtime(store)
        with pytest.raises(RunnerAlreadyRunning):
            rt2.start()
    finally:
        rt1.stop()
    rt3 = runtime(store)
    rt3.start()
    rt3.stop()


def test_build_runtime_refuses_real(tmp_path):
    with pytest.raises(RealDisabled):
        build_runtime(Env.REAL, tmp_path)


# ------------------------------------------------------------------ M5：常驻进程启动、停止通知器


def test_runtime_starts_and_stops_notifier():
    class Dummy:
        def __init__(self):
            self.events = []

        def start(self):
            self.events.append("start")

        def stop(self):
            self.events.append("stop")

    rt = runtime()
    rt.notifier = Dummy()
    rt.start()
    rt.stop()
    assert rt.notifier.events == ["start", "stop"]


def test_build_runtime_wires_notifier_with_raw_store():
    """静态检查：build_runtime 构造 Notifier，并传入数据库本身（store=store，不经 journal）。"""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "src" / "mooapp" / "ops" / "run.py").read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "Notifier"]
    assert calls
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(kw.get("store"), ast.Name) and kw["store"].id == "store"
