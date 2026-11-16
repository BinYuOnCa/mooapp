"""M3 验收 3：模拟盘下单后立刻杀掉程序，重启后订单状态与券商一致（与 App 的对照由你目视）。
子进程用 build_runtime 启动、下单后 os._exit(9) 模拟被杀；父进程等单实例锁释放后重新启动（start() 里做启动恢复），
与券商查询对比，最后撤单。运行时 mooapp run 必须停着，且不处于 halt。"""
import subprocess, sys, textwrap
import pytest
from test_m3_idempotent_retry import build, cancel_if_active   # 同目录复用

pytestmark = pytest.mark.sim

CHILD = textwrap.dedent('''
    import os, sys
    sys.path.insert(0, "tests/acceptance")
    from test_m3_idempotent_retry import build, limit_px
    from mooapp.broker.port import OrderRequest, new_client_order_id
    from mooapp.common import OrderKind, Side, Source
    from mooapp.risk.gate import Intent
    rt = build()
    cid = new_client_order_id()
    d = rt.gate.submit(Intent(source=Source.CLI, action="place", actor="acceptance:m3",
                              request=OrderRequest(cid, "US.SPY", Side.BUY, 1, OrderKind.LIMIT, limit_px(rt))))
    print("ALLOWED" if d.allowed else f"REJECTED {d.rule} {d.reason}", flush=True)
    print(cid, flush=True)
    os._exit(9)
''')


def test_kill_after_place_then_recover():
    out = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True, timeout=90)
    lines = out.stdout.strip().splitlines()
    assert len(lines) >= 2 and lines[-2] == "ALLOWED", (out.stdout, out.stderr[-2000:])
    cid = lines[-1]
    rt = build()          # 等子进程的锁释放；start() 内已做启动恢复
    try:
        o = rt.oms.get(cid)
        b = [x for x in rt.broker.query_orders(open_only=False, refresh=True) if x.client_order_id == cid]
        assert len(b) == 1 and o.broker_order_id == b[0].broker_order_id
        assert (o.status.value, b[0].raw_status) in {("NEW", "SUBMITTED"), ("PENDING_NEW", "SUBMITTING"), ("UNKNOWN", "SUBMITTING")}
        print(f"\n请在 App 里核对订单 {b[0].broker_order_id}：本地 {o.status.value}，券商 {b[0].raw_status}")
    finally:
        cancel_if_active(rt, cid)
        rt.stop()
