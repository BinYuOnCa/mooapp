"""闸门骨架（M3 计划第 5 节）。M3 只检查：REAL 一律拒绝、OMS 未就绪、halt（内存）、白名单；限额规则在 M4。"""

from __future__ import annotations

from decimal import Decimal

from oms_harness import make_system

from mooapp.broker.port import OrderRequest, new_client_order_id
from mooapp.common import Env, OrderKind, Side, Source
from mooapp.risk.gate import Intent


def req(symbol="US.SPY"):
    return OrderRequest(new_client_order_id(), symbol, Side.BUY, 1, OrderKind.LIMIT, Decimal("10"))


def test_place_allowed_returns_order():
    sys = make_system()
    d = sys.place(qty=1)
    assert d.allowed and d.order is not None and d.rule is None


def test_real_refused_in_m3():
    sys = make_system(env=Env.REAL)
    d = sys.gate.submit(Intent(source=Source.CLI, action="place", request=req()))
    assert not d.allowed and d.rule == "real_not_in_m3"
    assert sys.broker.calls("place_order") == []


def test_not_whitelisted_refused():
    sys = make_system()
    d = sys.gate.submit(Intent(source=Source.CLI, action="place", request=req("US.AAPL")))
    assert not d.allowed and d.rule == "whitelist"
    assert sys.broker.calls("place_order") == []


def test_halt_refuses_new_orders_but_not_cancel():
    sys = make_system()
    d = sys.place(qty=1)
    sys.broker.step()
    sys.pump.drain()
    sys.gate.halt("测试", actor="test")
    assert sys.gate.halted
    d2 = sys.place(qty=1)
    assert not d2.allowed and d2.rule == "halt"
    d3 = sys.cancel(d.order.client_order_id)
    assert d3.allowed  # ADR-0016：撤单不受 halt 限制


def test_cancel_unknown_order_refused():
    sys = make_system()
    d = sys.cancel(new_client_order_id())
    assert not d.allowed and d.rule == "not_own_active_order"


def test_place_intent_without_request_refused():
    sys = make_system()
    d = sys.gate.submit(Intent(source=Source.CLI, action="place"))
    assert not d.allowed and d.rule == "bad_intent"


def test_limits_untouched():
    """骨架不读也不改风控限额：RiskGate 构造参数里只有白名单。"""
    import inspect

    from mooapp.risk.gate import RiskGate

    params = set(inspect.signature(RiskGate.__init__).parameters)
    assert params == {"self", "env", "whitelist", "oms", "executor"}
