"""M3 验收 4：同一个 client_order_id 重试 3 次，券商端只有 1 张单。US.SPY 1 股，限价 = 最新价 × 0.9，最后撤销。"""
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
import pytest
from mooapp.broker.moomoo import open_broker
from mooapp.broker.port import OrderRequest, new_client_order_id
from mooapp.clock import SystemClock
from mooapp.common import Env, OrderKind, Side, Source
from mooapp.config.loader import dsn_for, load_broker_cfg, load_db_cfg, load_db_local
from mooapp.oms.events import EventBus
from mooapp.oms.executor import Executor
from mooapp.oms.oms import OMS
from mooapp.oms.pg_store import PostgresStore
from mooapp.oms.poller import Poller
from mooapp.oms.pump import EventPump
from mooapp.risk.gate import Intent, RiskGate

pytestmark = pytest.mark.sim
REPO = Path(__file__).resolve().parents[2]

def build():
    clock = SystemClock()
    broker = open_broker(Env.SIMULATE, load_broker_cfg(REPO / "config/broker.toml"), clock)
    dsn = dsn_for(Env.SIMULATE, load_db_cfg(REPO / "config/db.toml"), load_db_local(REPO / "config/db.local.toml"), None)
    store = PostgresStore(dsn, schema="simulate", other_schema="real")
    bus = EventBus()
    oms = OMS(store=store, broker=broker, bus=bus, clock=clock, env=Env.SIMULATE)
    pump = EventPump(oms, threaded=True); pump.attach(broker); pump.start()
    ex = Executor(oms=oms, broker=broker, pump=pump, clock=clock, bus=bus)
    oms.set_executor(ex)
    gate = RiskGate(env=Env.SIMULATE, whitelist=frozenset({"US.SPY"}), oms=oms, executor=ex)
    oms.recover()
    return broker, store, oms, pump, gate

def test_same_cid_three_times_one_order():
    broker, store, oms, pump, gate = build()
    try:
        last = broker.snapshot(["US.SPY"])[0].last
        px = (last * Decimal("0.9")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        cid = new_client_order_id()
        req = OrderRequest(cid, "US.SPY", Side.BUY, 1, OrderKind.LIMIT, px)
        for _ in range(3):
            assert gate.submit(Intent(source=Source.CLI, action="place", request=req)).allowed
        mine = [o for o in broker.query_orders(open_only=False, refresh=True) if o.client_order_id == cid]
        assert len(mine) == 1
    finally:
        o = oms.get(cid)
        if o.status.value in {"NEW", "PENDING_NEW", "UNKNOWN", "PARTIALLY_FILLED"}:
            gate.submit(Intent(source=Source.CLI, action="cancel", client_order_id=cid))
        pump.stop(); store.close(); broker.close()