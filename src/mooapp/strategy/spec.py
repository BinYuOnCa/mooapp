"""规则文件（手册 M6 第 1 点；SPEC 5.6 RuleSpec）：YAML + JSON Schema，加载时校验，并编译两个表达式。

- 只用 yaml.safe_load：不构造任意 Python 对象（!!python/... 标签会被拒绝）
- Schema 在 rule.schema.json（Draft 2020-12）；报错里带字段路径，一次列出全部问题
- 限价必须落在最小变动单位上（ADR-0009：不自动取整）
- 表达式编译失败即加载失败（SPEC 8 节 M6 验收 2：非法表达式在加载时被拒）
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from mooapp.broker.port import is_on_tick, tick_size
from mooapp.common import MooappError, Side
from mooapp.strategy.expr import CompiledExpr, ExprError, compile_expr

SCHEMA_PATH = Path(__file__).with_name("rule.schema.json")
_VALIDATOR = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


class RuleSpecError(MooappError, ValueError):
    """规则文件不合法。消息可直接给人看（含字段路径），不带堆栈。"""


@dataclass(frozen=True)
class EntrySpec:
    side: Side
    qty: int
    limit_price: Decimal


@dataclass(frozen=True)
class RuleSpec:
    id: str
    symbol: str
    entry: EntrySpec
    stop_src: str
    take_src: str
    stop: CompiledExpr
    take: CompiledExpr


def _path(err: Any) -> str:
    return ".".join(str(p) for p in err.absolute_path) or "（顶层）"


def load_rule(text: str) -> RuleSpec:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RuleSpecError(f"YAML 无法解析：{e}") from e
    if not isinstance(data, dict):
        raise RuleSpecError(f"规则文件顶层必须是映射（key: value），收到 {type(data).__name__}")
    errors = sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        raise RuleSpecError("规则不符合 Schema：" + "；".join(f"{_path(e)}：{e.message}" for e in errors))

    limit = Decimal(data["entry"]["limit_price"])
    if limit <= 0:
        raise RuleSpecError(f"entry.limit_price：必须为正数，收到 {limit}")
    if not is_on_tick(limit):
        raise RuleSpecError(f"entry.limit_price：{limit} 不在最小变动单位 {tick_size(limit)} 上（ADR-0009：不自动取整）")

    compiled: dict[str, CompiledExpr] = {}
    for field in ("stop", "take"):
        try:
            compiled[field] = compile_expr(data[field])
        except ExprError as e:
            raise RuleSpecError(f"{field}：{e}") from e

    return RuleSpec(
        id=data["id"],
        symbol=data["symbol"],
        entry=EntrySpec(side=Side(data["entry"]["side"]), qty=data["entry"]["qty"], limit_price=limit),
        stop_src=data["stop"],
        take_src=data["take"],
        stop=compiled["stop"],
        take=compiled["take"],
    )


def load_rule_file(path: Path) -> RuleSpec:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise RuleSpecError(f"读不了规则文件 {path}：{e.strerror or e}") from e
    return load_rule(text)
