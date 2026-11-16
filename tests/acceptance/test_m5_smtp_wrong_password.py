"""M5 验收 1：SMTP 密码填错时交易照常，改正后补发。分两步运行，中间你改 secrets：
  第 1 步（常规时段；先把 ~/.config/mooapp/secrets.toml 的 [smtp] app_password 改错）：
      uv run pytest -m sim tests/acceptance/test_m5_smtp_wrong_password.py -k step1 -s
  第 2 步（把密码改回正确值之后；任何时段）：
      uv run pytest -m sim tests/acceptance/test_m5_smtp_wrong_password.py -k step2 -s
mooapp run 必须停着（单实例锁）；不处于 halt。"""
import time
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
import pytest
from mooapp.common import Env
from mooapp.ops.commands import CommandClient
from mooapp.ops.run import build_runtime

pytestmark = pytest.mark.sim
REPO = Path(__file__).resolve().parents[2]


def wait(pred, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(1.0)
    return pred()


def failed_pending(rt):
    return [e for e in rt.store.due_emails(rt.clock.now() + timedelta(days=30)) if e.attempts >= 1]


def test_step1_wrong_password_trading_continues():
    rt = build_runtime(Env.SIMULATE, REPO / "config")
    rt.start()
    client = CommandClient(store=rt.store, clock=rt.clock, timeout=30.0, poll_interval=0.5)
    try:
        assert rt.notifier is not None, "settings.toml [notify.email] enabled 应为 true"
        assert not rt.gate.halted, "开始前不应处于 halt：先运行 mooapp resume"
        r = client.submit("place", client.place_payload("US.MSFT", "BUY", 1, Decimal("100.00")), actor="acceptance:m5")
        assert r["allowed"] is False and r["rule"] == "whitelist", r  # 产生一封「拒单」邮件
        last = rt.md.snapshot(["US.SPY"])[0].last
        px = (last * Decimal("0.96")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        r = client.submit("place", client.place_payload("US.SPY", "BUY", 1, px), actor="acceptance:m5")
        assert r["allowed"], r  # 交易照常
        cid = r["client_order_id"]
        assert wait(lambda: rt.oms.get(cid).status.value == "NEW", 30)
        assert client.submit("cancel", {"client_order_id": cid}, actor="acceptance:m5")["allowed"]
        assert wait(lambda: rt.oms.get(cid).status.value == "CANCELED", 30)
        assert wait(lambda: any(e.kind == "risk_rejected" for e in failed_pending(rt)), 30), "拒单邮件应发送失败并留在 outbox"
        for e in failed_pending(rt):
            print(f"\noutbox #{e.id} {e.kind} 第 {e.attempts} 次失败：{e.last_error}；下次 {e.next_attempt_at}")
    finally:
        rt.stop()


def test_step2_after_fix_resent():
    rt = build_runtime(Env.SIMULATE, REPO / "config")
    rt.start()
    try:
        before = failed_pending(rt)
        assert before, "outbox 里没有失败待重发的邮件：先跑第 1 步"
        assert wait(lambda: not failed_pending(rt), 200), [f"#{e.id} {e.last_error}" for e in failed_pending(rt)]
        print(f"\n已补发 {len(before)} 封：{[f'#{e.id} {e.kind}' for e in before]}；请在邮箱核对")
    finally:
        rt.stop()
