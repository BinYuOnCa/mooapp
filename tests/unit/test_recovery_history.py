"""修复计划 2（PROGRESS「修复计划」；learnings「P9 第二次 --check」）：启动恢复对当日列表查不到的本地未完成订单改查历史订单。

实测（2026-11-16）：前一天的订单 order_list_query 查不到，只有 history_order_list_query 能查到；
DAY 单在当天盘后时段结束时过期为 CANCELLED_ALL。FakeBroker.roll_day() 模拟跨日。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from oms_harness import make_system

from mooapp.broker.port import BrokerError, OrderRequest, new_client_order_id
from mooapp.common import OrderKind, Side
from mooapp.oms.state import OrdStatus

S = OrdStatus
ONE_DAY = timedelta(days=1)


def working_order(sys) -> str:
    cid = sys.place(qty=1).order.client_order_id
    sys.broker.step()
    sys.pump.drain()
    assert sys.oms.get(cid).status is S.NEW
    return cid


def restart(sys1):
    """进程已退出（sys1 的推送队列不再处理），第二天用同一个库、同一个券商重新启动。"""
    return make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock)


# ------------------------------------------------------------------ FakeBroker.roll_day / query_order_history


def test_fake_roll_day_expires_open_orders_into_history():
    sys = make_system()
    cid = working_order(sys)
    bid = sys.oms.get(cid).broker_order_id
    sys.clock.advance(5 * 3600)
    sys.broker.roll_day()
    assert sys.broker.query_orders(open_only=False) == []
    hist = sys.broker.query_order_history(sys.clock.now() - 2 * ONE_DAY, sys.clock.now())
    assert [(o.broker_order_id, o.raw_status) for o in hist] == [(bid, "CANCELLED_ALL")]
    assert sys.broker.calls("query_order_history")[-1] == (sys.clock.now() - 2 * ONE_DAY, sys.clock.now())


def test_fake_history_filters_by_created_at():
    sys = make_system()
    working_order(sys)
    sys.broker.roll_day()
    now = sys.clock.now()
    assert sys.broker.query_order_history(now + ONE_DAY, now + 2 * ONE_DAY) == []


def test_fake_history_includes_todays_orders():
    """P9 实测：当天新下的单 history_order_list_query 也能查到。"""
    sys = make_system()
    cid = working_order(sys)
    hist = sys.broker.query_order_history(sys.clock.now() - ONE_DAY, sys.clock.now() + ONE_DAY)
    assert [o.client_order_id for o in hist] == [cid]


# ------------------------------------------------------------------ 启动恢复


def test_expired_previous_day_order_closed_from_history():
    sys1 = make_system()
    cid = working_order(sys1)
    sys1.clock.advance(18 * 3600)
    sys1.broker.roll_day()
    sys2 = restart(sys1)
    assert sys2.oms.get(cid).status is S.CANCELED
    assert "missing_at_broker" not in sys2.alert_kinds()
    assert len(sys2.broker.calls("query_order_history")) == 1


def test_filled_previous_day_order_closed_with_fill_from_history():
    sys1 = make_system()
    cid = working_order(sys1)
    sys1.broker.fill(sys1.oms.get(cid).broker_order_id, 1, Decimal("10.00"))  # 进程已退出，推送没人处理
    sys1.broker.roll_day()
    sys1.clock.advance(18 * 3600)
    sys2 = restart(sys1)
    o = sys2.oms.get(cid)
    assert o.status is S.FILLED and o.cum_qty == 1
    assert "missing_at_broker" not in sys2.alert_kinds()


def test_history_window_covers_oldest_missing_order_with_margin():
    """vendor § Get Historical Orders 没写清 start/end 的时区：窗口前后各放宽 1 天，再按订单号匹配。"""
    sys1 = make_system()
    cid = working_order(sys1)
    created = sys1.oms.get(cid).created_at
    sys1.clock.advance(40 * 3600)
    sys1.broker.roll_day()
    sys2 = restart(sys1)
    start, end = sys2.broker.calls("query_order_history")[0]
    assert start <= created - ONE_DAY + timedelta(seconds=1)
    assert end >= sys2.clock.now() + ONE_DAY - timedelta(seconds=1)


def test_not_in_history_either_still_missing_at_broker(monkeypatch):
    sys1 = make_system()
    cid = working_order(sys1)
    sys1.broker.roll_day()
    monkeypatch.setattr(sys1.broker, "query_order_history", lambda start, end: [])
    sys2 = restart(sys1)
    assert sys2.oms.get(cid).status is S.NEW
    assert "missing_at_broker" in sys2.alert_kinds()


def test_history_query_error_alerts_and_recovery_completes(monkeypatch):
    sys1 = make_system()
    cid = working_order(sys1)
    sys1.broker.roll_day()

    def down(start, end):
        raise BrokerError("history_order_list_query 失败：网络错误（测试）")

    monkeypatch.setattr(sys1.broker, "query_order_history", down)
    sys2 = restart(sys1)
    assert sys2.oms.ready
    assert "history_query_failed" in sys2.alert_kinds()
    assert "missing_at_broker" in sys2.alert_kinds()
    assert sys2.oms.get(cid).status is S.NEW


def test_no_history_query_when_nothing_missing():
    sys1 = make_system()
    working_order(sys1)
    sys2 = restart(sys1)  # 同一天：当日列表里查得到
    assert sys2.broker.calls("query_order_history") == []


def test_one_history_query_for_many_missing_orders():
    sys1 = make_system()
    cids = [working_order(sys1) for _ in range(3)]
    sys1.broker.roll_day()
    sys2 = restart(sys1)
    assert len(sys2.broker.calls("query_order_history")) == 1
    assert {sys2.oms.get(c).status for c in cids} == {S.CANCELED}


def test_unrelated_history_orders_not_reported():
    """历史里只处理本地缺失的那几张；其他历史订单（含本程序以前的、外部的）不报 unknown / external。"""
    sys1 = make_system()
    cid = working_order(sys1)
    sys1.broker.place_order(
        OrderRequest(new_client_order_id(), "US.SPY", Side.BUY, 1, OrderKind.LIMIT, Decimal("9.00"))
    )  # 绕过 OMS 直接下到券商：本地没有记录
    sys1.broker.roll_day()
    sys2 = restart(sys1)
    assert sys2.oms.get(cid).status is S.CANCELED
    assert not {"unknown_client_order_id", "external_order"} & set(sys2.alert_kinds())
