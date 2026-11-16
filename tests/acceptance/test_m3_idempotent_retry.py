"""M3 验收 4：同一个 client_order_id 重试 3 次，券商端只有 1 张单。US.SPY 1 股，最后撤销。
限价 = 最新价 × 0.96（偏离约 4%，在 [risk.simulate] 的 5% 以内；不会成交）。
M4 起由 mooapp.ops.run.build_runtime 组装；运行时 mooapp run 必须停着（单实例锁），且不处于 halt。"""
import time
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
import pytest
from mooapp.broker.port import OrderRequest, new_client_order_id
from mooapp.common import Env, OrderKind, Side, Source
from mooapp.ops.run import RunnerAlreadyRunning, build_runtime
from mooapp.risk.gate import Intent

pytestmark = pytest.mark.sim
REPO = Path(__file__).resolve().parents[2]
ACTIVE = {"NEW", "PENDING_NEW", "UNKNOWN", "PARTIALLY_FILLED"}


def build(lock_wait: float = 15.0):
    """组装并启动常驻进程的全部部件（start() 里做启动恢复）。上一个持锁进程刚被杀时，等 PostgreSQL 释放单实例锁。"""
    deadline = time.monotonic() + lock_wait
    while True:
        rt = build_runtime(Env.SIMULATE, REPO / "config")
        try:
            rt.start()
            return rt
        except RunnerAlreadyRunning:
            rt.stop()
            if time.monotonic() >= deadline:
                raise
            time.sleep(1.0)


def limit_px(rt):
    last = rt.md.snapshot(["US.SPY"])[0].last
    return (last * Decimal("0.96")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def cancel_if_active(rt, cid):
    o = rt.oms.get_or_none(cid)
    if o is not None and o.status.value in ACTIVE:
        rt.gate.submit(Intent(source=Source.CLI, action="cancel", client_order_id=cid, actor="acceptance:m3"))


def test_same_cid_three_times_one_order():
    rt = build()
    cid = new_client_order_id()
    try:
        assert not rt.gate.halted, "开始前不应处于 halt：先运行 mooapp resume"
        req = OrderRequest(cid, "US.SPY", Side.BUY, 1, OrderKind.LIMIT, limit_px(rt))
        for _ in range(3):
            d = rt.gate.submit(Intent(source=Source.CLI, action="place", request=req, actor="acceptance:m3"))
            assert d.allowed, d
        mine = [o for o in rt.broker.query_orders(open_only=False, refresh=True) if o.client_order_id == cid]
        assert len(mine) == 1
    finally:
        cancel_if_active(rt, cid)
        rt.stop()