"""ADR-0031 在模拟盘上的端到端验证（修复计划 1；只跑 -m sim）。经过 build_runtime 组装的闸门，不直接调 BrokerPort。

FakeBroker 版本在 tests/unit/test_can_sell_adr31.py；这里确认真实券商的 can_sell_qty（P10d：已扣已确认卖单）配合新规则的结果。
运行条件：常规时段；mooapp run 停着（单实例锁）；不处于 halt；模拟账户 US.SPY 持仓为 0（否则跳过，避免动到你的持仓）。
由你用 `!` 运行（sandbox 连不上 OpenD）：

    ! uv run pytest -m sim tests/contract/test_gate_can_sell_sim.py -s

模拟盘副作用（全部经闸门，remark 为 moo:）：
注意：闸门的持仓来自 OpenD 缓存，已确认卖单不一定马上从 can_sell_qty 扣掉；断言只依赖两种情况下都成立的结论。

1. 限价买 2 股 @ 卖一价 × 1.002（期望成交；2 股是本测试的目的：留 1 股挂卖单、1 股验证「卖剩下的」）
2. 限价卖 1 股 @ 最新价 × 1.04（偏离约 4%，在 [risk.simulate] 的 5% 以内；不期望成交），等券商确认
3. 再卖 1 股（同价）：修复前被 can_sell_qty 错误拒绝（1 − 1 = 0），修复后放行；等确认
4. 第 3 次卖 1 股：两项都为 0，必须被 can_sell_qty 拒绝（不卖超）
5. finally：撤掉本测试的卖单，市价卖出买入的股数，恢复原持仓
"""

from __future__ import annotations

import time
from decimal import ROUND_UP, Decimal
from pathlib import Path

import pytest

from mooapp.broker.port import OrderRequest, new_client_order_id
from mooapp.common import Env, OrderKind, Side, Source
from mooapp.ops.run import build_runtime
from mooapp.risk.gate import Intent

pytestmark = pytest.mark.sim
REPO = Path(__file__).resolve().parents[2]
SYMBOL = "US.SPY"
TICK = Decimal("0.01")
ACTOR = "contract:adr0031"


def wait(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.5)
    return pred()


def submit(rt, side: Side, qty: int, price: Decimal | None):
    kind = OrderKind.LIMIT if price is not None else OrderKind.MARKET
    req = OrderRequest(new_client_order_id(), SYMBOL, side, qty, kind, price)
    return rt.gate.submit(Intent(source=Source.CLI, action="place", request=req, actor=ACTOR))


def test_gate_sells_rest_after_acked_sell_and_never_oversells():
    rt = build_runtime(Env.SIMULATE, REPO / "config")
    rt.start()
    mine: list[str] = []
    bought = 0
    try:
        assert not rt.gate.halted, "开始前不应处于 halt：先运行 mooapp resume"
        if rt.md.market_state(SYMBOL) != "AFTERNOON":
            pytest.skip("需要常规时段（成交）")
        pos0 = rt.oms.broker_position(SYMBOL)
        if pos0 is not None and pos0.qty:
            pytest.skip(f"模拟账户已持有 {SYMBOL} {pos0.qty} 股：本测试要求从 0 开始")

        q = rt.md.snapshot([SYMBOL])[0]
        d = submit(rt, Side.BUY, 2, ((q.ask or q.last) * Decimal("1.002")).quantize(TICK, rounding=ROUND_UP))
        assert d.allowed, d
        mine.append(d.order.client_order_id)
        assert wait(lambda: rt.oms.get(mine[0]).status.value == "FILLED", 60.0), rt.oms.get(mine[0]).status
        bought = 2
        assert wait(lambda: (p := rt.oms.broker_position(SYMBOL)) is not None and p.can_sell_qty == 2, 10.0)

        px = (rt.md.snapshot([SYMBOL])[0].last * Decimal("1.04")).quantize(TICK, rounding=ROUND_UP)
        d1 = submit(rt, Side.SELL, 1, px)
        assert d1.allowed, d1
        mine.append(d1.order.client_order_id)
        assert wait(lambda: rt.oms.get(mine[1]).status.value == "NEW", 30.0)
        # 闸门读的是 OpenD 缓存（refresh_cache=False）：2026-11-16 实测 10 秒内 can_sell_qty 仍是 2，未反映这张已确认卖单
        # （P10d 用 refresh_cache=True 才看到扣减）。无论缓存是否刷新，下面两步的结论都应成立（ADR-0031 两项取小）
        cached = rt.oms.broker_position(SYMBOL)
        print(f"\n已确认 1 股卖单后，缓存持仓：qty={cached.qty} can_sell_qty={cached.can_sell_qty}")

        d2 = submit(rt, Side.SELL, 1, px)  # 修复前：1 − 1 = 0，被错误拒绝
        assert d2.allowed, d2
        mine.append(d2.order.client_order_id)
        assert wait(lambda: rt.oms.get(mine[2]).status.value == "NEW", 30.0)

        d3 = submit(rt, Side.SELL, 1, px)  # 两项都为 0：不能卖超
        assert not d3.allowed and d3.rule == "can_sell_qty", d3
        print(f"\nADR-0031 模拟盘验证：第 2 张卖单放行（{d2.reason}）；第 3 张被拒：{d3.reason}")
    finally:
        for cid in mine[1:]:
            o = rt.oms.get_or_none(cid)
            if o is not None and o.status.value in {"NEW", "PENDING_NEW", "UNKNOWN", "PARTIALLY_FILLED"}:
                rt.gate.submit(Intent(source=Source.CLI, action="cancel", client_order_id=cid, actor=ACTOR))
        wait(lambda: all(rt.oms.get(c).status.value in {"CANCELED", "FILLED", "REJECTED"} for c in mine[1:]), 30.0)
        sold = sum(rt.oms.get(c).cum_qty for c in mine[1:])
        if bought - sold > 0:
            wait(lambda: rt.oms.broker_position(SYMBOL).can_sell_qty >= bought - sold, 10.0)
            back = submit(rt, Side.SELL, bought - sold, None)
            assert back.allowed, f"恢复原持仓的市价卖单被拒：{back}；请在 App 里手动卖出 {bought - sold} 股"
            wait(lambda: rt.oms.get(back.order.client_order_id).status.value == "FILLED", 60.0)
        rt.stop()
