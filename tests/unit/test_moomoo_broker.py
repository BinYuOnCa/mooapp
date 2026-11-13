"""MoomooBroker（broker/moomoo.py）用 SDK 桩测试；另含 PushRecorder 与 opend.connect 的快速失败。

sandbox 里 `import moomoo` 会失败（learnings「阶段 0」），所以 MoomooBroker 的 SDK 模块由外部注入，
这里注入一个只含所需枚举的桩。桩里的枚举值用字符串，与 SDK DataFrame 里的取值一致。
"""

from __future__ import annotations

import json
import socket
import sys
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from mooapp.broker import opend
from mooapp.broker.moomoo import MoomooBroker
from mooapp.broker.port import (
    BrokerError,
    BrokerOrder,
    BrokerUnavailable,
    NotSupported,
    OrderRejected,
    OrderRequest,
)
from mooapp.broker.recorder import PushRecorder
from mooapp.clock import FakeClock
from mooapp.common import Env, OrderKind, Side
from mooapp.config.models import BrokerCfg

ACC = 2964165
CID = "0123456789abcdef0123456789abcdef"
T0 = datetime(2026, 11, 13, 14, 31, 0, tzinfo=UTC)


class DF:
    """DataFrame 桩：MoomooBroker 只允许用 to_dict("records")。"""

    def __init__(self, rows: list[dict]):
        self.rows = rows

    def to_dict(self, orient: str) -> list[dict]:
        assert orient == "records"
        return [dict(r) for r in self.rows]


class HandlerBase:
    """TradeOrderHandlerBase 桩：真 SDK 在 on_recv_rsp 里把 protobuf 解析成 (ret, DataFrame)；
    桩直接把传入的 (ret, DF) 原样返回。"""

    def on_recv_rsp(self, rsp_pb):
        return rsp_pb


class OrderHandlerBase(HandlerBase):
    pass


class DealHandlerBase(HandlerBase):
    pass


FT = SimpleNamespace(
    RET_OK=0,
    RET_ERROR=-1,
    TrdEnv=SimpleNamespace(SIMULATE="SIMULATE", REAL="REAL"),
    TrdSide=SimpleNamespace(BUY="BUY", SELL="SELL"),
    OrderType=SimpleNamespace(NORMAL="NORMAL", MARKET="MARKET"),
    TimeInForce=SimpleNamespace(DAY="DAY"),
    Session=SimpleNamespace(RTH="RTH"),
    ModifyOrderOp=SimpleNamespace(CANCEL="CANCEL"),
    Currency=SimpleNamespace(USD="USD"),
    TradeDateMarket=SimpleNamespace(US="US"),
    TradeOrderHandlerBase=OrderHandlerBase,
    TradeDealHandlerBase=DealHandlerBase,
)


def order_row(**kw) -> dict:
    row = {
        "code": "US.SPY",
        "trd_side": "BUY",
        "order_type": "NORMAL",
        "order_status": "SUBMITTING",
        "order_id": "840514",
        "qty": 1.0,
        "price": 683.61,
        "create_time": "2026-11-13 09:31:31",
        "updated_time": "2026-11-13 09:31:31",
        "dealt_qty": 0.0,
        "dealt_avg_price": 0.0,
        "last_err_msg": "",
        "remark": "moo:" + CID,
    }
    row.update(kw)
    return row


class TradeCtx:
    """记录每次调用的参数；返回值可按接口名预置。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.handlers: list[object] = []
        self.closed = False
        self.responses: dict[str, tuple[int, object]] = {
            "place_order": (0, DF([order_row()])),
            "modify_order": (0, DF([{"trd_env": "SIMULATE", "order_id": "840514"}])),
            "order_list_query": (0, DF([order_row(order_status="SUBMITTED"), order_row(order_id="840515", order_status="CANCELLED_ALL")])),
            "position_list_query": (
                0,
                DF(
                    [
                        {"code": "US.SPY", "qty": 1.0, "can_sell_qty": 1.0, "average_cost": 759.67},
                        {"code": "US.TD", "qty": 0.0, "can_sell_qty": 0.0, "average_cost": 0.0},
                    ]
                ),
            ),
            "accinfo_query": (0, DF([{"currency": "N/A", "cash": 1000000.0, "power": 2000000.0}])),
        }

    def _call(self, name, kw):
        self.calls.append((name, kw))
        return self.responses[name]

    def place_order(self, **kw):
        return self._call("place_order", kw)

    def modify_order(self, **kw):
        return self._call("modify_order", kw)

    def order_list_query(self, **kw):
        return self._call("order_list_query", kw)

    def position_list_query(self, **kw):
        return self._call("position_list_query", kw)

    def accinfo_query(self, **kw):
        return self._call("accinfo_query", kw)

    def deal_list_query(self, **kw):
        return self._call("deal_list_query", kw)

    def order_fee_query(self, **kw):
        return self._call("order_fee_query", kw)

    def set_handler(self, h):
        self.handlers.append(h)
        return 0

    def close(self):
        self.closed = True


class QuoteCtx:
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.closed = False

    def get_market_snapshot(self, codes):
        self.calls.append(("get_market_snapshot", (codes,), {}))
        return 0, DF([{"code": c, "update_time": "2026-11-13 09:55:08", "last_price": 124.06, "open_price": 124.045} for c in codes])

    def get_market_state(self, codes):
        self.calls.append(("get_market_state", (codes,), {}))
        return 0, DF([{"code": c, "market_state": "AFTERNOON"} for c in codes])

    def request_trading_days(self, **kw):
        # vendor § Get Trading Calendar：返回 list[dict]（不是 DataFrame）
        self.calls.append(("request_trading_days", (), kw))
        if kw["start"] == "2026-11-14":
            return 0, []
        return 0, [{"time": kw["start"], "trade_date_type": "WHOLE"}]

    def close(self):
        self.closed = True


@pytest.fixture
def ctxs():
    return TradeCtx(), QuoteCtx()


@pytest.fixture
def broker(ctxs):
    trade, quote = ctxs
    return MoomooBroker(FT, trade, quote, env=Env.SIMULATE, acc_id=ACC)


def limit_req(**kw) -> OrderRequest:
    base = dict(client_order_id=CID, symbol="US.SPY", side=Side.BUY, qty=1, kind=OrderKind.LIMIT, limit_price=Decimal("683.61"))
    base.update(kw)
    return OrderRequest(**base)


# ------------------------------------------------------------------ 环境


def test_real_refused_in_m2(ctxs):
    trade, quote = ctxs
    with pytest.raises(BrokerError, match="M4"):
        MoomooBroker(FT, trade, quote, env=Env.REAL, acc_id=ACC)
    assert trade.handlers == [] and trade.calls == []


# ------------------------------------------------------------------ 下单（vendor § Place Orders）


def test_place_limit_order_kwargs(broker, ctxs):
    trade, _ = ctxs
    o = broker.place_order(limit_req())
    assert isinstance(o, BrokerOrder) and o.client_order_id == CID
    name, kw = trade.calls[-1]
    assert name == "place_order"
    assert kw == {
        "price": 683.61,
        "qty": 1,
        "code": "US.SPY",
        "trd_side": "BUY",
        "order_type": "NORMAL",
        "adjust_limit": 0,  # 不让 OpenD 改价（SPEC 5.3）
        "trd_env": "SIMULATE",  # 默认值是 REAL，必须显式传（SPEC 5.3）
        "acc_id": ACC,
        "remark": "moo:" + CID,
        "time_in_force": "DAY",  # ADR-0004
        "session": "RTH",  # ADR-0003
    }


def test_place_market_order_kwargs(broker, ctxs):
    trade, _ = ctxs
    broker.place_order(limit_req(side=Side.SELL, kind=OrderKind.MARKET, limit_price=None))
    _, kw = trade.calls[-1]
    assert kw["order_type"] == "MARKET" and kw["trd_side"] == "SELL"
    assert kw["price"] == 0.0  # 市价单 price 可传任意值（vendor § Place Orders）


def test_place_order_failure_maps_to_order_rejected(broker, ctxs):
    trade, _ = ctxs
    trade.responses["place_order"] = (-1, "Insufficient buying power")
    with pytest.raises(OrderRejected, match="Insufficient buying power"):
        broker.place_order(limit_req())


# ------------------------------------------------------------------ 撤单（vendor § Modify or Cancel Orders）


def test_cancel_kwargs(broker, ctxs):
    trade, _ = ctxs
    assert broker.cancel_order("840514") is None
    name, kw = trade.calls[-1]
    assert (name, kw) == (
        "modify_order",
        {"modify_order_op": "CANCEL", "order_id": "840514", "qty": 0, "price": 0, "trd_env": "SIMULATE", "acc_id": ACC},
    )


def test_cancel_failure_raises(broker, ctxs):
    trade, _ = ctxs
    trade.responses["modify_order"] = (-1, "Order status does not allow cancel")
    with pytest.raises(BrokerError, match="does not allow"):
        broker.cancel_order("840514")


# ------------------------------------------------------------------ 查询


def test_query_orders_open_only(broker):
    assert [o.broker_order_id for o in broker.query_orders(open_only=True)] == ["840514"]
    assert [o.broker_order_id for o in broker.query_orders(open_only=False)] == ["840514", "840515"]


def test_query_positions_drops_zero_qty(broker):
    ps = broker.query_positions()
    assert [(p.symbol, p.qty) for p in ps] == [("US.SPY", 1)]


def test_query_cash(broker, ctxs):
    trade, _ = ctxs
    c = broker.query_cash()
    assert c.buying_power == Decimal("2000000.0")
    assert trade.calls[-1][1]["currency"] == "USD"


def test_query_failure_raises(broker, ctxs):
    trade, _ = ctxs
    trade.responses["order_list_query"] = (-1, "network error")
    with pytest.raises(BrokerError, match="network error"):
        broker.query_orders()


def test_deals_and_fees_not_supported_on_simulate_without_calling(broker, ctxs):
    # P4、P7 实测：模拟盘返回 ret=-1；不必发请求，直接报 NotSupported
    trade, _ = ctxs
    with pytest.raises(NotSupported):
        broker.query_deals()
    with pytest.raises(NotSupported):
        broker.query_fees(["840514"])
    assert not any(n in ("deal_list_query", "order_fee_query") for n, _ in trade.calls)


def test_every_trade_call_passes_simulate_env_and_acc_id(broker, ctxs):
    """SPEC 5.3 契约：每个交易调用都显式带 trd_env 和 acc_id（漏传即实盘）。"""
    trade, _ = ctxs
    broker.place_order(limit_req())
    broker.cancel_order("840514")
    broker.query_orders()
    broker.query_positions()
    broker.query_cash()
    assert {n for n, _ in trade.calls} == {"place_order", "modify_order", "order_list_query", "position_list_query", "accinfo_query"}
    for name, kw in trade.calls:
        assert kw.get("trd_env") == "SIMULATE", name
        assert kw.get("acc_id") == ACC, name
        if name.endswith("_query"):
            assert kw.get("refresh_cache") is False, name  # 用缓存，不受限频（各接口 Interface Limitations）


# ------------------------------------------------------------------ 推送（vendor § Orders Push Callback）


def order_handler(trade: TradeCtx):
    hs = [h for h in trade.handlers if isinstance(h, OrderHandlerBase)]
    assert len(hs) == 1
    return hs[0]


def test_only_order_handler_registered_on_simulate(broker, ctxs):
    trade, _ = ctxs
    order_handler(trade)
    assert not any(isinstance(h, DealHandlerBase) for h in trade.handlers)  # 模拟盘没有成交推送（Q2）


def test_push_dispatch_and_synthetic_fill(broker, ctxs):
    trade, _ = ctxs
    updates, fills = [], []
    broker.on_order_update(updates.append)
    broker.on_fill(fills.append)
    h = order_handler(trade)
    h.on_recv_rsp((0, DF([order_row(order_status="SUBMITTED")])))
    h.on_recv_rsp((0, DF([order_row(order_status="FILLED_ALL", dealt_qty=1.0, dealt_avg_price=759.67, updated_time="2026-11-13 09:31:39")])))
    h.on_recv_rsp((0, DF([order_row(order_status="FILLED_ALL", dealt_qty=1.0, dealt_avg_price=759.67, updated_time="2026-11-13 09:31:39")])))
    assert [u.raw_status for u in updates] == ["SUBMITTED", "FILLED_ALL", "FILLED_ALL"]
    assert len(fills) == 1  # 重复推送不重复生成成交
    f = fills[0]
    assert (f.fill_id, f.qty, f.price, f.client_order_id, f.synthetic) == ("840514:1", 1, Decimal("759.67"), CID, True)


def test_push_error_is_raised_not_swallowed(broker, ctxs):
    trade, _ = ctxs
    with pytest.raises(BrokerError, match="bad push"):
        order_handler(trade).on_recv_rsp((-1, "bad push"))


# ------------------------------------------------------------------ 会话


def test_unlock_refused_and_lock_noop_on_simulate(broker, ctxs):
    trade, _ = ctxs
    with pytest.raises(BrokerError):
        broker.unlock("pw-not-used")
    broker.lock()
    assert trade.calls == []


def test_close_closes_both(broker, ctxs):
    trade, quote = ctxs
    broker.close()
    assert trade.closed and quote.closed


# ------------------------------------------------------------------ 行情（MarketDataPort）


def test_snapshot_and_market_state(broker):
    qs = broker.snapshot(["US.TD"])
    assert qs[0].last == Decimal("124.06")
    assert broker.market_state("US.SPY") == "AFTERNOON"


def test_is_trading_day(broker, ctxs):
    _, quote = ctxs
    assert broker.is_trading_day(date(2026, 11, 13)) is True
    assert broker.is_trading_day(date(2026, 11, 14)) is False
    _, _, kw = quote.calls[-1]
    assert kw == {"market": "US", "start": "2026-11-14", "end": "2026-11-14"}


# ------------------------------------------------------------------ 推送录制（recordings/pushes/）


def test_recorder_writes_meta_and_masks_accounts(tmp_path, ctxs):
    trade, quote = ctxs
    rec = PushRecorder(tmp_path, env=Env.SIMULATE, clock=FakeClock(T0), meta={"sdk": "10.12.7208", "opend": "1012"})
    b = MoomooBroker(FT, trade, quote, env=Env.SIMULATE, acc_id=ACC, recorder=rec)
    order_handler(trade).on_recv_rsp((0, DF([order_row(acc_id=ACC, order_status="SUBMITTED")])))
    b.close()
    assert rec.path.parent == tmp_path
    assert rec.path.name == "SIMULATE_20261113T143100Z.jsonl"
    lines = [json.loads(x) for x in rec.path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["meta"] is True and lines[0]["env"] == "SIMULATE" and lines[0]["sdk"] == "10.12.7208"
    assert lines[1]["kind"] == "order"
    assert lines[1]["row"]["order_status"] == "SUBMITTED"
    assert lines[1]["row"]["acc_id"] == "****4165"  # 只脱敏账号（与 R5 一致）
    assert lines[1]["row"]["remark"] == "moo:" + CID
    assert str(ACC) not in rec.path.read_text(encoding="utf-8")


# ------------------------------------------------------------------ opend.connect 快速失败（M1 已知问题）


def test_connect_fails_fast_when_port_refused(monkeypatch):
    def refuse(addr, timeout=None):
        raise ConnectionRefusedError(61, "Connection refused")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.delitem(sys.modules, "moomoo", raising=False)
    cfg = BrokerCfg(host="127.0.0.1", port=11111, filter_trdmarket="US", simulate_acc_id=ACC)
    t0 = time.monotonic()
    with pytest.raises(BrokerUnavailable, match="拒绝"):
        opend.connect(cfg, timeout=5.0)
    assert time.monotonic() - t0 < 1.0
    assert "moomoo" not in sys.modules  # 端口不通时不加载 SDK


def test_opend_reexports_broker_errors():
    # doctor 从 opend 导入异常；搬到 port 后仍是同一个类
    assert opend.BrokerError is BrokerError
    assert opend.BrokerUnavailable is BrokerUnavailable
