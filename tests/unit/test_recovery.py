"""启动恢复（M3 计划第 7 节；SPEC 8 节 M3 验收 3 的 FakeBroker 版）。

「下单后立刻杀掉程序」：第一个系统的推送队列不处理就丢弃；第二个系统用同一个 Store、同一个券商重新启动。
完成恢复之前，闸门拒绝新指令。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from oms_harness import make_system

from mooapp.broker.port import BrokerError, BrokerOrder, OrderRequest, new_client_order_id
from mooapp.common import OrderKind, Side, Source
from mooapp.oms.state import Event, EventType, OrdStatus

S, E = OrdStatus, EventType


def killed_after_place():
    sys1 = make_system()
    d = sys1.place(qty=100)
    cid = d.order.client_order_id
    # 进程被杀：此后 sys1 的队列不再处理。券商那边订单继续走
    sys1.broker.step()
    sys1.broker.fill("900001", 100, Decimal("10"))
    return sys1, cid


def test_not_ready_until_recovered():
    sys1, _ = killed_after_place()
    sys2 = make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock, recover=False)
    assert sys2.oms.ready is False
    d = sys2.place()
    assert not d.allowed and d.rule == "not_ready"
    assert len(sys1.broker.calls("place_order")) == 1
    sys2.oms.recover()
    assert sys2.oms.ready is True
    assert sys2.place().allowed


def test_restart_aligns_with_broker():
    sys1, cid = killed_after_place()
    sys2 = make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock, recover=False)
    report = sys2.oms.recover()
    o = sys2.oms.get(cid)
    assert (o.status, o.cum_qty, o.avg_px) == (S.FILLED, 100, Decimal("10"))
    assert sum(f.qty for f in sys2.store.fills_for(cid)) == 100
    assert any(r.origin == "poll" and r.outcome == "applied" for r in sys2.store.events_for(cid))
    assert report.orders_checked >= 1
    assert report.mismatches == []


def test_recovery_uses_fresh_queries():
    sys1, _ = killed_after_place()
    sys2 = make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock, recover=False)
    n_before = len(sys1.broker.calls("query_orders"))
    sys2.oms.recover()
    new_calls = sys1.broker.calls("query_orders")[n_before:]
    assert (False, True) in new_calls, "恢复时 query_orders(open_only=False, refresh=True)"
    assert sys1.broker.calls("query_positions")


def local_pending_new_without_broker(sys):
    req = OrderRequest(new_client_order_id(), "US.SPY", Side.BUY, 10, OrderKind.LIMIT, Decimal("10"))
    o = sys.pump.call(lambda: sys.oms.create_order(req, Source.CLI))
    sys.pump.call(lambda: sys.oms.handle_local(o.client_order_id, Event(E.SUBMIT)))
    return o.client_order_id


def test_local_pending_new_missing_at_broker_is_resolved_and_resent():
    sys1 = make_system()
    cid = local_pending_new_without_broker(sys1)  # 写库后、调用 place_order 之前被杀
    sys2 = make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock, recover=False)
    report = sys2.oms.recover()
    assert "missing_at_broker" in [m.kind for m in report.mismatches]
    assert "missing_at_broker" in sys2.alert_kinds()
    o = sys2.oms.get(cid)
    assert o.attempts == 2 and o.status is S.PENDING_NEW
    assert [c[0].client_order_id for c in sys1.broker.calls("place_order")] == [cid]


def test_local_working_order_missing_at_broker_alerts_only():
    sys1 = make_system()
    cid = local_pending_new_without_broker(sys1)
    sys1.pump.call(lambda: sys1.oms.handle_local(cid, Event(E.ACK)))
    sys2 = make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock, recover=False)
    report = sys2.oms.recover()
    assert sys2.oms.get(cid).status is S.NEW
    assert "missing_at_broker" in [m.kind for m in report.mismatches]
    assert sys1.broker.calls("place_order") == []
    assert {r.kind for r in sys2.store.recon_events()} >= {"missing_at_broker"}


def foreign(sys, remark: str, oid: str = "555") -> BrokerOrder:
    now = sys.clock.now()
    from mooapp.broker.port import parse_remark

    return BrokerOrder(oid, parse_remark(remark), "US.SPY", Side.BUY, OrderKind.LIMIT, 1, Decimal("1"), "SUBMITTED", 0, Decimal(0), "", now, now, remark)


def test_unknown_moo_order_at_broker_alerts():
    sys1 = make_system()
    sys1.broker.seed_order(foreign(sys1, "moo:" + new_client_order_id()))
    sys2 = make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock, recover=False)
    report = sys2.oms.recover()
    assert "unknown_client_order_id" in [m.kind for m in report.mismatches]
    assert "unknown_client_order_id" in sys2.alert_kinds()


def test_external_order_alerted_not_managed():
    sys1 = make_system()
    sys1.broker.seed_order(foreign(sys1, "", oid="556"))
    sys2 = make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock, recover=False)
    sys2.oms.recover()
    assert "external_order" in sys2.alert_kinds()
    assert sys2.oms.open_orders() == []
    assert sys1.broker.calls("cancel_order") == []


def test_position_shortfall_is_mismatch():
    sys1, cid = killed_after_place()
    sys2 = make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock)
    assert sys2.oms.get(cid).status is S.FILLED
    sys1.broker.set_position("US.SPY", 50, Decimal("10"))  # 券商只剩 50，本程序记账买入 100
    sys3 = make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock, recover=False)
    report = sys3.oms.recover()
    assert "position_mismatch" in [m.kind for m in report.mismatches]
    assert "position_mismatch" in sys3.alert_kinds()


def test_extra_broker_position_is_external_and_only_recorded():
    sys1 = make_system()
    sys1.broker.set_position("US.QQQ", 5, Decimal("400"))
    sys2 = make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock, recover=False)
    report = sys2.oms.recover()
    assert report.mismatches == []
    assert "external_position" in {r.kind for r in sys2.store.recon_events()}
    assert "external_position" not in sys2.alert_kinds()


def test_recovery_failure_keeps_not_ready(monkeypatch):
    sys1, _ = killed_after_place()
    sys2 = make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock, recover=False)

    def boom(*a, **k):
        raise BrokerError("order_list_query 失败：network error")

    monkeypatch.setattr(sys1.broker, "query_orders", boom)
    with pytest.raises(BrokerError):
        sys2.oms.recover()
    assert sys2.oms.ready is False
    assert not sys2.place().allowed


def test_simulate_recovery_notes_deals_unavailable():
    sys1, _ = killed_after_place()
    sys2 = make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock, recover=False)
    report = sys2.oms.recover()
    assert report.deals_source == "snapshot"  # 模拟盘不支持 deal_list_query（P4）


def test_report_timestamp_is_utc():
    sys = make_system(recover=False)
    report = sys.oms.recover()
    assert report.finished_at.tzinfo is not None and report.finished_at.utcoffset().total_seconds() == 0
    assert report.finished_at >= datetime(2026, 1, 1, tzinfo=UTC)
