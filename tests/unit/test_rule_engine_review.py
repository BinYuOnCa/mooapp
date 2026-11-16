"""M6b 审查修复（/code-review high 的 10 条发现，加上 2 条小问题；PROGRESS「M6b 审查修复」）。每条一个复现测试。

除了 #1 死锁用真正的线程（带超时，不会卡住测试），其余用同步模式的 harness。
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from oms_harness import LOOSE_LIMITS, make_system

from mooapp.broker.fake import FakeBroker
from mooapp.broker.port import BrokerError, OrderRequest, new_client_order_id
from mooapp.clock import FakeClock, SystemClock
from mooapp.common import Env, OrderKind, Side, Source
from mooapp.oms.events import EventBus
from mooapp.oms.executor import Executor
from mooapp.oms.oms import OMS
from mooapp.oms.pump import EventPump
from mooapp.oms.store import MemoryStore, StoreUnavailable
from mooapp.risk.gate import Intent, RiskGate
from mooapp.strategy.engine import RuleEngine
from mooapp.strategy.machine import RuleStatus

D = Decimal
SYMBOL = "US.SPY"
RULE = """\
id: spy-oco-1
symbol: US.SPY
entry: {side: BUY, qty: 10, limit_price: "100.21"}
stop: "min(open, avg_cost) * 0.95"
take: "max(open, avg_cost) * 1.05"
"""


def engine_for(sys) -> RuleEngine:
    return RuleEngine(gate=sys.gate, oms=sys.oms, store=sys.store, md=sys.broker, bus=sys.bus, clock=sys.clock, pump=sys.pump,
                      poll_fallback_seconds=5.0)  # fmt: skip


def setup(**kw):
    sys = make_system(**kw)
    sys.market_open(last=D("100.21"))
    return sys, engine_for(sys)


def push(sys, last, open_="99.99"):
    sys.broker.push_quote(SYMBOL, last=D(last), open=D(open_) if open_ else None)
    sys.pump.drain()


def entry(sys, eng):
    return sys.oms.get(eng.rule("spy-oco-1").entry_cid)


def fill_entry(sys, eng, qty, price="100.21"):
    o = entry(sys, eng)
    if o.status.value == "PENDING_NEW":
        sys.broker.step()
    sys.broker.fill(o.broker_order_id, qty, D(price))
    sys.pump.drain()


def sells(sys):
    return [c[0] for c in sys.broker.calls("place_order") if c[0].side is Side.SELL]


def armed(**kw):
    sys, eng = setup(**kw)
    assert eng.add(RULE, actor="cli:test").allowed
    push(sys, "100.21")
    fill_entry(sys, eng, 10)
    assert eng.status("spy-oco-1") is RuleStatus.ARMED
    return sys, eng


def restart(sys1):
    sys2 = make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock, recover=False)
    eng2 = engine_for(sys2)
    eng2.recover()
    sys2.oms.recover()
    sys2.pump.drain()
    return sys2, eng2


# ------------------------------------------------------------------ #1 死锁：闸门锁 ↔ 事件线程（真正的线程）


def threaded_system():
    clock = SystemClock()
    broker = FakeBroker(Env.SIMULATE, clock)
    for s in LOOSE_LIMITS.whitelist:
        broker.set_market_state(s, "AFTERNOON")
        broker.set_quote(s, last=D("100.21"), bid=D("100.21"), ask=D("100.21"))
    store = MemoryStore()
    bus = EventBus()
    oms = OMS(store=store, broker=broker, bus=bus, clock=clock, env=Env.SIMULATE)
    pump = EventPump(oms, threaded=True)
    pump.attach(broker)
    ex = Executor(oms=oms, broker=broker, pump=pump, clock=clock, bus=bus, resolver_grace=0.01)
    oms.set_executor(ex)
    gate = RiskGate(env=Env.SIMULATE, limits=LOOSE_LIMITS, oms=oms, executor=ex, md=broker, store=store, bus=bus, clock=clock,
                    stale_after_seconds=3600)  # fmt: skip
    eng = RuleEngine(gate=gate, oms=oms, store=store, md=broker, bus=bus, clock=clock, pump=pump, threaded_actions=True)
    return clock, broker, store, oms, pump, gate, eng


def wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_no_deadlock_between_gate_lock_and_event_thread(monkeypatch):
    clock, broker, store, oms, pump, gate, eng = threaded_system()
    pump.start()
    eng.start()
    try:
        oms.recover()
        assert eng.add(RULE, actor="cli:test").allowed
        broker.push_quote(SYMBOL, last=D("100.21"), open=D("99.99"))
        cid = store.get_rule("spy-oco-1").entry_cid
        assert wait_until(lambda: oms.get(cid).broker_order_id is not None)
        broker.step()
        broker.fill(oms.get(cid).broker_order_id, 10, D("100.21"))
        assert wait_until(lambda: eng.status("spy-oco-1") is RuleStatus.ARMED)

        original = oms.orders_sent_today
        fired = threading.Event()

        def hooked():  # 在闸门锁里：让规则此刻触发，事件线程随即需要闸门
            if not fired.is_set():
                fired.set()
                broker.push_quote(SYMBOL, last=D("95.00"), open=D("99.99"))
                time.sleep(0.3)
            return original()

        monkeypatch.setattr(oms, "orders_sent_today", hooked)
        req = OrderRequest(new_client_order_id(), SYMBOL, Side.BUY, 1, OrderKind.LIMIT, D("100.21"))
        done = threading.Event()
        t = threading.Thread(target=lambda: (gate.submit(Intent(Source.CLI, "place", request=req, actor="cli")), done.set()), daemon=True)
        t.start()
        assert done.wait(5.0), "闸门锁与事件线程死锁"
        assert wait_until(lambda: any(c[0].side is Side.SELL for c in broker.calls("place_order")))
    finally:
        eng.stop()
        pump.stop()


# ------------------------------------------------------------------ #2 保护性卖单被撤销（例如 halt）后重试


def test_protective_sell_cancelled_by_halt_is_retried_after_resume():
    sys, eng = armed()
    push(sys, "95.00")
    (s1,) = sells(sys)
    sys.broker.step()  # 卖单 NEW
    sys.pump.drain()
    sys.gate.halt("测试", actor="cli:test")  # 撤掉全部挂单，包括这张保护性卖单
    sys.broker.step()
    sys.pump.drain()
    assert sys.oms.get(s1.client_order_id).status.value == "CANCELED"
    assert eng.status("spy-oco-1") is RuleStatus.TRIGGERED
    sys.gate.resume(actor="cli:test")
    sys.pump.drain()
    push(sys, "94.90")
    assert len(sells(sys)) == 2 and sells(sys)[1].qty == 10


# ------------------------------------------------------------------ #3 闸门抛异常


def test_gate_exception_on_sell_counts_as_failure_and_retries(monkeypatch):
    sys, eng = armed()
    real = sys.gate.submit
    state = {"raised": False}

    def boom(intent):
        if intent.request is not None and intent.request.side is Side.SELL and not state["raised"]:
            state["raised"] = True
            raise StoreUnavailable("数据库不可用（测试）")
        return real(intent)

    monkeypatch.setattr(sys.gate, "submit", boom)
    push(sys, "95.00")
    assert sells(sys) == []
    assert any(e.kind == "sell_rejected" for e in sys.store.rule_events("spy-oco-1"))
    push(sys, "94.90")
    assert len(sells(sys)) == 1


def test_gate_exception_on_add_voids_rule(monkeypatch):
    sys, eng = setup()

    def boom(intent):
        raise BrokerError("测试")

    monkeypatch.setattr(sys.gate, "submit", boom)
    r = eng.add(RULE, actor="cli:test")
    assert not r.allowed and "BrokerError" in r.reason
    assert eng.status("spy-oco-1") is RuleStatus.VOID
    assert sys.store.active_rule_for(SYMBOL) is None


# ------------------------------------------------------------------ #4 恢复时按闸门当前状态设置暂停


def test_paused_state_not_stuck_after_restart():
    sys1, eng1 = setup()
    eng1.add(RULE, actor="cli:test")
    push(sys1, "100.21")
    fill_entry(sys1, eng1, 4)
    sys1.gate.halt("测试", actor="cli:test")  # 撤掉入场单余量；入场单结束时状态以 paused=True 写库
    sys1.broker.step()
    sys1.pump.drain()
    assert eng1.rule("spy-oco-1").state["paused"] is True
    sys1.gate.resume(actor="cli:test")
    sys1.pump.drain()
    sys2, eng2 = restart(sys1)
    sys2.broker.push_quote(SYMBOL, last=D("95.00"), open=D("99.99"))
    sys2.pump.drain()
    assert len(sells(sys2)) == 1 and sells(sys2)[0].qty == 4


# ------------------------------------------------------------------ #5 OMS 就绪之前的动作先排队


def test_actions_deferred_until_oms_ready():
    sys1, eng1 = setup()
    eng1.add(RULE, actor="cli:test")
    push(sys1, "100.21")
    fill_entry(sys1, eng1, 4)
    push(sys1, "95.00")  # 触发：卖 4、撤余量
    # 撤单生效前又成交 2 股；OMS 记了账，但引擎还没处理这条 OrderChanged 进程就被杀
    eng1._on_order_changed = lambda o: None  # type: ignore[method-assign]
    sys1.broker.fill(entry(sys1, eng1).broker_order_id, 2, D("100.21"))
    sys1.pump.drain()
    assert sys1.oms.get(eng1.rule("spy-oco-1").entry_cid).cum_qty == 6
    assert eng1.rule("spy-oco-1").state["bought"] == 4
    n_before = len(sells(sys1))
    sys2, eng2 = restart(sys1)
    events = sys2.store.rule_events("spy-oco-1")
    assert not any("not_ready" in e.message for e in events), [e.message for e in events]
    assert len(sells(sys2)) == n_before + 1 and sells(sys2)[-1].qty == 2
    assert eng2.rule("spy-oco-1").state["failures"] == 0


# ------------------------------------------------------------------ #6 只订阅一次；不再需要时退订


def test_subscribe_once_per_symbol_and_unsubscribe_when_idle():
    sys, eng = setup()
    eng.add(RULE, actor="cli:test")
    eng.cancel("spy-oco-1", actor="cli:test")
    sys.pump.drain()
    eng.add(RULE.replace("spy-oco-1", "spy-oco-2"), actor="cli:test")
    sys.pump.drain()
    assert len(sys.broker.calls("subscribe_quotes")) == 1
    eng.cancel("spy-oco-2", actor="cli:test")
    sys.pump.drain()
    sys.clock.advance(61)  # 订阅满 1 分钟（vendor § Subscribe and Unsubscribe）
    eng.poll_stale_quotes()
    sys.pump.drain()
    assert sys.broker.calls("unsubscribe_quotes") == [((SYMBOL,),)]


# ------------------------------------------------------------------ #7 时段缓存跨过 9:30 / 16:00 立即失效；失败也缓存


def test_market_state_cache_invalidated_at_close():
    clock = FakeClock(datetime(2026, 11, 16, 20, 59, 50, tzinfo=UTC))  # 美东 15:59:50
    sys, eng = armed(clock=clock)
    clock.advance(20)  # 美东 16:00:10
    sys.broker.set_market_state(SYMBOL, "AFTER_HOURS_BEGIN")
    push(sys, "94.00")
    # 闸门本来也会拒绝盘后的卖单；这里要证明的是引擎根本没有按盘后价格触发
    assert not any(e.kind == "triggered" for e in sys.store.rule_events("spy-oco-1"))
    assert eng.status("spy-oco-1") is RuleStatus.ARMED


def test_market_state_failure_cached_briefly(monkeypatch):
    sys, eng = armed()
    sys.clock.advance(31)
    calls = {"n": 0}

    def failing(symbol):
        calls["n"] += 1
        raise BrokerError("get_market_state 失败（测试）")

    monkeypatch.setattr(sys.broker, "market_state", failing)
    for p in ("100.00", "100.01", "100.02"):
        push(sys, p)
    assert calls["n"] == 1


# ------------------------------------------------------------------ #8（审查第 9 条）兜底失败的报警限频


def test_fallback_failure_alert_throttled(monkeypatch):
    sys, eng = armed()

    def failing(symbols):
        raise BrokerError("get_market_snapshot 失败（测试）")

    monkeypatch.setattr(sys.broker, "snapshot", failing)
    for _ in range(10):
        sys.clock.advance(6)
        eng.poll_stale_quotes()
        sys.pump.drain()
    assert sys.alert_kinds().count("quote_fallback_failed") == 1  # 60 秒内只报一次


# ------------------------------------------------------------------ #9（审查第 10 条）加载失败的规则


def test_unloadable_rule_marked_error_and_cancellable():
    sys1, eng1 = setup()
    eng1.add(RULE, actor="cli:test")
    sys1.pump.drain()
    # 模拟：以后 Schema 或白名单变了，库里保存的规则加载不了（Store 没有改 spec_yaml 的接口，测试直接改 MemoryStore 内部）
    from dataclasses import replace

    sys1.store._s.rules["spy-oco-1"] = replace(sys1.store._s.rules["spy-oco-1"], spec_yaml="id: spy-oco-1\nsymbol: US.SPY\n")
    sys2, eng2 = restart(sys1)
    assert eng2.status("spy-oco-1") is RuleStatus.ERROR
    assert "rule_error" in sys2.alert_kinds()
    assert sys2.store.active_rule_for(SYMBOL) is None


def test_cancel_unloaded_active_rule_marks_canceled():
    sys1, eng1 = setup()
    eng1.add(RULE, actor="cli:test")
    sys1.pump.drain()
    sys2 = make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock)
    eng2 = engine_for(sys2)  # 没有调用 recover：规则在库里是活动的，但没加载
    r = eng2.cancel("spy-oco-1", actor="cli:test")
    assert r["status"] == "CANCELED"
    assert sys2.store.active_rule_for(SYMBOL) is None


# ------------------------------------------------------------------ 小问题：深拷贝；取消后的入场成交报警


def test_persisted_order_counts_follow_fills():
    sys, eng = setup()
    eng.add(RULE, actor="cli:test")
    push(sys, "100.21")
    fill_entry(sys, eng, 4)
    fill_entry(sys, eng, 3)
    rec = sys.store.get_rule("spy-oco-1")
    assert rec.state["orders"][rec.entry_cid]["qty"] == 7


def test_fill_after_cancel_alerts_unprotected():
    sys, eng = setup()
    eng.add(RULE, actor="cli:test")
    push(sys, "100.21")
    fill_entry(sys, eng, 4)
    eng.cancel("spy-oco-1", actor="cli:test")
    sys.broker.fill(entry(sys, eng).broker_order_id, 2, D("100.21"))  # 撤单生效前又成交
    sys.pump.drain()
    assert "rule_unprotected_fill" in sys.alert_kinds()


@pytest.fixture(autouse=True)
def _no_leaked_threads():
    before = {t.name for t in threading.enumerate()}
    yield
    time.sleep(0.05)
    leaked = {t.name for t in threading.enumerate() if t.is_alive()} - before
    assert not {n for n in leaked if n.startswith("mooapp-")}, leaked
