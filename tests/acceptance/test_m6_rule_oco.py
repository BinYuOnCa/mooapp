"""M6 验收 3：模拟盘买 1 股，设很窄的止损止盈，观察一边触发后另一边作废。
常规时段运行；与其他 sim 测试间隔 31 秒（限频按账户算）；开始前不能处于 halt（必要时先 mooapp resume）。
US.SPY 限价买 1 股 @ 卖一价 × 1.0005（向上取到 0.01，期望立即成交）；止损 = avg_cost − 0.05，止盈 = avg_cost + 0.05。
最长等 10 分钟。结束后：规则 CLOSED，只有 1 张卖单且已成交，持仓恢复原状。"""
import time
from decimal import ROUND_UP, Decimal
from pathlib import Path
import pytest
from mooapp.common import Env
from mooapp.ops.commands import CommandClient
from mooapp.ops.run import build_runtime

pytestmark = pytest.mark.sim
REPO = Path(__file__).resolve().parents[2]


def test_rule_oco_one_side_triggers_other_voids():
    rt = build_runtime(Env.SIMULATE, REPO / "config")
    rt.start()
    client = CommandClient(store=rt.store, clock=rt.clock, timeout=30.0, poll_interval=0.5)
    rule_id = f"accept-m6-{int(time.time())}"
    try:
        assert not rt.gate.halted, "开始前不应处于 halt：先运行 mooapp resume"
        ask = rt.md.snapshot(["US.SPY"])[0].ask
        px = (ask * Decimal("1.0005")).quantize(Decimal("0.01"), rounding=ROUND_UP)
        yaml_text = (
            f"id: {rule_id}\nsymbol: US.SPY\n"
            f'entry: {{side: BUY, qty: 1, limit_price: "{px}"}}\n'
            'stop: "avg_cost - 0.05"\ntake: "avg_cost + 0.05"\n'
        )
        r = client.submit("rule_add", {"yaml": yaml_text}, actor="acceptance:m6")
        assert r["allowed"], r
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline and rt.engine.status(rule_id).value not in ("CLOSED", "ERROR", "VOID"):
            time.sleep(1)
        rule = rt.engine.rule(rule_id)
        assert rule.status == "CLOSED", (rule.status, rule.state)
        sells = [cid for cid, role in rt.store.rule_orders(rule_id) if role == "sell"]
        assert len(sells) == 1, sells  # 另一边作废：只有一张卖单
        o = rt.oms.get(sells[0])
        assert o.status.value == "FILLED" and o.cum_qty == 1
        kinds = [e.kind for e in rt.store.rule_events(rule_id)]
        assert "triggered" in kinds and "closed" in kinds
        print(f"\n规则 {rule_id}：{rule.state.get('side')} 触发，卖单 {o.broker_order_id} @{o.avg_px}；请在 App 里核对")
    finally:
        rt.stop()