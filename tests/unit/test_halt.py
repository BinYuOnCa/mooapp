"""Kill switch（M4 计划设计要点；ADR-0016、0019、0022、0026、0015）。

- halt：持久化；拒绝一切新单；撤销本程序全部活动订单（未确认的打「待撤」标记）；不碰外部订单；REAL 时上锁
- resume：只能在交互终端，确认码正确才解除（交互检查在 ops/humanlock.py，见 test_humanlock.py）
"""

from __future__ import annotations

from decimal import Decimal

from oms_harness import make_system

from mooapp.broker.port import BrokerError, BrokerOrder
from mooapp.common import Env, OrderKind, Side
from mooapp.oms.state import OrdStatus
from mooapp.oms.store import StoreUnavailable
from mooapp.risk.gate import HaltChanged

S = OrdStatus


def three_working(sys) -> list[str]:
    cids = [sys.place(qty=1).order.client_order_id for _ in range(3)]
    sys.broker.step()
    sys.pump.drain()
    assert all(sys.oms.get(c).status is S.NEW for c in cids)
    return cids


def test_halt_cancels_all_and_rejects_new():
    """验收 3 的 FakeBroker 版：3 张挂单 → halt → 全部撤销，新单被拒。"""
    sys = make_system()
    cids = three_working(sys)
    report = sys.gate.halt("测试", actor="cli:test")
    assert sorted(report.cancel_sent) == sorted(cids)
    assert report.failed == {} and report.deferred == []
    sys.broker.step()
    sys.pump.drain()
    assert all(sys.oms.get(c).status is S.CANCELED for c in cids)
    d = sys.place(qty=1)
    assert not d.allowed and d.rule == "halt"


def test_halt_is_persisted_and_survives_restart():
    sys = make_system()
    sys.gate.halt("测试", actor="cli:test")
    sys2 = make_system(store=sys.store, broker=sys.broker, clock=sys.clock)
    assert sys2.gate.halted
    assert sys2.place(qty=1).rule == "halt"


def test_resume_clears_halt_and_is_audited():
    sys = make_system()
    sys.gate.halt("测试", actor="cli:test")
    sys.gate.resume(actor="cli:test")
    assert not sys.gate.halted
    assert sys.place(qty=1).allowed
    kinds = [r.action for r in sys.store.audit_records()]
    assert "halt" in kinds and "resume" in kinds
    sys3 = make_system(store=sys.store, broker=sys.broker, clock=sys.clock)
    assert not sys3.gate.halted


def test_halt_publishes_event():
    sys = make_system()
    got = []
    sys.bus.subscribe(HaltChanged, got.append)
    sys.gate.halt("测试", actor="cli:test")
    sys.gate.resume(actor="cli:test")
    assert [e.halted for e in got] == [True, False]


def test_halt_defers_unacknowledged_orders():
    sys = make_system(attach=False)  # 推送丢失：订单停在 PENDING_NEW
    cid = sys.place(qty=1).order.client_order_id
    assert sys.oms.get(cid).status is S.PENDING_NEW
    report = sys.gate.halt("测试", actor="cli:test")
    assert report.deferred == [cid]
    assert sys.broker.calls("cancel_order") == []
    assert sys.oms.get(cid).cancel_pending
    sys.broker.step()  # 券商确认
    sys.poller.poll_once()
    sys.pump.drain()
    assert sys.broker.calls("cancel_order") == [("900001",)]
    sys.broker.step()
    sys.poller.poll_once()
    sys.pump.drain()
    assert sys.oms.get(cid).status is S.CANCELED


def test_halt_leaves_external_orders_alone():
    sys = make_system()
    now = sys.clock.now()
    sys.broker.seed_order(
        BrokerOrder("555", None, "US.SPY", Side.BUY, OrderKind.LIMIT, 1, Decimal("1"), "SUBMITTED", 0, Decimal(0), "", now, now, "")
    )
    three_working(sys)
    sys.gate.halt("测试", actor="cli:test")
    assert ("555",) not in sys.broker.calls("cancel_order")


def test_halt_reports_cancel_failures_and_stays_halted(monkeypatch):
    sys = make_system()
    cids = three_working(sys)
    real_cancel = sys.broker.cancel_order

    def flaky(bid):
        if bid == sys.oms.get(cids[1]).broker_order_id:
            raise BrokerError("modify_order(CANCEL) 失败：测试")
        return real_cancel(bid)

    monkeypatch.setattr(sys.broker, "cancel_order", flaky)
    report = sys.gate.halt("测试", actor="cli:test")
    assert set(report.failed) == {cids[1]}
    assert sorted(report.cancel_sent) == sorted([cids[0], cids[2]])
    assert sys.gate.halted
    assert "halt_cancel_failed" in sys.alert_kinds()


def test_halt_proceeds_when_store_down(monkeypatch):
    sys = make_system()
    cids = three_working(sys)

    def down(*a, **k):
        raise StoreUnavailable("数据库不可用（测试）")

    monkeypatch.setattr(sys.store, "set_kv", down)
    report = sys.gate.halt("测试", actor="cli:test")
    assert sys.gate.halted
    assert sorted(report.cancel_sent) == sorted(cids)
    assert "halt_not_persisted" in sys.alert_kinds()


def test_halt_locks_real_broker():
    sys = make_system(env=Env.REAL)
    sys.gate.halt("测试", actor="cli:test")
    assert sys.broker.calls("lock")


def test_halt_is_idempotent():
    sys = make_system()
    three_working(sys)
    sys.gate.halt("第一次", actor="cli:test")
    report = sys.gate.halt("第二次", actor="cli:test")
    assert report.cancel_sent == []  # 已在撤单中，不重复发


def test_halt_does_not_touch_terminal_orders():
    sys = make_system()
    cid = sys.place(qty=1).order.client_order_id
    sys.broker.step()
    sys.broker.fill("900001", 1, Decimal("10"))
    sys.pump.drain()
    report = sys.gate.halt("测试", actor="cli:test")
    assert cid not in report.cancel_sent and sys.broker.calls("cancel_order") == []


def test_halt_reason_kept_for_status():
    sys = make_system()
    sys.gate.halt("盘中异常", actor="cli:test")
    st = sys.gate.halt_state()
    assert st is not None and st.reason == "盘中异常" and st.actor == "cli:test"
    assert st.since == sys.clock.now()
