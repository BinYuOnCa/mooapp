"""M5 事件 → 邮件（notify/compose.py，纯逻辑；PROGRESS「M5 计划」决定 4）。

- 成交：同一订单在 merge_partial_fills_seconds 窗口内合并为一封；订单到终态立即发出（验收 2 的逻辑部分）
- RiskRejected、HaltChanged、关键报警：立即发
- 其余报警：每 alert_digest_minutes 汇总一封；没有新报警不发；email_* 报警不进邮件（避免发信失败的报警又触发发信）
- 账号只显示后 4 位（SPEC 5.7）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from mooapp.common import Env, OrderKind, Side
from mooapp.config.models import EmailCfg
from mooapp.notify.compose import CRITICAL_ALERTS, Composer
from mooapp.oms.events import Alert
from mooapp.oms.state import Order, OrdStatus
from mooapp.risk.gate import HaltChanged, RiskRejected

S = OrdStatus
T0 = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)
CFG = EmailCfg(enabled=True, recipients=("me@example.com",), merge_partial_fills_seconds=60, heartbeat_minutes_before_open=15,
               alert_digest_minutes=30)  # fmt: skip


def order(cum: int, status: S = S.PARTIALLY_FILLED, qty: int = 100, cid: str = "c1", avg: str = "10.00") -> Order:
    return Order(cid, "US.SPY", Side.BUY, OrderKind.LIMIT, qty, Decimal("10.05"), broker_order_id="900001",
                 status=status, cum_qty=cum, avg_px=Decimal(avg))  # fmt: skip


def comp() -> Composer:
    return Composer(CFG, Env.SIMULATE)


def at(sec: float) -> datetime:
    return T0 + timedelta(seconds=sec)


# ------------------------------------------------------------------ 成交合并


def test_five_partial_fills_one_email_on_terminal():
    c = comp()
    out = []
    for i, cum in enumerate((20, 40, 60, 80)):
        out += c.on_order(order(cum), at(i * 5))
    assert out == []  # 窗口内、未终态：不发
    out += c.on_order(order(100, S.FILLED, avg="10.02"), at(20))
    assert len(out) == 1
    e = out[0]
    assert e.kind == "fill" and "US.SPY" in e.subject
    assert "5 次" in e.body and "100/100" in e.body and "10.02" in e.body


def test_window_expiry_flushes_on_tick_then_new_window():
    c = comp()
    c.on_order(order(20), at(0))
    c.on_order(order(40), at(30))
    assert c.tick(at(59)) == []
    first = c.tick(at(60))
    assert len(first) == 1 and "2 次" in first[0].body and "40/100" in first[0].body
    c.on_order(order(60), at(70))
    rest = c.on_order(order(100, S.FILLED), at(80))
    assert len(rest) == 1 and "2 次" in rest[0].body  # 第二个窗口：60、100 两次


def test_order_change_without_new_fill_ignored():
    c = comp()
    assert c.on_order(order(0, S.NEW), at(0)) == []
    c.on_order(order(10), at(1))
    assert c.on_order(order(10), at(2)) == []  # 重复快照，cum 不变


def test_canceled_after_partial_flushes():
    c = comp()
    c.on_order(order(30), at(0))
    out = c.on_order(order(30, S.CANCELED), at(5))
    assert len(out) == 1 and "30/100" in out[0].body and "CANCELED" in out[0].body


def test_orders_merged_independently():
    c = comp()
    c.on_order(order(10, cid="a"), at(0))
    c.on_order(order(10, cid="b"), at(1))
    out = c.on_order(order(100, S.FILLED, cid="a"), at(2))
    assert len(out) == 1 and "a" in out[0].body
    assert len(c.tick(at(61))) == 1  # b 的窗口到期


# ------------------------------------------------------------------ 立即发


def test_risk_rejected_immediate():
    out = comp().on_risk_rejected(RiskRejected("max_order_qty", "数量 101 超过上限 100", "place", "cli", "cli:test", "c9", "US.SPY"), at(0))
    assert len(out) == 1 and out[0].kind == "risk_rejected" and "max_order_qty" in out[0].subject


def test_halt_and_resume_immediate():
    c = comp()
    assert "halt" in c.on_halt(HaltChanged(True, "手动", "cli"), at(0))[0].subject.lower()
    assert len(c.on_halt(HaltChanged(False, "resume", "cli"), at(1))) == 1


def test_critical_alerts_immediate():
    c = comp()
    for kind in ("daily_loss_halt", "halt_cancel_failed", "resume_rejected"):
        assert kind in CRITICAL_ALERTS
        out = c.on_alert(Alert(kind, "x"), at(0))
        assert len(out) == 1 and out[0].kind == "alert" and kind in out[0].subject


def test_merge_alert_kinds_classified():
    """合并 api-guard、M8 后的新报警（你的决定 3A）：签名批准被拒、版本变化立即发；API 形状漂移进汇总。"""
    c = comp()
    for kind in ("approve_rejected", "api_version_changed"):
        assert kind in CRITICAL_ALERTS
        assert len(c.on_alert(Alert(kind, "x"), at(0))) == 1
    for kind in ("api_new_field", "api_field_missing", "api_new_enum"):
        assert kind not in CRITICAL_ALERTS
        assert c.on_alert(Alert(kind, "x"), at(0)) == []


# ------------------------------------------------------------------ 汇总


def test_other_alerts_digested_every_30_minutes():
    c = comp()
    for i in range(51):
        assert c.on_alert(Alert("unknown_client_order_id", f"订单 {i}"), at(i)) == []
    c.on_alert(Alert("external_order", "外部订单"), at(60))
    assert c.tick(at(29 * 60)) == []
    out = c.tick(at(30 * 60))
    assert len(out) == 1 and out[0].kind == "digest"
    assert "unknown_client_order_id × 51" in out[0].body and "external_order × 1" in out[0].body
    assert c.tick(at(61 * 60)) == []  # 没有新报警：不发


def test_digest_interval_configurable():
    c = Composer(CFG.model_copy(update={"alert_digest_minutes": 15}), Env.SIMULATE)
    c.on_alert(Alert("poll_error", "x"), at(0))
    assert len(c.tick(at(15 * 60))) == 1


def test_email_alerts_never_emailed():
    c = comp()
    assert c.on_alert(Alert("email_send_failed", "SMTP 认证失败"), at(0)) == []
    assert c.tick(at(31 * 60)) == []


def test_account_numbers_masked():
    out = comp().on_alert(Alert("daily_loss_halt", "触线", details={"acc_id": 2964165, "card_num": "12345678"}), at(0))
    body = out[0].body
    assert "2964165" not in body and "****4165" in body
    assert "12345678" not in body and "****5678" in body


def test_subject_carries_env():
    out = comp().on_halt(HaltChanged(True, "x", "cli"), at(0))
    assert "SIMULATE" in out[0].subject


# ------------------------------------------------------------------ 显示格式（2026-11-16 你的全局要求：内部 UTC，给人看的一律美东；ADR-0033）


def test_fill_email_price_and_time_human_format():
    c = comp()
    c.on_order(order(2), at(3))
    e = c.on_order(order(5, S.FILLED, qty=5, avg="10.000000"), at(4))[0]
    assert "均价 10.00" in e.body and "10.000000" not in e.body
    assert "2026-11-17 10:00:03（美东）" in e.body  # T0 = 15:00 UTC = 美东 10:00
    assert "+00:00" not in e.body


def test_price_keeps_needed_decimals():
    c = comp()
    c.on_order(order(1), at(0))
    e = c.on_order(order(100, S.FILLED, avg="745.755"), at(1))[0]
    assert "均价 745.755" in e.body


def test_all_email_times_are_eastern():
    c = comp()
    emails = [
        *c.on_risk_rejected(RiskRejected("whitelist", "x", "place", "cli", "cli", None, "US.MSFT"), at(0)),
        *c.on_halt(HaltChanged(True, "x", "cli"), at(0)),
        *c.on_alert(Alert("daily_loss_halt", "x"), at(0)),
    ]
    c.on_alert(Alert("poll_error", "x"), at(0))
    emails += c.tick(at(30 * 60))
    for e in emails:
        assert "+00:00" not in e.body and "T15:" not in e.body, e.body
        assert "（美东）" in e.body
