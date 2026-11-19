"""mooapp run 连不上 OpenD 时只打一行并以 EXIT_BROKER 退出（2026-11-19 修复计划 C1）。

背景：2026-11-18 早上 OpenD 未启动，cron 每次重试都打出 55 行调用栈，日志里有用的只有最后一行。
其他异常仍然打出完整调用栈，不吞错。
"""

from __future__ import annotations

from typer.testing import CliRunner

from mooapp.broker.port import BrokerUnavailable
from mooapp.ops import cli
from mooapp.ops import run as run_mod


def test_broker_unavailable_one_line(monkeypatch):
    def refuse(*a, **kw):
        raise BrokerUnavailable("OpenD 127.0.0.1:11111 拒绝连接（OpenD 未启动？）")

    monkeypatch.setattr(run_mod, "build_runtime", refuse)
    result = CliRunner().invoke(cli.app, ["run"])
    assert result.exit_code == cli.EXIT_BROKER
    assert "Traceback" not in result.output
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert lines == ["无法连接 OpenD：OpenD 127.0.0.1:11111 拒绝连接（OpenD 未启动？）"]


def test_exit_broker_distinct():
    codes = [cli.EXIT_CONFIG, cli.EXIT_HUMAN_LOCK, cli.EXIT_COMMAND, cli.EXIT_BROKER]
    assert len(set(codes)) == len(codes)
    assert cli.EXIT_BROKER == 6  # 5 留给 M7 的 EXIT_BREAK（尚未合并）


def test_other_errors_still_raise(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("意外错误")

    monkeypatch.setattr(run_mod, "build_runtime", boom)
    result = CliRunner().invoke(cli.app, ["run"])
    assert result.exit_code != 0 and isinstance(result.exception, RuntimeError)  # 未被吞掉，调用栈照常
