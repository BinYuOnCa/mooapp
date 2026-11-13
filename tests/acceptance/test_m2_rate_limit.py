from __future__ import annotations

import time
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest

from mooapp.broker.moomoo import open_broker
from mooapp.broker.port import TERMINAL_STATUSES, OrderRequest, RateLimited, new_client_order_id
from mooapp.clock import SystemClock
from mooapp.common import Env, OrderKind, Side
from mooapp.config.loader import load_broker_cfg

pytestmark = pytest.mark.sim
REPO = Path(__file__).resolve().parents[2]
N = 20


def test_twenty_rapid_orders_queue_without_rate_limit_error():
    broker = open_broker(Env.SIMULATE, load_broker_cfg(REPO / "config" / "broker.toml"), clock=SystemClock())
    placed = []
    try:
        last = broker.snapshot(["US.SPY"])[0].last
        price = (last * Decimal("0.9")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        t0 = time.monotonic()
        for i in range(N):
            try:
                placed.append(
                    broker.place_order(
                        OrderRequest(new_client_order_id(), "US.SPY", Side.BUY, 1, OrderKind.LIMIT, price))
                )
            except RateLimited as e:
                pytest.fail(f"第 {i + 1} 笔收到限频错误：{e}")
            # 券商返回的任何错误（含限频）都会以 OrderRejected / BrokerError 抛出，测试同样失败
        elapsed = time.monotonic() - t0
        assert len(placed) == N
        assert elapsed >= 30.0, f"20 笔只用了 {elapsed:.1f} 秒，限流器没有排队"
    finally:
        for o in placed:
            broker.cancel_order(o.broker_order_id)
        deadline = time.monotonic() + 30
        ids = {o.broker_order_id for o in placed}
        while time.monotonic() < deadline:
            states = {o.broker_order_id: o.raw_status for o in broker.query_orders(open_only=False) if
                      o.broker_order_id in ids}
            if all(s in TERMINAL_STATUSES for s in states.values()) and len(states) == len(ids):
                break
            time.sleep(0.5)
        broker.close()
    assert all(s == "CANCELLED_ALL" for s in states.values()), states
