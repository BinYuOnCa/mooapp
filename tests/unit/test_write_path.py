"""唯一写路径（SPEC 2.3、5.4；M3 计划第 4、5 节）。

- 静态：src/ 里 .place_order( / .cancel_order( 只出现在 broker/ 和 oms/executor.py；
  GateTicket 只在 oms/ticket.py 定义、只在 risk/gate.py 签发
- 运行时：Executor 拒绝伪造、错配、重复使用的 ticket
"""

from __future__ import annotations

import ast
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from oms_harness import make_system

from mooapp.broker.port import OrderRequest, new_client_order_id
from mooapp.common import OrderKind, Side, Source
from mooapp.oms.ticket import GateTicket, TicketError
from mooapp.risk.gate import Intent

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "mooapp"
BROKER = SRC / "broker"
EXECUTOR = SRC / "oms" / "executor.py"
TICKET = SRC / "oms" / "ticket.py"
GATE = SRC / "risk" / "gate.py"


def calls_named(p: Path, names: set[str]) -> list[str]:
    tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            if name in names:
                out.append(f"{p.relative_to(REPO)}:{node.lineno}:{name}")
    return out


def test_scan_sees_executor_calls():
    assert calls_named(EXECUTOR, {"place_order"}), "executor 应调用 place_order（防止扫描失效）"
    assert calls_named(EXECUTOR, {"cancel_order"})


def test_broker_writes_only_in_broker_and_executor():
    bad = []
    for p in SRC.rglob("*.py"):
        if p.is_relative_to(BROKER) or p == EXECUTOR:
            continue
        bad += calls_named(p, {"place_order", "cancel_order", "modify_order", "unlock_trade"})
    assert not bad, bad


def test_ticket_issued_only_by_gate():
    bad = []
    for p in SRC.rglob("*.py"):
        if p in (TICKET, GATE):
            continue
        bad += calls_named(p, {"GateTicket", "_issue_ticket"})
    assert not bad, bad
    assert calls_named(GATE, {"_issue_ticket"}), "gate 应签发 ticket"


def test_executor_public_methods_all_require_ticket():
    """超时重发（T55）与启动恢复的重发由同一模块内的 Resolver 调用私有方法完成，不对外暴露无 ticket 的发单入口。"""
    tree = ast.parse(EXECUTOR.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Executor")
    public = {f.name: f for f in cls.body if isinstance(f, ast.FunctionDef) and not f.name.startswith("_")}
    assert set(public) == {"place", "cancel", "resolve_unknown"}
    for name in ("place", "cancel"):
        assert public[name].args.args[1].arg == "ticket", f"{name} 第一个参数必须是 ticket"


def method_calls_on(p: Path, methods: set[str], receiver_hint: str) -> list[str]:
    """形如 <...executor...>.place( / .cancel( 的调用（接收者名字里含 receiver_hint）。"""
    tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in methods:
            recv = ast.unparse(node.func.value)
            if receiver_hint in recv.lower():
                out.append(f"{p.relative_to(REPO)}:{node.lineno}:{recv}.{node.func.attr}")
    return out


def test_only_gate_calls_executor_write_methods():
    """M4 验收 2（防绕过）：命令行、常驻进程、规则引擎、Agent 都只能经 RiskGate；src/ 里只有 risk/gate.py 调用 Executor 的 place / cancel。"""
    bad = []
    for p in SRC.rglob("*.py"):
        if p in (GATE, EXECUTOR):
            continue
        bad += method_calls_on(p, {"place", "cancel"}, "executor")
    assert not bad, bad
    assert method_calls_on(GATE, {"place", "cancel"}, "executor"), "gate 应调用 executor（防止扫描失效）"


def test_entry_modules_do_not_import_executor():
    """SPEC 3.3 规则 2：strategy、agent、ops（命令行与命令处理）不 import oms.executor；ops/run.py 只负责组装，例外。"""
    allowed = {SRC / "ops" / "run.py"}
    bad = []
    for sub in ("strategy", "agent", "ops"):
        for p in (SRC / sub).rglob("*.py"):
            if p in allowed:
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("oms.executor"):
                    bad.append(f"{p.relative_to(REPO)}:{node.lineno}")
                if isinstance(node, ast.Import) and any(a.name.endswith("oms.executor") for a in node.names):
                    bad.append(f"{p.relative_to(REPO)}:{node.lineno}")
    assert not bad, bad


def test_run_module_never_calls_broker_or_executor_writes():
    run = SRC / "ops" / "run.py"
    assert run.exists()
    assert not calls_named(run, {"place_order", "cancel_order", "modify_order", "unlock_trade"})
    assert not method_calls_on(run, {"place", "cancel"}, "executor")


def _req():
    return OrderRequest(new_client_order_id(), "US.SPY", Side.BUY, 1, OrderKind.LIMIT, Decimal("10"))


def test_forged_ticket_rejected():
    sys = make_system()
    with pytest.raises(TicketError):
        GateTicket(action="place", client_order_id="x" * 32, seal=object())
    fake = type("T", (), {"action": "place", "client_order_id": _req().client_order_id})()
    with pytest.raises(TicketError):
        sys.executor.place(fake, _req(), Source.CLI)
    assert sys.broker.calls("place_order") == []


def test_ticket_used_by_gate_cannot_be_reused():
    sys = make_system()
    r1 = _req()
    d = sys.gate.submit(Intent(source=Source.CLI, action="place", request=r1))
    assert d.allowed and d.ticket is not None
    with pytest.raises(TicketError):
        sys.executor.place(d.ticket, replace(r1, client_order_id=new_client_order_id()), Source.CLI)
    assert len(sys.broker.calls("place_order")) == 1


def test_ticket_bound_to_cid_and_action():
    from mooapp.risk.gate import _issue_ticket  # 测试里直接签发，验证 Executor 的校验

    sys = make_system()
    r1, r2 = _req(), _req()
    t = _issue_ticket("place", r1.client_order_id)
    with pytest.raises(TicketError):
        sys.executor.place(t, r2, Source.CLI)  # cid 不符
    with pytest.raises(TicketError):
        sys.executor.cancel(t, r1.client_order_id)  # 动作不符
    sys.executor.place(t, r1, Source.CLI)  # 校验失败不消耗 ticket
    with pytest.raises(TicketError):
        sys.executor.place(t, r1, Source.CLI)  # 只能用一次
    assert len(sys.broker.calls("place_order")) == 1
