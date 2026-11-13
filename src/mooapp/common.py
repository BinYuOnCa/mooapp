"""共享基础类型（SPEC 5.1）。时钟在 mooapp/clock.py。"""

from __future__ import annotations

from enum import StrEnum


class Env(StrEnum):
    SIMULATE = "SIMULATE"
    REAL = "REAL"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderKind(StrEnum):
    """v1 只有限价和市价（ADR-0004）。"""

    LIMIT = "LIMIT"
    MARKET = "MARKET"


class Source(StrEnum):
    CLI = "cli"
    RULE = "rule"
    AGENT = "agent"
    SYSTEM = "system"


class MooappError(Exception):
    """所有自定义异常的基类。"""


def mask_account(acc_id: int | str) -> str:
    """账号只保留后 4 位；日志、邮件、doctor、录制统一用它（SPEC 5.2、4 节）。"""
    s = str(acc_id)
    return "****" + s[-4:]
