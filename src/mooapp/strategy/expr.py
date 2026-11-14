"""规则表达式（手册 M6 第 2 点；SPEC 5.6 compile_expr）：基于 ast 白名单编译，不使用 eval / exec / compile。

允许：变量 open、avg_cost、last；函数 min(...)、max(...)、round_tick(x, "up"|"down")；+ - * /；一元负号；数字常量。
其余一律在编译时拒绝（属性、下标、关键字参数、lambda、推导式、条件表达式、比较、乘方、取模、整除、布尔、字符串……）。
数字常量按源码原文转成 Decimal（0.95 就是 Decimal("0.95")，没有二进制浮点误差）；计算全部用 Decimal。
"""

from __future__ import annotations

import ast
import decimal
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from mooapp.common import MooappError
from mooapp.strategy.ticks import round_tick

MAX_LEN = 200
ALLOWED_NAMES = frozenset({"open", "avg_cost", "last"})
ALLOWED_FUNCS = frozenset({"min", "max", "round_tick"})
_BINOPS: dict[type, Callable[[Decimal, Decimal], Decimal]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}

Env = Mapping[str, Decimal]
_Node = Callable[[Env], Decimal]


class ExprError(MooappError, ValueError):
    """表达式不合法（编译时）或无法计算（计算时）。"""


@dataclass(frozen=True)
class CompiledExpr:
    src: str
    names: frozenset[str]
    _fn: _Node

    def __call__(self, env: Env) -> Decimal:
        try:
            return self._fn(env)
        except (decimal.DivisionByZero, decimal.InvalidOperation) as e:
            raise ExprError(f"表达式 {self.src!r} 无法计算：{type(e).__name__}") from e


def compile_expr(src: str) -> CompiledExpr:
    if not isinstance(src, str) or not src.strip():
        raise ExprError("表达式为空")
    if len(src) > MAX_LEN:
        raise ExprError(f"表达式超过 {MAX_LEN} 个字符")
    try:
        tree = ast.parse(src.strip(), mode="eval")
    except SyntaxError as e:
        raise ExprError(f"表达式语法错误：{e.msg}") from e
    names: set[str] = set()
    fn = _build(tree.body, src.strip(), names)
    return CompiledExpr(src, frozenset(names), fn)


def _build(node: ast.AST, src: str, names: set[str]) -> _Node:
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        op = _BINOPS[type(node.op)]
        left, right = _build(node.left, src, names), _build(node.right, src, names)
        return lambda env: op(left(env), right(env))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _build(node.operand, src, names)
        return lambda env: -inner(env)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        text = ast.get_source_segment(src, node) or repr(node.value)
        value = Decimal(text)
        return lambda env: value
    if isinstance(node, ast.Name):
        if node.id not in ALLOWED_NAMES:
            raise ExprError(f"不允许的名字 {node.id!r}（只允许 {sorted(ALLOWED_NAMES)}）")
        name = node.id
        names.add(name)

        def var(env: Env) -> Decimal:
            if name not in env:
                raise ExprError(f"计算时缺少变量 {name}")
            return env[name]

        return var
    if isinstance(node, ast.Call):
        return _build_call(node, src, names)
    raise ExprError(f"不允许的写法：{type(node).__name__}（{ast.get_source_segment(src, node) or ''}）")


def _build_call(node: ast.Call, src: str, names: set[str]) -> _Node:
    if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_FUNCS:
        shown = ast.get_source_segment(src, node.func) or type(node.func).__name__
        raise ExprError(f"不允许调用 {shown}（只允许 {sorted(ALLOWED_FUNCS)}）")
    if node.keywords:
        raise ExprError("不允许关键字参数")
    fname = node.func.id
    if fname == "round_tick":
        if len(node.args) != 2:
            raise ExprError('round_tick 需要两个参数：round_tick(x, "up" 或 "down")')
        d = node.args[1]
        if not (isinstance(d, ast.Constant) and d.value in ("up", "down")):
            raise ExprError('round_tick 的第二个参数只能是 "up" 或 "down"')
        x = _build(node.args[0], src, names)
        direction = d.value
        return lambda env: round_tick(x(env), direction)
    if not node.args:
        raise ExprError(f"{fname}() 至少需要一个参数")
    args = [_build(a, src, names) for a in node.args]
    pick = min if fname == "min" else max
    return lambda env: pick(a(env) for a in args)
