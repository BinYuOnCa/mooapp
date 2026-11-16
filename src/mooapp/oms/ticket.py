"""GateTicket：Executor 的写方法只认闸门签发的 ticket（SPEC 5.4 防绕过；M3 计划第 5 节）。

- 只有 _issue_ticket 能造出合法 ticket（构造时检查模块私有的封印）；静态测试保证只有 risk/gate.py 调用它
- ticket 绑定动作与 client_order_id，只能用一次（TicketBook 记账）
放在 oms 包里是为了不让 oms 反向依赖 risk（SPEC 3.3 依赖方向）。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

from mooapp.common import MooappError

_SEAL = object()


class TicketError(MooappError):
    """ticket 伪造、错配或重复使用。"""


@dataclass(frozen=True, eq=False)
class GateTicket:
    action: str  # "place" / "cancel"
    client_order_id: str
    seal: object = field(repr=False)
    ticket_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if self.seal is not _SEAL:
            raise TicketError("GateTicket 只能由闸门签发")


def _issue_ticket(action: str, client_order_id: str) -> GateTicket:
    """只供 risk/gate.py 调用（tests/unit/test_write_path.py 静态检查）。"""
    return GateTicket(action, client_order_id, _SEAL)


class TicketBook:
    """校验并消耗 ticket。校验失败不消耗。"""

    def __init__(self) -> None:
        self._used: set[str] = set()
        self._lock = threading.Lock()

    def consume(self, ticket: object, action: str, client_order_id: str) -> None:
        if not isinstance(ticket, GateTicket) or ticket.seal is not _SEAL:
            raise TicketError("不是闸门签发的 GateTicket")
        if ticket.action != action:
            raise TicketError(f"ticket 动作是 {ticket.action}，不能用于 {action}")
        if ticket.client_order_id != client_order_id:
            raise TicketError("ticket 的 client_order_id 与请求不符")
        with self._lock:
            if ticket.ticket_id in self._used:
                raise TicketError("ticket 已用过")
            self._used.add(ticket.ticket_id)
