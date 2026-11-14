"""限流（SPEC 5.3；M2 计划确认点 2）。

vendor 限频（各接口 Interface Limitations）：
- § Place Orders：每个 acc_id 30 秒 15 次，连续两次间隔 ≥ 0.02 秒
- § Modify or Cancel Orders：30 秒 20 次，间隔 ≥ 0.04 秒
窗口多留 1 秒余量。持仓、资金查询走缓存不限流（§ Get Positions：只有刷新缓存时才受限频）；
订单查询 M4 起全部按 § Get open Orders 限 30 秒 10 次。同一账户的实例经 ThrottleRegistry 共用限流器。

Throttle 采用「预约时间槽」：加锁计算本次最早可发出的时刻并登记，解锁后再 sleep。
多线程同时排队时各自拿到不同的槽，不会同时冲进同一个窗口。
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from mooapp.broker.port import BrokerFill, BrokerOrder, BrokerPort, Cash, OrderRequest, Position, Quote, RateLimited
from mooapp.clock import Clock, SystemClock
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
# vendor § Get open Orders › Interface Limitations：每个 acc_id 30 秒 10 次。M4 默认做法 2：不分是否刷新缓存，全部限流
QUERY_ORDERS_RULE = RateRule("order_list_query", max_calls=10, window=30.0, min_interval=0.0, margin=1.0)
_RULES = (PLACE_RULE, CANCEL_RULE, QUERY_ORDERS_RULE)

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


class ThrottleRegistry:
    """按账户共用限流器（learnings「M3 › vendor 限频按账户计算」：同一账户的多个连接共用一份额度）。

    同一个 key 的 ThrottledBroker 拿到同一组 Throttle。共用的限流器必须用同一个时钟，否则时间戳不可比，混用时报错。
    只解决「同一进程里接连新建 broker」；跨进程靠 SPEC 3.4：只有常驻进程连券商。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[Hashable, tuple[Clock, dict[str, Throttle]]] = {}

    def get(self, key: Hashable, clock: Clock, max_wait: float) -> dict[str, Throttle]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                throttles = {r.name: Throttle(r, clock, max_wait) for r in _RULES}
                self._entries[key] = (clock, throttles)
                return throttles
            same = entry[0] is clock or (type(entry[0]) is SystemClock and type(clock) is SystemClock)  # 真实时钟彼此等价
            if not same:
                raise ValueError(f"限流器 {key!r} 已绑定另一个时钟，不能混用")
            return entry[1]


SHARED_REGISTRY = ThrottleRegistry()  # 进程级；open_broker 按 (env, acc_id) 使用


class ThrottledBroker:
    """给写接口和订单查询加限流，其余原样转发。open_broker 返回的就是它。

    registry 与 account_key 都给出时，同一账户的实例共用限流器；否则用本实例私有的限流器（单元测试）。
    """

    def __init__(
        self,
        inner: BrokerPort,
        clock: Clock,
        max_wait: float = DEFAULT_MAX_WAIT,
        registry: ThrottleRegistry | None = None,
        account_key: Hashable | None = None,
    ):
        self.inner = inner
        if registry is not None and account_key is not None:
            throttles = registry.get(account_key, clock, max_wait)
        else:
            throttles = {r.name: Throttle(r, clock, max_wait) for r in _RULES}
        self._place = throttles[PLACE_RULE.name]
        self._cancel = throttles[CANCEL_RULE.name]
        self._query_orders = throttles[QUERY_ORDERS_RULE.name]

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
    def query_orders(self, open_only: bool = True, refresh: bool = False) -> list[BrokerOrder]:
        self._query_orders.acquire()
        return self.inner.query_orders(open_only=open_only, refresh=refresh)

    def query_deals(self) -> list[BrokerFill]:
        return self.inner.query_deals()

    def query_fees(self, broker_order_ids: list[str]) -> dict[str, Decimal]:
        return self.inner.query_fees(broker_order_ids)

    def query_positions(self) -> list[Position]:
        return self.inner.query_positions()

    def query_today_pnl(self) -> Decimal | None:
        return self.inner.query_today_pnl()

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

    def subscribe_quotes(self, symbols: list[str], cb: Callable[[Quote], None]) -> None:
        self.inner.subscribe_quotes(symbols, cb)  # type: ignore[attr-defined]
