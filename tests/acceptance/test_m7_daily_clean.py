"""M7 验收 3：模拟盘连续 5 个交易日对账零差异（PROGRESS「M7 计划」决定 5）。
每个交易日：盘中跑一次 M6 验收（保证当天有成交），收盘 30 分钟后运行本测试；结果写入 eod_reports 与 reports/。
连续 5 天的记录由你在 PROGRESS 里登记；有一天不为零或漏跑就从头数。"""
from pathlib import Path
import pytest
from mooapp.broker.moomoo import open_broker
from mooapp.clock import SystemClock
from mooapp.common import Env
from mooapp.config.loader import dsn_for, load_broker_cfg, load_db_cfg, load_db_local
from mooapp.eod.report import render_text, write_csv
from mooapp.eod.run import run_eod, save_report
from mooapp.oms.pg_store import PostgresStore

pytestmark = pytest.mark.sim
REPO = Path(__file__).resolve().parents[2]


def test_today_reconciles_with_trades():
    clock = SystemClock()
    dsn = dsn_for(Env.SIMULATE, load_db_cfg(REPO / "config/db.toml"), load_db_local(REPO / "config/db.local.toml"), None)
    store = PostgresStore(dsn, schema="simulate", other_schema="real")
    broker = open_broker(Env.SIMULATE, load_broker_cfg(REPO / "config/broker.toml"), clock)
    try:
        report = run_eod(None, store=store, broker=broker, md=broker, clock=clock, env=Env.SIMULATE)
        write_csv(report, REPO / "reports")
        save_report(store, report)
        print("\n" + render_text(report))
        assert any(r.buy_qty or r.sell_qty for r in report.rows.values()), f"{report.trade_date} 本程序没有成交，不计入 5 天"
        assert report.ok, [b.kind for b in report.breaks]
    finally:
        broker.close()
        store.close()
