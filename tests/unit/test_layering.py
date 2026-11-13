"""SPEC 3.3 规则 1：SDK 类型不出 broker 层。全项目只有 src/mooapp/broker/ 与 scripts/ 可以 import moomoo。"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "mooapp"
BROKER = SRC / "broker"


def moomoo_imports(p: Path) -> list[int]:
    tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "moomoo" for a in node.names):
            lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and not node.level and (node.module or "").split(".")[0] == "moomoo":
            lines.append(node.lineno)
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            if name in {"import_module", "__import__"} and node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str) and a0.value.split(".")[0] == "moomoo":
                    lines.append(node.lineno)
    return lines


def test_broker_opend_uses_sdk():
    """确认扫描确实能看到 SDK 的 import（防止扫描本身失效）。"""
    assert moomoo_imports(BROKER / "opend.py"), "broker/opend.py 应 import moomoo"


def test_moomoo_only_in_broker():
    bad = []
    for root in (SRC, REPO / "tests"):
        for p in root.rglob("*.py"):
            if p.is_relative_to(BROKER):
                continue
            for ln in moomoo_imports(p):
                bad.append(f"{p.relative_to(REPO)}:{ln}")
    assert not bad, f"只有 broker/ 与 scripts/ 可以 import moomoo：{bad}"


WRITE_METHODS = {"place_order", "cancel_order", "modify_order", "unlock_trade"}


def write_calls(p: Path) -> list[str]:
    tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
    return [
        f"{node.lineno}:{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in WRITE_METHODS
    ]


def test_write_calls_only_in_broker():
    """SPEC 5.4 防绕过（M2 先管住 src/）：券商写方法只在 broker/ 里调用。
    M3 加 oms/executor.py 后放行它；M4 再加 GateTicket 检查。tests/contract 在 M2 直接调用（你确认的第 1 点），不在扫描范围。"""
    bad = []
    for p in SRC.rglob("*.py"):
        if p.is_relative_to(BROKER):
            continue
        bad += [f"{p.relative_to(REPO)}:{c}" for c in write_calls(p)]
    assert not bad, f"券商写方法只能在 broker/ 中调用：{bad}"
