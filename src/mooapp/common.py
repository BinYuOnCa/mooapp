"""共享基础类型（SPEC 5.1）。时钟在 mooapp/clock.py。"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo


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


# ------------------------------------------------------------------ 给人看的格式（ADR-0033：内部 UTC，邮件、终端、报告一律美东）

MARKET_TZ = ZoneInfo("America/New_York")


def et_str(ts: datetime) -> str:
    """UTC（或任何带时区的时间）→ 「YYYY-MM-DD HH:MM:SS（美东）」。"""
    if ts.tzinfo is None:
        raise ValueError(f"需要带时区的时间：{ts!r}")
    return ts.astimezone(MARKET_TZ).strftime("%Y-%m-%d %H:%M:%S") + "（美东）"


def price_str(p: Decimal) -> str:
    """价格最多 4 位小数、至少 2 位，去掉多余的 0：10.000000 → 10.00，745.755 → 745.755。"""
    q = p.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP).normalize()
    return f"{q:.2f}" if -q.as_tuple().exponent <= 2 else f"{q:f}"

