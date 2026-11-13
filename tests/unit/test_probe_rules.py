"""探针规则 R1–R3 的静态检查（SPEC 7.1 末段「自动检查」）。不是验收测试。

只做 AST / 文本扫描，不 import、不执行任何 scripts/ 下的文件（R2）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
PROBES = sorted(SCRIPTS.glob("probe_*.py"))
WRITE_CALLS = {"place_order", "modify_order"}
FORBIDDEN_NAMES = {"unlock_trade", "getpass"}
ALLOWED_MOOAPP = "mooapp.config"


def parse(p: Path) -> ast.Module:
    return ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


def imported_modules(tree: ast.Module) -> list[str]:
    mods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                mods.append("." * node.level + (node.module or ""))
            else:
                mods.append(node.module or "")
    return mods


def is_trdenv_attr(node: ast.AST, member: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == member
        and isinstance(node.value, ast.Name)
        and node.value.id == "TrdEnv"
    )


def test_probe_m1_exists():
    assert (SCRIPTS / "probe_m1.py").is_file()


def test_no_scripts_init():
    assert not (SCRIPTS / "__init__.py").exists()


@pytest.mark.parametrize("probe", PROBES, ids=lambda p: p.name)
def test_r1_r3_forbidden_items(probe: Path):
    tree = parse(probe)
    text = probe.read_text(encoding="utf-8")
    problems = []
    for node in ast.walk(tree):
        if is_trdenv_attr(node, "REAL"):
            problems.append(f"第 {node.lineno} 行：TrdEnv.REAL")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"第 {node.lineno} 行：{node.id}")
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            problems.append(f"第 {node.lineno} 行：.{node.attr}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("--env"):
                problems.append(f"第 {node.lineno} 行：不接受环境参数（R1）")
    for mod in imported_modules(tree):
        if mod.split(".")[0] == "getpass":
            problems.append("import getpass")
    for needle in (".config/mooapp", "secrets.toml", "load_secrets", "smtplib"):
        if needle in text:
            problems.append(f"出现 {needle!r}（R3）")
    assert not problems, f"{probe.name}: " + "; ".join(problems)


@pytest.mark.parametrize("probe", PROBES, ids=lambda p: p.name)
def test_r1_write_calls_pass_simulate(probe: Path):
    tree = parse(probe)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
        if name not in WRITE_CALLS:
            continue
        kw = {k.arg: k.value for k in node.keywords}
        if "trd_env" not in kw or not is_trdenv_attr(kw["trd_env"], "SIMULATE"):
            bad.append(f"第 {node.lineno} 行：{name} 未显式传 trd_env=TrdEnv.SIMULATE")
        if None in kw:
            bad.append(f"第 {node.lineno} 行：{name} 使用 **kwargs，无法静态确认 trd_env")
    assert not bad, f"{probe.name}: " + "; ".join(bad)


@pytest.mark.parametrize("probe", PROBES, ids=lambda p: p.name)
def test_r2_probe_imports_only_sdk_stdlib_config(probe: Path):
    bad = []
    for mod in imported_modules(parse(probe)):
        top = mod.split(".")[0]
        if mod.startswith("."):
            bad.append(mod)
        elif top == "moomoo" or top in sys.stdlib_module_names:
            continue
        elif top == "mooapp":
            if not (mod == ALLOWED_MOOAPP or mod.startswith(ALLOWED_MOOAPP + ".")):
                bad.append(mod)
        else:
            bad.append(mod)
    assert not bad, f"{probe.name} 只能用 moomoo、标准库与 mooapp.config：{bad}"


def test_r2_src_and_tests_do_not_import_scripts():
    bad = []
    for root in (REPO / "src", REPO / "tests"):
        for p in root.rglob("*.py"):
            tree = parse(p)
            for mod in imported_modules(tree):
                if mod.split(".")[0] in {"scripts", "runpy"} or mod.startswith("probe_"):
                    bad.append(f"{p.relative_to(REPO)}: import {mod}")
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    f = node.func
                    name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
                    if name in {"exec", "run_path", "spec_from_file_location"}:
                        bad.append(f"{p.relative_to(REPO)}:{node.lineno} 调用 {name}")
    assert not bad, bad
