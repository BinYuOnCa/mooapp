"""M8 验收 2：REAL 环境下，Agent 提议的订单在你批准之前不会发出。FakeBroker（REAL），不连 OpenD、不连数据库：
    uv run pytest tests/acceptance/test_m8_real_needs_approval.py -s"""
import asyncio
from typer.testing import CliRunner
from oms_harness import make_system
from mooapp.agent.approvals import ApprovalQueue
from mooapp.agent.server import build_server
from mooapp.agent.tools import AgentTools
from mooapp.common import Env
from mooapp.ops import cli
from mooapp.ops.commands import CommandClient, CommandProcessor


def test_real_agent_proposals_never_sent_without_approval():
    sys = make_system(env=Env.REAL)
    tools = AgentTools(store=sys.store, broker=sys.broker, md=sys.broker, queue=ApprovalQueue(sys.store, sys.clock),
                       clock=sys.clock, env=Env.REAL, caller="agent:acceptance")
    server = build_server(tools)
    for _ in range(3):
        asyncio.run(server.call_tool("propose_order", {"symbol": "US.SPY", "side": "BUY", "qty": 1, "limit_price": "10.00", "reason": "验收"}))
    proc = CommandProcessor(gate=sys.gate, store=sys.store, clock=sys.clock, bus=sys.bus, resume_key=bytes.fromhex("77" * 32))
    proc.heartbeat()
    while proc.process_one():
        pass
    assert proc.auto_approve_pending(env=Env.REAL, enabled=True) == 0          # REAL 永不自动批准
    assert sys.broker.calls("place_order") == []                                  # 没批准：券商收不到任何下单
    assert [a.status for a in sys.store.list_approvals()] == ["pending"] * 3
    r = CliRunner().invoke(cli.app, ["--env", "real", "approve", sys.store.list_approvals()[0].id], input="y\n")
    assert r.exit_code == cli.EXIT_HUMAN_LOCK                                     # 非交互终端：人锁拒绝
    assert sys.broker.calls("place_order") == []
    print("\nREAL：3 个提议都待批准，券商 0 次下单；非交互终端的 approve 被人锁拒绝")