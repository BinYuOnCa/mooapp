"""approve 命令签名（ADR-0034）。做法同 ADR-0029（resume）：HMAC-SHA256，60 秒内有效，nonce 只能用一次（调用方检查）。

消息为 `approve|<approval_id>|<ts>|<nonce>`，与 resume 的 `resume|<ts>|<nonce>` 前缀不同，两种签名不能互换。
密钥与 resume 共用 secrets 的 `[resume] key`；MCP server 不读 secrets，所以生成不了有效签名。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime
from typing import Any

MAX_AGE_SECONDS = 60.0


class ApproveAuthError(Exception):
    """approve 签名不合法。"""


def _sig(key: bytes, approval_id: str, ts: str, nonce: str) -> str:
    return hmac.new(key, f"approve|{approval_id}|{ts}|{nonce}".encode(), hashlib.sha256).hexdigest()


def approve_payload(key: bytes, approval_id: str, ts: datetime, nonce: str | None = None) -> dict[str, str]:
    n = nonce or secrets.token_hex(16)
    t = ts.isoformat()
    return {"approval_id": approval_id, "ts": t, "nonce": n, "sig": _sig(key, approval_id, t, n)}


def verify(key: bytes | None, payload: dict[str, Any], now: datetime) -> tuple[str, str]:
    """验签通过返回 (approval_id, nonce)；否则抛 ApproveAuthError。"""
    if key is None:
        raise ApproveAuthError("常驻进程没有配置 [resume] key，拒绝 approve（ADR-0034）")
    aid, ts, nonce, sig = (payload.get(k) for k in ("approval_id", "ts", "nonce", "sig"))
    if not (isinstance(aid, str) and isinstance(ts, str) and isinstance(nonce, str) and isinstance(sig, str)):
        raise ApproveAuthError("approve 命令没有签名（只能用交互终端里的 mooapp approve）")
    if not hmac.compare_digest(_sig(key, aid, ts, nonce), sig):
        raise ApproveAuthError("approve 签名不对")
    try:
        when = datetime.fromisoformat(ts)
    except ValueError as e:
        raise ApproveAuthError(f"approve 时间戳不合法：{e}") from e
    age = (now - when).total_seconds()
    if not -5.0 <= age <= MAX_AGE_SECONDS:
        raise ApproveAuthError(f"approve 签名已过期或时间不对（{age:.0f} 秒前）")
    return aid, nonce
