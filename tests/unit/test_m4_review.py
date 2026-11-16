"""M4 审查修复（/code-review high dev/milestone_4；PROGRESS「M4 审查修复」）。每条一个复现测试。

除了常驻进程的线程测试（真实时钟、带超时），其余用同步模式的 harness。
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import time
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from oms_harness import make_system

from mooapp.common import Side
from mooapp.oms.events import Alert
from mooapp.oms.journal import Journal, JournaledStore
from mooapp.oms.state import OrdStatus
from mooapp.oms.store import MemoryStore, StoreUnavailable
from mooapp.ops.commands import CommandClient, CommandProcessor

S = OrdStatus
D = Decimal
REPO = Path(__file__).resolve().parents[2]


def three_working(sys) -> list[str]:
    cids = [sys.place(qty=1).order.client_order_id for _ in range(3)]
    sys.broker.step()
    sys.pump.drain()
    return cids


# ------------------------------------------------------------------ #1 halt 遇到状态已变的单不中断


def test_halt_continues_past_order_filled_meanwhile(monkeypatch):
    sys = make_system()
    cids = three_working(sys)
    stale = sys.oms.open_orders()  # halt 开始时的快照
    sys.broker.fill(sys.oms.get(cids[0]).broker_order_id, 1, D("10"))  # 随后第一张成交
    sys.pump.drain()
    monkeypatch.setattr(sys.oms, "open_orders", lambda: stale)
    report = sys.gate.halt("测试", actor="cli:test")
    assert cids[0] in report.failed
    assert sorted(report.cancel_sent) == sorted(cids[1:])
    assert sys.gate.halted


# ------------------------------------------------------------------ #2 撤单的「判断未确认」与「挂待撤标记」是原子的


def test_cancel_not_broken_by_ack_between_check_and_mark(monkeypatch):
    sys = make_system(attach=False)
    cid = sys.place(qty=1).order.client_order_id
    assert sys.oms.get(cid).status is S.PENDING_NEW
    real_get = sys.oms.get_or_none
    calls = {"n": 0}

    def get_then_ack(c):
        o = real_get(c)
        calls["n"] += 1
        if calls["n"] == 2:  # 第 1 次是闸门检查；第 2 次是撤单时的判断：此刻确认推送到达
            sys.broker.step()
            snap = [s for s in sys.broker.query_orders(open_only=False) if s.client_order_id == c][0]
            sys.oms.ingest_snapshot(snap, "push")
        return o

    monkeypatch.setattr(sys.oms, "get_or_none", get_then_ack)
    d = sys.cancel(cid)
    assert d.allowed
    o = real_get(cid)
    assert o.cancel_pending or sys.broker.calls("cancel_order")


# ------------------------------------------------------------------ #3 库不可用时 halt 照常撤单（ADR-0025）


DB_WRITES_AND_READS = ("list_orders", "get_order", "update_order", "append_event", "set_kv", "append_audit", "append_recon")


def db_down(monkeypatch, store):
    def down(*a, **k):
        raise StoreUnavailable("数据库不可用（测试）")

    for name in DB_WRITES_AND_READS:
        monkeypatch.setattr(store, name, down)


def test_halt_cancels_at_broker_when_db_down(monkeypatch):
    sys = make_system()
    cids = three_working(sys)
    bids = {sys.oms.get(c).broker_order_id for c in cids}
    db_down(monkeypatch, sys.store)
    report = sys.gate.halt("库挂了", actor="cli:test")
    assert sys.gate.halted
    assert {c[0] for c in sys.broker.calls("cancel_order")} == bids
    assert sorted(report.cancel_sent) == sorted(cids)
    assert "halt_degraded" in sys.alert_kinds()


def test_user_cancel_works_when_db_down(monkeypatch):
    sys = make_system()
    cid = three_working(sys)[0]
    bid = sys.oms.get(cid).broker_order_id
    db_down(monkeypatch, sys.store)
    d = sys.cancel(cid)
    assert d.allowed and (bid,) in sys.broker.calls("cancel_order")


# ------------------------------------------------------------------ #4 journal 里的 halt 重启后仍然有效（ADR-0022）


class FlakyKvStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.down = False

    def set_kv(self, key, value):
        if self.down:
            raise StoreUnavailable("数据库不可用（测试）")
        return super().set_kv(key, value)

    def append_audit(self, a):
        if self.down:
            raise StoreUnavailable("数据库不可用（测试）")
        return super().append_audit(a)


def test_halt_written_only_to_journal_survives_restart(tmp_path):
    sys1 = make_system()
    journal = Journal(tmp_path, sys1.env)
    inner = FlakyKvStore()
    jstore = JournaledStore(inner, journal, sys1.clock.now)
    sys1 = make_system(store=jstore, broker=sys1.broker, clock=sys1.clock, journal=journal)
    inner.down = True
    sys1.gate.halt("库挂着时 halt", actor="cli:test")
    assert "halt_not_persisted" in sys1.alert_kinds()
    assert journal.pending()
    inner.down = False
    sys2 = make_system(store=jstore, broker=sys1.broker, clock=sys1.clock, journal=journal)
    assert sys2.gate.halted, "journal 里的 halt 回放后，闸门必须处于 halt"
    assert sys2.place(qty=1).rule == "halt"


# ------------------------------------------------------------------ #5 当日亏损触线后只拦买单（ADR-0028）


def test_daily_loss_blocks_buys_but_allows_sells():
    from oms_harness import LOOSE_LIMITS

    sys = make_system(limits=LOOSE_LIMITS.model_copy(update={"max_daily_loss": Decimal("20"), "halt_on_daily_loss": False}))
    sys.broker.set_position("US.SPY", 5, D("10"))
    sys.broker.set_today_pnl(D("-25"))
    assert sys.place(qty=1).rule == "max_daily_loss"
    assert sys.place(qty=1, side=Side.SELL).allowed


def test_daily_loss_rule_sell_passes_and_buy_requests_halt():
    from dataclasses import replace as dc_replace

    from test_risk_rules import BASE, LIMITS, req

    from mooapp.risk.rules import check_order

    sell = dc_replace(BASE, req=req(side=Side.SELL), position_qty=10, can_sell_qty=10, daily_pnl=D("-500"))
    assert check_order(sell, LIMITS, 30.0).ok
    buy = check_order(dc_replace(BASE, daily_pnl=D("-500")), LIMITS, 30.0)
    assert not buy.ok and buy.halt


# ------------------------------------------------------------------ #6 / #9 常驻进程：LISTEN 出错不退出；心跳独立


def test_listen_error_does_not_kill_command_thread():
    from test_runtime import runtime

    rt = runtime()
    errors = {"n": 0}

    def broken_listen(timeout):
        errors["n"] += 1
        raise OSError("LISTEN 连接断开（测试）")

    rt.listen = broken_listen
    alerts: list[Alert] = []
    rt.bus.subscribe(Alert, alerts.append)
    rt.start()
    try:
        time.sleep(1.5)
        assert rt._thread is not None and rt._thread.is_alive()
        assert errors["n"] >= 1 and any(a.kind == "runner_error" for a in alerts)
        client = CommandClient(store=rt.store, clock=rt.clock, timeout=5.0, poll_interval=0.05)
        assert client.submit("halt", {"reason": "测试"}, actor="test")["halted"] is True
    finally:
        rt.stop()


def test_heartbeat_fresh_while_command_is_slow(monkeypatch):
    from test_runtime import runtime

    from mooapp.ops.commands import HEARTBEAT_KEY

    rt = runtime()
    real_dispatch = rt.processor._dispatch

    def slow(kind, payload, actor):
        time.sleep(4.0)
        return real_dispatch(kind, payload, actor)

    monkeypatch.setattr(rt.processor, "_dispatch", slow)
    rt.start()
    try:
        client = CommandClient(store=rt.store, clock=rt.clock, timeout=10.0, poll_interval=0.05)
        client.enqueue("halt", {"reason": "慢命令"}, actor="test")
        time.sleep(3.0)  # 命令还在处理中（开始处理前的那次心跳至少已过去 2 秒）
        from datetime import datetime

        age = (rt.clock.now() - datetime.fromisoformat(rt.store.get_kv(HEARTBEAT_KEY))).total_seconds()
        assert age < 1.6, f"心跳已 {age:.1f} 秒未更新"
    finally:
        rt.stop()


# ------------------------------------------------------------------ #7 命令过期；遗留的「处理中」命令


def processor_for(sys, **kw):
    proc = CommandProcessor(gate=sys.gate, store=sys.store, clock=sys.clock, bus=sys.bus, **kw)
    client = CommandClient(store=sys.store, clock=sys.clock, timeout=10.0, poll_interval=0.5)
    proc.heartbeat()
    return proc, client


def test_place_command_expires_when_cli_gave_up():
    sys = make_system()
    proc, client = processor_for(sys)
    c = client.enqueue("place", client.place_payload("US.SPY", "BUY", 1, D("10.00")), actor="cli")
    sys.clock.advance(11)  # 命令行 10 秒超时之后才被处理
    proc.process_one()
    r = sys.store.get_command(c).result
    assert r["allowed"] is False and r["rule"] == "expired"
    assert sys.broker.calls("place_order") == []


def test_orphaned_running_commands_marked_error_on_start():
    sys = make_system()
    proc, client = processor_for(sys)
    c = client.enqueue("halt", {"reason": "x"}, actor="cli")
    assert sys.store.claim_next_command(sys.clock.now()) is not None  # 被取走后常驻进程崩溃
    proc2, _ = processor_for(sys)
    assert proc2.recover_orphans() == 1
    got = sys.store.get_command(c)
    assert got.status == "error" and "重启" in got.result["error"]


# ------------------------------------------------------------------ #8 重启后补发「已确认且带待撤标记」的撤单


def test_deferred_cancel_resent_after_restart(monkeypatch):
    sys1 = make_system(attach=False)
    cid = sys1.place(qty=1).order.client_order_id
    sys1.cancel(cid)  # PENDING_NEW → 待撤
    monkeypatch.setattr(sys1.executor, "_send_deferred_cancel", lambda c: None)  # 确认处理完、撤单还没发就崩溃
    sys1.broker.step()
    sys1.poller.poll_once()
    sys1.pump.drain()
    o = sys1.oms.get(cid)
    assert o.status is S.NEW and o.cancel_pending and sys1.broker.calls("cancel_order") == []
    sys2 = make_system(store=sys1.store, broker=sys1.broker, clock=sys1.clock, attach=False)
    sys2.pump.drain()
    assert sys1.broker.calls("cancel_order") == [(o.broker_order_id,)]


# ------------------------------------------------------------------ #10 resume 签名（ADR-0029）


KEY = bytes.fromhex("11" * 32)


def signed_resume(sys, key=KEY, ts=None, nonce="n1"):
    from mooapp.ops.resume_auth import resume_payload

    return resume_payload(key, ts or sys.clock.now(), nonce=nonce)


def run_resume(sys, proc, client, payload):
    c = client.enqueue("resume", payload, actor="cli")
    proc.process_one()
    return sys.store.get_command(c)


def test_unsigned_resume_rejected():
    sys = make_system()
    sys.gate.halt("测试", actor="cli")
    proc, client = processor_for(sys, resume_key=KEY)
    got = run_resume(sys, proc, client, {})
    assert got.status == "error" and sys.gate.halted
    assert "resume_rejected" in sys.alert_kinds()


def test_signed_resume_accepted_once():
    sys = make_system()
    sys.gate.halt("测试", actor="cli")
    proc, client = processor_for(sys, resume_key=KEY)
    payload = signed_resume(sys)
    assert run_resume(sys, proc, client, payload).status == "done"
    assert not sys.gate.halted
    sys.gate.halt("再次", actor="cli")
    assert run_resume(sys, proc, client, payload).status == "error"  # 随机数不能重用
    assert sys.gate.halted


@pytest.mark.parametrize("variant", ["wrong_key", "too_old", "tampered"])
def test_bad_resume_signatures_rejected(variant):
    sys = make_system()
    sys.gate.halt("测试", actor="cli")
    proc, client = processor_for(sys, resume_key=KEY)
    if variant == "wrong_key":
        payload = signed_resume(sys, key=bytes.fromhex("22" * 32))
    elif variant == "too_old":
        payload = signed_resume(sys, ts=sys.clock.now() - timedelta(seconds=61))
    else:
        payload = {**signed_resume(sys), "nonce": "other"}
    assert run_resume(sys, proc, client, payload).status == "error"
    assert sys.gate.halted


def test_resume_without_configured_key_rejected():
    sys = make_system()
    sys.gate.halt("测试", actor="cli")
    proc, client = processor_for(sys)  # 常驻进程读不到 [resume] key
    assert run_resume(sys, proc, client, signed_resume(sys)).status == "error"
    assert sys.gate.halted


def test_signature_is_hmac_sha256_of_documented_message():
    sys = make_system()
    p = signed_resume(sys, nonce="abc")
    expected = hmac.new(KEY, f"resume|{p['ts']}|abc".encode(), hashlib.sha256).hexdigest()
    assert p["sig"] == expected


def test_cli_resume_signs_with_secrets_key(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from mooapp.ops import cli, humanlock

    secrets_file = tmp_path / "secrets.toml"
    secrets_file.write_text(
        '[smtp]\nhost = "h"\nport = 465\nusername = "u"\napp_password = "p"\n[resume]\nkey = "' + KEY.hex() + '"\n', encoding="utf-8"
    )
    secrets_file.chmod(0o600)
    monkeypatch.setattr(cli, "deps", replace(cli.deps, secrets_path=secrets_file))
    monkeypatch.setattr(humanlock, "confirm_interactively", lambda *a, **k: None)
    sent = {}
    monkeypatch.setattr(cli, "_submit", lambda ctx, kind, payload, actor="cli": sent.update(kind=kind, payload=payload) or {})
    result = CliRunner().invoke(cli.app, ["resume"])
    assert result.exit_code == 0, result.output
    p = sent["payload"]
    assert sent["kind"] == "resume"
    assert p["sig"] == hmac.new(KEY, f"resume|{p['ts']}|{p['nonce']}".encode(), hashlib.sha256).hexdigest()
    assert KEY.hex() not in result.output


# ------------------------------------------------------------------ 小问题


def test_deferred_cancel_runs_in_background_not_event_thread():
    """确认后的自动撤单交给 Executor 的 background（常驻进程里是独立线程），不在事件线程上调用券商。"""
    sys = make_system(attach=False)
    ran = []
    sys.executor.background = lambda fn: ran.append(fn)  # 截住，不执行
    cid = sys.place(qty=1).order.client_order_id
    sys.cancel(cid)
    sys.broker.step()
    sys.poller.poll_once()
    sys.pump.drain()
    assert len(ran) == 1 and sys.broker.calls("cancel_order") == []
    ran[0]()
    assert len(sys.broker.calls("cancel_order")) == 1


def test_runtime_gives_executor_a_background_thread():
    src = (REPO / "src" / "mooapp" / "ops" / "run.py").read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "Executor"]
    assert calls and all("background" in {k.arg for k in c.keywords} for c in calls)


def test_audit_marks_voided_allowed_row_when_db_down(monkeypatch):
    sys = make_system()

    def down(a):
        raise StoreUnavailable("数据库不可用（测试）")

    monkeypatch.setattr(sys.store, "append_audit", down)
    d = sys.place(qty=1)
    assert d.rule == "store_unavailable" and "作废" in d.reason


def test_gate_scans_open_orders_once_per_place(monkeypatch):
    sys = make_system()
    real = sys.store.list_orders
    calls = {"n": 0}

    def counting(open_only=False):
        calls["n"] += 1
        return real(open_only)

    monkeypatch.setattr(sys.store, "list_orders", counting)
    sys.place(qty=1)
    assert calls["n"] == 1


def test_second_halt_keeps_first_reason():
    sys = make_system()
    sys.gate.halt("第一次", actor="a")
    sys.gate.halt("第二次", actor="b")
    import json

    st = json.loads(sys.store.get_kv("halt"))
    assert (st["reason"], st["actor"]) == ("第一次", "a")


def test_moomoo_positions_queried_once_for_position_and_pnl():
    from test_moomoo_broker import FT, QuoteCtx, TradeCtx

    from mooapp.broker.moomoo import MoomooBroker
    from mooapp.common import Env

    trade = TradeCtx()
    b = MoomooBroker(FT, trade, QuoteCtx(), env=Env.SIMULATE, acc_id=2964165)
    b.query_positions()
    b.query_today_pnl()
    assert [n for n, _ in trade.calls].count("position_list_query") == 1


def test_probe_p10_checks_can_sell_with_open_sell():
    text = (REPO / "scripts" / "probe_m4_pnl.py").read_text(encoding="utf-8")
    assert "with_open_sell" in text
