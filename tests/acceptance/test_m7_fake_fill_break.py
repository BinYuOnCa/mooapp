"""M7 验收 2：往本地库手动插一条假成交，当天报告出现 [BREAK]。
假成交插在一个最后会回滚的事务里（不留在库里，不影响之后的对账）。收盘后 30 分钟以后运行；mooapp run 不必停。"""
from pathlib import Path
import pytest
from mooapp.broker.moomoo import open_broker
from mooapp.clock import SystemClock
from mooapp.common import Env
from mooapp.config.loader import dsn_for, load_broker_cfg, load_db_cfg, load_db_local
from mooapp.eod.report import render_text
from mooapp.eod.run import run_eod
from mooapp.oms.pg_store import PostgresStore
from mooapp.oms.store import FillRecord

pytestmark = pytest.mark.sim
REPO = Path(__file__).resolve().parents[2]


class _Rollback(Exception):
    pass


def test_fake_local_fill_shows_break():
    clock = SystemClock()
    dsn = dsn_for(Env.SIMULATE, load_db_cfg(REPO / "config/db.toml"), load_db_local(REPO / "config/db.local.toml"), None)
    store = PostgresStore(dsn, schema="simulate", other_schema="real")
    broker = open_broker(Env.SIMULATE, load_broker_cfg(REPO / "config/broker.toml"), clock)
    try:
        orders = [o for o in store.list_orders(open_only=False) if o.broker_order_id and o.cum_qty > 0]
        assert orders, "本地库里没有成交过的订单：先在盘中跑一次 M6 验收"
        o = max(orders, key=lambda x: x.updated_at)
        report = None
        try:
            with store.transaction():
                store.insert_fill_once(FillRecord(f"fake:{clock.now().timestamp()}", o.client_order_id, o.broker_order_id,
                                                  o.symbol, o.side, 1, o.avg_px, clock.now(), False, "local"))
                report = run_eod(None, store=store, broker=broker, md=broker, clock=clock, env=Env.SIMULATE)
                raise _Rollback()
        except _Rollback:
            pass
        print("\n" + render_text(report))
        assert not report.ok
        assert "fill_qty_mismatch" in [b.kind for b in report.breaks]
        assert not [f for f in store.fills_for(o.client_order_id) if f.fill_id.startswith("fake:")], "假成交应已回滚"
    finally:
        broker.close()
        store.close()