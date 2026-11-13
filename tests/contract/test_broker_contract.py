"""BrokerPort 契约（M2 验收 1、2；SPEC 7.1 R9）。同一组用例跑两种实现：

- fake（默认）：FakeBroker(strict_limits=True) 外包 ThrottledBroker，FakeClock，瞬间跑完
- sim（-m sim）：open_broker(Env.SIMULATE, …) 连模拟盘。sandbox 不放行 11111，由你用 `!` 运行：
      ! uv run pytest -m sim tests/contract/test_broker_contract.py
  推送录到 recordings/pushes/（M3 回放用）

M2 还没有闸门：这里直接调用 BrokerPort.place_order / cancel_order，只用 SIMULATE（写死），
你 2026-11-12 确认（PROGRESS「M2 计划 › 你确认的点」第 1 条）。
模拟盘上的副作用：
- US.SPY 限价买 1 股 × 21 笔，价格 = 最新价 × 0.9（向下取到 0.01），不期望成交，全部撤销
- 常规时段另有：US.SPY 限价买 1 股 @ 卖一价 × 1.002（期望成交），随后市价卖 1 股恢复原持仓
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path

import pytest

from mooapp.broker.fake import FakeBroker
from mooapp.broker.moomoo import open_broker
from mooapp.broker.port import (
    TERMINAL_STATUSES,
    BrokerFill,
    BrokerOrder,
    NotSupported,
    OrderRequest,
    RateLimited,
    new_client_order_id,
)
from mooapp.broker.throttle import ThrottledBroker
from mooapp.clock import FakeClock, SystemClock
from mooapp.common import Env, OrderKind, Side
from mooapp.config.loader import load_broker_cfg

REPO = Path(__file__).resolve().parents[2]
SYMBOL = "US.SPY"
WORKING = {"WAITING_SUBMIT", "SUBMITTING", "SUBMITTED"}
TICK = Decimal("0.01")
FILL_TIMEOUT = 60.0  # 模拟盘可成交单 6–28 秒才成交（learnings M2 其他发现）
CANCEL_TIMEOUT = 30.0


class Harness:
    """屏蔽两种实现的差异：Fake 需要手动推进（step / fill），模拟盘靠真实时间。"""

    broker: ThrottledBroker
    updates: list[BrokerOrder]
    fills: list[BrokerFill]

    def wait(self, pred: Callable[[], bool], timeout: float) -> bool:
        raise NotImplementedError

    def exchange_fills(self, order: BrokerOrder, price: Decimal) -> None:
        """可成交单：Fake 手动成交；模拟盘什么也不做（交易所撮合）。"""
        raise NotImplementedError

    def in_regular_hours(self) -> bool:
        raise NotImplementedError

    def order(self, oid: str) -> BrokerOrder:
        found = [o for o in self.broker.query_orders(open_only=False) if o.broker_order_id == oid]
        assert len(found) == 1, f"订单 {oid} 查询结果 {found}"
        return found[0]

    def wait_status(self, oid: str, statuses: set[str], timeout: float) -> BrokerOrder:
        last: list[BrokerOrder] = []

        def done() -> bool:
            last[:] = [self.order(oid)]
            return last[0].raw_status in statuses

        assert self.wait(done, timeout), f"订单 {oid} {timeout:g} 秒内未到 {statuses}，最后为 {last[0].raw_status if last else None}"
        return last[0]


class FakeHarness(Harness):
    def __init__(self):
        self.clock = FakeClock(datetime(2026, 11, 13, 14, 31, tzinfo=UTC))
        self.fake = FakeBroker(env=Env.SIMULATE, clock=self.clock, strict_limits=True)
        self.fake.set_quote(SYMBOL, last=Decimal("759.57"), ask=Decimal("759.58"), bid=Decimal("759.53"))
        self.fake.set_market_state(SYMBOL, "AFTERNOON")
        self.broker = ThrottledBroker(self.fake, self.clock)
        self.updates, self.fills = [], []
        self.broker.on_order_update(self.updates.append)
        self.broker.on_fill(self.fills.append)

    def wait(self, pred, timeout):
        for _ in range(3):
            if pred():
                return True
            self.fake.step()
        return pred()

    def exchange_fills(self, order, price):
        self.fake.fill(order.broker_order_id, order.qty, price)

    def in_regular_hours(self):
        return True


class SimHarness(Harness):
    def __init__(self):
        cfg = load_broker_cfg(REPO / "config" / "broker.toml")
        self.broker = open_broker(Env.SIMULATE, cfg, clock=SystemClock(), record_dir=REPO / "recordings" / "pushes")
        self.updates, self.fills = [], []
        self.broker.on_order_update(self.updates.append)
        self.broker.on_fill(self.fills.append)

    def wait(self, pred, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(0.5)
        return pred()

    def exchange_fills(self, order, price):
        return None

    def in_regular_hours(self):
        return self.broker.market_state(SYMBOL) == "AFTERNOON"  # vendor § Trading Definitions › Market State


@pytest.fixture(scope="module")
def sim_harness():
    h = SimHarness()
    yield h
    h.broker.close()


@pytest.fixture(params=["fake", pytest.param("sim", marks=pytest.mark.sim)])
def h(request) -> Harness:
    if request.param == "fake":
        return FakeHarness()
    return request.getfixturevalue("sim_harness")


def last_price(h: Harness) -> Decimal:
    return h.broker.snapshot([SYMBOL])[0].last


def far_buy(h: Harness) -> OrderRequest:
    price = (last_price(h) * Decimal("0.9")).quantize(TICK, rounding=ROUND_DOWN)
    return OrderRequest(new_client_order_id(), SYMBOL, Side.BUY, 1, OrderKind.LIMIT, price)


def cancel_all(h: Harness, orders: list[BrokerOrder]) -> None:
    for o in orders:
        if o.broker_order_id not in {x.broker_order_id for x in h.broker.query_orders(open_only=False) if x.is_terminal}:
            h.broker.cancel_order(o.broker_order_id)
    for o in orders:
        h.wait_status(o.broker_order_id, TERMINAL_STATUSES, CANCEL_TIMEOUT)


# ------------------------------------------------------------------ 下单、查询、撤单、推送


def test_place_query_cancel_push(h: Harness):
    req = far_buy(h)
    o = h.broker.place_order(req)
    try:
        assert o.raw_status in WORKING
        assert o.client_order_id == req.client_order_id
        assert o.symbol == SYMBOL and o.side is Side.BUY and o.kind is OrderKind.LIMIT and o.qty == 1
        assert o.price == req.limit_price
        assert o.created_at.tzinfo is UTC
        assert o.broker_order_id in {x.broker_order_id for x in h.broker.query_orders(open_only=True)}
        h.broker.cancel_order(o.broker_order_id)
        final = h.wait_status(o.broker_order_id, TERMINAL_STATUSES, CANCEL_TIMEOUT)
        assert final.raw_status == "CANCELLED_ALL"
        assert o.broker_order_id not in {x.broker_order_id for x in h.broker.query_orders(open_only=True)}
        assert h.wait(lambda: any(u.broker_order_id == o.broker_order_id and u.raw_status == "CANCELLED_ALL" for u in h.updates), 5.0)
        mine = [u for u in h.updates if u.broker_order_id == o.broker_order_id]
        assert all(u.client_order_id == req.client_order_id for u in mine)  # 推送里带回 remark
    finally:
        cancel_all(h, [o])


def test_twenty_rapid_orders_no_rate_limit(h: Harness):
    """验收 2：连续快速下 20 笔，限流器自动排队，不出现限频错误（模拟盘约 31 秒以上）。"""
    placed: list[BrokerOrder] = []
    try:
        for _ in range(20):
            try:
                placed.append(h.broker.place_order(far_buy(h)))
            except RateLimited as e:
                pytest.fail(f"第 {len(placed) + 1} 笔收到限频错误：{e}")
        assert len({o.broker_order_id for o in placed}) == 20
        open_ids = {x.broker_order_id for x in h.broker.query_orders(open_only=True)}
        assert {o.broker_order_id for o in placed} <= open_ids
    finally:
        cancel_all(h, placed)
    assert all(h.order(o.broker_order_id).raw_status == "CANCELLED_ALL" for o in placed)


def test_marketable_buy_fills_and_emits_one_fill(h: Harness):
    if not h.in_regular_hours():
        pytest.skip("需要常规时段（成交）")
    q = h.broker.snapshot([SYMBOL])[0]
    ask = q.ask or q.last
    price = (ask * Decimal("1.002")).quantize(TICK, rounding=ROUND_UP)
    req = OrderRequest(new_client_order_id(), SYMBOL, Side.BUY, 1, OrderKind.LIMIT, price)
    buy = h.broker.place_order(req)
    sell = None
    try:
        h.exchange_fills(buy, ask)
        done = h.wait_status(buy.broker_order_id, {"FILLED_ALL"}, FILL_TIMEOUT)
        assert done.dealt_qty == 1 and done.dealt_avg_price > 0
        assert h.wait(lambda: any(f.broker_order_id == buy.broker_order_id for f in h.fills), 5.0)
        mine = [f for f in h.fills if f.broker_order_id == buy.broker_order_id]
        assert len(mine) == 1
        assert (mine[0].qty, mine[0].client_order_id, mine[0].fill_id) == (1, req.client_order_id, f"{buy.broker_order_id}:1")
        pos = {p.symbol: p for p in h.broker.query_positions()}
        assert pos[SYMBOL].can_sell_qty >= 1  # P5：当日买入可卖
    finally:
        if h.order(buy.broker_order_id).dealt_qty:
            sell = h.broker.place_order(OrderRequest(new_client_order_id(), SYMBOL, Side.SELL, 1, OrderKind.MARKET, None))
            h.exchange_fills(sell, last_price(h))
            h.wait_status(sell.broker_order_id, {"FILLED_ALL"}, FILL_TIMEOUT)
        else:
            cancel_all(h, [buy])


# ------------------------------------------------------------------ 只读


def test_deals_and_fees_not_supported_on_simulate(h: Harness):
    with pytest.raises(NotSupported):
        h.broker.query_deals()
    with pytest.raises(NotSupported):
        h.broker.query_fees(["0"])


def test_positions_have_no_zero_rows(h: Harness):
    assert all(p.qty > 0 for p in h.broker.query_positions())


def test_cash_is_usd(h: Harness):
    c = h.broker.query_cash()
    assert c.currency == "USD" and c.buying_power > 0


def test_env_is_simulate(h: Harness):
    assert h.broker.env is Env.SIMULATE
