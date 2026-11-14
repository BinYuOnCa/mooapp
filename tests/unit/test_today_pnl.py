"""当日盈亏的数据来源（M4 确认点 2；ADR-0017）：position_list_query 各行 today_pl_val 之和，含 qty=0 的行。

vendor § Get Positions › today_pl_val：Gain or loss today（3 位小数）。模拟盘是否有效待探针 P10；
任一行缺失、为 N/A 或 NaN，整体视为「不可用」（返回 None），由闸门拒绝买单，不猜。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from mooapp.broker.convert import position_from_row, today_pnl_from_rows
from mooapp.broker.fake import FakeBroker
from mooapp.clock import FakeClock
from mooapp.common import Env


def row(code="US.SPY", qty=1.0, today="1.25"):
    r = {"code": code, "qty": qty, "can_sell_qty": qty, "average_cost": 10.0}
    if today is not None:
        r["today_pl_val"] = float(today) if today not in ("N/A",) else today
    return r


def test_sum_includes_closed_rows():
    rows = [row(today="1.25"), row(code="US.TD", qty=0.0, today="-3.5")]
    assert today_pnl_from_rows(rows) == Decimal("-2.25")


def test_empty_positions_is_zero():
    assert today_pnl_from_rows([]) == Decimal("0")


def test_missing_field_is_unavailable():
    assert today_pnl_from_rows([row(today=None)]) is None


def test_na_is_unavailable():
    assert today_pnl_from_rows([row(today="N/A")]) is None


def test_nan_is_unavailable():
    r = row()
    r["today_pl_val"] = float("nan")
    assert today_pnl_from_rows([r]) is None


def test_float_parsed_without_binary_noise():
    assert today_pnl_from_rows([row(today="0.1"), row(today="0.2")]) == Decimal("0.3")


def test_position_row_carries_today_pl_val():
    p = position_from_row(row(today="1.25"))
    assert p is not None and p.today_pl_val == Decimal("1.25")
    p2 = position_from_row(row(today=None))
    assert p2 is not None and p2.today_pl_val is None


def test_fake_broker_today_pnl_settable():
    fb = FakeBroker(Env.SIMULATE, FakeClock(datetime(2026, 11, 16, 15, 0, tzinfo=UTC)))
    assert fb.query_today_pnl() == Decimal("0")
    fb.set_today_pnl(Decimal("-7.5"))
    assert fb.query_today_pnl() == Decimal("-7.5")
    fb.set_today_pnl(None)
    assert fb.query_today_pnl() is None
