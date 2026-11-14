"""M4 命令行：resume 必须在交互终端确认（ADR-0022、Q10）；非交互终端在连数据库之前就被拒绝。"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from mooapp.ops import cli


def test_resume_rejected_without_tty_before_touching_db(monkeypatch):
    monkeypatch.setattr(cli, "_client", lambda env: pytest.fail("非交互终端不应连数据库"))
    result = CliRunner().invoke(cli.app, ["resume"])
    assert result.exit_code == cli.EXIT_HUMAN_LOCK
    assert "交互终端" in result.output


def test_run_refuses_real_via_human_lock():
    """REAL 先被全局 --env 的人锁拦下（CliRunner 不是 TTY），根本到不了 build_runtime。"""
    result = CliRunner().invoke(cli.app, ["--env", "real", "run"])
    assert result.exit_code == cli.EXIT_HUMAN_LOCK


def test_rule_add_validates_locally_before_touching_db(monkeypatch, tmp_path):
    """M6b：rule add 先在本地校验；规则不合法时不连数据库。"""
    monkeypatch.setattr(cli, "_client", lambda env: pytest.fail("规则不合法时不应连数据库"))
    bad = tmp_path / "bad.yaml"
    bad.write_text("id: x\nsymbol: US.SPY\n", encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["rule", "add", str(bad)])
    assert result.exit_code != 0 and "规则不合法" in result.output
