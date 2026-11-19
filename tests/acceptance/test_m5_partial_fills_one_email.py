"""M5 验收 2：一笔单分 5 次成交，只收到 1 封合并邮件。FakeBroker 注入 5 次部分成交（模拟盘不会部分成交，learnings M2），
经真实 SMTP 发出。不连 OpenD、不连数据库；任何时段可跑：
    uv run pytest -m sim tests/acceptance/test_m5_partial_fills_one_email.py -s
运行后在邮箱核对：只有 1 封「成交 US.SPY BUY 5/5」，正文「本次合并 5 次成交」。"""
from decimal import Decimal
from pathlib import Path
import pytest
from oms_harness import make_system
from mooapp.common import Env
from mooapp.config.loader import load_secrets, load_settings
from mooapp.notify.mailer import SmtpMailer
from mooapp.notify.notifier import HEARTBEAT_KEY, Notifier

pytestmark = pytest.mark.sim
REPO = Path(__file__).resolve().parents[2]


class Counting:
    def __init__(self, inner):
        self.inner, self.sent = inner, []

    def send(self, email, recipients):
        self.inner.send(email, recipients)
        self.sent.append(email)


def test_five_partial_fills_one_real_email():
    sys = make_system()
    with sys.store.transaction():
        sys.store.set_kv(HEARTBEAT_KEY + "2026-11-13", "acceptance")  # harness 日期的心跳不发
    mailer = Counting(SmtpMailer(lambda: load_secrets().smtp))
    cfg = load_settings(REPO / "config" / "settings.toml").notify_email
    n = Notifier(bus=sys.bus, store=sys.store, mailer=mailer, cfg=cfg, clock=sys.clock, env=Env.SIMULATE, md=sys.broker, threaded=False)
    n.start()
    cid = sys.place(qty=5).order.client_order_id
    sys.broker.step()
    sys.pump.drain()
    bid = sys.oms.get(cid).broker_order_id
    for _ in range(5):
        sys.broker.fill(bid, 1, Decimal("10.00"))
        sys.pump.drain()
        sys.clock.advance(3)
        n.tick()
    assert sys.oms.get(cid).status.value == "FILLED"
    assert [e.kind for e in mailer.sent] == ["fill"], [e.subject for e in mailer.sent]
    print(f"\n已发出 1 封：{mailer.sent[0].subject}\n{mailer.sent[0].body}")