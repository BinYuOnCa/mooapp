"""M5 验收 3：连续 3 个交易日收到心跳邮件。在第 3 天 9:30 之后运行一次（只读：查 kv 与 outbox）：
    uv run pytest -m sim tests/acceptance/test_m5_heartbeat_3days.py -s
另外请在邮箱核对这 3 封「系统正常 YYYY-MM-DD」。"""
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest
from mooapp.broker.moomoo import open_broker
from mooapp.clock import SystemClock
from mooapp.common import Env
from mooapp.config.loader import dsn_for, load_broker_cfg, load_db_cfg, load_db_local
from mooapp.notify.notifier import HEARTBEAT_KEY
from mooapp.oms.pg_store import PostgresStore

pytestmark = pytest.mark.sim
REPO = Path(__file__).resolve().parents[2]
ET = ZoneInfo("America/New_York")


def test_last_three_trading_days_have_heartbeats():
    clock = SystemClock()
    dsn = dsn_for(Env.SIMULATE, load_db_cfg(REPO / "config/db.toml"), load_db_local(REPO / "config/db.local.toml"), None)
    store = PostgresStore(dsn, schema="simulate", other_schema="real")
    broker = open_broker(Env.SIMULATE, load_broker_cfg(REPO / "config/broker.toml"), clock)
    try:
        d, days = clock.now().astimezone(ET).date(), []
        while len(days) < 3:
            if broker.is_trading_day(d):
                days.append(d)
            d -= timedelta(days=1)
        sent = {day: store.get_kv(HEARTBEAT_KEY + day.isoformat()) for day in days}
        print("\n" + "\n".join(f"{day}: {ts or '没有'}" for day, ts in sorted(sent.items())))
        assert all(sent.values()), f"缺心跳：{[str(k) for k, v in sent.items() if not v]}"
        assert not [e for e in store.due_emails(clock.now() + timedelta(days=30)) if e.kind == "heartbeat"], "有心跳邮件还没发出"
    finally:
        broker.close()
        store.close()
