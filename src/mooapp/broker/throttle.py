"""限流（SPEC 5.3；M2 计划确认点 2）。

vendor 限频（各接口 Interface Limitations）：
- § Place Orders：每个 acc_id 30 秒 15 次，连续两次间隔 ≥ 0.02 秒
- § Modify or Cancel Orders：30 秒 20 次，间隔 ≥ 0.04 秒
窗口多留 1 秒余量。查询类接口用 refresh_cache=False 走缓存，不限流。

Throttle 采用「预约时间槽」：加锁计算本次最早可发出的时刻并登记，解锁后再 sleep。
多线程同时排队时各自拿到不同的槽，不会同时冲进同一个窗口。
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from mooapp.broker.port import BrokerFill, BrokerOrder, BrokerPort, Cash, OrderRequest, Position, Quote, RateLimited
from mooapp.clock import Clock
from mooapp.common import Env


@dataclass(frozen=True)
class RateRule:
    name: str
    max_calls: int
    window: float  # 秒
    min_interval: float  # 秒
    margin: float  # 秒，加在窗口上


PLACE_RULE = RateRule("place_order", max_calls=15, window=30.0, min_interval=0.02, margin=1.0)
CANCEL_RULE = RateRule("modify_order", max_calls=20, window=30.0, min_interval=0.04, margin=1.0)

DEFAULT_MAX_WAIT = 60.0


class Throttle:
    def __init__(self, rule: RateRule, clock: Clock, max_wait: float = DEFAULT_MAX_WAIT):
        self.rule = rule
        self._clock = clock
        self._max_wait = max_wait
        self._slots: deque[float] = deque(maxlen=rule.max_calls)
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """阻塞到可以发出下一次调用；需要等待超过 max_wait 时抛 RateLimited（不等待、不占名额）。"""
        r = self.rule
        with self._lock:
            now = self._clock.monotonic()
            earliest = now
            if self._slots:
                earliest = max(earliest, self._slots[-1] + r.min_interval)
            if len(self._slots) == r.max_calls:
                earliest = max(earliest, self._slots[0] + r.window + r.margin)
            wait = earliest - now
            if wait > self._max_wait:
                raise RateLimited(f"{r.name} 需要等待 {wait:.1f} 秒，超过上限 {self._max_wait:g} 秒")
            self._slots.append(earliest)
        if wait > 0:
            self._clock.sleep(wait)


class ThrottledBroker:
    """给写接口加限流，其余原样转发。open_broker 返回的就是它。"""

    def __init__(self, inner: BrokerPort, clock: Clock, max_wait: float = DEFAULT_MAX_WAIT):
        self.inner = inner
        self._place = Throttle(PLACE_RULE, clock, max_wait)
        self._cancel = Throttle(CANCEL_RULE, clock, max_wait)

    @property
    def env(self) -> Env:
        return self.inner.env

    # 写
    def place_order(self, req: OrderRequest) -> BrokerOrder:
        self._place.acquire()
        return self.inner.place_order(req)

    def cancel_order(self, broker_order_id: str) -> None:
        self._cancel.acquire()
        self.inner.cancel_order(broker_order_id)

    # 读
    def query_orders(self, open_only: bool = True) -> list[BrokerOrder]:
        return self.inner.query_orders(open_only=open_only)

    def query_deals(self) -> list[BrokerFill]:
        return self.inner.query_deals()

    def query_fees(self, broker_order_ids: list[str]) -> dict[str, Decimal]:
        return self.inner.query_fees(broker_order_ids)

    def query_positions(self) -> list[Position]:
        return self.inner.query_positions()

    def query_cash(self) -> Cash:
        return self.inner.query_cash()

    # 推送
    def on_order_update(self, cb: Callable[[BrokerOrder], None]) -> None:
        self.inner.on_order_update(cb)

    def on_fill(self, cb: Callable[[BrokerFill], None]) -> None:
        self.inner.on_fill(cb)

    # 会话
    def unlock(self, password: str) -> None:
        self.inner.unlock(password)

    def lock(self) -> None:
        self.inner.lock()

    def close(self) -> None:
        self.inner.close()

    # 行情（inner 同时实现 MarketDataPort）
    def snapshot(self, symbols: list[str]) -> list[Quote]:
        return self.inner.snapshot(symbols)  # type: ignore[attr-defined]

    def market_state(self, symbol: str) -> str:
        return self.inner.market_state(symbol)  # type: ignore[attr-defined]

    def is_trading_day(self, d: date) -> bool:
        return self.inner.is_trading_day(d)  # type: ignore[attr-defined]
