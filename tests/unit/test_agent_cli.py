"""M8 命令行：`mooapp approve <id>` / `mooapp reject <id>` / `mooapp agent serve`（PROGRESS「M8 计划」；ADR-0034）。

approve：显示提议 → SIMULATE 输入 y 确认（REAL：全局 --env real 的人锁 + 交互终端确认码）→ 用 secrets 的 [resume] key 签名 → 写命令表。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from mooapp.ops import cli
from mooapp.oms.store import ApprovalRecord

KEY = bytes.fromhex("66" * 32)
T = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)
REC = ApprovalRecord("ap1", "order", {"symbol": "US.SPY", "side": "BUY", "qty": 1, "limit_price": "10.00"}, "试单", "agent:mcp",
                     "pending", T, T + timedelta(minutes=30), None, None)  # fmt: skip


@pytest.fixture
def env(monkeypatch, tmp_path):
    secrets_file = tmp_path / "secrets.toml"
    secrets_file.write_text(
        '[smtp]\nhost = "h"\nport = 465\nusername = "u"\napp_password = "p"\n[resume]\nkey = "' + KEY.hex() + '"\n', encoding="utf-8"
    )
    secrets_file.chmod(0o600)
    monkeypatch.setattr(cli, "deps", replace(cli.deps, secrets_path=secrets_file))
    monkeypatch.setattr(cli, "_load_approval", lambda env, approval_id: REC if approval_id == "ap1" else None)
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(cli, "_submit", lambda ctx, kind, payload, actor="cli": sent.append((kind, payload)) or {"approved": True, "allowed": True, "client_order_id": "c1"})
    return sent


def test_approve_shows_proposal_signs_and_submits(env):
    result = CliRunner().invoke(cli.app, ["approve", "ap1"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "US.SPY" in result.output and "试单" in result.output
    kind, p = env[0]
    assert kind == "approve" and p["approval_id"] == "ap1"
    assert p["sig"] == hmac.new(KEY, f"approve|ap1|{p['ts']}|{p['nonce']}".encode(), hashlib.sha256).hexdigest()
    assert KEY.hex() not in result.output


def test_approve_declined_submits_nothing(env):
    result = CliRunner().invoke(cli.app, ["approve", "ap1"], input="n\n")
    assert result.exit_code == 0 and "取消" in result.output
    assert env == []


def test_approve_unknown_id(env):
    result = CliRunner().invoke(cli.app, ["approve", "zzz"], input="y\n")
    assert result.exit_code == cli.EXIT_COMMAND and env == []


def test_approve_not_pending(env, monkeypatch):
    monkeypatch.setattr(cli, "_load_approval", lambda env, approval_id: replace(REC, status="rejected"))
    result = CliRunner().invoke(cli.app, ["approve", "ap1"], input="y\n")
    assert result.exit_code == cli.EXIT_COMMAND and "rejected" in result.output and env == []


def test_approve_without_key_is_config_error(env, monkeypatch, tmp_path):
    f = tmp_path / "nokey.toml"
    f.write_text('[smtp]\nhost = "h"\nport = 465\nusername = "u"\napp_password = "p"\n', encoding="utf-8")
    f.chmod(0o600)
    monkeypatch.setattr(cli, "deps", replace(cli.deps, secrets_path=f))
    result = CliRunner().invoke(cli.app, ["approve", "ap1"], input="y\n")
    assert result.exit_code == cli.EXIT_CONFIG and env == []


def test_approve_real_needs_human_lock(env):
    """REAL：全局 --env real 的人锁先拦下（CliRunner 不是 TTY），不会写命令表。"""
    result = CliRunner().invoke(cli.app, ["--env", "real", "approve", "ap1"], input="y\n")
    assert result.exit_code == cli.EXIT_HUMAN_LOCK and env == []


def test_reject_submits_with_reason(env):
    result = CliRunner().invoke(cli.app, ["reject", "ap1", "--reason", "不看好"])
    assert result.exit_code == 0, result.output
    assert env == [("reject", {"approval_id": "ap1", "reason": "不看好"})]


def test_agent_serve_command_exists():
    result = CliRunner().invoke(cli.app, ["agent", "serve", "--help"])
    assert result.exit_code == 0 and "MCP" in result.output
