"""M8 验收 4：Agent 把示例策略的文字翻译成 YAML，与手写的 examples/rules/spy-oco.yaml 一致（解析后逐字段相同，M8 决定 6）。
步骤：在 Claude Code 里把下面这段文字交给 Agent，让它写出 YAML，保存到 var/agent/spy-oco.agent.yaml，然后：
    uv run pytest tests/acceptance/test_m8_yaml_matches_hand_written.py -s
策略文字：规则 id 为 spy-oco-1，标的 US.SPY；以限价 100.21 买入 10 股；入场后，止损价 = min(开盘价, 买入均价) × 0.95，
止盈价 = max(开盘价, 买入均价) × 1.05，两者互为 OCO。"""
from pathlib import Path
from mooapp.agent.rules import rule_differences

REPO = Path(__file__).resolve().parents[2]


def test_agent_yaml_equals_hand_written():
    agent = REPO / "var" / "agent" / "spy-oco.agent.yaml"
    assert agent.exists(), f"先把 Agent 写的 YAML 保存到 {agent}"
    diffs = rule_differences((REPO / "examples/rules/spy-oco.yaml").read_text(encoding="utf-8"), agent.read_text(encoding="utf-8"))
    print("\n" + ("一致" if not diffs else "\n".join(diffs)))
    assert diffs == []
