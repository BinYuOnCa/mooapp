"""命令行拿到 expired 结果时（ADR-0030；M4 审查 #7）：说明「已过期、未执行」，以 EXIT_COMMAND 退出，不说成闸门拒绝，也不打印 None。"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from mooapp.ops import cli

EXPIRED = {"allowed": False, "rule": "expired", "reason": "命令行已在 2026-11-14T15:00:10+00:00 放弃等待，不再执行", "status": None}


@pytest.fixture
def submit_expired(monkeypatch):
    sent = {}

    def fake(ctx, kind, payload, actor="cli"):
        sent["kind"] = kind
        return {**EXPIRED, "client_order_id": payload.get("client_order_id"), "rule_id": payload.get("rule_id")}

    monkeypatch.setattr(cli, "_submit", fake)
    return sent


@pytest.mark.parametrize(
    "args",
    [["order", "buy", "US.SPY", "1", "--limit", "10.00"], ["order", "cancel", "abc"], ["rule", "cancel", "spy-oco-1"]],
)
def test_expired_result_reported_as_not_executed(submit_expired, args):
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == cli.EXIT_COMMAND, result.output
    assert "过期" in result.output and "未执行" in result.output
    assert "闸门拒绝" not in result.output and "None" not in result.output


def test_rule_add_expired(submit_expired, tmp_path):
    from test_commands import RULE_YAML

    f = tmp_path / "r.yaml"
    f.write_text(RULE_YAML, encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["rule", "add", str(f)])
    assert submit_expired["kind"] == "rule_add"
    assert result.exit_code == cli.EXIT_COMMAND
    assert "过期" in result.output and "未执行" in result.output
