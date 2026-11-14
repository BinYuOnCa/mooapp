"""M6 表达式白名单与最小变动单位取整（手册 M6 第 2 点；SPEC 8 节 M6 验收 2；ADR-0009）。

compile_expr 基于 ast 白名单，不使用 eval / exec：只允许 open、avg_cost、last、min、max、round_tick、+ - * /、负号、数字常量。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from mooapp.strategy.expr import MAX_LEN, ExprError, compile_expr
from mooapp.strategy.ticks import round_tick

ENV = {"open": Decimal("99.99"), "avg_cost": Decimal("100.21"), "last": Decimal("101.00")}


@pytest.mark.parametrize(
    "src, expected",
    [
        ("min(open, avg_cost) * 0.95", Decimal("94.9905")),
        ("max(open, avg_cost) * 1.05", Decimal("105.2205")),
        ("last", Decimal("101.00")),
        ("avg_cost - 2.5", Decimal("97.71")),
        ("(open + avg_cost) / 2", Decimal("100.10")),
        ("-open + 200", Decimal("100.01")),
        ("min(open, avg_cost, last)", Decimal("99.99")),
        ("round_tick(avg_cost * 0.95, \"up\")", Decimal("95.20")),
        ("round_tick(avg_cost * 1.05, 'down')", Decimal("105.22")),
        ("1 + 2 * 3", Decimal("7")),
    ],
)
def test_allowed_expressions(src, expected):
    e = compile_expr(src)
    v = e(ENV)
    assert isinstance(v, Decimal)
    assert v == expected


def test_float_literals_are_exact_decimals():
    assert compile_expr("avg_cost * 0.1")(ENV) == Decimal("10.021")  # 不是 10.021000000000001


def test_names_used_reported():
    assert compile_expr("min(open, avg_cost) * 0.95").names == frozenset({"open", "avg_cost"})


@pytest.mark.parametrize(
    "src",
    [
        "__import__('os').system('echo hi')",
        "open.__class__",
        "open.real",
        "eval('1')",
        "exec('x=1')",
        "abs(open)",
        "close * 0.95",
        "[x for x in (1, 2)][0]",
        "(lambda: 1)()",
        "open if last > 0 else avg_cost",
        "open ** 2",
        "open % 7",
        "open // 2",
        "open > last",
        "min(open, key=avg_cost)",
        "min()",
        "round_tick(open)",
        "round_tick(open, 'sideways')",
        "round_tick(open, last)",
        "'95'",
        "open; last",
        "x := 1",
        "{'a': 1}",
        "(1, 2)",
        "True",
        "",
        "   ",
    ],
)
def test_disallowed_expressions_rejected_at_compile(src):
    with pytest.raises(ExprError):
        compile_expr(src)


def test_too_long_rejected():
    with pytest.raises(ExprError, match="200"):
        compile_expr("open + " * 40 + "1")
    assert MAX_LEN == 200


def test_syntax_error_is_expr_error():
    with pytest.raises(ExprError):
        compile_expr("min(open,")


def test_missing_variable_at_eval():
    e = compile_expr("open * 0.95")
    with pytest.raises(ExprError, match="open"):
        e({"avg_cost": Decimal("1"), "last": Decimal("1")})


def test_division_by_zero_at_eval():
    e = compile_expr("open / (last - last)")
    with pytest.raises(ExprError):
        e(ENV)


def test_compiler_never_uses_eval_or_exec():
    """静态检查：strategy/expr.py 里不调用 eval / exec / compile。"""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "src" / "mooapp" / "strategy" / "expr.py").read_text(encoding="utf-8")
    calls = {n.func.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not calls & {"eval", "exec", "compile", "__import__"}


# ------------------------------------------------------------------ round_tick（vendor FAQ Q14：≥ $1 为 0.01，< $1 为 0.0001；ADR-0009）


@pytest.mark.parametrize(
    "price, direction, expected",
    [
        ("94.9905", "up", "95.00"),
        ("105.2205", "down", "105.22"),
        ("95.00", "up", "95.00"),
        ("95.00", "down", "95.00"),
        ("1.005", "up", "1.01"),
        ("1.005", "down", "1.00"),
        ("0.12345", "up", "0.1235"),
        ("0.12345", "down", "0.1234"),
        ("0.99995", "up", "1.0000"),
        ("0.99995", "down", "0.9999"),
    ],
)
def test_round_tick(price, direction, expected):
    assert round_tick(Decimal(price), direction) == Decimal(expected)


def test_round_tick_rejects_bad_input():
    with pytest.raises(ValueError):
        round_tick(Decimal("-1"), "up")
    with pytest.raises(ValueError):
        round_tick(Decimal("1"), "sideways")  # type: ignore[arg-type]
