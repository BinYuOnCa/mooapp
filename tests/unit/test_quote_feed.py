"""行情推送（SPEC 5.3 MarketDataPort.subscribe_quotes；vendor § Subscribe and Unsubscribe、§ Real-time Quote Callback）。

推送行字段：code、data_date、data_time（美股为美东时间）、last_price、open_price、high_price、low_price……
开盘前 open_price 的取值、盘前盘后的 last_price 含义待探针 P11；解析时 open_price 为 0 视为缺失。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from mooapp.broker.convert import quote_from_push_row
from mooapp.broker.fake import FakeBroker
from mooapp.broker.port import BrokerError
from mooapp.broker.throttle import ThrottledBroker
from mooapp.clock import FakeClock
from mooapp.common import Env

T0 = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)


def push_row(**kw):
    row = {
        "code": "US.SPY", "name": "SPDR S&P 500 ETF", "data_date": "2026-11-16", "data_time": "10:00:01",
        "last_price": 676.5, "open_price": 675.0, "high_price": 677.0, "low_price": 674.2, "prev_close_price": 674.0,
    }  # fmt: skip
    row.update(kw)
    return row


def test_parse_push_row():
    q = quote_from_push_row(push_row())
    assert q.symbol == "US.SPY" and q.last == Decimal("676.5") and q.open == Decimal("675.0")
    assert q.ts == datetime(2026, 11, 16, 15, 0, 1, tzinfo=UTC)  # 美东 10:00:01 = UTC 15:00:01（冬令时）


def test_parse_push_row_open_zero_is_none():
    assert quote_from_push_row(push_row(open_price=0.0)).open is None


def test_parse_push_row_data_time_with_date():
    """有的版本 data_time 已含日期（yyyy-MM-dd HH:mm:ss），也要能解析。"""
    q = quote_from_push_row(push_row(data_time="2026-11-16 10:00:01"))
    assert q.ts == datetime(2026, 11, 16, 15, 0, 1, tzinfo=UTC)


def test_parse_push_row_missing_field_raises():
    row = push_row()
    del row["last_price"]
    with pytest.raises(BrokerError):
        quote_from_push_row(row)


def test_fake_push_reaches_only_subscribers_of_symbol():
    fb = FakeBroker(Env.SIMULATE, FakeClock(T0))
    spy, qqq = [], []
    fb.subscribe_quotes(["US.SPY"], spy.append)
    fb.subscribe_quotes(["US.QQQ"], qqq.append)
    fb.push_quote("US.SPY", last=Decimal("100"), open=Decimal("99"))
    assert [q.last for q in spy] == [Decimal("100")] and qqq == []
    assert fb.snapshot(["US.SPY"])[0].last == Decimal("100")  # 推送也更新快照
    assert fb.calls("subscribe_quotes") == [(("US.SPY",),), (("US.QQQ",),)]


def test_throttled_broker_passes_subscription_through():
    c = FakeClock(T0)
    fb = FakeBroker(Env.SIMULATE, c)
    b = ThrottledBroker(fb, c)
    got = []
    b.subscribe_quotes(["US.SPY"], got.append)
    fb.push_quote("US.SPY", last=Decimal("1.5"))
    assert len(got) == 1


# ------------------------------------------------------------------ MoomooBroker（SDK 桩）


class DF:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orient):
        assert orient == "records"
        return list(self.rows)


class QuoteHandlerBase:
    def on_recv_rsp(self, rsp_pb):
        return rsp_pb  # 桩：测试直接把 (ret, DF) 传进来


def moomoo_broker(subscribe_ret=0):
    from test_moomoo_broker import FT, QuoteCtx, TradeCtx

    from mooapp.broker.moomoo import MoomooBroker

    ft = SimpleNamespace(**vars(FT), SubType=SimpleNamespace(QUOTE="QUOTE"), StockQuoteHandlerBase=QuoteHandlerBase)
    trade, quote = TradeCtx(), QuoteCtx()
    quote.handlers = []
    quote.subscribed = []
    quote.set_handler = lambda h: quote.handlers.append(h) or 0
    quote.subscribe = lambda code_list, subtype_list, **kw: (quote.subscribed.append((code_list, subtype_list, kw)) or (subscribe_ret, "订阅失败（桩）" if subscribe_ret else None))
    return MoomooBroker(ft, trade, quote, env=Env.SIMULATE, acc_id=2964165), quote


def test_moomoo_subscribe_uses_quote_subtype_with_push():
    b, quote = moomoo_broker()
    got = []
    b.subscribe_quotes(["US.SPY"], got.append)
    (codes, subtypes, kw) = quote.subscribed[0]
    assert codes == ["US.SPY"] and subtypes == ["QUOTE"] and kw.get("subscribe_push") is True
    handler = quote.handlers[0]
    handler.on_recv_rsp((0, DF([push_row()])))
    assert got and got[0].last == Decimal("676.5")


def test_moomoo_subscribe_failure_raises():
    b, _ = moomoo_broker(subscribe_ret=-1)
    with pytest.raises(BrokerError, match="订阅"):
        b.subscribe_quotes(["US.SPY"], lambda q: None)


def test_moomoo_quote_push_error_not_swallowed():
    b, quote = moomoo_broker()
    b.subscribe_quotes(["US.SPY"], lambda q: None)
    with pytest.raises(BrokerError):
        quote.handlers[0].on_recv_rsp((-1, "推送出错（桩）"))
