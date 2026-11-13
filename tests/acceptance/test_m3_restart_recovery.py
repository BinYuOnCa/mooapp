"""M3 验收 3：模拟盘下单后立刻杀掉程序，重启后订单状态与券商一致（与 App 的对照由你目视）。
子进程下单后 os._exit(9) 模拟被杀；父进程重建 OMS、recover，与券商查询对比，最后撤单。"""
import os, subprocess, sys, textwrap
import pytest
from test_m3_idempotent_retry import build   # 同目录复用

pytestmark = pytest.mark.sim

CHILD = textwrap.dedent('''
    import os, sys
    from decimal import ROUND_DOWN, Decimal
    sys.path.insert(0, "tests/acceptance")
    from test_m3_idempotent_retry import build
    from mooapp.broker.port import OrderRequest, new_client_order_id
    from mooapp.common import OrderKind, Side, Source
    from mooapp.risk.gate import Intent
    broker, store, oms, pump, gate = build()
    last = broker.snapshot(["US.SPY"])[0].last
    px = (last * Decimal("0.9")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    cid = new_client_order_id()
    gate.submit(Intent(source=Source.CLI, action="place",
                       request=OrderRequest(cid, "US.SPY", Side.BUY, 1, OrderKind.LIMIT, px)))
    print(cid, flush=True)
    os._exit(9)
''')

def test_kill_after_place_then_recover():
    out = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True, timeout=60)
    cid = out.stdout.strip().splitlines()[-1]
    broker, store, oms, pump, gate = build()          # build() 内已 recover()
    try:
        o = oms.get(cid)
        b = [x for x in broker.query_orders(open_only=False, refresh=True) if x.client_order_id == cid]
        assert len(b) == 1 and o.broker_order_id == b[0].broker_order_id
        assert (o.status.value, b[0].raw_status) in {("NEW", "SUBMITTED"), ("PENDING_NEW", "SUBMITTING"), ("UNKNOWN", "SUBMITTING")}
        print(f"\n请在 App 里核对订单 {b[0].broker_order_id}：本地 {o.status.value}，券商 {b[0].raw_status}")
    finally:
        gate.submit(__import__("mooapp.risk.gate", fromlist=["Intent"]).Intent(
            source=__import__("mooapp.common", fromlist=["Source"]).Source.CLI, action="cancel", client_order_id=cid))
        pump.stop(); store.close(); broker.close()