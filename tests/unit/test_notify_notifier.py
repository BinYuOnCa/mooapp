"""M5 通知器（notify/notifier.py）：订阅 EventBus 只排队；outbox 持久化；退避重试；SMTP 失败不阻塞交易；重启后补发。
PROGRESS「M5 计划」决定 2、3、6、7。同步模式（threaded=False）由测试显式调用 tick()。"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from decimal import Decimal

from oms_harness import make_system

from mooapp.clock import SystemClock
from mooapp.common import Env
from mooapp.config.models import EmailCfg
from mooapp.notify.mailer import Email, MailError
from mooapp.notify.notifier import BACKOFF_SECONDS, Notifier, deliver_or_enqueue
from mooapp.oms.events import Alert, EventBus
from mooapp.oms.store import MemoryStore, StoreUnavailable
from mooapp.oms.state import OrdStatus

S = OrdStatus
CFG = EmailCfg(enabled=True, recipients=("me@example.com",), merge_partial_fills_seconds=60, heartbeat_minutes_before_open=15,
               alert_digest_minutes=30)  # fmt: skip


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[tuple[Email, tuple[str, ...]]] = []
        self.fail: Exception | None = None
        self.delay = 0.0
        self.lock = threading.Lock()

    def send(self, email: Email, recipients) -> None:
        if self.delay:
            time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        with self.lock:
            self.sent.append((email, tuple(recipients)))

    def kinds(self) -> list[str]:
        return [e.kind for e, _ in self.sent]


def skip_heartbeat(sys) -> None:
    """harness 的时间是交易日美东 10:00，已过 9:15：先把当天的心跳记为已发，这里的测试只看各自的邮件（心跳另测）。"""
    from mooapp.notify.notifier import HEARTBEAT_KEY

    with sys.store.transaction():
        sys.store.set_kv(HEARTBEAT_KEY + "2026-11-13", "test")


def notifier(sys, mailer, **kw) -> Notifier:
    skip_heartbeat(sys)
    n = Notifier(bus=sys.bus, store=sys.store, mailer=mailer, cfg=CFG, clock=sys.clock, env=Env.SIMULATE, md=sys.broker,
                 threaded=False, **kw)  # fmt: skip
    n.start()
    return n


def filled_order(sys, qty=1):
    cid = sys.place(qty=qty).order.client_order_id
    sys.broker.step()
    sys.broker.fill(sys.oms.get(cid).broker_order_id, qty, Decimal("10.00"))
    sys.pump.drain()
    return cid


# ------------------------------------------------------------------ 验收 1 的 FakeBroker 版：SMTP 失败交易照常，改正后补发


def test_smtp_failure_does_not_block_trading_then_resends():
    sys = make_system()
    m = FakeMailer()
    m.fail = MailError("SMTP 认证失败（测试）")
    n = notifier(sys, m)
    cid = filled_order(sys)
    n.tick()
    assert sys.oms.get(cid).status is S.FILLED  # 交易照常
    pending = sys.store.due_emails(sys.clock.now() + timedelta(days=1))
    assert len(pending) == 1 and pending[0].attempts == 1 and "认证失败" in pending[0].last_error
    assert "email_send_failed" in sys.alert_kinds()
    m.fail = None  # 你改正了密码
    sys.clock.advance(BACKOFF_SECONDS[0])
    n.tick()
    assert m.kinds() == ["fill"]
    assert sys.store.due_emails(sys.clock.now() + timedelta(days=1)) == []


def test_backoff_schedule():
    sys = make_system()
    m = FakeMailer()
    m.fail = MailError("down")
    n = notifier(sys, m)
    sys.bus.publish(Alert("daily_loss_halt", "x"))
    n.tick()
    gaps = []
    for _ in range(6):
        e = sys.store.due_emails(sys.clock.now() + timedelta(days=1))[0]
        gaps.append((e.next_attempt_at - sys.clock.now()).total_seconds())
        sys.clock.advance(gaps[-1])
        n.tick()
    assert BACKOFF_SECONDS == (60, 120, 300, 900, 1800)
    assert gaps == [60, 120, 300, 900, 1800, 1800]


def test_not_due_not_retried():
    sys = make_system()
    m = FakeMailer()
    m.fail = MailError("down")
    n = notifier(sys, m)
    sys.bus.publish(Alert("daily_loss_halt", "x"))
    n.tick()
    m.fail = None
    sys.clock.advance(30)
    n.tick()
    assert m.sent == []  # 1 分钟还没到


def test_restart_resends_pending_from_outbox():
    sys = make_system()
    m1 = FakeMailer()
    m1.fail = MailError("down")
    n1 = notifier(sys, m1)
    sys.bus.publish(Alert("daily_loss_halt", "x"))
    n1.tick()
    n1.stop()
    m2 = FakeMailer()
    n2 = Notifier(bus=EventBus(), store=sys.store, mailer=m2, cfg=CFG, clock=sys.clock, env=Env.SIMULATE, md=sys.broker, threaded=False)
    n2.start()
    sys.clock.advance(BACKOFF_SECONDS[0])
    n2.tick()
    assert m2.kinds() == ["alert"]


def test_store_unavailable_keeps_email_in_memory_until_db_back(monkeypatch):
    sys = make_system()
    m = FakeMailer()
    n = notifier(sys, m)
    real = sys.store.enqueue_email
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise StoreUnavailable("数据库不可用（测试）")
        return real(*a, **k)

    monkeypatch.setattr(sys.store, "enqueue_email", flaky)
    sys.bus.publish(Alert("daily_loss_halt", "x"))
    n.tick()
    assert m.sent == []
    n.tick()
    assert m.kinds() == ["alert"]


# ------------------------------------------------------------------ 验收 2 的 FakeBroker 版：5 次成交 1 封


def test_five_partial_fills_one_email():
    sys = make_system()
    m = FakeMailer()
    n = notifier(sys, m)
    cid = sys.place(qty=5).order.client_order_id
    sys.broker.step()
    sys.pump.drain()
    bid = sys.oms.get(cid).broker_order_id
    for _ in range(5):
        sys.broker.fill(bid, 1, Decimal("10.00"))
        sys.pump.drain()
        sys.clock.advance(3)
        n.tick()
    assert sys.oms.get(cid).status is S.FILLED
    assert m.kinds() == ["fill"]
    assert "5 次" in m.sent[0][0].body


# ------------------------------------------------------------------ 不阻塞


def test_publish_does_not_block_on_slow_smtp():
    clock = SystemClock()
    bus = EventBus()
    m = FakeMailer()
    m.delay = 1.0
    store = MemoryStore()
    n = Notifier(bus=bus, store=store, mailer=m, cfg=CFG, clock=clock, env=Env.SIMULATE, md=None, threaded=True, poll_seconds=0.05)
    n.start()
    try:
        t0 = time.monotonic()
        for i in range(10):
            bus.publish(Alert("daily_loss_halt", f"x{i}"))
        assert time.monotonic() - t0 < 0.2
    finally:
        n.stop()


def test_email_failure_alert_not_emailed():
    sys = make_system()
    m = FakeMailer()
    m.fail = MailError("down")
    n = notifier(sys, m)
    sys.bus.publish(Alert("daily_loss_halt", "x"))
    n.tick()
    m.fail = None
    sys.clock.advance(31 * 60)
    n.tick()
    assert "digest" not in m.kinds()  # email_send_failed 不进汇总


# ------------------------------------------------------------------ eod 用：同步发送，失败转 outbox（决定 7）


def test_deliver_or_enqueue_success_leaves_no_outbox():
    sys = make_system()
    m = FakeMailer()
    assert deliver_or_enqueue(Email("eod", "EOD 2026-11-17", "无差异"), CFG.recipients, m, sys.store, sys.clock) is True
    assert m.kinds() == ["eod"]
    assert sys.store.due_emails(sys.clock.now() + timedelta(days=1)) == []


def test_deliver_or_enqueue_failure_goes_to_outbox():
    sys = make_system()
    m = FakeMailer()
    m.fail = MailError("down")
    assert deliver_or_enqueue(Email("eod", "EOD", "b"), CFG.recipients, m, sys.store, sys.clock) is False
    pending = sys.store.due_emails(sys.clock.now())
    assert len(pending) == 1 and pending[0].kind == "eod"
