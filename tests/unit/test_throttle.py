"""限流器（broker/throttle.py）与 FakeClock（mooapp/clock.py）。

vendor 限频（各接口 Interface Limitations）：
- § Place Orders：每个 acc_id 30 秒 15 次，连续两次间隔 ≥ 0.02 秒
- § Modify or Cancel Orders：30 秒 20 次，间隔 ≥ 0.04 秒
限流器在窗口上多留 1 秒余量（M2 计划确认点 2）。
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mooapp.broker.fake import FakeBroker
from mooapp.broker.port import OrderRequest, RateLimited, new_client_order_id
from mooapp.broker.throttle import CANCEL_RULE, PLACE_RULE, RateRule, Throttle, ThrottledBroker
from mooapp.clock import FakeClock, SystemClock
from mooapp.common import Env, OrderKind, Side

T0 = datetime(2026, 11, 13, 14, 31, 0, tzinfo=UTC)


def far_buy() -> OrderRequest:
    return OrderRequest(
        client_order_id=new_client_order_id(),
        symbol="US.SPY",
        side=Side.BUY,
        qty=1,
        kind=OrderKind.LIMIT,
        limit_price=Decimal("683.61"),
    )


# ------------------------------------------------------------------ Clock


def test_fake_clock_sleep_advances_both_clocks():
    c = FakeClock(T0)
    m0 = c.monotonic()
    c.sleep(2.5)
    assert c.monotonic() - m0 == pytest.approx(2.5)
    assert c.now() == T0 + timedelta(seconds=2.5)
    c.advance(1)
    assert c.now() == T0 + timedelta(seconds=3.5)


def test_fake_clock_requires_aware_utc():
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 11, 13, 9, 31))


def test_system_clock_now_is_utc():
    assert SystemClock().now().tzinfo is UTC


# ------------------------------------------------------------------ 规则常量


def test_vendor_rules():
    assert (PLACE_RULE.max_calls, PLACE_RULE.window, PLACE_RULE.min_interval) == (15, 30.0, 0.02)
    assert (CANCEL_RULE.max_calls, CANCEL_RULE.window, CANCEL_RULE.min_interval) == (20, 30.0, 0.04)
    assert PLACE_RULE.margin == CANCEL_RULE.margin == 1.0


# ------------------------------------------------------------------ Throttle


def test_first_15_only_wait_min_interval_16th_waits_window():
    c = FakeClock(T0)
    t = Throttle(PLACE_RULE, c, max_wait=60.0)
    start = c.monotonic()
    times = []
    for _ in range(16):
        t.acquire()
        times.append(c.monotonic() - start)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(g >= 0.02 - 1e-9 for g in gaps)
    assert times[14] == pytest.approx(14 * 0.02)  # 前 15 次只受最小间隔约束
    assert times[15] >= 31.0 - 1e-9  # 第 16 次等到第 1 次之后 30 + 1 秒


def test_window_slides():
    c = FakeClock(T0)
    rule = RateRule("t", max_calls=2, window=10.0, min_interval=0.0, margin=0.0)
    t = Throttle(rule, c, max_wait=60.0)
    t.acquire()
    c.advance(6)
    t.acquire()
    before = c.monotonic()
    t.acquire()  # 要等到第 1 次之后 10 秒，即再等 4 秒
    assert c.monotonic() - before == pytest.approx(4.0)


def test_exceeding_max_wait_raises_rate_limited_without_sleeping():
    c = FakeClock(T0)
    t = Throttle(PLACE_RULE, c, max_wait=5.0)
    for _ in range(15):
        t.acquire()
    before = c.monotonic()
    with pytest.raises(RateLimited):
        t.acquire()
    assert c.monotonic() == before  # 不阻塞，直接报错
    # 报错的那次不占名额：等窗口过去后仍可继续
    c.advance(31)
    t.acquire()


def test_thread_safety_never_exceeds_limit():
    """真实时钟、缩小的规则：4 个线程各取 5 次，任意 0.5 秒窗口内不超过 5 次（约 2 秒）。"""
    clock = SystemClock()
    rule = RateRule("t", max_calls=5, window=0.5, min_interval=0.0, margin=0.05)
    t = Throttle(rule, clock, max_wait=10.0)
    stamps: list[float] = []
    lock = threading.Lock()

    def worker():
        for _ in range(5):
            t.acquire()
            with lock:
                stamps.append(clock.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    stamps.sort()
    assert len(stamps) == 20
    for i in range(len(stamps) - 5):
        assert stamps[i + 5] - stamps[i] >= 0.5 - 1e-3, f"第 {i} 次起 6 次落在 0.5 秒内"


# ------------------------------------------------------------------ ThrottledBroker + FakeBroker(strict_limits)


def test_fake_strict_limits_rejects_burst_without_throttle():
    c = FakeClock(T0)
    fb = FakeBroker(env=Env.SIMULATE, clock=c, strict_limits=True)
    for _ in range(15):
        fb.place_order(far_buy())
        c.advance(0.02)
    with pytest.raises(RateLimited):
        fb.place_order(far_buy())


def test_fake_strict_limits_rejects_min_interval():
    c = FakeClock(T0)
    fb = FakeBroker(env=Env.SIMULATE, clock=c, strict_limits=True)
    fb.place_order(far_buy())
    c.advance(0.01)
    with pytest.raises(RateLimited):
        fb.place_order(far_buy())


def test_throttled_broker_20_orders_and_20_cancels_no_rate_limit():
    """验收 2 的 FakeBroker 版本：连下 20 笔、再撤 20 笔，严格限频的 FakeBroker 不报错。"""
    c = FakeClock(T0)
    fb = FakeBroker(env=Env.SIMULATE, clock=c, strict_limits=True)
    b = ThrottledBroker(fb, c)
    start = c.monotonic()
    orders = [b.place_order(far_buy()) for _ in range(20)]
    assert c.monotonic() - start >= 31.0 - 1e-9
    for o in orders:
        b.cancel_order(o.broker_order_id)
    assert len(fb.calls("place_order")) == 20
    assert len(fb.calls("cancel_order")) == 20


def test_throttled_broker_passes_reads_through_unthrottled():
    c = FakeClock(T0)
    fb = FakeBroker(env=Env.SIMULATE, clock=c, strict_limits=True)
    b = ThrottledBroker(fb, c)
    before = c.monotonic()
    for _ in range(50):
        b.query_orders()
        b.query_positions()
    assert c.monotonic() == before
    assert b.env is Env.SIMULATE
