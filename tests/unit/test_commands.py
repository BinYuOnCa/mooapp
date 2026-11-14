"""commands 表与常驻进程（SPEC 3.4；M4 计划设计要点）。

- 命令行（CommandClient）只写 commands 表并同步等待判定；常驻进程（CommandProcessor）按顺序取出、交给闸门、写回判定
- 常驻进程不在（心跳过期）时，写命令直接报错
- 处理命令出错：写回 error 状态和原因，并报警，不吞
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from oms_harness import make_system

from mooapp.ops.commands import CommandClient, CommandProcessor, CommandTimeout, RunnerNotRunning
from mooapp.ops.resume_auth import resume_payload
from mooapp.oms.state import OrdStatus

S = OrdStatus


RESUME_KEY = bytes.fromhex("33" * 32)  # 测试用；ADR-0029 的 resume 签名


def setup(**kw):
    sys = make_system(**kw)
    proc = CommandProcessor(gate=sys.gate, store=sys.store, clock=sys.clock, bus=sys.bus, resume_key=RESUME_KEY)
    client = CommandClient(store=sys.store, clock=sys.clock, timeout=10.0, poll_interval=0.5)
    return sys, proc, client


def test_no_runner_means_error():
    sys, proc, client = setup()
    with pytest.raises(RunnerNotRunning):
        client.submit("place", {"symbol": "US.SPY", "side": "BUY", "qty": 1, "limit_price": "10.00"}, actor="cli")


def test_stale_heartbeat_means_error():
    sys, proc, client = setup()
    proc.heartbeat()
    sys.clock.advance(proc.HEARTBEAT_TTL + 1)
    with pytest.raises(RunnerNotRunning):
        client.submit("halt", {"reason": "x"}, actor="cli")


def test_place_round_trip():
    sys, proc, client = setup()
    proc.heartbeat()
    cmd_id = client.enqueue("place", {"symbol": "US.SPY", "side": "BUY", "qty": 1, "limit_price": "10.00"}, actor="cli:test")
    assert proc.process_one() is True
    result = client.wait(cmd_id)
    assert result["allowed"] is True and result["rule"] is None
    cid = result["client_order_id"]
    assert sys.oms.get(cid).status is S.PENDING_NEW
    assert sys.store.audit_records()[-1].actor == "cli:test"


def test_place_rejection_round_trip():
    sys, proc, client = setup()
    proc.heartbeat()
    cmd_id = client.enqueue("place", {"symbol": "US.AAPL", "side": "BUY", "qty": 1, "limit_price": "10.00"}, actor="cli")
    proc.process_one()
    result = client.wait(cmd_id)
    assert result["allowed"] is False and result["rule"] == "whitelist"


def test_client_generates_cid_and_retry_reuses_it():
    """命令里的 client_order_id 由命令行生成；同一条命令重试（同一 cid）不会产生第二张单（M3 幂等）。"""
    sys, proc, client = setup()
    proc.heartbeat()
    payload = client.place_payload("US.SPY", "BUY", 1, Decimal("10.00"))
    for _ in range(2):
        cmd = client.enqueue("place", payload, actor="cli")
        proc.process_one()
        client.wait(cmd)
    assert len(sys.broker.calls("place_order")) == 1


def test_cancel_and_halt_and_resume():
    sys, proc, client = setup()
    proc.heartbeat()
    c = client.enqueue("place", client.place_payload("US.SPY", "BUY", 1, Decimal("10.00")), actor="cli")
    proc.process_one()
    cid = client.wait(c)["client_order_id"]
    sys.broker.step()
    sys.pump.drain()
    c = client.enqueue("cancel", {"client_order_id": cid}, actor="cli")
    proc.process_one()
    assert client.wait(c)["allowed"] is True
    c = client.enqueue("halt", {"reason": "测试"}, actor="cli")
    proc.process_one()
    assert client.wait(c)["halted"] is True and sys.gate.halted
    c = client.enqueue("resume", resume_payload(RESUME_KEY, sys.clock.now()), actor="cli")
    proc.process_one()
    assert client.wait(c)["halted"] is False and not sys.gate.halted


def test_commands_processed_in_order():
    sys, proc, client = setup()
    proc.heartbeat()
    ids = [client.enqueue("place", client.place_payload("US.SPY", "BUY", 1, Decimal("10.00")), actor="cli") for _ in range(3)]
    while proc.process_one():
        pass
    cids = [client.wait(i)["client_order_id"] for i in ids]
    created = [sys.oms.get(c).created_at for c in cids]
    assert created == sorted(created)


def test_unknown_kind_is_error_status():
    sys, proc, client = setup()
    proc.heartbeat()
    c = client.enqueue("modify", {}, actor="cli")
    proc.process_one()
    with pytest.raises(RuntimeError, match="不支持"):
        client.wait(c)


def test_processing_exception_is_recorded_and_alerted(monkeypatch):
    sys, proc, client = setup()
    proc.heartbeat()

    def boom(*a, **k):
        raise ValueError("测试异常")

    monkeypatch.setattr(sys.gate, "submit", boom)
    c = client.enqueue("place", client.place_payload("US.SPY", "BUY", 1, Decimal("10.00")), actor="cli")
    proc.process_one()
    with pytest.raises(RuntimeError, match="测试异常"):
        client.wait(c)
    assert "command_error" in sys.alert_kinds()


def test_wait_times_out():
    sys, proc, client = setup()
    proc.heartbeat()
    c = client.enqueue("halt", {"reason": "x"}, actor="cli")
    with pytest.raises(CommandTimeout):
        client.wait(c)


def test_bad_payload_rejected_before_gate():
    sys, proc, client = setup()
    proc.heartbeat()
    c = client.enqueue("place", {"symbol": "US.SPY", "side": "BUY", "qty": 0, "limit_price": "10.00"}, actor="cli")
    proc.process_one()
    result = client.wait(c)
    assert result["allowed"] is False and result["rule"] == "bad_intent"
    assert sys.broker.calls("place_order") == []
