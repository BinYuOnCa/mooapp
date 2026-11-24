"""Agent 工具的实现（M8；SPEC 5.9）。纯逻辑，可以脱离 MCP 单独测试；agent/server.py 负责注册与审计。

- 只读：get_quote、get_positions、get_orders、get_pnl、explain_order、list_my_proposals（查自己的提议状态）
- 提议：propose_order、propose_rule —— 只写 approvals，返回 approval_id；不调用闸门
- 不提供：批准、拒绝、撤单、halt、修改限额、切换环境、读取配置（本包也不 import 这些模块，测试静态检查）
- 外部文本（券商回报、事件原因、审计理由、提议理由）一律放在 "untrusted" 字段里，只当数据
- 时间给美东（ADR-0033），价格最多 4 位小数
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from mooapp.agent.approvals import ApprovalError, ApprovalQueue
from mooapp.broker.port import BrokerPort, MarketDataPort
from mooapp.clock import Clock
from mooapp.common import Env
from mooapp.oms.state import Order
from mooapp.oms.store import ApprovalRecord, Store

MARKET_TZ = ZoneInfo("America/New_York")


def et(ts: datetime | None) -> str | None:
    """ADR-0033：给人（和 Agent）看的时间用美东。"""
    return None if ts is None else ts.astimezone(MARKET_TZ).strftime("%Y-%m-%d %H:%M:%S") + "（美东）"


def px(p: Decimal | None) -> str | None:
    if p is None:
        return None
    q = p.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP).normalize()
    return f"{q:.2f}" if -q.as_tuple().exponent <= 2 else f"{q:f}"


def money(p: Decimal | None) -> str | None:
    return None if p is None else str(p.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class AgentTools:
    def __init__(self, store: Store, broker: BrokerPort, md: MarketDataPort, queue: ApprovalQueue, clock: Clock, env: Env, caller: str):
        self.store = store
        self.broker = broker
        self.md = md
        self.queue = queue
        self.clock = clock
        self.env = env
        self.caller = caller

    # ------------------------------------------------------------------ 只读

    def get_quote(self, symbol: str) -> dict[str, Any]:
        qs = self.md.snapshot([symbol])  # vendor § Get Market Snapshot
        if not qs:
            raise ApprovalError(f"没有 {symbol} 的行情")
        q = qs[0]
        return {"symbol": q.symbol, "last": px(q.last), "bid": px(q.bid), "ask": px(q.ask), "time": et(q.ts)}

    def get_positions(self) -> list[dict[str, Any]]:
        return [
            {"symbol": p.symbol, "qty": p.qty, "can_sell_qty": p.can_sell_qty, "avg_cost": px(p.avg_cost)}
            for p in self.broker.query_positions()  # vendor § Get Positions（OpenD 缓存）
        ]

    def _order(self, o: Order) -> dict[str, Any]:
        return {
            "client_order_id": o.client_order_id, "broker_order_id": o.broker_order_id, "symbol": o.symbol, "side": o.side.value,
            "qty": o.qty, "price": px(o.price), "status": o.status.value, "cum_qty": o.cum_qty, "avg_px": px(o.avg_px),
            "source": o.source.value, "created": et(o.created_at), "untrusted": {"broker_reason": o.reason},
        }  # fmt: skip

    def get_orders(self, open_only: bool = True) -> list[dict[str, Any]]:
        return [self._order(o) for o in self.store.list_orders(open_only=open_only)]

    def get_pnl(self) -> dict[str, Any]:
        return {"today_pl": money(self.broker.query_today_pnl()), "positions": self.get_positions()}  # vendor § Get Positions › today_pl_val

    def explain_order(self, client_order_id: str) -> dict[str, Any]:
        o = self.store.get_order(client_order_id)
        if o is None:
            raise ApprovalError(f"没有订单 {client_order_id}")
        events = [
            {"event_type": e.event_type, "origin": e.origin, "outcome": e.outcome, "from": e.from_status, "to": e.to_status,
             "qty": e.qty, "price": px(e.price), "time": et(e.ts), "untrusted": {"reason": e.reason}}
            for e in self.store.events_for(client_order_id)
        ]  # fmt: skip
        audit = [
            {"time": et(a.ts), "action": a.action, "source": a.source, "allowed": a.allowed, "rule": a.rule, "untrusted": {"reason": a.reason}}
            for a in self.store.audit_records() if a.client_order_id == client_order_id
        ]  # fmt: skip
        return {"order": self._order(o), "events": events, "audit": audit}

    def _proposal(self, a: ApprovalRecord) -> dict[str, Any]:
        return {
            "approval_id": a.id, "kind": a.kind, "status": a.status, "payload": a.payload, "result": a.result,
            "created": et(a.created_at), "expires_at": et(a.expires_at), "untrusted": {"reason": a.reason},
        }  # fmt: skip

    def list_my_proposals(self) -> list[dict[str, Any]]:
        return [self._proposal(a) for a in self.store.list_approvals() if a.caller == self.caller]

    # ------------------------------------------------------------------ 提议（只写 approvals）

    def _proposed(self, a: ApprovalRecord) -> dict[str, Any]:
        return {"approval_id": a.id, "status": a.status, "expires_at": et(a.expires_at),
                "next_step": f"提议已记录，等你在终端运行 mooapp approve {a.id} 批准（或 mooapp reject {a.id}）"}  # fmt: skip

    def propose_order(self, symbol: str, side: str, qty: int, limit_price: str | None = None, reason: str = "") -> dict[str, Any]:
        payload = {"symbol": symbol, "side": side, "qty": qty, "limit_price": limit_price}
        return self._proposed(self.queue.propose("order", payload, reason, self.caller))

    def propose_rule(self, yaml_text: str, reason: str = "") -> dict[str, Any]:
        return self._proposed(self.queue.propose("rule", {"yaml": yaml_text}, reason, self.caller))
