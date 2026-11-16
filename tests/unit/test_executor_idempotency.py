"""幂等与超时重发（M3 计划第 4 节；SPEC 8 节 M3 验收 4 的 FakeBroker 版）。

- 同一个 client_order_id 提交多次，券商端只有 1 张单
- place_order 超时：先查询（refresh_cache=True），查到就不重发；连续 2 次查不到才按 T55 用同一 cid 重发，最多 3 次
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from oms_harness import make_system

from mooapp.broker.port import BrokerError, BrokerUnavailable, OrderRejected, new_client_order_id
from mooapp.oms.state import OrdStatus

S = OrdStatus


def broker_orders(sys, cid):
    return [o for o in sys.broker.query_orders(open_only=False) if o.client_order_id == cid]


def refresh_queries(sys) -> int:
    return sum(1 for args in sys.broker.calls("query_orders") if args[1] is True)


def test_same_cid_three_times_one_broker_order():
    sys = make_system()
    cid = new_client_order_id()
    results = [sys.place(cid=cid) for _ in range(3)]
    assert all(d.allowed for d in results), [d.reason for d in results]
    assert {d.order.client_order_id for d in results} == {cid}
    assert len(sys.broker.calls("place_order")) == 1
    assert len(broker_orders(sys, cid)) == 1


def test_same_cid_after_fill_not_resent():
    sys = make_system()
    cid = new_client_order_id()
    sys.place(cid=cid)
    sys.broker.step()
    sys.broker.fill("900001", 100, Decimal("10"))
    sys.pump.drain()
    d = sys.place(cid=cid)
    assert d.order.status is S.FILLED
    assert len(sys.broker.calls("place_order")) == 1


def test_timeout_but_accepted_is_found_and_not_resent():
    sys = make_system(attach=False)  # 推送也丢了，只能靠查询
    sys.broker.fail_next_place(BrokerUnavailable("place_order 超时"), accepted=True)
    cid = new_client_order_id()
    d = sys.place(cid=cid)
    assert d.allowed
    o = sys.oms.get(cid)
    assert o.status is S.UNKNOWN, "SUBMITTING 不产生事件，订单在 UNKNOWN 等确认"
    assert o.broker_order_id == "900001"
    assert o.attempts == 1
    assert len(sys.broker.calls("place_order")) == 1
    assert refresh_queries(sys) >= 1
    sys.broker.step()
    sys.poller.poll_once()
    sys.pump.drain()
    assert sys.oms.get(cid).status is S.NEW  # T18
    assert len(broker_orders(sys, cid)) == 1


def test_timeout_not_accepted_resends_with_same_cid():
    sys = make_system()
    sys.broker.fail_next_place(BrokerUnavailable("place_order 超时"), accepted=False)
    cid = new_client_order_id()
    d = sys.place(cid=cid)
    o = sys.oms.get(cid)
    assert o.status is S.PENDING_NEW and o.attempts == 2
    assert len(sys.broker.calls("place_order")) == 2
    assert [c[0].client_order_id for c in sys.broker.calls("place_order")] == [cid, cid]
    assert len(broker_orders(sys, cid)) == 1
    assert refresh_queries(sys) >= 2, "确认不存在要查 2 次"
    assert "resubmit" in sys.alert_kinds()
    assert any(r.event_type == "RESUBMIT" for r in sys.store.events_for(cid))
    assert d.order.client_order_id == cid


def test_grace_period_waited_before_query():
    sys = make_system()
    sys.broker.fail_next_place(BrokerUnavailable("超时"), accepted=False)
    t0 = sys.clock.monotonic()
    sys.place()
    assert sys.clock.monotonic() - t0 >= 3.0


def test_always_lost_gives_up_after_three_attempts():
    sys = make_system()
    sys.broker.fail_next_place(BrokerUnavailable("超时"), accepted=False, times=10)
    cid = new_client_order_id()
    sys.place(cid=cid)
    o = sys.oms.get(cid)
    assert o.status is S.REJECTED and o.attempts == 3
    assert len(sys.broker.calls("place_order")) == 3
    assert "resolve_failed" in sys.alert_kinds()
    assert broker_orders(sys, cid) == []


def test_timeout_on_resend_but_accepted_stops_resending():
    sys = make_system()
    sys.broker.fail_next_place(BrokerUnavailable("超时"), accepted=False)
    sys.broker.fail_next_place(BrokerUnavailable("超时"), accepted=True)
    cid = new_client_order_id()
    sys.place(cid=cid)
    assert len(sys.broker.calls("place_order")) == 2
    assert len(broker_orders(sys, cid)) == 1
    assert sys.oms.get(cid).status is S.UNKNOWN


def test_retry_command_while_unknown_does_not_resend():
    sys = make_system(attach=False)
    sys.broker.fail_next_place(BrokerUnavailable("超时"), accepted=True)
    cid = new_client_order_id()
    sys.place(cid=cid)
    sys.place(cid=cid)
    sys.place(cid=cid)
    assert len(sys.broker.calls("place_order")) == 1


def test_broker_rejection_is_exch_reject():
    sys = make_system()
    sys.broker.fail_next_place(OrderRejected("place_order 失败：资金不足"), accepted=False)
    cid = new_client_order_id()
    d = sys.place(cid=cid)
    o = sys.oms.get(cid)
    assert o.status is S.REJECTED
    assert "资金不足" in (o.reason or "")
    assert len(sys.broker.calls("place_order")) == 1
    assert d.order.status is S.REJECTED


# ------------------------------------------------------------------ 撤单


def test_cancel_rejected_by_broker_goes_back_and_raises():
    # 推送全丢（attach=False）：本地靠轮询到 NEW；券商随后成交，本地不知道，撤单被券商拒绝
    sys = make_system(attach=False)
    cid = new_client_order_id()
    sys.place(cid=cid)
    sys.broker.step()
    sys.poller.poll_once()
    sys.pump.drain()
    assert sys.oms.get(cid).status is S.NEW
    sys.broker.fill("900001", 100, Decimal("10"))
    with pytest.raises(BrokerError):
        sys.cancel(cid)
    assert any(r.event_type == "CANCEL_REJECT" and r.outcome == "applied" for r in sys.store.events_for(cid))
    assert sys.oms.get(cid).status is S.NEW  # T38 回到挂撤单前的状态
    sys.poller.poll_once()
    sys.pump.drain()
    assert sys.oms.get(cid).status is S.FILLED


def test_cancel_of_terminal_order_refused_without_broker_call():
    sys = make_system()
    cid = new_client_order_id()
    sys.place(cid=cid)
    sys.broker.step()
    sys.broker.fill("900001", 100, Decimal("10"))
    sys.pump.drain()
    d = sys.cancel(cid)
    assert not d.allowed
    assert sys.broker.calls("cancel_order") == []
