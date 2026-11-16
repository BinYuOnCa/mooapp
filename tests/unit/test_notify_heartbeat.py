"""M5 心跳（PROGRESS「M5 计划」决定 5）：交易日开盘前 heartbeat_minutes_before_open 分钟（默认 15，即 9:15 美东；可配 5–25）；
常驻进程晚于该时刻启动则启动后补发一封并标「迟到」；每天一封（记在 kv，重启不重发）；非交易日不发。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from oms_harness import make_system
from test_notify_notifier import CFG, FakeMailer

from mooapp.clock import FakeClock
from mooapp.common import Env
from mooapp.config.models import EmailCfg
from mooapp.notify.notifier import Notifier

TUE_0914 = datetime(2026, 11, 17, 14, 14, tzinfo=UTC)  # 美东 9:14


def setup(start: datetime, cfg: EmailCfg = CFG, status=None):
    sys = make_system(clock=FakeClock(start), open_market=False)
    m = FakeMailer()
    n = Notifier(bus=sys.bus, store=sys.store, mailer=m, cfg=cfg, clock=sys.clock, env=Env.SIMULATE, md=sys.broker,
                 threaded=False, status=status)  # fmt: skip
    n.start()
    return sys, m, n


def test_sent_at_0915_once():
    sys, m, n = setup(TUE_0914)
    n.tick()
    assert m.kinds() == []
    sys.clock.advance(60)
    n.tick()
    assert m.kinds() == ["heartbeat"]
    assert "迟到" not in m.sent[0][0].subject
    sys.clock.advance(3600)
    n.tick()
    assert m.kinds() == ["heartbeat"]


def test_configurable_minutes_before_open():
    sys, m, n = setup(datetime(2026, 11, 17, 14, 5, tzinfo=UTC), cfg=CFG.model_copy(update={"heartbeat_minutes_before_open": 25}))
    n.tick()
    assert m.kinds() == ["heartbeat"]  # 9:05


def test_late_start_sends_marked_late():
    sys, m, n = setup(datetime(2026, 11, 17, 15, 0, tzinfo=UTC))  # 10:00 才启动
    n.tick()
    assert m.kinds() == ["heartbeat"] and "迟到" in m.sent[0][0].subject


def test_not_on_non_trading_day():
    sys, m, n = setup(datetime(2026, 11, 21, 14, 30, tzinfo=UTC))  # 周六
    n.tick()
    assert m.kinds() == []


def test_not_resent_after_restart():
    sys, m, n = setup(datetime(2026, 11, 17, 14, 20, tzinfo=UTC))
    n.tick()
    n.stop()
    m2 = FakeMailer()
    n2 = Notifier(bus=sys.bus, store=sys.store, mailer=m2, cfg=CFG, clock=sys.clock, env=Env.SIMULATE, md=sys.broker, threaded=False)
    n2.start()
    n2.tick()
    assert m.kinds() == ["heartbeat"] and m2.kinds() == []


def test_body_has_status():
    sys, m, n = setup(datetime(2026, 11, 17, 14, 15, tzinfo=UTC), status=lambda: {"oms_ready": True, "halted": False, "alerts_today": 3})
    n.tick()
    body = m.sent[0][0].body
    assert "oms_ready" in body and "halted" in body and "alerts_today" in body


# ------------------------------------------------------------------ 配置范围


@pytest.mark.parametrize("minutes, ok", [(4, False), (5, True), (25, True), (26, False)])
def test_heartbeat_minutes_bounds(minutes, ok):
    kw = dict(enabled=True, recipients=("a@b.c",), merge_partial_fills_seconds=60, alert_digest_minutes=30)
    if ok:
        EmailCfg(heartbeat_minutes_before_open=minutes, **kw)
    else:
        with pytest.raises(ValidationError):
            EmailCfg(heartbeat_minutes_before_open=minutes, **kw)


@pytest.mark.parametrize("minutes, ok", [(14, False), (15, True), (120, True), (121, False)])
def test_digest_minutes_bounds(minutes, ok):
    kw = dict(enabled=True, recipients=("a@b.c",), merge_partial_fills_seconds=60, heartbeat_minutes_before_open=15)
    if ok:
        EmailCfg(alert_digest_minutes=minutes, **kw)
    else:
        with pytest.raises(ValidationError):
            EmailCfg(alert_digest_minutes=minutes, **kw)


def test_settings_defaults():
    from pathlib import Path

    from mooapp.config.loader import load_settings

    s = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.toml")
    assert s.notify_email.heartbeat_minutes_before_open == 15
    assert s.notify_email.alert_digest_minutes == 30
