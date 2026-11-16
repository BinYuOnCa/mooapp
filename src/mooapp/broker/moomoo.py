"""MoomooBroker：BrokerPort + MarketDataPort 的 Moomoo 实现（M2，只 SIMULATE；REAL 在 M4 经人锁）。

- SDK 模块由外部注入（open_broker 里来自 opend.open_contexts）；本模块不 import moomoo，单元测试注入桩。
- 每个交易调用显式传 trd_env 和 acc_id：vendor § Place Orders、§ Modify or Cancel Orders 的 trd_env
  默认值是 REAL，漏传即实盘（SPEC 5.3）。查询类接口用 refresh_cache=False（走 OpenD 缓存，不受限频）。
- ret != RET_OK 一律抛异常：下单 → OrderRejected，其它 → BrokerError（SPEC 2.3）。
- 模拟盘没有成交推送和 deal_list_query（vendor § Deals Push Callback、§ Get Today's Deals；P4）：
  只注册订单推送，由 FillSynthesizer 按 dealt_qty 增量合成成交（M2 计划第 5 点）。
- 推送回调在 SDK 线程里执行。SDK 会接住回调抛出的异常，只写进它自己的日志文件
  （SDK 源码 common/open_context_base.py packet_callback），所以这里先用 logging 记 ERROR 再抛出。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from mooapp.broker import opend
from mooapp.broker.convert import (
    FillSynthesizer,
    cash_from_row,
    order_from_row,
    position_from_row,
    quote_from_row,
    today_pnl_from_rows,
)
from mooapp.broker.port import (
    BrokerError,
    BrokerFill,
    BrokerOrder,
    Cash,
    NotSupported,
    OrderRejected,
    OrderRequest,
    Position,
    Quote,
)
from mooapp.broker.recorder import PushRecorder
from mooapp.broker.throttle import SHARED_REGISTRY, ThrottledBroker
from mooapp.clock import Clock
from mooapp.common import Env, OrderKind, Side
from mooapp.config.models import BrokerCfg

log = logging.getLogger(__name__)

_M4 = "REAL 在 M4 经人锁实现；M2 的 MoomooBroker 只接受 SIMULATE"


def _one_line(data: Any) -> str:
    return " ".join(str(data).split())


class MoomooBroker:
    def __init__(
        self,
        ft: Any,
        trade_ctx: Any,
        quote_ctx: Any,
        env: Env,
        acc_id: int,
        recorder: PushRecorder | None = None,
    ):
        if env is not Env.SIMULATE:
            raise BrokerError(_M4)
        self._ft = ft
        self._trade = trade_ctx
        self._quote = quote_ctx
        self.env = env
        self._acc_id = acc_id
        self._trd_env = ft.TrdEnv.SIMULATE
        self._recorder = recorder
        self._synth = FillSynthesizer()
        self._order_cbs: list[Callable[[BrokerOrder], None]] = []
        self._fill_cbs: list[Callable[[BrokerFill], None]] = []
        self._cb_lock = threading.Lock()
        # 闸门每次下单都要持仓和当日盈亏，两者来自同一个 position_list_query：短时缓存，只查一次（M4 审查小问题）
        self._pos_lock = threading.Lock()
        self._pos_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._register_order_handler()

    # ------------------------------------------------------------------ 内部

    def _ok(self, api: str, result: tuple[int, Any], exc: type[BrokerError] = BrokerError) -> Any:
        ret, data = result
        if ret != self._ft.RET_OK:
            raise exc(f"{api} 失败：{_one_line(data)}")
        return data

    def _rows(self, api: str, result: tuple[int, Any]) -> list[dict[str, Any]]:
        return self._ok(api, result).to_dict("records")

    POSITIONS_CACHE_SECONDS = 0.5

    def _position_rows(self) -> list[dict[str, Any]]:
        # vendor § Get Positions；refresh_cache=False 走 OpenD 缓存
        with self._pos_lock:
            now = time.monotonic()
            if self._pos_cache is not None and now - self._pos_cache[0] < self.POSITIONS_CACHE_SECONDS:
                return self._pos_cache[1]
            rows = self._rows(
                "position_list_query",
                self._trade.position_list_query(trd_env=self._trd_env, acc_id=self._acc_id, refresh_cache=False),
            )
            self._pos_cache = (now, rows)
            return rows

    def _invalidate_positions(self) -> None:
        with self._pos_lock:
            self._pos_cache = None

    def _register_order_handler(self) -> None:
        # vendor § Orders Push Callback：继承 TradeOrderHandlerBase，super().on_recv_rsp 解析出 (ret, DataFrame)
        broker = self

        class _OrderHandler(self._ft.TradeOrderHandlerBase):
            def on_recv_rsp(self, rsp_pb: Any) -> Any:
                ret, data = super().on_recv_rsp(rsp_pb)
                broker._handle_order_push(ret, data)
                return ret, data

        ret = self._trade.set_handler(_OrderHandler())
        if ret != self._ft.RET_OK:
            raise BrokerError(f"set_handler 失败：ret={ret}")

    def _handle_order_push(self, ret: int, data: Any) -> None:
        self._invalidate_positions()  # 订单有变化（可能成交）：下次重新查持仓
        try:
            if ret != self._ft.RET_OK:
                if self._recorder is not None:
                    self._recorder.record("order_error", _one_line(data))
                raise BrokerError(f"订单推送失败：{_one_line(data)}")
            rows = data.to_dict("records")
            if self._recorder is not None:
                for row in rows:
                    self._recorder.record("order", row)
            for row in rows:
                order = order_from_row(row)
                fill = self._synth.observe(order)
                with self._cb_lock:
                    order_cbs, fill_cbs = list(self._order_cbs), list(self._fill_cbs)
                for cb in order_cbs:
                    cb(order)
                if fill is not None:
                    for cb in fill_cbs:
                        cb(fill)
        except Exception:
            log.exception("处理订单推送出错（SDK 只会把异常写进它自己的日志）")
            raise

    # ------------------------------------------------------------------ 写

    def place_order(self, req: OrderRequest) -> BrokerOrder:
        self._invalidate_positions()
        ft = self._ft
        # vendor § Place Orders。市价单 price 可传任意值，传 0.0；adjust_limit=0：不让 OpenD 改价（FAQ Q14、ADR-0009）
        rows = self._ok(
            "place_order",
            self._trade.place_order(
                price=float(req.limit_price) if req.kind is OrderKind.LIMIT else 0.0,
                qty=req.qty,
                code=req.symbol,
                trd_side=ft.TrdSide.BUY if req.side is Side.BUY else ft.TrdSide.SELL,
                order_type=ft.OrderType.NORMAL if req.kind is OrderKind.LIMIT else ft.OrderType.MARKET,
                adjust_limit=0,
                trd_env=self._trd_env,
                acc_id=self._acc_id,
                remark=req.remark,
                time_in_force=ft.TimeInForce.DAY,  # ADR-0004
                session=ft.Session.RTH,  # ADR-0003
            ),
            OrderRejected,
        ).to_dict("records")
        if len(rows) != 1:
            raise BrokerError(f"place_order 应返回 1 行，实际 {len(rows)} 行")
        return order_from_row(rows[0])

    def cancel_order(self, broker_order_id: str) -> None:
        self._invalidate_positions()
        # vendor § Modify or Cancel Orders：撤单时 qty、price 被忽略，传 0
        self._ok(
            "modify_order(CANCEL)",
            self._trade.modify_order(
                modify_order_op=self._ft.ModifyOrderOp.CANCEL,
                order_id=broker_order_id,
                qty=0,
                price=0,
                trd_env=self._trd_env,
                acc_id=self._acc_id,
            ),
        )

    # ------------------------------------------------------------------ 读

    def query_orders(self, open_only: bool = True, refresh: bool = False) -> list[BrokerOrder]:
        # vendor § Get open Orders：全部未完成订单 + 24 小时内成交或撤销的订单；
        # refresh_cache=True 直接向服务器请求、不用 OpenD 缓存（受 30 秒 10 次限频，ThrottledBroker 负责）
        rows = self._rows(
            "order_list_query",
            self._trade.order_list_query(trd_env=self._trd_env, acc_id=self._acc_id, refresh_cache=refresh),
        )
        orders = [order_from_row(r) for r in rows]
        return [o for o in orders if not o.is_terminal] if open_only else orders

    def query_deals(self) -> list[BrokerFill]:
        # vendor § Get Today's Deals：只支持实盘（P4 实测 "Paper trading does not support deal data."）
        raise NotSupported("模拟盘不支持 deal_list_query（P4）；成交由订单推送的 dealt_qty 合成")

    def query_fees(self, broker_order_ids: list[str]) -> dict[str, Decimal]:
        # vendor § Get Order Fee：不支持模拟账户（P7 实测 "Paper trading is not supported."）
        raise NotSupported("模拟盘不支持 order_fee_query（P7）")

    def query_positions(self) -> list[Position]:
        # vendor § Get Positions
        return [p for p in (position_from_row(r) for r in self._position_rows()) if p is not None]

    def query_today_pnl(self) -> Decimal | None:
        # vendor § Get Positions › today_pl_val；走缓存（只有刷新缓存时才受限频）；全部行，含 qty=0
        return today_pnl_from_rows(self._position_rows())

    def query_cash(self) -> Cash:
        # vendor § Get Account Funds：currency 只对综合账户生效；与 M1 一致显式传 USD
        rows = self._rows(
            "accinfo_query",
            self._trade.accinfo_query(
                trd_env=self._trd_env, acc_id=self._acc_id, refresh_cache=False, currency=self._ft.Currency.USD
            ),
        )
        if len(rows) != 1:
            raise BrokerError(f"accinfo_query 应返回 1 行，实际 {len(rows)} 行")
        return cash_from_row(rows[0])

    # ------------------------------------------------------------------ 推送

    def on_order_update(self, cb: Callable[[BrokerOrder], None]) -> None:
        with self._cb_lock:
            self._order_cbs.append(cb)

    def on_fill(self, cb: Callable[[BrokerFill], None]) -> None:
        with self._cb_lock:
            self._fill_cbs.append(cb)

    # ------------------------------------------------------------------ 会话

    def unlock(self, password: str) -> None:
        raise BrokerError("模拟盘不需要解锁；" + _M4)

    def lock(self) -> None:
        return None  # 模拟盘没有锁

    def close(self) -> None:
        # vendor § Quote Object › Close Connection
        self._trade.close()
        self._quote.close()
        if self._recorder is not None:
            self._recorder.close()

    # ------------------------------------------------------------------ 行情（MarketDataPort）

    def snapshot(self, symbols: list[str]) -> list[Quote]:
        # vendor § Get Market Snapshot
        rows = self._rows("get_market_snapshot", self._quote.get_market_snapshot(list(symbols)))
        return [quote_from_row(r) for r in rows]

    def market_state(self, symbol: str) -> str:
        # vendor § Get Market Status of Securities；取值见 § Trading Definitions › Market State
        rows = self._rows("get_market_state", self._quote.get_market_state([symbol]))
        states = [str(r["market_state"]) for r in rows if r.get("code") == symbol]
        if len(states) != 1:
            raise BrokerError(f"get_market_state 未返回 {symbol} 的唯一结果：{rows}")
        return states[0]

    def is_trading_day(self, d: date) -> bool:
        # vendor § Get Trading Calendar：返回 list[dict]；只排除周末和节假日，不含临时休市
        day = d.isoformat()
        days = self._ok(
            "request_trading_days",
            self._quote.request_trading_days(market=self._ft.TradeDateMarket.US, start=day, end=day),
        )
        return len(days) > 0


def open_broker(
    env: Env,
    cfg: BrokerCfg,
    clock: Clock,
    record_dir: Path | None = None,
    timeout: float = 5.0,
) -> ThrottledBroker:
    """连接 OpenD，返回带限流的 MoomooBroker。record_dir 不为 None 时把推送录到该目录。"""
    if env is not Env.SIMULATE:
        raise BrokerError(_M4)
    ft, quote, trade = opend.open_contexts(cfg, timeout)
    recorder = None
    try:
        if record_dir is not None:
            # vendor § Get global market status：server_ver 为 OpenD 版本
            ret, gs = quote.get_global_state()
            if ret != ft.RET_OK:
                raise BrokerError(f"get_global_state 失败：{_one_line(gs)}")
            recorder = PushRecorder(
                record_dir, env=env, clock=clock, meta={"sdk": opend.sdk_version(), "opend": str(gs.get("server_ver", ""))}
            )
        broker = MoomooBroker(ft, trade, quote, env=env, acc_id=cfg.simulate_acc_id, recorder=recorder)
    except BaseException:  # 清理连接后原样重新抛出，不吞
        trade.close()
        quote.close()
        if recorder is not None:
            recorder.close()
        raise
    # 限频按账户计算（learnings M3）：同一进程里按 (env, acc_id) 共用限流器
    return ThrottledBroker(broker, clock, registry=SHARED_REGISTRY, account_key=(env, cfg.simulate_acc_id))
