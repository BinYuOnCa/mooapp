"""ADR-0026：订单确认之前收到的撤单 —— 记「待撤」标记，确认后自动发出；确认前就结束则清除标记。"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from oms_harness import make_system

from mooapp.broker.port import BrokerUnavailable
from mooapp.oms.state import OrdStatus

S = OrdStatus


def pending_new(sys) -> str:
    cid = sys.place(qty=10).order.client_order_id
    assert sys.oms.get(cid).status is S.PENDING_NEW
    return cid


def snap(sys, **changes):
    s = sys.broker.query_orders(open_only=False)[0]
    return replace(s, **changes)


def test_cancel_on_pending_new_is_deferred_until_ack():
    sys = make_system(attach=False)
    cid = pending_new(sys)
    d = sys.cancel(cid)
    assert d.allowed
    o = sys.oms.get(cid)
    assert o.status is S.PENDING_NEW and o.cancel_pending
    assert sys.broker.calls("cancel_order") == []
    assert any(r.event_type == "CANCEL_DEFERRED" and r.origin == "local" for r in sys.store.events_for(cid))
    sys.broker.step()
    sys.poller.poll_once()
    sys.pump.drain()
    o = sys.oms.get(cid)
    assert o.status is S.PENDING_CANCEL and not o.cancel_pending
    assert sys.broker.calls("cancel_order") == [("900001",)]
    sys.broker.step()
    sys.poller.poll_once()
    sys.pump.drain()
    assert sys.oms.get(cid).status is S.CANCELED


def test_cancel_on_unknown_is_deferred():
    sys = make_system(attach=False)
    sys.broker.fail_next_place(BrokerUnavailable("超时"), accepted=True)
    cid = sys.place(qty=10).order.client_order_id
    assert sys.oms.get(cid).status is S.UNKNOWN
    assert sys.cancel(cid).allowed
    assert sys.oms.get(cid).cancel_pending
    sys.broker.step()
    sys.poller.poll_once()
    sys.pump.drain()
    assert sys.oms.get(cid).status is S.PENDING_CANCEL
    assert len(sys.broker.calls("cancel_order")) == 1


def test_rejected_before_ack_clears_flag():
    sys = make_system(attach=False)
    cid = pending_new(sys)
    sys.cancel(cid)
    sys.pump.submit_snapshot(snap(sys, raw_status="FAILED", last_err_msg="测试拒单"), "push")
    sys.pump.drain()
    o = sys.oms.get(cid)
    assert o.status is S.REJECTED and not o.cancel_pending
    assert sys.broker.calls("cancel_order") == []
    assert any(r.event_type == "CANCEL_DEFERRED_CLEARED" for r in sys.store.events_for(cid))


def test_full_fill_before_ack_clears_flag_without_cancel():
    sys = make_system(attach=False)
    cid = pending_new(sys)
    sys.cancel(cid)
    sys.pump.submit_snapshot(snap(sys, raw_status="FILLED_ALL", dealt_qty=10, dealt_avg_price=Decimal("10")), "push")
    sys.pump.drain()
    o = sys.oms.get(cid)
    assert o.status is S.FILLED and not o.cancel_pending
    assert sys.broker.calls("cancel_order") == []


def test_partial_fill_before_ack_sends_cancel():
    sys = make_system(attach=False)
    cid = pending_new(sys)
    sys.cancel(cid)
    sys.pump.submit_snapshot(snap(sys, raw_status="FILLED_PART", dealt_qty=4, dealt_avg_price=Decimal("10")), "push")
    sys.pump.drain()
    o = sys.oms.get(cid)
    assert o.status is S.PENDING_CANCEL and o.cum_qty == 4
    assert len(sys.broker.calls("cancel_order")) == 1


def test_second_cancel_while_deferred_is_noop():
    sys = make_system(attach=False)
    cid = pending_new(sys)
    assert sys.cancel(cid).allowed
    assert sys.cancel(cid).allowed
    deferred = [r for r in sys.store.events_for(cid) if r.event_type == "CANCEL_DEFERRED"]
    assert len(deferred) == 1


def test_deferred_cancel_rejected_by_broker_alerts(monkeypatch):
    sys = make_system(attach=False)
    cid = pending_new(sys)
    sys.cancel(cid)

    def boom(bid):
        from mooapp.broker.port import BrokerError

        raise BrokerError("modify_order(CANCEL) 失败：测试")

    monkeypatch.setattr(sys.broker, "cancel_order", boom)
    sys.broker.step()
    sys.poller.poll_once()
    sys.pump.drain()
    assert sys.oms.get(cid).status is S.NEW  # T38
    assert "deferred_cancel_failed" in sys.alert_kinds()


def test_flag_persists_across_restart():
    sys = make_system(attach=False)
    cid = pending_new(sys)
    sys.cancel(cid)
    sys.broker.step()
    sys2 = make_system(store=sys.store, broker=sys.broker, clock=sys.clock, recover=False, attach=False)
    sys2.oms.recover()  # 恢复时看到 SUBMITTED → ACK → 发出待撤的撤单
    sys2.pump.drain()
    assert sys2.oms.get(cid).status is S.PENDING_CANCEL
