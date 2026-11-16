"""M6 dry-run：用一串价格回放规则，打印何时触发什么（手册 M6 第 4 点）。

价格文件 CSV：ts,type,a,b；type 取 open | fill | tick | bar | reject_next_sell | expire（M6a 计划）。
卖单默认按触发时看到的价格立即成交（K 线按触发价估计），并打印滑点。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mooapp.ops import cli
from mooapp.strategy.dryrun import DryRunError, dry_run, format_events, parse_prices
from mooapp.strategy.machine import RuleStatus
from mooapp.strategy.spec import load_rule_file

REPO = Path(__file__).resolve().parents[2]
RULE = REPO / "examples" / "rules" / "spy-oco.yaml"
PRICES = REPO / "examples" / "prices"
D = Decimal


def run(csv_name: str):
    return dry_run(load_rule_file(RULE), parse_prices((PRICES / csv_name).read_text(encoding="utf-8")))


def test_parse_prices_rows():
    rows = parse_prices("ts,type,a,b\n09:30:00,open,99.99,\n09:31:00,fill,4,100.21\n09:32:00,bar,94,106\n")
    assert [(r.ts, r.type, r.a, r.b) for r in rows] == [
        ("09:30:00", "open", D("99.99"), None),
        ("09:31:00", "fill", D("4"), D("100.21")),
        ("09:32:00", "bar", D("94"), D("106")),
    ]


@pytest.mark.parametrize(
    "text, line",
    [
        ("ts,type,a,b\n09:30:00,teleport,1,\n", 2),
        ("ts,type,a,b\n09:30:00,tick,abc,\n", 2),
        ("ts,type,a,b\n09:30:00,open,99.99,\n09:31:00,fill,4,\n", 3),  # fill 缺价格
        ("ts,type,a,b\n09:30:00,fill,1.5,100\n", 2),  # 数量不是整数
        ("ts,price\n09:30:00,1\n", 1),  # 表头不对
    ],
)
def test_parse_errors_report_line(text, line):
    with pytest.raises(DryRunError, match=f"第 {line} 行"):
        parse_prices(text)


@pytest.mark.parametrize(
    "csv_name, side, qty, price, slippage",
    [
        ("path1_stop_first.csv", "stop", 10, "95.00", "0.00"),
        ("path2_take_first.csv", "take", 10, "105.22", "0.00"),
        ("path3_gap_stop.csv", "stop", 10, "92.50", "2.50"),
        ("path4_bar_both.csv", "take", 10, "105.22", "0.00"),
        ("path5a_partial_then_stop.csv", "stop", 10, "94.80", "0.20"),
    ],
)
def test_example_paths(csv_name, side, qty, price, slippage):
    r = run(csv_name)
    assert (r.stop_price, r.take_price) == (D("95.00"), D("105.22"))
    assert len(r.sells) == 1
    s = r.sells[0]
    assert (s.side, s.qty, s.price, s.slippage) == (side, qty, D(price), D(slippage))
    assert r.final_status is RuleStatus.CLOSED


def test_path5b_partial_cancel_entry_in_dry_run():
    r = run("path5b_partial_trigger.csv")
    assert [(s.side, s.qty) for s in r.sells] == [("stop", 4)]
    assert any(e.kind == "cancel_entry" for e in r.events)
    assert r.final_status is RuleStatus.CLOSED


def test_reject_then_retry_in_dry_run():
    r = run("retry_after_reject.csv")
    kinds = [e.kind for e in r.events]
    assert "sell_rejected" in kinds
    assert [s.qty for s in r.sells] == [10]
    assert r.sells[0].attempt == 2


def test_expired_without_fill_is_void():
    r = run("expire_no_fill.csv")
    assert r.final_status is RuleStatus.VOID and r.sells == []


def test_format_events_is_readable_table():
    text = format_events(run("path4_bar_both.csv"))
    assert "止盈触发" in text and "按止盈计" in text and "105.22" in text


def test_cli_dry_run_prints_events():
    result = CliRunner().invoke(cli.app, ["rule", "dry-run", str(RULE), "--prices", str(PRICES / "path1_stop_first.csv")])
    assert result.exit_code == 0, result.output
    assert "止损触发" in result.output and "95.00" in result.output


def test_cli_rule_check_ok_and_bad(tmp_path):
    ok = CliRunner().invoke(cli.app, ["rule", "check", str(RULE)])
    assert ok.exit_code == 0, ok.output
    bad = tmp_path / "bad.yaml"
    bad.write_text(RULE.read_text(encoding="utf-8").replace("min(open, avg_cost) * 0.95", "__import__('os')"), encoding="utf-8")
    res = CliRunner().invoke(cli.app, ["rule", "check", str(bad)])
    assert res.exit_code != 0 and "stop" in res.output
    assert "Traceback" not in res.output


def test_cli_dry_run_does_not_touch_db_or_opend(monkeypatch):
    """dry-run 纯本地：不连数据库、不连 OpenD。"""
    monkeypatch.setattr(cli, "_dsn", lambda env: pytest.fail("dry-run 不应读数据库配置"))
    monkeypatch.setattr(cli, "_client", lambda env: pytest.fail("dry-run 不应连数据库"))
    result = CliRunner().invoke(cli.app, ["rule", "dry-run", str(RULE), "--prices", str(PRICES / "path2_take_first.csv")])
    assert result.exit_code == 0, result.output
