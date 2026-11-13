"""FakeBroker：内存里的 BrokerPort + MarketDataPort，行为照模拟盘实测（learnings「M2 券商适配层：探针结论」）。

- place_order 立即返回 SUBMITTING，并先推送一条 SUBMITTING（实测推送比返回早约 3 毫秒）
- step()：处理排队的事件——SUBMITTING → SUBMITTED；已请求的撤单 → CANCELLED_ALL / CANCELLED_PART
  （实测撤单刚返回时查询仍是 SUBMITTED，推送随后到达）
- fill(oid, qty, price)：测试手动注入成交（可部分成交，M2 改验收口径：部分成交由 FakeBroker 注入）
- 成交与 MoomooBroker 一样经 FillSynthesizer 合成（synthetic=True）
- strict_limits=True：按 vendor 限频（不加余量）检查写调用，超频抛 RateLimited，用来验证限流器
- 回调在锁外调用，避免回调里再调 broker 时死锁
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Any

from mooapp.broker.convert import FillSynthesizer
from mooapp.broker.port import (
    BrokerError,
    BrokerFill,
    BrokerOrder,
    Cash,
    NotSupported,
    NotUnlocked,
    OrderRejected,
    OrderRequest,
    Position,
    Quote,
    RateLimited,
)
from mooapp.broker.throttle import CANCEL_RULE, PLACE_RULE, RateRule
from mooapp.clock import Clock
from mooapp.common import Env, OrderKind, Side

_EPS = 1e-9
_FIRST_ORDER_ID = 900001


@dataclass
class _Pos:
    qty: int
    cost: Decimal  # 总成本


class _StrictLimit:
    """按 vendor 规则（不加余量）检查调用时刻。"""

    def __init__(self, rule: RateRule):
        self.rule = rule
        self._stamps: deque[float] = deque()

    def check(self, now: float) -> None:
        r = self.rule
        while self._stamps and now - self._stamps[0] >= r.window - _EPS:
            self._stamps.popleft()
        if self._stamps and now - self._stamps[-1] < r.min_interval - _EPS:
            raise RateLimited(f"{r.name}：两次调用间隔小于 {r.min_interval} 秒（FakeBroker 严格模式）")
        if len(self._stamps) >= r.max_calls:
            raise RateLimited(f"{r.name}：{r.window:g} 秒内超过 {r.max_calls} 次（FakeBroker 严格模式）")
        self._stamps.append(now)


class FakeBroker:
    def __init__(
        self,
        env: Env,
        clock: Clock,
        strict_limits: bool = False,
        cash: Decimal = Decimal("1000000"),
    ):
        self.env = env
        self._clock = clock
        self._lock = threading.RLock()
        self._limits = {"place_order": _StrictLimit(PLACE_RULE), "cancel_order": _StrictLimit(CANCEL_RULE)} if strict_limits else {}
        self._calls: dict[str, list[tuple[Any, ...]]] = {}
        self._orders: dict[str, BrokerOrder] = {}
        self._pending: list[tuple[str, str]] = []  # (动作, order_id)，由 step() 处理
        self._next_id = _FIRST_ORDER_ID
        self._cash = cash
        self._positions: dict[str, _Pos] = {}
        self._fills: list[BrokerFill] = []
        self._quotes: dict[str, Quote] = {}
        self._market_states: dict[str, str] = {}
        self._holidays: set[date] = set()
        self._unlocked = False
        self._synth = FillSynthesizer()
        self._order_cbs: list[Callable[[BrokerOrder], None]] = []
        self._fill_cbs: list[Callable[[BrokerFill], None]] = []

    # ------------------------------------------------------------------ 测试辅助

    def calls(self, name: str) -> list[tuple[Any, ...]]:
        with self._lock:
            return list(self._calls.get(name, []))

    def set_quote(
        self, symbol: str, last: Decimal, ask: Decimal | None = None, bid: Decimal | None = None, open: Decimal | None = None
    ) -> None:
        with self._lock:
            self._quotes[symbol] = Quote(symbol=symbol, last=last, open=open, ts=self._clock.now(), bid=bid, ask=ask)

    def set_market_state(self, symbol: str, state: str) -> None:
        with self._lock:
            self._market_states[symbol] = state

    def set_holiday(self, d: date) -> None:
        with self._lock:
            self._holidays.add(d)

    def set_position(self, symbol: str, qty: int, avg_cost: Decimal) -> None:
        with self._lock:
            self._positions[symbol] = _Pos(qty, avg_cost * qty)

    def step(self) -> None:
        """处理排队的状态变化并推送。"""
        events: list[BrokerOrder] = []
        with self._lock:
            pending, self._pending = self._pending, []
            for action, oid in pending:
                o = self._orders[oid]
                if o.is_terminal:
                    continue
                if action == "ack" and o.raw_status == "SUBMITTING":
                    events.append(self._update(oid, raw_status="SUBMITTED"))
                elif action == "cancel":
                    events.append(self._update(oid, raw_status="CANCELLED_PART" if o.dealt_qty else "CANCELLED_ALL"))
        self._dispatch(events, [])

    def fill(self, broker_order_id: str, qty: int, price: Decimal) -> BrokerFill:
        """注入一笔成交（可部分成交）。返回合成的成交。"""
        with self._lock:
            o = self._orders.get(broker_order_id)
            if o is None:
                raise BrokerError(f"订单不存在：{broker_order_id}")
            if o.is_terminal:
                raise BrokerError(f"订单 {broker_order_id} 已是终态 {o.raw_status}，不能成交")
            if type(qty) is not int or qty <= 0 or o.dealt_qty + qty > o.qty:
                raise BrokerError(f"成交数量 {qty!r} 非法：订单 {o.qty}，已成交 {o.dealt_qty}")
            cum = o.dealt_qty + qty
            avg = (o.dealt_avg_price * o.dealt_qty + price * qty) / cum
            updated = self._update(
                broker_order_id, dealt_qty=cum, dealt_avg_price=avg, raw_status="FILLED_ALL" if cum == o.qty else "FILLED_PART"
            )
            self._apply_fill(o.symbol, o.side, qty, price)
            f = self._synth.observe(updated)
            if f is None:
                raise BrokerError(f"内部错误：订单 {broker_order_id} 成交后没有生成成交记录")
            self._fills.append(f)
        self._dispatch([updated], [f])
        return f

    # ------------------------------------------------------------------ 内部

    def _record(self, name: str, *args: Any) -> None:
        self._calls.setdefault(name, []).append(args)
        lim = self._limits.get(name)
        if lim is not None:
            lim.check(self._clock.monotonic())

    def _update(self, oid: str, **changes: Any) -> BrokerOrder:
        o = replace(self._orders[oid], updated_at=self._clock.now(), **changes)
        self._orders[oid] = o
        return o

    def _apply_fill(self, symbol: str, side: Side, qty: int, price: Decimal) -> None:
        pos = self._positions.get(symbol, _Pos(0, Decimal(0)))
        if side is Side.BUY:
            self._cash -= price * qty
            self._positions[symbol] = _Pos(pos.qty + qty, pos.cost + price * qty)
        else:
            if qty > pos.qty:
                raise BrokerError(f"卖出 {qty} 股超过持仓 {pos.qty}（v1 不做空）")
            self._cash += price * qty
            left = pos.qty - qty
            self._positions[symbol] = _Pos(left, pos.cost * left / pos.qty if pos.qty else Decimal(0))

    def _dispatch(self, orders: list[BrokerOrder], fills: list[BrokerFill]) -> None:
        with self._lock:
            order_cbs, fill_cbs = list(self._order_cbs), list(self._fill_cbs)
        for o in orders:
            for cb in order_cbs:
                cb(o)
        for f in fills:
            for cb in fill_cbs:
                cb(f)

    def _check_real_unlocked(self) -> None:
        if self.env is Env.REAL and not self._unlocked:
            raise NotUnlocked("REAL 交易接口未解锁")

    # ------------------------------------------------------------------ 写

    def place_order(self, req: OrderRequest) -> BrokerOrder:
        with self._lock:
            self._record("place_order", req)
            self._check_real_unlocked()
            if req.side is Side.SELL:
                held = self._positions.get(req.symbol, _Pos(0, Decimal(0))).qty
                if req.qty > held:
                    raise OrderRejected(f"卖出 {req.qty} 股超过持仓 {held}（v1 不做空）")
            oid = str(self._next_id)
            self._next_id += 1
            now = self._clock.now()
            o = BrokerOrder(
                broker_order_id=oid,
                client_order_id=req.client_order_id,
                symbol=req.symbol,
                side=req.side,
                kind=req.kind,
                qty=req.qty,
                price=req.limit_price if req.kind is OrderKind.LIMIT else None,
                raw_status="SUBMITTING",
                dealt_qty=0,
                dealt_avg_price=Decimal(0),
                last_err_msg="",
                created_at=now,
                updated_at=now,
                remark=req.remark,
            )
            self._orders[oid] = o
            self._pending.append(("ack", oid))
        self._dispatch([o], [])
        return o

    def cancel_order(self, broker_order_id: str) -> None:
        with self._lock:
            self._record("cancel_order", broker_order_id)
            self._check_real_unlocked()
            o = self._orders.get(broker_order_id)
            if o is None:
                raise BrokerError(f"modify_order(CANCEL) 失败：订单不存在 {broker_order_id}")
            if o.is_terminal:
                raise BrokerError(f"modify_order(CANCEL) 失败：订单状态 {o.raw_status} 不允许撤单")
            self._pending.append(("cancel", broker_order_id))

    # ------------------------------------------------------------------ 读

    def query_orders(self, open_only: bool = True) -> list[BrokerOrder]:
        with self._lock:
            self._record("query_orders", open_only)
            orders = list(self._orders.values())
        return [o for o in orders if not o.is_terminal] if open_only else orders

    def query_deals(self) -> list[BrokerFill]:
        with self._lock:
            self._record("query_deals")
            if self.env is Env.SIMULATE:
                raise NotSupported("模拟盘不支持 deal_list_query（P4）")
            return list(self._fills)

    def query_fees(self, broker_order_ids: list[str]) -> dict[str, Decimal]:
        with self._lock:
            self._record("query_fees", tuple(broker_order_ids))
            if self.env is Env.SIMULATE:
                raise NotSupported("模拟盘不支持 order_fee_query（P7）")
            return {oid: Decimal(0) for oid in broker_order_ids}

    def query_positions(self) -> list[Position]:
        with self._lock:
            self._record("query_positions")
            return [
                Position(symbol=s, qty=p.qty, can_sell_qty=p.qty, avg_cost=p.cost / p.qty)  # P5：当日买入即可卖
                for s, p in self._positions.items()
                if p.qty > 0
            ]

    def query_cash(self) -> Cash:
        with self._lock:
            self._record("query_cash")
            return Cash(currency="USD", cash=self._cash, buying_power=self._cash)

    # ------------------------------------------------------------------ 推送

    def on_order_update(self, cb: Callable[[BrokerOrder], None]) -> None:
        with self._lock:
            self._order_cbs.append(cb)

    def on_fill(self, cb: Callable[[BrokerFill], None]) -> None:
        with self._lock:
            self._fill_cbs.append(cb)

    # ------------------------------------------------------------------ 会话

    def unlock(self, password: str) -> None:
        with self._lock:
            self._record("unlock")  # 不记录口令
            if self.env is Env.SIMULATE:
                raise BrokerError("模拟盘不需要解锁")
            self._unlocked = True

    def lock(self) -> None:
        with self._lock:
            self._record("lock")
            self._unlocked = False

    def close(self) -> None:
        with self._lock:
            self._record("close")

    # ------------------------------------------------------------------ 行情

    def snapshot(self, symbols: list[str]) -> list[Quote]:
        with self._lock:
            missing = [s for s in symbols if s not in self._quotes]
            if missing:
                raise BrokerError(f"get_market_snapshot 失败：没有行情 {missing}")
            return [self._quotes[s] for s in symbols]

    def market_state(self, symbol: str) -> str:
        with self._lock:
            if symbol not in self._market_states:
                raise BrokerError(f"get_market_state 未返回 {symbol}")
            return self._market_states[symbol]

    def is_trading_day(self, d: date) -> bool:
        with self._lock:
            return d.weekday() < 5 and d not in self._holidays
