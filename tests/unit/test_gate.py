"""闸门的基本行为（M3 写的骨架测试，M4 起闸门换成完整规则后按新行为更新）。限额规则见 test_risk_rules.py、test_gate_m4.py。"""

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


def test_real_refused():
    sys = make_system(env=Env.REAL)
    d = sys.gate.submit(Intent(source=Source.CLI, action="place", request=req()))
    assert not d.allowed and d.rule == "real_disabled"  # M4 确认点 4：REAL 仍关闭
    assert sys.broker.calls("place_order") == []


def test_not_whitelisted_refused():
    sys = make_system()
    d = sys.gate.submit(Intent(source=Source.CLI, action="place", request=req("US.AAPL")))
    assert not d.allowed and d.rule == "whitelist"
    assert sys.broker.calls("place_order") == []


def test_halt_refuses_new_orders_but_not_cancel(monkeypatch):
    sys = make_system()
    d = sys.place(qty=1)
    sys.broker.step()
    sys.pump.drain()
    real_cancel = sys.broker.cancel_order
    calls = []

    def fail_first(bid):  # halt 时这张单撤单失败，halt 之后手动再撤
        calls.append(bid)
        if len(calls) == 1:
            from mooapp.broker.port import BrokerError

            raise BrokerError("modify_order(CANCEL) 失败：测试")
        return real_cancel(bid)

    monkeypatch.setattr(sys.broker, "cancel_order", fail_first)
    sys.gate.halt("测试", actor="test")
    assert sys.gate.halted
    d2 = sys.place(qty=1)
    assert not d2.allowed and d2.rule == "halt"
    d3 = sys.cancel(d.order.client_order_id)
    assert d3.allowed  # ADR-0016：撤单不受 halt 限制
    assert len(calls) == 2


def test_cancel_unknown_order_refused():
    sys = make_system()
    d = sys.cancel(new_client_order_id())
    assert not d.allowed and d.rule == "not_own_active_order"


def test_place_intent_without_request_refused():
    sys = make_system()
    d = sys.gate.submit(Intent(source=Source.CLI, action="place"))
    assert not d.allowed and d.rule == "bad_intent"


def test_gate_takes_limits_object():
    """M4：闸门接收整份 RiskLimits（只读）；不再单独传白名单。"""
    import inspect

    from mooapp.risk.gate import RiskGate

    params = set(inspect.signature(RiskGate.__init__).parameters)
    assert "limits" in params and "whitelist" not in params
