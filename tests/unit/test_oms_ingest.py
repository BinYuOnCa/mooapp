"""单一事件入口（M3 计划第 6 节）与 SPEC 8 节 M3 验收 2：FakeBroker 注入重复、乱序、丢失推送，状态仍然正确。"""

from __future__ import annotations

import itertools
import random
from dataclasses import replace
from decimal import Decimal

import pytest

from oms_harness import T0, load_push_rows, make_system

from mooapp.broker.convert import order_from_row
from mooapp.broker.port import BrokerFill, BrokerOrder, OrderRequest, new_client_order_id
from mooapp.common import Env, OrderKind, Side, Source
from mooapp.oms.state import Event, EventType, OrdStatus

S, E = OrdStatus, EventType


def placed(sys, qty=100):
    d = sys.place(qty=qty)
    assert d.allowed, d.reason
    return d.order.client_order_id


def collect(sys) -> list[BrokerOrder]:
    got: list[BrokerOrder] = []
    sys.broker.on_order_update(got.append)
    return got


def applied(sys, cid, origin=None):
    return [r for r in sys.store.events_for(cid) if r.outcome == "applied" and (origin is None or r.origin == origin)]


# ------------------------------------------------------------------ 正常路径


def test_place_ack_partial_full():
    sys = make_system()
    cid = placed(sys)
    o = sys.oms.get(cid)
    assert o.status is S.PENDING_NEW and o.broker_order_id == "900001"
    sys.broker.step()
    sys.pump.drain()
    assert sys.oms.get(cid).status is S.NEW
    sys.broker.fill("900001", 40, Decimal("10"))
    sys.pump.drain()
    o = sys.oms.get(cid)
    assert (o.status, o.cum_qty) == (S.PARTIALLY_FILLED, 40)
    sys.broker.fill("900001", 60, Decimal("11"))
    sys.pump.drain()
    o = sys.oms.get(cid)
    assert (o.status, o.cum_qty, o.avg_px) == (S.FILLED, 100, Decimal("10.6"))
    fills = sys.store.fills_for(cid)
    assert [f.fill_id for f in fills] == ["900001:40", "900001:100"]
    assert sum(f.qty for f in fills) == 100


def test_every_event_records_origin():
    sys = make_system()
    cid = placed(sys)
    sys.broker.step()
    sys.pump.drain()
    origins = {r.origin for r in sys.store.events_for(cid)}
    assert origins <= {"push", "poll", "local"}
    assert "local" in origins and "push" in origins
    assert any(r.event_type == "SUBMIT" and r.origin == "local" for r in sys.store.events_for(cid))
    assert any(r.event_type == "ACK" and r.origin == "push" for r in sys.store.events_for(cid))


def test_push_before_place_returns_is_linked_by_remark():
    """FakeBroker 在 place_order 返回前先推送 SUBMITTING（实测早约 3 毫秒，learnings M2）。"""
    sys = make_system()
    cid = placed(sys)
    recs = sys.store.events_for(cid)
    push_snaps = [r for r in recs if r.origin == "push" and r.raw_status == "SUBMITTING"]
    assert len(push_snaps) == 1
    assert sys.oms.get(cid).broker_order_id == "900001"


def test_unknown_broker_id_snapshot_links_by_cid():
    sys = make_system(attach=False)
    cid = placed(sys)
    o = sys.oms.get(cid)
    # 构造一条来自另一个 broker_order_id 字段尚未记录的推送：本地 broker_order_id 清空后由 remark 关联
    sys.store.update_order(replace(o, broker_order_id=None))
    s = sys.broker.query_orders(open_only=False)[0]
    sys.pump.submit_snapshot(replace(s, raw_status="SUBMITTED"), "push")
    sys.pump.drain()
    o = sys.oms.get(cid)
    assert o.broker_order_id == "900001" and o.status is S.NEW


# ------------------------------------------------------------------ 重复、乱序、丢失（验收 2）


def scripted_snapshots():
    """不接 pump，收集一张单完整生命周期的推送：SUBMITTING、SUBMITTED、FILLED_PART(50)、FILLED_PART(80)、FILLED_ALL(100)。

    价格选成任意两次快照之间反推出的成交价都是有限小数（10、10、12.5；均价 10、10、10.5），断言可以精确比较。
    """
    sys = make_system(attach=False)
    got = collect(sys)
    cid = placed(sys)
    sys.broker.step()
    sys.broker.fill("900001", 50, Decimal("10"))
    sys.broker.fill("900001", 30, Decimal("10"))
    sys.broker.fill("900001", 20, Decimal("12.5"))
    assert [s.raw_status for s in got] == ["SUBMITTING", "SUBMITTED", "FILLED_PART", "FILLED_PART", "FILLED_ALL"]
    return sys, cid, got


def assert_filled(sys, cid):
    o = sys.oms.get(cid)
    assert o.status is S.FILLED
    assert o.cum_qty == 100
    assert o.avg_px == Decimal("10.5")
    fills = sys.store.fills_for(cid)
    assert sum(f.qty for f in fills) == 100
    assert sum(f.qty * f.price for f in fills) == Decimal("1050")


def test_duplicate_pushes():
    sys, cid, got = scripted_snapshots()
    for s in got + got + got:
        sys.pump.submit_snapshot(s, "push")
    sys.pump.drain()
    assert_filled(sys, cid)
    assert len(sys.store.fills_for(cid)) == 3


@pytest.mark.parametrize("perm", list(itertools.permutations(range(5)))[::7])
def test_out_of_order_pushes(perm):
    sys, cid, got = scripted_snapshots()
    for i in perm:
        sys.pump.submit_snapshot(got[i], "push")
    sys.pump.drain()
    assert_filled(sys, cid)


def test_random_duplicates_and_reordering():
    rng = random.Random(20261113)
    for _ in range(20):
        sys, cid, got = scripted_snapshots()
        stream = got + [rng.choice(got) for _ in range(5)]
        rng.shuffle(stream)
        for s in stream:
            sys.pump.submit_snapshot(s, "push")
        sys.pump.drain()
        assert_filled(sys, cid)


def test_lost_pushes_recovered_by_poll():
    sys, cid, got = scripted_snapshots()
    sys.pump.submit_snapshot(got[1], "push")  # 只收到 SUBMITTED，其余全丢
    sys.pump.drain()
    assert sys.oms.get(cid).status is S.NEW
    sys.poller.poll_once()
    sys.pump.drain()
    assert_filled(sys, cid)
    assert applied(sys, cid, origin="poll"), "补上的成交应标记来源 poll"


def test_poll_and_push_same_state_do_not_double_count():
    sys, cid, got = scripted_snapshots()
    sys.poller.poll_once()
    for s in got:
        sys.pump.submit_snapshot(s, "push")
    sys.poller.poll_once()
    sys.pump.drain()
    assert_filled(sys, cid)


def test_unchanged_poll_does_not_spam_events():
    sys = make_system()
    cid = placed(sys)
    sys.broker.step()
    sys.pump.drain()
    n = len(sys.store.events_for(cid))
    for _ in range(5):
        sys.poller.poll_once()
    sys.pump.drain()
    assert len(sys.store.events_for(cid)) == n


def test_cancel_flow_via_push():
    sys = make_system()
    cid = placed(sys)
    sys.broker.step()
    sys.pump.drain()
    d = sys.cancel(cid)
    assert d.allowed, d.reason
    assert sys.oms.get(cid).status is S.PENDING_CANCEL
    sys.broker.step()
    sys.pump.drain()
    assert sys.oms.get(cid).status is S.CANCELED
    assert any(r.event_type == "CANCEL_ACK" for r in applied(sys, cid))


def test_partial_fill_then_cancel():
    sys = make_system()
    cid = placed(sys)
    sys.broker.step()
    sys.broker.fill("900001", 40, Decimal("10"))
    sys.pump.drain()
    sys.cancel(cid)
    sys.broker.step()
    sys.pump.drain()
    o = sys.oms.get(cid)
    assert (o.status, o.cum_qty) == (S.CANCELED, 40)


# ------------------------------------------------------------------ 报警路径


def test_unmapped_status_alerts_and_keeps_state():
    sys = make_system()
    cid = placed(sys)
    sys.broker.step()
    sys.pump.drain()
    s = sys.broker.query_orders(open_only=False)[0]
    sys.pump.submit_snapshot(replace(s, raw_status="DISABLED"), "push")
    sys.pump.drain()
    assert sys.oms.get(cid).status is S.NEW
    assert "unmapped_status" in sys.alert_kinds()
    rec = [r for r in sys.store.events_for(cid) if r.outcome == "unmapped"]
    assert len(rec) == 1 and rec[0].raw_status == "DISABLED"


def test_overfill_report_alerts_and_keeps_state():
    sys = make_system()
    cid = placed(sys)
    sys.broker.step()
    sys.pump.drain()
    s = sys.broker.query_orders(open_only=False)[0]
    sys.pump.submit_snapshot(replace(s, raw_status="FILLED_ALL", dealt_qty=150, dealt_avg_price=Decimal("10")), "push")
    sys.pump.drain()
    o = sys.oms.get(cid)
    assert (o.status, o.cum_qty) == (S.NEW, 0)
    assert "unexpected_report" in sys.alert_kinds()
    assert any(r.outcome == "rejected" for r in sys.store.events_for(cid))


def test_external_orders_alert_once():
    sys = make_system()
    rows = load_push_rows("pushes_m2_probe_external.jsonl")
    for row in rows:
        sys.pump.submit_snapshot(order_from_row(row), "push")
    sys.pump.drain()
    ext_ids = {order_from_row(r).broker_order_id for r in rows}
    kinds = sys.alert_kinds()
    assert kinds.count("external_order") == len(ext_ids)
    assert sys.oms.open_orders() == []
    assert {r.kind for r in sys.store.recon_events()} == {"external_order"}


def test_moo_remark_with_unknown_cid_alerts():
    sys = make_system()
    stranger = new_client_order_id()
    s = BrokerOrder(
        broker_order_id="777",
        client_order_id=stranger,
        symbol="US.SPY",
        side=Side.BUY,
        kind=OrderKind.LIMIT,
        qty=1,
        price=Decimal("1"),
        raw_status="SUBMITTED",
        dealt_qty=0,
        dealt_avg_price=Decimal(0),
        last_err_msg="",
        created_at=sys.clock.now(),
        updated_at=sys.clock.now(),
        remark="moo:" + stranger,
    )
    sys.pump.submit_snapshot(s, "push")
    sys.pump.submit_snapshot(s, "poll")
    sys.pump.drain()
    assert sys.alert_kinds().count("unknown_client_order_id") == 1
    assert sys.oms.get_or_none(stranger) is None


def test_synthetic_fill_callbacks_ignored_by_pump():
    """模拟盘的成交由 OMS 按本地 cum_qty 从快照算（避免重启后 FillSynthesizer 从 0 起算造成超额）。"""
    sys = make_system()
    cid = placed(sys)
    sys.broker.step()
    sys.broker.fill("900001", 100, Decimal("10"))  # 同时触发 on_order_update 与 on_fill(synthetic)
    sys.pump.drain()
    assert len(sys.store.fills_for(cid)) == 1
    assert sys.oms.get(cid).cum_qty == 100


def test_restart_after_partial_does_not_overfill():
    sys = make_system()
    cid = placed(sys)
    sys.broker.step()
    sys.broker.fill("900001", 40, Decimal("10"))
    sys.pump.drain()
    s = sys.broker.query_orders(open_only=False)[0]
    # 新进程：同一个 Store，新 OMS；推送累计 100
    sys2 = make_system(store=sys.store, broker=sys.broker, clock=sys.clock)
    sys2.pump.submit_snapshot(replace(s, raw_status="FILLED_ALL", dealt_qty=100, dealt_avg_price=Decimal("10.6")), "push")
    sys2.pump.drain()
    o = sys2.oms.get(cid)
    assert (o.status, o.cum_qty) == (S.FILLED, 100)
    assert [f.qty for f in sys2.store.fills_for(cid)] == [40, 60]


# ------------------------------------------------------------------ REAL：成交只来自成交记录（M3 不经闸门下 REAL 单，直接驱动 OMS）


def real_order(sys):
    req = OrderRequest(new_client_order_id(), "US.SPY", Side.BUY, 100, OrderKind.LIMIT, Decimal("10"))
    o = sys.pump.call(lambda: sys.oms.create_order(req, Source.CLI))
    sys.pump.call(lambda: sys.oms.handle_local(o.client_order_id, Event(E.SUBMIT)))
    return o.client_order_id


def real_snap(cid, raw, dealt=0, avg="0"):
    return BrokerOrder(
        broker_order_id="R1",
        client_order_id=cid,
        symbol="US.SPY",
        side=Side.BUY,
        kind=OrderKind.LIMIT,
        qty=100,
        price=Decimal("10"),
        raw_status=raw,
        dealt_qty=dealt,
        dealt_avg_price=Decimal(avg),
        last_err_msg="",
        created_at=T0,
        updated_at=T0,
        remark="moo:" + cid,
    )


def deal(cid, fid, qty, px="10"):
    return BrokerFill(fid, "R1", cid, "US.SPY", Side.BUY, qty, Decimal(px), T0, synthetic=False)


def test_real_fills_from_deals_only():
    sys = make_system(env=Env.REAL)
    cid = real_order(sys)
    sys.pump.submit_snapshot(real_snap(cid, "SUBMITTED"), "push")
    sys.pump.submit_snapshot(real_snap(cid, "FILLED_ALL", dealt=100, avg="10"), "push")
    sys.pump.drain()
    o = sys.oms.get(cid)
    assert (o.status, o.cum_qty) == (S.NEW, 0), "REAL 不从快照合成成交"
    sys.pump.submit_fill(deal(cid, "D1", 60), "push")
    sys.pump.submit_fill(deal(cid, "D1", 60), "poll")  # 推送与轮询重复
    sys.pump.submit_fill(deal(cid, "D2", 40), "poll")
    sys.pump.drain()
    o = sys.oms.get(cid)
    assert (o.status, o.cum_qty) == (S.FILLED, 100)
    assert [f.fill_id for f in sys.store.fills_for(cid)] == ["D1", "D2"]


def test_real_snapshot_ahead_of_fills_triggers_deal_poll():
    sys = make_system(env=Env.REAL)
    cid = real_order(sys)
    sys.broker.seed_fill(deal(cid, "D9", 100))
    sys.pump.submit_snapshot(real_snap(cid, "FILLED_ALL", dealt=100, avg="10"), "push")
    sys.pump.drain()
    assert sys.poller.deal_poll_pending
    sys.poller.poll_once()
    sys.pump.drain()
    assert sys.oms.get(cid).status is S.FILLED
    assert not sys.poller.deal_poll_pending
