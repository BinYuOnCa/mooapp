"""FakeBroker：内存里的 BrokerPort + MarketDataPort，行为照模拟盘实测（learnings「M2 券商适配层：探针结论」）。

- place_order 立即返回 SUBMITTING，并先推送一条 SUBMITTING（实测推送比返回早约 3 毫秒）
- step()：处理排队的事件——SUBMITTING → SUBMITTED；已请求的撤单 → CANCELLED_ALL / CANCELLED_PART
  （实测撤单刚返回时查询仍是 SUBMITTED，推送随后到达）
- fill(oid, qty, price)：测试手动注入成交（可部分成交，M2 改验收口径：部分成交由 FakeBroker 注入）
- 成交与 MoomooBroker 一样经 FillSynthesizer 合成（synthetic=True）
- strict_limits=True：按 vendor 限频（不加余量）检查写调用，超频抛 RateLimited，用来验证限流器
- 回调在锁外调用，避免回调里再调 broker 时死锁

M3 新增（测试辅助）：
- fail_next_place(exc, accepted, times)：下一次（或几次）place_order 抛 exc；accepted=True 表示券商其实已收单
  （模拟「返回丢了」），accepted=False 表示根本没到券商
- replay(rows)：把录制的推送行（PushRecorder 格式里的 row）经 order_from_row 推给回调，同时更新券商端状态
- seed_order / seed_fill：预置券商端订单或成交，不推送
- REAL 环境的 fill() 生成非合成的成交 id（D1、D2…），模拟实盘成交推送带 deal_id（vendor § Deals Push Callback）
- M4：set_today_pnl / query_today_pnl（当日盈亏，None 表示字段无效）
- M6b：subscribe_quotes / push_quote（报价推送）；snapshot 也记录调用
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Any

from mooapp.broker.convert import FillSynthesizer, order_from_row
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
        self._place_failures: deque[tuple[BrokerError, bool]] = deque()
        self._next_deal = 1
        self._today_pnl: Decimal | None = Decimal(0)  # M4：query_today_pnl 的返回值，测试用 set_today_pnl 设置
        self._quote_cbs: list[tuple[frozenset[str], Callable[[Quote], None]]] = []  # M6b

    # ------------------------------------------------------------------ 测试辅助（M3）

    def fail_next_place(self, exc: BrokerError, accepted: bool, times: int = 1) -> None:
        with self._lock:
            for _ in range(times):
                self._place_failures.append((exc, accepted))

    def replay(self, rows: list[dict[str, Any]]) -> None:
        """回放录制的推送行：逐行转换、更新券商端状态并推送。"""
        for row in rows:
            o = order_from_row(row)
            with self._lock:
                self._orders[o.broker_order_id] = o
            self._dispatch([o], [])

    def seed_order(self, order: BrokerOrder) -> None:
        with self._lock:
            self._orders[order.broker_order_id] = order

    def seed_fill(self, fill: BrokerFill) -> None:
        with self._lock:
            self._fills.append(fill)

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

    def set_today_pnl(self, value: Decimal | None) -> None:
        """M4：设定当日盈亏（None 表示 today_pl_val 无效）。FakeBroker 不按成交推算盈亏。"""
        with self._lock:
            self._today_pnl = value

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
            if self.env is Env.REAL:
                f = BrokerFill(
                    fill_id=f"D{self._next_deal}",
                    broker_order_id=broker_order_id,
                    client_order_id=o.client_order_id,
                    symbol=o.symbol,
                    side=o.side,
                    qty=qty,
                    price=price,
                    ts=updated.updated_at,
                    synthetic=False,
                )
                self._next_deal += 1
            else:
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
            failure = self._place_failures.popleft() if self._place_failures else None
            if failure is not None and not failure[1]:
                raise failure[0]  # 没到券商
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
        if failure is not None:
            raise failure[0]  # 券商已收单，但返回丢了
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

    def query_orders(self, open_only: bool = True, refresh: bool = False) -> list[BrokerOrder]:
        with self._lock:
            self._record("query_orders", open_only, refresh)
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

    def query_today_pnl(self) -> Decimal | None:
        with self._lock:
            self._record("query_today_pnl")
            return self._today_pnl

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

    def subscribe_quotes(self, symbols: list[str], cb: Callable[[Quote], None]) -> None:
        with self._lock:
            self._record("subscribe_quotes", tuple(symbols))
            self._quote_cbs.append((frozenset(symbols), cb))

    def push_quote(self, symbol: str, last: Decimal, open: Decimal | None = None) -> Quote:
        """M6b 测试辅助：推一条报价（同时更新快照），只推给订阅了该标的的回调；回调在锁外调用。"""
        with self._lock:
            prev = self._quotes.get(symbol)
            q = Quote(symbol=symbol, last=last, open=open if open is not None else (prev.open if prev else None),
                      ts=self._clock.now(), bid=last, ask=last)  # fmt: skip
            self._quotes[symbol] = q
            cbs = [cb for syms, cb in self._quote_cbs if symbol in syms]
        for cb in cbs:
            cb(q)
        return q

    def snapshot(self, symbols: list[str]) -> list[Quote]:
        with self._lock:
            self._record("snapshot", tuple(symbols))
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
