"""M4 验收 3：模拟盘挂 3 张单后执行 halt：全部撤销，新单被拒。
常规时段运行；与其他 sim 测试间隔 31 秒（限频按账户算）。US.SPY 限价买 1 股 × 3，价格 = 最新价 × 0.9（不会成交）。
结束后 halt 状态保留（ADR-0022），需要你在终端里运行 mooapp resume 解除。"""
import time
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
import pytest
from mooapp.common import Env
from mooapp.ops.commands import CommandClient
from mooapp.ops.run import build_runtime

pytestmark = pytest.mark.sim
REPO = Path(__file__).resolve().parents[2]


def wait_all(rt, cids, status, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(rt.oms.get(c).status.value == status for c in cids):
            return True
        time.sleep(0.5)
    return False


def test_halt_cancels_three_orders_and_rejects_new():
    rt = build_runtime(Env.SIMULATE, REPO / "config")
    rt.start()  # 启动恢复 → 事件线程、轮询、命令处理线程；持有单实例锁
    client = CommandClient(store=rt.store, clock=rt.clock, timeout=30.0, poll_interval=0.5)
    try:
        assert not rt.gate.halted, "开始前不应处于 halt：先运行 mooapp resume"
        last = rt.md.snapshot(["US.SPY"])[0].last
        px = (last * Decimal("0.9")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        cids = []
        for _ in range(3):
            r = client.submit("place", client.place_payload("US.SPY", "BUY", 1, px), actor="acceptance:m4")
            assert r["allowed"], r
            cids.append(r["client_order_id"])
        assert wait_all(rt, cids, "NEW"), [rt.oms.get(c).status.value for c in cids]
        r = client.submit("halt", {"reason": "M4 验收 3"}, actor="acceptance:m4")
        assert r["halted"] is True
        assert wait_all(rt, cids, "CANCELED"), [rt.oms.get(c).status.value for c in cids]
        broker = {o.client_order_id: o.raw_status for o in rt.broker.query_orders(open_only=False, refresh=True)}
        assert all(broker[c] == "CANCELLED_ALL" for c in cids), broker
        r = client.submit("place", client.place_payload("US.SPY", "BUY", 1, px), actor="acceptance:m4")
        assert r["allowed"] is False and r["rule"] == "halt"
    finally:
        rt.stop()  # 停线程、上锁、关连接