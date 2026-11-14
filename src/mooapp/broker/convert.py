"""SDK 返回的行（DataFrame.to_dict("records") 的一项，或推送的一行）→ port 数据类。

纯函数，不 import SDK。字段名与取值以 vendor 各接口的 Return 表为准，样例见 learnings「M2 券商适配层：探针结论」。
解析失败一律抛 BrokerError（SPEC 2.3：不吞错，不猜）。
"""

from __future__ import annotations

import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from mooapp.broker.port import BrokerError, BrokerFill, BrokerOrder, Cash, Position, Quote, parse_remark
from mooapp.common import OrderKind, Side

MARKET_TZ = ZoneInfo("America/New_York")
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f")

# vendor § Trading Definitions › Order Type：v1 只认这两种（ADR-0004）；其它类型的外部订单 kind=None
_KIND = {"NORMAL": OrderKind.LIMIT, "MARKET": OrderKind.MARKET}
_MISSING = ("", "N/A")


def _req(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except KeyError as e:
        raise BrokerError(f"返回行缺少字段 {key!r}：{sorted(row)}") from e


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value in _MISSING) or (isinstance(value, float) and math.isnan(value))


def _dec(value: Any, field: str) -> Decimal:
    """float 经 str 转换，避免二进制尾数（123.965 → Decimal("123.965")）。"""
    if isinstance(value, bool):
        raise BrokerError(f"{field} 不是数值：{value!r}")
    try:
        d = Decimal(str(value))
    except InvalidOperation as e:
        raise BrokerError(f"{field} 不是数值：{value!r}") from e
    if not d.is_finite():
        raise BrokerError(f"{field} 不是有限数值：{value!r}")
    return d


def _opt_dec(value: Any, field: str, zero_is_none: bool = False) -> Decimal | None:
    if _is_missing(value):
        return None
    d = _dec(value, field)
    return None if zero_is_none and d == 0 else d


def _qty(value: Any, field: str) -> int:
    """美股整股（SPEC 5 节）。出现零股说明假设不成立：报错，不截断。"""
    d = _dec(value, field)
    if d != d.to_integral_value():
        raise BrokerError(f"{field} 不是整数股：{value!r}")
    return int(d)


def parse_et_time(text: Any) -> datetime:
    """P3：create_time / updated_time / update_time 是美东本地时间 yyyy-MM-dd HH:mm:ss（可带毫秒）→ UTC。"""
    if isinstance(text, str):
        for fmt in _TIME_FORMATS:
            try:
                local = datetime.strptime(text, fmt)
            except ValueError:
                continue
            return local.replace(tzinfo=MARKET_TZ).astimezone(UTC)
    raise BrokerError(f"无法解析的时间：{text!r}（期望美东时间 yyyy-MM-dd HH:mm:ss）")


def _side(value: Any) -> Side:
    # vendor § Trading Definitions › TrdSide 还有 SELL_SHORT、BUY_BACK；v1 不支持，报错而不是猜映射
    try:
        return Side(value)
    except ValueError as e:
        raise BrokerError(f"不支持的交易方向 {value!r}（v1 只有 BUY / SELL）") from e


def order_from_row(row: Mapping[str, Any]) -> BrokerOrder:
    """order_list_query、place_order 的返回行与订单推送行同构（vendor § Get open Orders、§ Orders Push Callback）。"""
    kind = _KIND.get(str(_req(row, "order_type")))
    price = _dec(_req(row, "price"), "price")
    remark = row.get("remark")
    remark = remark if isinstance(remark, str) else ""
    last_err = row.get("last_err_msg")
    return BrokerOrder(
        broker_order_id=str(_req(row, "order_id")),
        client_order_id=parse_remark(remark),
        symbol=str(_req(row, "code")),
        side=_side(_req(row, "trd_side")),
        kind=kind,
        qty=_qty(_req(row, "qty"), "qty"),
        price=None if kind is OrderKind.MARKET or price == 0 else price,  # 市价单回报 price=0.0（learnings M2）
        raw_status=str(_req(row, "order_status")),
        dealt_qty=_qty(_req(row, "dealt_qty"), "dealt_qty"),
        dealt_avg_price=_dec(_req(row, "dealt_avg_price"), "dealt_avg_price"),
        last_err_msg=last_err if isinstance(last_err, str) else "",
        created_at=parse_et_time(_req(row, "create_time")),
        updated_at=parse_et_time(_req(row, "updated_time")),
        remark=remark,
    )


def position_from_row(row: Mapping[str, Any]) -> Position | None:
    """vendor § Get Positions。卖光后仍返回 qty=0 的行（learnings M2）→ None。"""
    qty = _qty(_req(row, "qty"), "qty")
    if qty == 0:
        return None
    return Position(
        symbol=str(_req(row, "code")),
        qty=qty,
        can_sell_qty=_qty(_req(row, "can_sell_qty"), "can_sell_qty"),
        avg_cost=_opt_dec(row.get("average_cost"), "average_cost"),
        today_pl_val=_opt_dec(row.get("today_pl_val"), "today_pl_val"),
    )


def today_pnl_from_rows(rows: list[Mapping[str, Any]]) -> Decimal | None:
    """vendor § Get Positions › today_pl_val 之和，含 qty=0 的行（当天卖光的盈亏，learnings M2：卖光后行仍在）。

    任一行缺失、N/A、NaN → None（不可用），由调用方决定怎么处理（M4：闸门拒绝买单），不猜、不当 0。
    """
    total = Decimal(0)
    for row in rows:
        v = row.get("today_pl_val")
        if _is_missing(v):
            return None
        total += _dec(v, "today_pl_val")
    return total


def cash_from_row(row: Mapping[str, Any]) -> Cash:
    """vendor § Get Account Funds。查询时已指定 currency=USD；单市场账户回报 currency 为 N/A，因此固定写 USD。"""
    return Cash(
        currency="USD",
        cash=_dec(_req(row, "cash"), "cash"),
        buying_power=_dec(_req(row, "power"), "power"),
    )


def quote_from_row(row: Mapping[str, Any]) -> Quote:
    """vendor § Get Market Snapshot。open_price 开盘前为 0 → None；ask/bid 缺失或为 0 → None。"""
    return Quote(
        symbol=str(_req(row, "code")),
        last=_dec(_req(row, "last_price"), "last_price"),
        open=_opt_dec(row.get("open_price"), "open_price", zero_is_none=True),
        ts=parse_et_time(_req(row, "update_time")),
        bid=_opt_dec(row.get("bid_price"), "bid_price", zero_is_none=True),
        ask=_opt_dec(row.get("ask_price"), "ask_price", zero_is_none=True),
    )


# ------------------------------------------------------------------ 合成成交（M2 计划第 5 点；SPEC Q2；P4：模拟盘没有成交推送和 deal_list_query）

_PRICE_Q = Decimal("0.000001")  # 反推的成交价保留 6 位小数（vendor：dealt_avg_price 无精度限制）


@dataclass
class _Seen:
    cum: int
    avg: Decimal


class FillSynthesizer:
    """按订单记住已见到的累计成交量；dealt_qty 增加时生成一条成交，价格由均价反推。

    重复推送、乱序到达的旧推送（累计量不增加）不生成成交，也不回退基准。线程安全。
    """

    def __init__(self) -> None:
        self._seen: dict[str, _Seen] = {}
        self._lock = threading.Lock()

    def observe(self, order: BrokerOrder) -> BrokerFill | None:
        with self._lock:
            prev = self._seen.get(order.broker_order_id, _Seen(0, Decimal(0)))
            if order.dealt_qty <= prev.cum:
                return None
            delta = order.dealt_qty - prev.cum
            price = (order.dealt_avg_price * order.dealt_qty - prev.avg * prev.cum) / delta
            self._seen[order.broker_order_id] = _Seen(order.dealt_qty, order.dealt_avg_price)
        return BrokerFill(
            fill_id=f"{order.broker_order_id}:{order.dealt_qty}",
            broker_order_id=order.broker_order_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            qty=delta,
            price=price.quantize(_PRICE_Q),
            ts=order.updated_at,
            synthetic=True,
        )
