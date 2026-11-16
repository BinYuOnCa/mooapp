"""券商端口（SPEC 5.3）：数据类、异常、remark 编解码、最小变动单位，以及 BrokerPort / MarketDataPort 协议。

本模块不 import SDK；上层（oms、risk、strategy）只依赖这里的类型。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from mooapp.common import Env, MooappError, OrderKind, Side

# ------------------------------------------------------------------ 异常（SDK 的 ret != RET_OK 全部映射，不得吞掉）


class BrokerError(MooappError):
    """SDK 返回 ret != RET_OK，或返回值无法解析。"""


class BrokerUnavailable(BrokerError):
    """OpenD 不可达 / 未在限定时间内就绪 / 断线。"""


class RateLimited(BrokerError):
    """超过 vendor 限频（限流器等待时间超过上限，或 FakeBroker 严格模式）。"""


class OrderRejected(BrokerError):
    """下单被券商拒绝（place_order 返回 ret != RET_OK）。"""


class NotUnlocked(BrokerError):
    """REAL 交易接口未解锁。"""


class NotSupported(BrokerError):
    """当前环境不支持该接口（如模拟盘的 deal_list_query、order_fee_query，learnings P4、P7）。"""


class InvalidOrder(MooappError, ValueError):
    """OrderRequest 构造时校验失败（M2 计划第 6 点）。"""


# ------------------------------------------------------------------ remark 与 client_order_id（ADR-0019；M2 计划第 4 点）

REMARK_PREFIX = "moo:"
_CID = re.compile(r"^[0-9a-f]{32}$")
_SYMBOL = re.compile(r"^US\.[A-Z0-9][A-Z0-9.\-]*$")  # 只做美股（ADR-0001），与 config/models.py 一致


def new_client_order_id() -> str:
    return uuid.uuid4().hex


def _check_cid(cid: object) -> str:
    if not isinstance(cid, str) or not _CID.match(cid):
        raise InvalidOrder(f"client_order_id 必须是 32 位小写十六进制，收到 {cid!r}")
    return cid


def encode_remark(client_order_id: str) -> str:
    # vendor § Place Orders › remark：≤ 64 字节 UTF-8；"moo:" + 32 位 = 36 字节
    return REMARK_PREFIX + _check_cid(client_order_id)


def parse_remark(remark: object) -> str | None:
    """本程序的订单返回 client_order_id；其它（含 probe:、手工单、空）返回 None，视为外部订单。"""
    if not isinstance(remark, str) or not remark.startswith(REMARK_PREFIX):
        return None
    cid = remark[len(REMARK_PREFIX) :]
    return cid if _CID.match(cid) else None


# ------------------------------------------------------------------ 最小变动单位（vendor FAQ Q14 › Rules on Price Spread：美股 ≥ $1 为 0.01，< $1 为 0.0001）

_TICK_GE_1 = Decimal("0.01")
_TICK_LT_1 = Decimal("0.0001")


def tick_size(price: Decimal) -> Decimal:
    return _TICK_GE_1 if price >= 1 else _TICK_LT_1


def is_on_tick(price: Decimal) -> bool:
    return price % tick_size(price) == 0


# ------------------------------------------------------------------ 数据类


@dataclass(frozen=True)
class OrderRequest:
    """time_in_force 固定 DAY，session 固定 RTH（ADR-0003、0004）。构造时校验，价格不自动取整（ADR-0009）。"""

    client_order_id: str
    symbol: str
    side: Side
    qty: int
    kind: OrderKind
    limit_price: Decimal | None

    def __post_init__(self) -> None:
        _check_cid(self.client_order_id)
        if not isinstance(self.symbol, str) or not _SYMBOL.match(self.symbol):
            raise InvalidOrder(f"symbol 只收 US.* 代码（ADR-0001），收到 {self.symbol!r}")
        if not isinstance(self.side, Side):
            raise InvalidOrder(f"side 必须是 Side，收到 {self.side!r}")
        if not isinstance(self.kind, OrderKind):
            raise InvalidOrder(f"kind 必须是 OrderKind，收到 {self.kind!r}")
        if type(self.qty) is not int or self.qty <= 0:  # bool 是 int 的子类，用 type() 排除
            raise InvalidOrder(f"qty 必须是正整数（美股整股），收到 {self.qty!r}")
        if self.kind is OrderKind.MARKET:
            if self.limit_price is not None:
                raise InvalidOrder("市价单不能带 limit_price")
            return
        p = self.limit_price
        if not isinstance(p, Decimal):
            raise InvalidOrder(f"限价单的 limit_price 必须是 Decimal，收到 {p!r}")
        if not p.is_finite() or p <= 0:
            raise InvalidOrder(f"limit_price 必须为正数，收到 {p}")
        if not is_on_tick(p):
            raise InvalidOrder(f"limit_price {p} 不在最小变动单位 {tick_size(p)} 上（ADR-0009：不自动取整）")

    @property
    def remark(self) -> str:
        return encode_remark(self.client_order_id)


# vendor § Trading Definitions › Order Status：这些状态不会再变化
TERMINAL_STATUSES = frozenset({"FILLED_ALL", "CANCELLED_PART", "CANCELLED_ALL", "FAILED", "DISABLED", "DELETED"})


@dataclass(frozen=True)
class BrokerOrder:
    """券商视角的订单快照（查询或推送）。raw_status 是 Moomoo OrderStatus 原文，映射在 oms（M3）。"""

    broker_order_id: str
    client_order_id: str | None  # 来自 remark；外部订单为 None
    symbol: str
    side: Side
    kind: OrderKind | None  # None = 非 v1 类型的外部订单（M2 计划第 8 点）
    qty: int
    price: Decimal | None  # 市价单为 None
    raw_status: str
    dealt_qty: int
    dealt_avg_price: Decimal
    last_err_msg: str
    created_at: datetime  # UTC（P3）
    updated_at: datetime  # UTC（P3）
    remark: str  # 原文

    @property
    def is_terminal(self) -> bool:
        return self.raw_status in TERMINAL_STATUSES


@dataclass(frozen=True)
class BrokerFill:
    """fill_id：实盘为 deal_id；模拟盘合成为 "order_id:累计成交量"（synthetic=True，SPEC Q2、P4）。"""

    fill_id: str
    broker_order_id: str
    client_order_id: str | None
    symbol: str
    side: Side
    qty: int
    price: Decimal
    ts: datetime  # UTC
    synthetic: bool


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: int
    can_sell_qty: int
    avg_cost: Decimal | None  # vendor § Get Positions：average_cost 对证券模拟账户可能无效
    today_pl_val: Decimal | None = None  # vendor § Get Positions › today_pl_val；缺失或无效为 None（模拟盘是否有效见 P10）


@dataclass(frozen=True)
class Cash:
    currency: str
    cash: Decimal
    buying_power: Decimal


@dataclass(frozen=True)
class Quote:
    symbol: str
    last: Decimal
    open: Decimal | None  # 开盘前为 None
    ts: datetime  # UTC
    bid: Decimal | None = None
    ask: Decimal | None = None


# ------------------------------------------------------------------ 协议


class BrokerPort(Protocol):
    env: Env

    # 写（M3 起只允许 oms.executor 调用）；v1 不提供改单（Q4）
    def place_order(self, req: OrderRequest) -> BrokerOrder: ...
    def cancel_order(self, broker_order_id: str) -> None: ...

    # 读
    # refresh=True 绕过 OpenD 缓存（vendor § Get open Orders › refresh_cache），受 30 秒 10 次限频；
    # M3 的 Resolver 与启动恢复用它确认订单是否存在
    def query_orders(self, open_only: bool = True, refresh: bool = False) -> list[BrokerOrder]: ...
    def query_deals(self) -> list[BrokerFill]: ...
    def query_fees(self, broker_order_ids: list[str]) -> dict[str, Decimal]: ...
    def query_positions(self) -> list[Position]: ...
    # 当日盈亏：全部持仓行（含当天卖光、qty=0 的行）today_pl_val 之和；任一行无效返回 None（M4 确认点 2）
    def query_today_pnl(self) -> Decimal | None: ...
    def query_cash(self) -> Cash: ...

    # 推送
    def on_order_update(self, cb: Callable[[BrokerOrder], None]) -> None: ...
    def on_fill(self, cb: Callable[[BrokerFill], None]) -> None: ...

    # 会话
    def unlock(self, password: str) -> None: ...
    def lock(self) -> None: ...
    def close(self) -> None: ...


class MarketDataPort(Protocol):
    def snapshot(self, symbols: list[str]) -> list[Quote]: ...
    def market_state(self, symbol: str) -> str: ...  # 按标的查询（M1 实测，learnings）
    def is_trading_day(self, d: date) -> bool: ...
    # subscribe_quotes 推到 M6（M2 计划第 7 点）
