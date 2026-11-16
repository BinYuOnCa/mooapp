"""`mooapp eod [--date]`（PROGRESS「M7 计划」决定 1、4）：只读命令，直接读库、查券商；写 CSV 和 eod_reports；
退出码：0 无差异，EXIT_BREAK 有差异，EXIT_CONFIG 运行时点或日期不对。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from oms_harness import make_system
from test_eod_recon import CLOSE_UTC, FRI, close_market, trade_day

from mooapp.common import Side
from mooapp.ops import cli
from mooapp.oms.store import FillRecord


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    sys = make_system()
    closed: list[str] = []
    monkeypatch.setattr(cli, "_eod_runtime", lambda env: (sys.store, sys.broker, sys.clock, [lambda: closed.append("x")]))
    monkeypatch.setattr(cli, "REPORTS_DIR", tmp_path)
    return sys, closed, tmp_path


def invoke(*args):
    return CliRunner().invoke(cli.app, ["eod", *args])


def test_eod_clean_day_exit_0_writes_csv_and_db(runtime):
    sys, closed, out = runtime
    trade_day(sys)
    close_market(sys)
    result = invoke()
    assert result.exit_code == 0, result.output
    assert "2026-11-13" in result.output and "无差异" in result.output
    assert (out / "2026-11-13.csv").exists()
    assert len(sys.store.eod_reports(FRI)) == 1
    assert closed == ["x"]  # 连接都关闭


def test_eod_break_exit_code_and_tag(runtime):
    sys, closed, _ = runtime
    cid = trade_day(sys)
    with sys.store.transaction():
        sys.store.insert_fill_once(
            FillRecord("fake:1", cid, sys.oms.get(cid).broker_order_id, "US.SPY", Side.BUY, 1, Decimal("10"), sys.clock.now(), False, "local")
        )
    close_market(sys)
    result = invoke("--date", "2026-11-13")
    assert result.exit_code == cli.EXIT_BREAK
    assert "[BREAK]" in result.output and "fill_qty_mismatch" in result.output
    assert sys.store.eod_reports(FRI)[0].ok is False


def test_eod_too_early_is_config_error_and_writes_nothing(runtime):
    sys, closed, out = runtime
    trade_day(sys)
    close_market(sys, run_at=CLOSE_UTC + timedelta(minutes=5))
    result = invoke()
    assert result.exit_code == cli.EXIT_CONFIG
    assert "30 分钟" in result.output
    assert not list(out.iterdir()) and sys.store.eod_reports(FRI) == []
    assert closed == ["x"]


def test_eod_bad_date_format(runtime):
    result = invoke("--date", "11/13/2026")
    assert result.exit_code == cli.EXIT_CONFIG


def test_eod_default_date_is_latest_completed_session(runtime):
    """不给 --date：取最近一个已过「收盘后 30 分钟」的交易日（周六运行 → 周五）。"""
    sys, _, out = runtime
    trade_day(sys)
    close_market(sys, run_at=datetime(2026, 11, 14, 15, 0, tzinfo=UTC))
    result = invoke()
    assert result.exit_code == 0, result.output
    assert (out / "2026-11-13.csv").exists()
