"""M7 三方对账与运行规则（PROGRESS「M7 计划」；learnings「M7 探针 P13」「P12」「P8」）。FakeBroker 版本。

harness 的 T0 = 2026-11-13（周五）美东 10:00。收盘 16:00 = 21:00 UTC；对账不早于收盘后 30 分钟。
"""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from oms_harness import make_system

from mooapp.broker.port import OrderRequest, new_client_order_id
from mooapp.common import Env, OrderKind, Side
from mooapp.eod.pnl import money
from mooapp.eod.report import write_csv
from mooapp.eod.run import EodError, run_eod, save_report
from mooapp.oms.store import FillRecord

D = Decimal
FRI = date(2026, 11, 13)
CLOSE_UTC = datetime(2026, 11, 13, 21, 0, tzinfo=UTC)  # 美东 16:00


def to(sys, ts: datetime) -> None:
    sys.clock.advance((ts - sys.clock.now()).total_seconds())


def trade_day(sys) -> str:
    """买 2 @10.00 全部成交，卖 1 @10.50 成交 → 已实现 0.50；剩 1 股 @10.00。返回买单 cid。"""
    cid = sys.place(qty=2).order.client_order_id
    sys.broker.step()
    sys.broker.fill(sys.oms.get(cid).broker_order_id, 2, D("10.00"))
    sys.pump.drain()
    sell = sys.place(qty=1, side=Side.SELL, price="10.50").order.client_order_id
    sys.broker.step()
    sys.broker.fill(sys.oms.get(sell).broker_order_id, 1, D("10.50"))
    sys.pump.drain()
    return cid


def close_market(sys, last="10.20", close_ts=CLOSE_UTC, run_at=CLOSE_UTC + timedelta(minutes=31)) -> None:
    to(sys, close_ts)
    sys.broker.set_quote("US.SPY", last=D(last), ts=close_ts)
    sys.broker.set_market_state("US.SPY", "AFTER_HOURS_BEGIN")
    to(sys, run_at)


def eod(sys, d: date = FRI):
    return run_eod(d, store=sys.store, broker=sys.broker, md=sys.broker, clock=sys.clock, env=Env.SIMULATE)


def kinds(report) -> list[str]:
    return [b.kind for b in report.breaks]


# ------------------------------------------------------------------ 一致的一天


def test_consistent_day_no_breaks():
    sys = make_system()
    trade_day(sys)
    close_market(sys)
    r = eod(sys)
    assert r.breaks == [] and r.ok
    row = r.rows["US.SPY"]
    assert money(row.realized) == D("0.50")
    assert money(row.unrealized) == D("0.20")  # 1 × (10.20 − 10.00)
    assert r.close_sources["US.SPY"].startswith("snapshot.last")
    assert r.trade_date == FRI


def test_positions_checked_with_refresh_query():
    """P12：缓存持仓可能过时，对账用一次刷新查询。"""
    sys = make_system()
    trade_day(sys)
    close_market(sys)
    eod(sys)
    assert sys.broker.calls("query_positions")[-1] == (True,)


def test_today_orders_from_refreshed_order_list():
    """P13：收盘后 30 分钟当日列表完整；用刷新查询。"""
    sys = make_system()
    trade_day(sys)
    close_market(sys)
    eod(sys)
    assert sys.broker.calls("query_orders")[-1] == (False, True)


def test_simulate_fees_zero_with_note():
    sys = make_system()
    trade_day(sys)
    close_market(sys)
    r = eod(sys)
    assert r.rows["US.SPY"].fees == D("0")
    assert any("P7" in n for n in r.notes)


# ------------------------------------------------------------------ [BREAK]


def test_fake_local_fill_is_break():
    """M7 验收 2 的 FakeBroker 版：往本地库手动插一条假成交 → [BREAK]。"""
    sys = make_system()
    cid = trade_day(sys)
    o = sys.oms.get(cid)
    with sys.store.transaction():
        sys.store.insert_fill_once(
            FillRecord("fake:1", cid, o.broker_order_id, "US.SPY", Side.BUY, 1, D("10.00"), sys.clock.now(), False, "local")
        )
    close_market(sys)
    r = eod(sys)
    assert not r.ok
    assert "fill_qty_mismatch" in kinds(r)
    assert "position_mismatch" in kinds(r)  # 本地净持仓 2 > 券商 1


def test_fake_local_sell_fill_is_position_break_not_external():
    """验收 2 实跑（2026-11-16）发现：假成交是卖出时，本程序净持仓为负；不能把差额当成「程序外持仓」。"""
    sys = make_system()
    trade_day(sys)
    sell_cid = [o for o in sys.store.list_orders(open_only=False) if o.side is Side.SELL][0].client_order_id
    o = sys.oms.get(sell_cid)
    with sys.store.transaction():
        for i in range(2):  # 本地多卖 2 股：净持仓 1 − 2 = −1
            sys.store.insert_fill_once(
                FillRecord(f"fake:{i}", sell_cid, o.broker_order_id, "US.SPY", Side.SELL, 1, D("10.50"), sys.clock.now(), False, "local")
            )
    close_market(sys)
    r = eod(sys)
    assert "position_mismatch" in kinds(r)
    assert r.info["external_qty"] == {}


def test_broker_fill_missing_locally_is_break():
    sys = make_system()
    cid = sys.place(qty=2).order.client_order_id
    sys.broker.step()
    sys.pump.drain()
    sys.broker.fill(sys.oms.get(cid).broker_order_id, 2, D("10.00"))  # 推送没处理：本地没有这笔成交
    close_market(sys)
    r = eod(sys)
    assert "fill_qty_mismatch" in kinds(r)
    b = next(b for b in r.breaks if b.kind == "fill_qty_mismatch")
    assert b.client_order_id == cid and b.details["local_qty"] == 0 and b.details["broker_qty"] == 2


def test_avg_price_mismatch_is_break(monkeypatch):
    sys = make_system()
    cid = trade_day(sys)
    close_market(sys)
    real = sys.broker.query_orders

    def skewed(open_only=True, refresh=False):
        return [replace(o, dealt_avg_price=D("10.05")) if o.client_order_id == cid else o for o in real(open_only, refresh)]

    monkeypatch.setattr(sys.broker, "query_orders", skewed)
    assert "fill_price_mismatch" in kinds(eod(sys))


def test_broker_position_short_of_own_is_break():
    sys = make_system()
    trade_day(sys)
    sys.broker.set_position("US.SPY", 0, D("10"))
    close_market(sys)
    assert "position_mismatch" in kinds(eod(sys))


def test_external_position_reported_not_break():
    """ADR-0019：券商持仓多于本程序净持仓的部分是程序外的，只报告。"""
    sys = make_system()
    trade_day(sys)
    sys.broker.set_position("US.SPY", 6, D("10"))  # 本程序 1 + 程序外 5
    close_market(sys)
    r = eod(sys)
    assert r.ok
    assert r.info["external_qty"] == {"US.SPY": 5}


def test_unknown_broker_orders_listed_not_break():
    """券商有 moo: 订单但本地没有（例如契约测试直接下的单）：只列出，不算 BREAK。"""
    sys = make_system()
    trade_day(sys)
    stranger = sys.broker.place_order(OrderRequest(new_client_order_id(), "US.SPY", Side.BUY, 1, OrderKind.LIMIT, D("9.00")))
    sys.broker.step()
    close_market(sys)
    r = eod(sys)
    assert r.ok
    assert stranger.broker_order_id in r.info["unknown_orders"]


# ------------------------------------------------------------------ 运行时点与日期（P8、P13）


def test_refuses_before_close_plus_30_minutes():
    sys = make_system()
    trade_day(sys)
    close_market(sys, run_at=CLOSE_UTC + timedelta(minutes=10))
    with pytest.raises(EodError, match="30 分钟"):
        eod(sys)


def test_half_day_closes_at_13():
    sys = make_system()
    sys.broker.set_half_day(FRI)  # P8：MORNING
    trade_day(sys)
    half_close = datetime(2026, 11, 13, 18, 0, tzinfo=UTC)  # 美东 13:00
    close_market(sys, close_ts=half_close, run_at=half_close + timedelta(minutes=31))
    assert eod(sys).ok


def test_snapshot_not_at_close_is_rejected():
    sys = make_system()
    trade_day(sys)
    close_market(sys, close_ts=CLOSE_UTC - timedelta(minutes=1))  # 快照停在 15:59，不是收盘
    with pytest.raises(EodError, match="收盘价"):
        eod(sys)


def test_previous_trading_day_uses_prev_close():
    """周一盘中补跑上周五：收盘价用周一快照的 prev_close（最终值，P13）；持仓按运行时刻对。"""
    sys = make_system()
    trade_day(sys)
    mon_noon = datetime(2026, 11, 16, 17, 0, tzinfo=UTC)  # 美东 12:00
    to(sys, mon_noon)
    sys.broker.set_quote("US.SPY", last=D("10.40"), prev_close=D("10.20"), ts=mon_noon)
    r = eod(sys)
    assert r.ok
    assert r.close_sources["US.SPY"].startswith("snapshot.prev_close")
    assert money(r.rows["US.SPY"].unrealized) == D("0.20")


def test_date_older_than_previous_session_rejected():
    sys = make_system()
    close_market(sys, run_at=datetime(2026, 11, 16, 22, 0, tzinfo=UTC))  # 周一收盘后：可跑周一、周五
    with pytest.raises(EodError, match="只支持"):
        eod(sys, date(2026, 11, 12))


def test_non_trading_day_rejected():
    sys = make_system()
    close_market(sys, run_at=datetime(2026, 11, 16, 22, 0, tzinfo=UTC))  # 周一收盘后请求周六
    with pytest.raises(EodError, match="不是交易日"):
        eod(sys, date(2026, 11, 14))


# ------------------------------------------------------------------ 报告：CSV 与 eod_reports


def test_report_csv_rounded_to_cents(tmp_path):
    sys = make_system()
    trade_day(sys)
    close_market(sys)
    path = write_csv(eod(sys), tmp_path)
    assert path.name == "2026-11-13.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    spy = next(r for r in rows if r["symbol"] == "US.SPY")
    assert (spy["qty"], spy["realized"], spy["unrealized"], spy["fees"]) == ("1", "0.50", "0.20", "0.00")


def test_broker_today_pl_for_sold_out_symbol(tmp_path):
    """当天卖光（qty=0 的行被 query_positions 丢掉）仍要填券商 today_pl_val 参考列，取自持仓刷新之后的缓存。"""
    sys = make_system()
    cid = sys.place(qty=1).order.client_order_id
    sys.broker.step()
    sys.broker.fill(sys.oms.get(cid).broker_order_id, 1, D("10.00"))
    sys.pump.drain()
    sell = sys.place(qty=1, side=Side.SELL, price="10.50").order.client_order_id
    sys.broker.step()
    sys.broker.fill(sys.oms.get(sell).broker_order_id, 1, D("10.50"))
    sys.pump.drain()
    sys.broker.set_today_pnl_by_symbol({"US.SPY": D("0.50")})
    close_market(sys)
    order: list[str] = []
    for name in ("query_positions", "query_today_pnl_by_symbol"):
        inner = getattr(sys.broker, name)
        setattr(sys.broker, name, lambda *a, _n=name, _f=inner, **k: (order.append(_n), _f(*a, **k))[1])
    r = eod(sys)
    assert r.ok and r.rows["US.SPY"].qty == 0
    assert r.broker_today_pl == {"US.SPY": D("0.50")}
    assert order == ["query_positions", "query_today_pnl_by_symbol"]  # 先刷新持仓，再读缓存
    spy = next(row for row in csv.DictReader(write_csv(r, tmp_path).open(encoding="utf-8")) if row["symbol"] == "US.SPY")
    assert spy["broker_today_pl"] == "0.50"


def test_broker_today_pl_empty_for_previous_trading_day():
    """补跑上一个交易日：券商 today_pl_val 是今天的，不能填进去。"""
    sys = make_system()
    trade_day(sys)
    sys.broker.set_today_pnl_by_symbol({"US.SPY": D("0.70")})
    mon_noon = datetime(2026, 11, 16, 17, 0, tzinfo=UTC)
    to(sys, mon_noon)
    sys.broker.set_quote("US.SPY", last=D("10.40"), prev_close=D("10.20"), ts=mon_noon)
    assert eod(sys).broker_today_pl == {}


def test_report_saved_append_only():
    sys = make_system()
    trade_day(sys)
    close_market(sys)
    r = eod(sys)
    save_report(sys.store, r)
    save_report(sys.store, r)  # 同一交易日可以多次运行，只追加
    saved = sys.store.eod_reports(FRI)
    assert len(saved) == 2 and saved[0].ok and saved[0].env == "SIMULATE"
    assert saved[0].rows[0]["symbol"] == "US.SPY" and saved[0].rows[0]["realized"] == "0.50"
