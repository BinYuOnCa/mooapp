"""resume 命令签名（ADR-0029）。

命令表是普通数据库表，能连库的人都能插一行 resume。为了让「解除 halt」仍然只能经过交互终端里的
`mooapp resume`，命令行用 secrets 里的 `[resume] key` 对 `resume|<ts>|<nonce>` 做 HMAC-SHA256，
常驻进程用同一把密钥验签：60 秒内有效，nonce 不得重复。密钥只在内存里，不写日志、不进命令表。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime
from typing import Any

MAX_AGE_SECONDS = 60.0


class ResumeAuthError(Exception):
    """resume 签名不合法。"""


def _sig(key: bytes, ts: str, nonce: str) -> str:
    return hmac.new(key, f"resume|{ts}|{nonce}".encode(), hashlib.sha256).hexdigest()


def resume_payload(key: bytes, ts: datetime, nonce: str | None = None) -> dict[str, str]:
    n = nonce or secrets.token_hex(16)
    t = ts.isoformat()
    return {"ts": t, "nonce": n, "sig": _sig(key, t, n)}


def verify(key: bytes | None, payload: dict[str, Any], now: datetime) -> str:
    """验签通过返回 nonce（调用方负责检查并记录 nonce 是否用过）；否则抛 ResumeAuthError。"""
    if key is None:
        raise ResumeAuthError("常驻进程没有配置 [resume] key，拒绝 resume（ADR-0029）")
    ts, nonce, sig = payload.get("ts"), payload.get("nonce"), payload.get("sig")
    if not (isinstance(ts, str) and isinstance(nonce, str) and isinstance(sig, str)):
        raise ResumeAuthError("resume 命令没有签名（只能用交互终端里的 mooapp resume）")
    if not hmac.compare_digest(_sig(key, ts, nonce), sig):
        raise ResumeAuthError("resume 签名不对")
    try:
        when = datetime.fromisoformat(ts)
    except ValueError as e:
        raise ResumeAuthError(f"resume 时间戳不合法：{e}") from e
    age = (now - when).total_seconds()
    if not -5.0 <= age <= MAX_AGE_SECONDS:
        raise ResumeAuthError(f"resume 签名已过期或时间不对（{age:.0f} 秒前）")
    return nonce
