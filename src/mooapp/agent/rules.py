"""M8 验收 4：比较两份规则 YAML 是否一致（PROGRESS「M8 计划」决定 6）。

都用 M6 的 load_rule 解析，逐字段比较；表达式比较语法树（空格、多余括号不计），格式、注释、字段顺序不计。
"""

from __future__ import annotations

import ast

from mooapp.strategy.spec import load_rule


def _expr(src: str) -> str:
    return ast.dump(ast.parse(src.strip(), mode="eval"))


def rule_differences(a_text: str, b_text: str) -> list[str]:
    """返回不同的字段（空列表表示一致）。每项以字段名开头，例如 "entry.qty：10 ≠ 11"。"""
    a, b = load_rule(a_text), load_rule(b_text)
    pairs = [
        ("id", a.id, b.id),
        ("symbol", a.symbol, b.symbol),
        ("entry.side", a.entry.side.value, b.entry.side.value),
        ("entry.qty", a.entry.qty, b.entry.qty),
        ("entry.limit_price", a.entry.limit_price, b.entry.limit_price),
        ("stop", _expr(a.stop_src), _expr(b.stop_src)),
        ("take", _expr(a.take_src), _expr(b.take_src)),
    ]
    out = []
    for name, x, y in pairs:
        if x != y:
            shown = (a.stop_src, b.stop_src) if name == "stop" else (a.take_src, b.take_src) if name == "take" else (x, y)
            out.append(f"{name}：{shown[0]} ≠ {shown[1]}")
    return out
