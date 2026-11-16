"""M6b 默认做法 2：停机重启后，规则从数据库恢复并继续监控；停机期间的成交经 OrderChanged 补喂、不重复计。

启动顺序（与 Runtime.start 一致）：引擎先从快照恢复规则 → OMS 启动恢复（产生 OrderChanged）→ 处理事件。
"""

from __future__ import annotations

from decimal import Decimal

from oms_harness import make_system

from mooapp.common import Side
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


def first_run():
    sys = make_system()
    sys.market_open(last=D("100.21"))
    eng = engine_for(sys)
    assert eng.add(RULE, actor="cli:test").allowed
    sys.broker.push_quote(SYMBOL, last=D("100.21"), open=D("99.99"))
    sys.pump.drain()
    return sys, eng


def restart(sys1):
    """同一个库、同一个券商；新进程。"""
    sys2 = make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock, recover=False)
    eng2 = engine_for(sys2)
    eng2.recover()
    sys2.oms.recover()
    sys2.pump.drain()
    return sys2, eng2


def sells(sys):
    return [c[0] for c in sys.broker.calls("place_order") if c[0].side is Side.SELL]


def test_armed_rule_restored_and_keeps_monitoring():
    sys1, eng1 = first_run()
    o = sys1.oms.get(eng1.rule("spy-oco-1").entry_cid)
    sys1.broker.step()
    sys1.broker.fill(o.broker_order_id, 10, D("100.21"))
    sys1.pump.drain()
    assert eng1.status("spy-oco-1") is RuleStatus.ARMED
    n_sub = len(sys1.broker.calls("subscribe_quotes"))

    sys2, eng2 = restart(sys1)
    assert eng2.status("spy-oco-1") is RuleStatus.ARMED
    st = eng2.rule("spy-oco-1").state
    assert (st["stop_price"], st["take_price"], st["bought"]) == ("95.00", "105.22", 10)
    assert len(sys1.broker.calls("subscribe_quotes")) > n_sub  # 重新订阅
    sys2.broker.push_quote(SYMBOL, last=D("95.00"), open=D("99.99"))
    sys2.pump.drain()
    (s,) = sells(sys2)
    assert s.qty == 10


def test_fills_during_downtime_fed_once():
    sys1, eng1 = first_run()
    o = sys1.oms.get(eng1.rule("spy-oco-1").entry_cid)
    sys1.broker.step()
    sys1.pump.drain()
    # 进程被杀：之后的推送没人处理；券商端入场单成交
    sys1.broker.fill(o.broker_order_id, 10, D("100.21"))
    sys2, eng2 = restart(sys1)
    st = eng2.rule("spy-oco-1").state
    assert st["bought"] == 10
    assert eng2.status("spy-oco-1") is RuleStatus.ARMED
    # 同一成交再来一次（例如推送重放）不重复计
    sys2.poller.poll_once()
    sys2.pump.drain()
    assert eng2.rule("spy-oco-1").state["bought"] == 10


def test_closed_rules_not_restored_as_active():
    sys1, eng1 = first_run()
    eng1.cancel("spy-oco-1", actor="cli:test")
    sys1.pump.drain()
    sys2, eng2 = restart(sys1)
    assert eng2.status("spy-oco-1") is RuleStatus.CANCELED
    assert sys2.store.active_rule_for(SYMBOL) is None


def test_rule_events_kept_across_restart():
    sys1, eng1 = first_run()
    n = len(sys1.store.rule_events("spy-oco-1"))
    assert n >= 1
    sys2, _ = restart(sys1)
    assert len(sys2.store.rule_events("spy-oco-1")) >= n
