"""run_eod：组装收盘 P&L 与三方对账（PROGRESS「M7 计划」；数据来源依据 learnings「M7 探针 P13」「P12」「P8」）。

只读：读本地库、查券商；不下单、不经闸门（决定 1）。由 `mooapp eod` 在命令行进程里调用。

- 运行时点：不早于交易日收盘后 30 分钟（收盘价在收盘后几分钟内会被修订，P13）；收盘时刻按交易日历（半日市 13:00，P8）
- 日期：默认今天（美东；今天不是交易日时取最近一个已完成的交易日）；只支持最近两个已完成的交易日
- 券商订单：今天用当日列表刷新查询（P13：收盘后 30 分钟仍完整）；上一个交易日用历史订单
- 券商持仓：刷新查询一次（P12：缓存可能过时）；持仓对账按运行时刻（本程序全部成交的净持仓 vs 券商当前持仓）
- 收盘价：快照 update_time 是该交易日收盘时刻 → last_price；快照已是下一个交易日 → prev_close_price（最终值）
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from mooapp.broker.port import BrokerOrder, BrokerPort, MarketDataPort, NotSupported
from mooapp.clock import Clock
from mooapp.common import Env, MooappError
from mooapp.eod.pnl import compute_pnl
from mooapp.eod.recon import ReconBreak, reconcile
from mooapp.eod.report import EodReport, to_record
from mooapp.oms.store import Store

MARKET_TZ = ZoneInfo("America/New_York")
RUN_DELAY = timedelta(minutes=30)
CALENDAR_LOOKBACK = timedelta(days=14)
HISTORY_MARGIN = timedelta(days=1)  # 历史订单查询窗口前后放宽（vendor 未写明时区；修复计划 2 同样做法）


class EodError(MooappError):
    """不能对这个日期、在这个时刻跑对账（时点、日期或收盘价不满足条件）。"""


def _close_dt(d: date, t: time) -> datetime:
    return datetime.combine(d, t, MARKET_TZ).astimezone(UTC)


def _day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time(0), MARKET_TZ)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def resolve_trade_date(md: MarketDataPort, now: datetime, requested: date | None) -> tuple[date, dict[date, time]]:
    today = now.astimezone(MARKET_TZ).date()
    cal = md.close_times(today - CALENDAR_LOOKBACK, today)  # vendor § Get Trading Calendar（P8）
    completed = [d for d in sorted(cal) if now >= _close_dt(d, cal[d]) + RUN_DELAY]
    d = requested if requested is not None else (today if today in cal else (completed[-1] if completed else None))
    if d is None:
        raise EodError(f"最近 {CALENDAR_LOOKBACK.days} 天没有已完成的交易日")
    if d > today:
        raise EodError(f"{d} 还没到")
    if d >= today - CALENDAR_LOOKBACK and d not in cal:
        raise EodError(f"{d} 不是交易日")
    if d in cal and now < _close_dt(d, cal[d]) + RUN_DELAY:
        ready = (_close_dt(d, cal[d]) + RUN_DELAY).astimezone(MARKET_TZ)
        raise EodError(f"{d} 收盘后 30 分钟（美东 {ready:%H:%M}）之后才能对账：收盘价在收盘后几分钟内还会被修订（P13）")
    if d not in completed[-2:]:
        raise EodError(f"只支持最近两个已完成的交易日 {[x.isoformat() for x in completed[-2:]]}，收到 {d}")
    return d, cal


def _close_price(d: date, cal: dict[date, time], q) -> tuple[Decimal, str]:  # type: ignore[no-untyped-def]
    ts_et = q.ts.astimezone(MARKET_TZ)
    if ts_et.date() == d and d in cal and ts_et.timetz().replace(tzinfo=None) == cal[d]:
        return q.last, f"snapshot.last@{ts_et:%Y-%m-%d %H:%M:%S}"
    later = ts_et.date() > d and max((x for x in cal if x < ts_et.date()), default=None) == d
    if later and q.prev_close is not None:
        return q.prev_close, f"snapshot.prev_close@{ts_et:%Y-%m-%d %H:%M:%S}"
    raise EodError(f"无法取得 {q.symbol} 在 {d} 的收盘价：快照时间 {ts_et:%Y-%m-%d %H:%M:%S}（美东）既不是当天收盘时刻，也不是下一个交易日")


def _broker_orders(broker: BrokerPort, d: date, today: date, start: datetime, end: datetime) -> list[BrokerOrder]:
    if d == today:
        orders = broker.query_orders(open_only=False, refresh=True)  # vendor § Get open Orders（P13）
    else:
        orders = broker.query_order_history(start - HISTORY_MARGIN, end + HISTORY_MARGIN)  # vendor § Get Historical Orders
    return [o for o in orders if start <= o.created_at < end]


def run_eod(trade_date: date | None, store: Store, broker: BrokerPort, md: MarketDataPort, clock: Clock, env: Env) -> EodReport:
    now = clock.now()
    d, cal = resolve_trade_date(md, now, trade_date)
    today = now.astimezone(MARKET_TZ).date()
    start, end = _day_bounds(d)

    fills = store.fills_until(end)
    day_fills = [f for f in fills if f.ts >= start]
    own_cids = {o.client_order_id for o in store.list_orders(open_only=False)}
    day_orders = _broker_orders(broker, d, today, start, end)
    positions = broker.query_positions(refresh=True)  # vendor § Get Positions（P12）
    # 参考列：query_positions 丢掉 qty=0 的行，当天卖光的标的要从全部行取；紧跟上面的刷新读缓存，与对账持仓同一时刻
    broker_today_pl = broker.query_today_pnl_by_symbol() if d == today else {}  # vendor § Get Positions › today_pl_val

    held = {s for s, r in compute_pnl(fills, start, end, {}).items() if r.qty}
    closes: dict[str, Decimal] = {}
    sources: dict[str, str] = {}
    if held:
        for q in md.snapshot(sorted(held)):  # vendor § Get Market Snapshot
            closes[q.symbol], sources[q.symbol] = _close_price(d, cal, q)
    rows = compute_pnl(fills, start, end, closes)

    notes: list[str] = []
    if env is Env.SIMULATE:
        notes.append("模拟盘没有订单费用接口（P7），费用记 0")
    else:
        try:
            fees = broker.query_fees([o.broker_order_id for o in day_orders if o.client_order_id in own_cids])  # vendor § Get Order Fee
        except NotSupported as e:
            notes.append(f"订单费用查询不可用，费用记 0：{e}")
        else:
            by_id = {o.broker_order_id: o.symbol for o in day_orders}
            for oid, fee in fees.items():
                if by_id.get(oid) in rows:
                    rows[by_id[oid]].fees += fee
    notes.append("持仓对账按运行时刻：本程序全部成交的净持仓 vs 券商当前持仓（刷新查询）")

    rec = reconcile(day_fills, own_cids, day_orders, store.net_fill_qty_by_symbol(), positions)
    breaks = list(rec.breaks)
    for r in rows.values():
        breaks.extend(ReconBreak("local_anomaly", r.symbol, None, {"message": a}) for a in r.anomalies)
    return EodReport(
        trade_date=d,
        generated_at=now,
        env=env.value,
        rows=rows,
        breaks=breaks,
        info={"unknown_orders": rec.unknown_orders, "external_qty": rec.external_qty},
        close_sources=sources,
        broker_today_pl=broker_today_pl,
        notes=notes,
    )


def save_report(store: Store, report: EodReport) -> None:
    """写 eod_reports（只追加，决定 4）。"""
    with store.transaction():
        store.append_eod_report(to_record(report))
