"""最小事件总线（SPEC 5.7 的 EventBus；M5 再接邮件）。

同步回调：publish 在调用者线程里依次调用订阅者。订阅者抛出的异常不吞，原样抛给发布者。
Alert 另外总是写一条 logging（WARNING），保证没有订阅者时告警也不会消失。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("mooapp.alert")


@dataclass(frozen=True)
class Alert:
    """运维告警（矩阵 Emits 的 ALERT.*）。kind 例：unmapped_status、unexpected_report、external_order、
    unknown_client_order_id、ack_timeout、resubmit、resolve_failed、missing_at_broker、position_mismatch、pump_error。"""

    kind: str
    message: str
    client_order_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DealPollRequested:
    """REAL：快照的累计成交量领先于本地，请 Poller 立即轮询成交记录（docs/status-mapping.md「REAL 的成交」）。"""

    client_order_id: str


class EventBus:
    def __init__(self) -> None:
        self._subs: list[tuple[type, Callable[[Any], None]]] = []

    def subscribe(self, kind: type, cb: Callable[[Any], None]) -> None:
        self._subs.append((kind, cb))

    def publish(self, e: object) -> None:
        if isinstance(e, Alert):
            log.warning("ALERT %s %s %s", e.kind, e.client_order_id or "-", e.message)
        for kind, cb in list(self._subs):
            if isinstance(e, kind):
                cb(e)


@dataclass(frozen=True)
class CancelDue:
    """ADR-0026：带「待撤」标记的订单已得到确认，请 Executor 发出撤单。处理方必须只排队、不阻塞（发布时 OMS 持有锁）。"""

    client_order_id: str


@dataclass(frozen=True)
class OmsReady:
    """M6b 审查修复：OMS 启动恢复完成、闸门开始接受新单。规则引擎据此执行恢复期间排队的动作。"""
