"""审批队列（M8；SPEC 5.9；PROGRESS「M8 计划」决定 3、4）。

提议只写 approvals 表，不调用闸门、不碰券商。批准由常驻进程处理（ops/commands.py，ADR-0034 签名）。
- 同时最多 MAX_PENDING 个待批准（已过期的不算）；当天（美东）最多 MAX_PER_DAY 个提议
- 过期：创建后 TTL（30 分钟），或当天收盘（美东 16:00），先到为准
- 提议内容在这里先做形式校验（方向、数量、价格、标的格式、规则 YAML）；风控规则在批准时由闸门检查
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from mooapp.clock import Clock
from mooapp.common import MooappError
from mooapp.oms.store import ApprovalRecord, Store
from mooapp.strategy.spec import RuleSpecError, load_rule

MARKET_TZ = ZoneInfo("America/New_York")
MAX_PENDING = 5
MAX_PER_DAY = 20
TTL = timedelta(minutes=30)
MARKET_CLOSE = time(16, 0)
REASON_MAX = 500
SYMBOL_RE = re.compile(r"^US\.[A-Z][A-Z.]{0,9}$")


class ApprovalError(MooappError):
    """提议或工具参数不合法、超出限额、找不到对象。消息直接返回给 Agent。"""


def _order_payload(p: dict[str, Any]) -> dict[str, Any]:
    missing = [k for k in ("symbol", "side", "qty") if k not in p]
    if missing:
        raise ApprovalError(f"订单提议缺少字段 {missing}")
    symbol, side, qty, price = p["symbol"], p["side"], p["qty"], p.get("limit_price")
    if not isinstance(symbol, str) or not SYMBOL_RE.match(symbol):
        raise ApprovalError(f"标的格式不对：{symbol!r}（应为 US.XXX）")
    if side not in ("BUY", "SELL"):
        raise ApprovalError(f"方向只能是 BUY 或 SELL，收到 {side!r}")
    if type(qty) is not int or qty <= 0:
        raise ApprovalError(f"数量必须是正整数，收到 {qty!r}")
    out: dict[str, Any] = {"symbol": symbol, "side": side, "qty": qty, "limit_price": None}
    if price is not None:
        try:
            d = Decimal(str(price))
        except InvalidOperation as e:
            raise ApprovalError(f"限价不是数字：{price!r}") from e
        if not d.is_finite() or d <= 0:
            raise ApprovalError(f"限价必须是正数，收到 {price!r}")
        out["limit_price"] = str(d)
    return out


class ApprovalQueue:
    def __init__(self, store: Store, clock: Clock):
        self._store = store
        self._clock = clock

    def propose(self, kind: str, payload: dict[str, Any], reason: str, caller: str) -> ApprovalRecord:
        if not isinstance(reason, str) or len(reason) > REASON_MAX:
            raise ApprovalError(f"理由最多 {REASON_MAX} 个字符")
        if kind == "order":
            body = _order_payload(payload)
        elif kind == "rule":
            text = payload.get("yaml")
            if not isinstance(text, str):
                raise ApprovalError("规则提议需要 yaml 文本")
            try:
                load_rule(text)  # M6 规则解析器：表达式白名单，不用 eval
            except RuleSpecError as e:
                raise ApprovalError(f"规则不合法：{e}") from e
            body = {"yaml": text}
        else:
            raise ApprovalError(f"不支持的提议类型 {kind!r}")
        now = self._clock.now()
        pending = [a for a in self._store.list_approvals(status="pending") if a.expires_at > now]
        if len(pending) >= MAX_PENDING:
            raise ApprovalError(f"已有 {len(pending)} 个待批准的提议（上限 {MAX_PENDING}），请等你批准或拒绝后再提")
        today = now.astimezone(MARKET_TZ).date()
        day_start = datetime.combine(today, time(0), MARKET_TZ)
        if len(self._store.list_approvals(since=day_start)) >= MAX_PER_DAY:
            raise ApprovalError(f"今天已提 {MAX_PER_DAY} 个提议（每天上限），明天再提")
        close = datetime.combine(today, MARKET_CLOSE, MARKET_TZ)
        expires = min(now + TTL, close) if now < close else now + TTL
        rec = ApprovalRecord(uuid.uuid4().hex[:12], kind, body, reason, caller, "pending", now, expires, None, None)
        with self._store.transaction():
            self._store.insert_approval(rec)
        return rec

    def get(self, approval_id: str) -> ApprovalRecord:
        a = self._store.get_approval(approval_id)
        if a is None:
            raise ApprovalError(f"没有提议 {approval_id}")
        return a
