"""审计记录的构造（手册 M4：每次拒单写审计日志；M4 计划：每次闸门判定、halt、resume 都写）。

写入失败（库不可用）时不阻断撤单与 halt（ADR-0025），由闸门报警；这里只负责把判定转成 AuditRecord。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mooapp.oms.store import AuditRecord


def audit_for(
    *,
    ts: datetime,
    action: str,
    source: str,
    actor: str,
    allowed: bool,
    rule: str | None,
    reason: str,
    client_order_id: str | None = None,
    symbol: str | None = None,
    side: str | None = None,
    qty: int | None = None,
    price: Any = None,
    details: dict[str, Any] | None = None,
) -> AuditRecord:
    return AuditRecord(
        ts=ts,
        action=action,
        source=source,
        actor=actor or "-",
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        allowed=allowed,
        rule=rule,
        reason=reason,
        details=details or {},
    )
