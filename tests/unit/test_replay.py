"""SPEC 8 节 M3 验收 1：回放录制的推送，最终状态与券商查询结果一致。

输入：tests/fixtures/pushes_m2_sim.jsonl（复制自 recordings/pushes/SIMULATE_20261113T184058Z.jsonl，
M2 模拟盘契约测试录制，23 张单 69 条推送：21 张撤销、2 张整单成交）。
「券商查询结果」取每张单最后一条快照（录制里每张单的终态推送；M2 契约测试同时轮询确认过终态）。
本地订单按每张单第一条快照预先建好（PENDING_NEW），相当于这些单是本程序发出的。
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from oms_harness import load_push_rows, make_system

from mooapp.broker.convert import order_from_row
from mooapp.broker.port import BrokerOrder, OrderRequest
from mooapp.common import OrderKind, Source
from mooapp.oms.state import Event, EventType, OrdStatus

S, E = OrdStatus, EventType
FIXTURE = "pushes_m2_sim.jsonl"
EXPECTED_STATUS = {"FILLED_ALL": S.FILLED, "CANCELLED_ALL": S.CANCELED, "CANCELLED_PART": S.CANCELED}


def recorded() -> list[BrokerOrder]:
    return [order_from_row(r) for r in load_push_rows(FIXTURE)]


def seed_local_orders(sys, snaps: list[BrokerOrder]) -> None:
    seen = set()
    for s in snaps:
        if s.client_order_id is None or s.client_order_id in seen:
            continue
        seen.add(s.client_order_id)
        req = OrderRequest(s.client_order_id, s.symbol, s.side, s.qty, s.kind, s.price if s.kind is OrderKind.LIMIT else None)
        sys.pump.call(lambda req=req: sys.oms.create_order(req, Source.CLI))
        sys.pump.call(lambda cid=s.client_order_id: sys.oms.handle_local(cid, Event(E.SUBMIT)))


def assert_matches_broker(sys, snaps: list[BrokerOrder]) -> None:
    final = {}
    for s in snaps:
        final[s.broker_order_id] = s
    problems = []
    for bid, s in final.items():
        o = sys.oms.get(s.client_order_id)
        want = EXPECTED_STATUS[s.raw_status]
        if (o.status, o.cum_qty, o.broker_order_id) != (want, s.dealt_qty, bid):
            problems.append(f"{bid}: 本地 {o.status}/{o.cum_qty}/{o.broker_order_id}，券商 {s.raw_status}/{s.dealt_qty}")
        if s.dealt_qty and o.avg_px != s.dealt_avg_price:
            problems.append(f"{bid}: 均价 {o.avg_px} ≠ {s.dealt_avg_price}")
        fills = sys.store.fills_for(s.client_order_id)
        if sum(f.qty for f in fills) != s.dealt_qty:
            problems.append(f"{bid}: 成交合计 {sum(f.qty for f in fills)} ≠ {s.dealt_qty}")
    assert not problems, problems
    assert sys.alerts == [], [a.kind for a in sys.alerts]


def test_fixture_shape():
    snaps = recorded()
    assert len(snaps) == 69
    assert len({s.broker_order_id for s in snaps}) == 23
    assert all(s.client_order_id for s in snaps), "M2 契约测试的单都带 moo: remark"


def test_straight_replay_via_fake_broker():
    snaps = recorded()
    sys = make_system()
    seed_local_orders(sys, snaps)
    sys.broker.replay(load_push_rows(FIXTURE))
    sys.pump.drain()
    assert_matches_broker(sys, snaps)
    # 「券商查询结果」：FakeBroker 回放后其查询结果就是每张单最后一条快照
    q = {o.broker_order_id: o for o in sys.broker.query_orders(open_only=False)}
    for bid, s in {s.broker_order_id: s for s in snaps}.items():
        assert q[bid].raw_status == s.raw_status


@pytest.mark.parametrize("seed", range(10))
def test_shuffled_with_duplicates(seed):
    rng = random.Random(seed)
    snaps = recorded()
    stream = snaps + [rng.choice(snaps) for _ in range(30)]
    rng.shuffle(stream)
    sys = make_system(attach=False)
    seed_local_orders(sys, snaps)
    for s in stream:
        sys.pump.submit_snapshot(s, "push")
    sys.pump.drain()
    assert_matches_broker(sys, snaps)


@pytest.mark.parametrize("seed", range(10))
def test_dropped_pushes_then_poll(seed):
    rng = random.Random(1000 + seed)
    snaps = recorded()
    sys = make_system(attach=False)
    seed_local_orders(sys, snaps)
    for s in {s.broker_order_id: s for s in snaps}.values():
        sys.broker.seed_order(s)  # 券商端真实状态 = 每张单最后一条快照
    for s in snaps:
        if rng.random() > 0.4:
            sys.pump.submit_snapshot(s, "push")
    sys.pump.drain()
    sys.poller.poll_once()
    sys.pump.drain()
    assert_matches_broker(sys, snaps)


def test_filled_orders_have_one_fill_each():
    snaps = recorded()
    sys = make_system()
    seed_local_orders(sys, snaps)
    sys.broker.replay(load_push_rows(FIXTURE))
    sys.pump.drain()
    filled = {s.broker_order_id: s for s in snaps if s.raw_status == "FILLED_ALL"}
    assert len(filled) == 2
    for bid, s in filled.items():
        fills = sys.store.fills_for(s.client_order_id)
        assert [f.fill_id for f in fills] == [f"{bid}:{s.dealt_qty}"]
        assert fills[0].price == s.dealt_avg_price
        assert fills[0].qty * fills[0].price == Decimal(s.dealt_qty) * s.dealt_avg_price
