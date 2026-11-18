"""API 行为守卫（ADR-0035；PROGRESS「API 行为守卫」）。

A 版本闸门：config/verified_versions.toml；doctor 不一致 WARN；常驻进程 SIMULATE 报警继续、REAL 拒绝
C 数据形状：返回字段与探针录制的基线比较（新字段 / 基线字段消失），关键枚举与 vendor 文档比较（新值）；只报警，每进程每项一次
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from mooapp.broker.drift import ENUMS, FIELDS, Drift, DriftDetector
from mooapp.common import Env
from mooapp.oms.events import Alert, EventBus

REPO = Path(__file__).resolve().parents[2]


def row_for(api: str, **override) -> dict:
    """基线字段齐全的一行；枚举字段给合法值。"""
    row = {f: "x" for f in FIELDS[api]}
    for f, v in {"order_status": "SUBMITTED", "trd_side": "BUY", "order_type": "NORMAL", "market_state": "AFTERNOON",
                 "trade_date_type": "WHOLE"}.items():  # fmt: skip
        if f in row:
            row[f] = v
    row.update(override)
    return row


def collect() -> tuple[DriftDetector, list[Drift]]:
    got: list[Drift] = []
    return DriftDetector(got.append), got


# ------------------------------------------------------------------ 基线来源


def test_field_baselines_from_recordings():
    assert len(FIELDS["order_list_query"]) == 28 and FIELDS["order_list_query"] == FIELDS["history_order_list_query"]
    assert {"trd_env", "trd_market"} <= FIELDS["push.order"] and len(FIELDS["push.order"]) == 29
    assert len(FIELDS["place_order"]) == 23 and len(FIELDS["position_list_query"]) == 33
    assert len(FIELDS["get_market_snapshot"]) == 142 and len(FIELDS["push.quote"]) == 62
    assert FIELDS["request_trading_days"] == frozenset({"time", "trade_date_type"})
    assert {"code", "market_state"} <= FIELDS["get_market_state"]


def test_enums_from_vendor_doc():
    from mooapp.oms.mapping import VENDOR_STATUSES

    assert ENUMS["order_status"] == frozenset(VENDOR_STATUSES)
    assert ENUMS["trd_side"] == frozenset({"NONE", "BUY", "SELL", "SELL_SHORT", "BUY_BACK"})
    assert ENUMS["trade_date_type"] == frozenset({"WHOLE", "MORNING", "AFTERNOON"})
    assert {"AFTERNOON", "AFTER_HOURS_BEGIN", "OVERNIGHT", "ASHARE_AFTER_HOURS_END"} <= ENUMS["market_state"]
    assert {"NORMAL", "MARKET", "STOP", "TRAILING_STOP_LIMIT", "TWAP"} <= ENUMS["order_type"]


# ------------------------------------------------------------------ 检测


def test_known_shape_no_drift():
    d, got = collect()
    assert d.check("order_list_query", [row_for("order_list_query")]) == []
    assert got == []


def test_new_field_reported_once():
    d, got = collect()
    d.check("order_list_query", [row_for("order_list_query", brand_new=1)])
    d.check("order_list_query", [row_for("order_list_query", brand_new=2)])
    assert [(x.kind, x.api, x.name) for x in got] == [("api_new_field", "order_list_query", "brand_new")]


def test_missing_baseline_field_reported_once():
    d, got = collect()
    row = row_for("position_list_query")
    del row["today_pl_val"]
    d.check("position_list_query", [row])
    d.check("position_list_query", [row])
    assert [(x.kind, x.name) for x in got] == [("api_field_missing", "today_pl_val")]


def test_field_present_in_any_row_is_not_missing():
    d, got = collect()
    a, b = row_for("order_list_query"), row_for("order_list_query")
    del a["remark"]
    d.check("order_list_query", [a, b])
    assert got == []


def test_new_enum_value_reported():
    d, got = collect()
    d.check("order_list_query", [row_for("order_list_query", order_status="HALF_CANCELLED")])
    d.check("request_trading_days", [{"time": "2026-11-27", "trade_date_type": "EVENING"}])
    assert [(x.kind, x.name, x.value) for x in got] == [
        ("api_new_enum", "order_status", "HALF_CANCELLED"),
        ("api_new_enum", "trade_date_type", "EVENING"),
    ]


def test_known_vendor_enum_and_placeholders_not_reported():
    d, got = collect()
    d.check("order_list_query", [row_for("order_list_query", trd_side="SELL_SHORT", order_type="N/A"), row_for("order_list_query", order_type=None)])
    assert got == []


def test_unknown_api_and_empty_rows_ignored():
    d, got = collect()
    assert d.check("some_new_api", [{"a": 1}]) == [] and d.check("order_list_query", []) == []
    assert got == []


def test_drift_message_human_readable():
    d, got = collect()
    d.check("get_market_state", [row_for("get_market_state", market_state="LUNAR_SESSION")])
    assert "get_market_state" in got[0].message and "LUNAR_SESSION" in got[0].message


# ------------------------------------------------------------------ 接入 MoomooBroker / ThrottledBroker / FakeBroker


def moomoo():
    from test_moomoo_broker import ACC, DF, FT, QuoteCtx, TradeCtx, order_row

    from mooapp.broker.moomoo import MoomooBroker

    trade, quote = TradeCtx(), QuoteCtx()
    b = MoomooBroker(FT, trade, quote, env=Env.SIMULATE, acc_id=ACC)
    got: list[Drift] = []
    b.on_drift(got.append)
    return b, trade, quote, got, DF, order_row


def test_moomoo_queries_checked():
    b, trade, _, got, DF, order_row = moomoo()
    trade.responses["order_list_query"] = (0, DF([{**row_for("order_list_query"), **order_row(), "brand_new": 1}]))
    b.query_orders(open_only=False)
    assert ("api_new_field", "order_list_query", "brand_new") in [(x.kind, x.api, x.name) for x in got]


def test_moomoo_order_push_checked():
    from test_moomoo_broker import order_handler

    b, trade, _, got, DF, order_row = moomoo()
    order_handler(trade).on_recv_rsp((0, DF([{**row_for("push.order"), **order_row(order_status="SUBMITTED"), "brand_new": 1}])))
    assert ("api_new_field", "push.order", "brand_new") in [(x.kind, x.api, x.name) for x in got]


def test_moomoo_trading_calendar_checked():
    b, _, quote, got, *_ = moomoo()
    quote.request_trading_days = lambda **kw: (0, [{"time": kw["start"], "trade_date_type": "EVENING"}])
    b.is_trading_day(date(2026, 11, 13))
    assert [(x.kind, x.value) for x in got] == [("api_new_enum", "EVENING")]


def test_moomoo_server_version():
    b, _, quote, *_ = moomoo()
    quote.get_global_state = lambda: (0, {"server_ver": "1012", "market_us": "AFTERNOON"})
    assert b.server_version() == "1012"


def test_throttled_and_fake_support_on_drift_and_version():
    from datetime import UTC, datetime

    from mooapp.broker.fake import FakeBroker
    from mooapp.broker.throttle import ThrottledBroker
    from mooapp.clock import FakeClock

    fb = FakeBroker(Env.SIMULATE, FakeClock(datetime(2026, 11, 13, 15, 0, tzinfo=UTC)))
    tb = ThrottledBroker(fb, FakeClock(datetime(2026, 11, 13, 15, 0, tzinfo=UTC)))
    tb.on_drift(lambda d: None)
    assert tb.server_version() == fb.server_version()


# ------------------------------------------------------------------ A 版本闸门


def test_verified_versions_file():
    from mooapp.config.loader import load_verified_versions

    v = load_verified_versions(REPO / "config" / "verified_versions.toml")
    assert (v.opend, v.sdk, v.verified_at) == ("1012", "10.12.7208", date(2026, 11, 16))
    assert v.evidence


def verified():
    from mooapp.config.models import VerifiedVersions

    return VerifiedVersions(opend="1012", sdk="10.12.7208", verified_at=date(2026, 11, 16), evidence="契约测试、P9–P13")


def test_version_diffs():
    from mooapp.ops.versions import version_diffs

    assert version_diffs("1012", "10.12.7208", verified()) == []
    assert version_diffs("1013", "10.12.7208", verified()) == ["OpenD 1012 → 1013"]
    assert version_diffs("1012", "10.13.0", verified()) == ["SDK 10.12.7208 → 10.13.0"]


def test_gate_simulate_alerts_and_continues():
    from mooapp.ops.versions import version_gate

    bus, alerts = EventBus(), []
    bus.subscribe(Alert, alerts.append)
    version_gate(Env.SIMULATE, ["OpenD 1012 → 1013"], bus)
    assert [a.kind for a in alerts] == ["api_version_changed"] and "1013" in alerts[0].message


def test_gate_real_refuses():
    from mooapp.ops.versions import VersionNotVerified, version_gate

    with pytest.raises(VersionNotVerified, match="1013"):
        version_gate(Env.REAL, ["OpenD 1012 → 1013"], EventBus())


def test_gate_no_diffs_silent():
    from mooapp.ops.versions import version_gate

    bus, alerts = EventBus(), []
    bus.subscribe(Alert, alerts.append)
    version_gate(Env.SIMULATE, [], bus)
    assert alerts == []


def test_doctor_version_check():
    from mooapp.ops.doctor import Status, version_check

    ok = version_check("1012", "10.12.7208", verified())
    assert ok.status is Status.PASS and "已验证" in ok.detail
    bad = version_check("1013", "10.12.7208", verified())
    assert bad.status is Status.WARN and "1012 → 1013" in bad.detail
    none = version_check("1012", "10.12.7208", None)
    assert none.status is Status.WARN and "verified_versions" in none.detail


def test_runtime_wires_drift_and_version_gate():
    src = (REPO / "src" / "mooapp" / "ops" / "run.py").read_text(encoding="utf-8")
    names = {n.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)
    }
    assert "on_drift" in names and "version_gate" in names and "version_diffs" in names
