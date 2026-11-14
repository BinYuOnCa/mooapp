"""命令表（SPEC 3.4）：命令行只写命令并同步等待；常驻进程按顺序取出交给闸门，把判定写回同一行。

- CommandClient（命令行进程）：不构造闸门、OMS、券商连接；常驻进程心跳过期时直接报错
- CommandProcessor（常驻进程）：place / cancel / halt / resume 交给 RiskGate；其他 kind、或处理中抛出的异常，
  写回 error 状态和原因、记 ERROR 日志、发 Alert("command_error")，然后继续处理下一条（不吞错，也不让一条坏命令卡住队列）
- 下单命令里的 client_order_id 由命令行生成（place_payload），同一条命令重试时沿用，靠 M3 的幂等保证只有 1 张单
- 本模块不 import oms.executor（SPEC 3.3 规则 2），只调用 RiskGate
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from mooapp.broker.port import InvalidOrder, OrderRequest, new_client_order_id
from mooapp.clock import Clock
from mooapp.common import MooappError, OrderKind, Side, Source
from mooapp.oms.events import Alert, EventBus
from mooapp.oms.store import COMMAND_FINAL, Store
from mooapp.risk.gate import Intent, RiskGate

log = logging.getLogger(__name__)
HEARTBEAT_KEY = "runner_heartbeat"
KINDS = frozenset({"place", "cancel", "halt", "resume"})


class RunnerNotRunning(MooappError):
    """常驻进程 mooapp run 没在运行（心跳过期）。"""


class CommandTimeout(MooappError):
    """等待判定超时。命令仍在队列里，常驻进程恢复后会处理。"""


# ------------------------------------------------------------------ 常驻进程一侧


class CommandProcessor:
    HEARTBEAT_TTL = 10.0  # 秒；常驻进程每秒写一次

    def __init__(self, gate: RiskGate, store: Store, clock: Clock, bus: EventBus):
        self.gate = gate
        self.store = store
        self.clock = clock
        self.bus = bus

    def heartbeat(self) -> None:
        with self.store.transaction():
            self.store.set_kv(HEARTBEAT_KEY, self.clock.now().isoformat())

    def process_one(self) -> bool:
        """处理一条命令；队列为空返回 False。"""
        c = self.store.claim_next_command(self.clock.now())
        if c is None:
            return False
        try:
            result = self._dispatch(c.kind, c.payload, c.actor)
            status = "done"
        except Exception as e:  # 写回 error、记日志、报警，然后继续下一条
            log.exception("处理命令 %s（%s）出错", c.id, c.kind)
            result, status = {"error": f"{type(e).__name__}：{e}"}, "error"
            self.bus.publish(Alert("command_error", f"命令 {c.id}（{c.kind}）出错：{e}", details={"command_id": c.id}))
        with self.store.transaction():
            self.store.finish_command(c.id, status, result, self.clock.now())
        return True

    def _dispatch(self, kind: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError(f"不支持的命令 {kind!r}（只支持 {sorted(KINDS)}）")
        if kind == "place":
            try:
                req = OrderRequest(
                    client_order_id=payload.get("client_order_id") or new_client_order_id(),
                    symbol=payload["symbol"],
                    side=Side(payload["side"]),
                    qty=payload["qty"],
                    kind=OrderKind(payload.get("kind", "LIMIT")),
                    limit_price=Decimal(payload["limit_price"]) if payload.get("limit_price") is not None else None,
                )
            except (InvalidOrder, KeyError, ValueError) as e:
                return {"allowed": False, "rule": "bad_intent", "reason": f"命令参数不合法：{e}", "client_order_id": payload.get("client_order_id")}
            d = self.gate.submit(Intent(Source.CLI, "place", request=req, actor=actor))
            return _decision(d, req.client_order_id)
        if kind == "cancel":
            cid = payload.get("client_order_id")
            d = self.gate.submit(Intent(Source.CLI, "cancel", client_order_id=cid, actor=actor))
            return _decision(d, cid)
        if kind == "halt":
            r = self.gate.halt(str(payload.get("reason") or "手动 halt"), actor=actor)
            return {"halted": True, "cancel_sent": r.cancel_sent, "deferred": r.deferred, "failed": r.failed}
        self.gate.resume(actor=actor)  # resume：交互确认已在命令行进程里完成（ops/humanlock.confirm_interactively）
        return {"halted": False}


def _decision(d: Any, cid: str | None) -> dict[str, Any]:
    return {
        "allowed": d.allowed,
        "rule": d.rule,
        "reason": d.reason,
        "client_order_id": d.order.client_order_id if d.order is not None else cid,
        "status": d.order.status.value if d.order is not None else None,
    }


# ------------------------------------------------------------------ 命令行一侧


class CommandClient:
    def __init__(self, store: Store, clock: Clock, timeout: float = 30.0, poll_interval: float = 0.5):
        self.store = store
        self.clock = clock
        self.timeout = timeout
        self.poll_interval = poll_interval

    @staticmethod
    def place_payload(symbol: str, side: str, qty: int, limit_price: Decimal | None, client_order_id: str | None = None) -> dict[str, Any]:
        return {
            "client_order_id": client_order_id or new_client_order_id(),
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "kind": "LIMIT" if limit_price is not None else "MARKET",
            "limit_price": str(limit_price) if limit_price is not None else None,
        }

    def runner_alive(self) -> bool:
        raw = self.store.get_kv(HEARTBEAT_KEY)
        if raw is None:
            return False
        age = (self.clock.now() - datetime.fromisoformat(raw)).total_seconds()
        return age <= CommandProcessor.HEARTBEAT_TTL

    def enqueue(self, kind: str, payload: dict[str, Any], actor: str) -> int:
        with self.store.transaction():
            return self.store.enqueue_command(kind, payload, actor, self.clock.now())

    def wait(self, command_id: int) -> dict[str, Any]:
        deadline = self.clock.monotonic() + self.timeout
        while True:
            c = self.store.get_command(command_id)
            if c is None:
                raise RuntimeError(f"命令 {command_id} 不存在")
            if c.status in COMMAND_FINAL:
                result = c.result or {}
                if c.status == "error":
                    raise RuntimeError(f"命令 {command_id}（{c.kind}）处理出错：{result.get('error')}")
                return result
            if self.clock.monotonic() >= deadline:
                raise CommandTimeout(f"命令 {command_id}（{c.kind}）{self.timeout:g} 秒内没有结果（状态 {c.status}）")
            self.clock.sleep(self.poll_interval)

    def submit(self, kind: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        if not self.runner_alive():
            raise RunnerNotRunning("常驻进程 mooapp run 没在运行（心跳过期）；写命令不会被处理，已拒绝")
        return self.wait(self.enqueue(kind, payload, actor))
