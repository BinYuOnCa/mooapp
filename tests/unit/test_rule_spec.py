"""M6 规则文件：YAML + JSON Schema，加载时校验（手册 M6 第 1 点）；非法表达式在加载时被拒（SPEC 8 节 M6 验收 2）。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from mooapp.common import Side
from mooapp.strategy.spec import RuleSpecError, load_rule, load_rule_file

REPO = Path(__file__).resolve().parents[2]

GOOD = """\
id: spy-oco-1
symbol: US.SPY
entry:
  side: BUY
  qty: 10
  limit_price: "100.21"
stop: "min(open, avg_cost) * 0.95"
take: "max(open, avg_cost) * 1.05"
"""


def variant(old: str, new: str) -> str:
    assert old in GOOD
    return GOOD.replace(old, new)


def test_good_rule_loads():
    spec = load_rule(GOOD)
    assert spec.id == "spy-oco-1" and spec.symbol == "US.SPY"
    assert spec.entry.side is Side.BUY and spec.entry.qty == 10 and spec.entry.limit_price == Decimal("100.21")
    env = {"open": Decimal("99.99"), "avg_cost": Decimal("100.21"), "last": Decimal("100")}
    assert spec.stop(env) == Decimal("94.9905")
    assert spec.take(env) == Decimal("105.2205")
    assert spec.stop_src == "min(open, avg_cost) * 0.95"


def test_example_rule_file_loads():
    spec = load_rule_file(REPO / "examples" / "rules" / "spy-oco.yaml")
    assert spec.symbol == "US.SPY"


@pytest.mark.parametrize(
    "yaml_text, where",
    [
        (variant('stop: "min(open, avg_cost) * 0.95"\n', ""), "stop"),  # 缺 stop
        (GOOD + "trail: 0.5\n", "trail"),  # 多余字段
        (variant("side: BUY", "side: SELL"), "side"),  # 只允许买入入场（ADR-0004）
        (variant("qty: 10", "qty: 0"), "qty"),
        (variant("qty: 10", "qty: 1.5"), "qty"),
        (variant("qty: 10", 'qty: "10"'), "qty"),
        (variant("symbol: US.SPY", "symbol: HK.00700"), "symbol"),  # 只做美股（ADR-0001）
        (variant('limit_price: "100.21"', "limit_price: 100.21"), "limit_price"),  # 必须写成字符串
        (variant('limit_price: "100.21"', 'limit_price: "100.215"'), "limit_price"),  # 不在最小变动单位上（ADR-0009：不自动取整）
        (variant('limit_price: "100.21"', 'limit_price: "-1"'), "limit_price"),
        (variant("id: spy-oco-1", "id: 'SPY OCO!'"), "id"),
        (variant("  qty: 10\n", "  qty: 10\n  kind: MARKET\n"), "kind"),  # 入场单只能限价 DAY
    ],
)
def test_schema_violations_rejected_with_location(yaml_text, where):
    with pytest.raises(RuleSpecError) as ei:
        load_rule(yaml_text)
    assert where in str(ei.value)


@pytest.mark.parametrize(
    "bad_expr",
    ["__import__('os').system('id')", "open.__class__", "eval('1')", "close * 0.95", "open ** 2"],
)
def test_illegal_expression_rejected_at_load(bad_expr):
    """SPEC 8 节 M6 验收 2：非法表达式（如 __import__）在加载时被拒。"""
    text = variant('stop: "min(open, avg_cost) * 0.95"', f'stop: "{bad_expr}"')
    with pytest.raises(RuleSpecError) as ei:
        load_rule(text)
    assert "stop" in str(ei.value)


def test_illegal_take_expression_rejected_at_load():
    with pytest.raises(RuleSpecError, match="take"):
        load_rule(variant('take: "max(open, avg_cost) * 1.05"', 'take: "exec(\'x\')"'))


def test_yaml_python_tags_rejected():
    """只用 yaml.safe_load：!!python/object 之类的标签不得构造任意对象。"""
    with pytest.raises(RuleSpecError):
        load_rule("!!python/object/apply:os.system ['echo hi']\n")


def test_not_a_mapping_rejected():
    with pytest.raises(RuleSpecError):
        load_rule("- just\n- a list\n")


def test_invalid_yaml_rejected():
    with pytest.raises(RuleSpecError):
        load_rule("id: [unclosed\n")


def test_schema_file_is_json_schema():
    import json

    from jsonschema import Draft202012Validator

    schema = json.loads((REPO / "src" / "mooapp" / "strategy" / "rule.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
