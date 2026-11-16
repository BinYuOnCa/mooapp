"""事件 → 邮件（M5，纯逻辑；PROGRESS「M5 计划」决定 4）。时间由调用方传入，便于测试。

- 成交：同一订单在 merge_partial_fills_seconds 窗口内合并为一封；订单到终态立即发出
- RiskRejected、HaltChanged、CRITICAL_ALERTS：立即发
- 其余报警：从第一条起每 alert_digest_minutes 汇总一封；没有新报警不发
- email_* 报警（通知器自己发出的）不进邮件，避免发信失败的报警又触发发信
- 账号字段只显示后 4 位（SPEC 5.7，mask_account）
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from mooapp.common import Env, mask_account
from mooapp.config.models import EmailCfg
from mooapp.notify.mailer import Email
from mooapp.oms.events import Alert
from mooapp.oms.state import TERMINAL, Order
from mooapp.risk.gate import HaltChanged, RiskRejected

CRITICAL_ALERTS = frozenset({
    "daily_loss_halt", "halt_cancel_failed", "resume_rejected", "halt_not_persisted", "halt_degraded",
    "lock_failed", "runner_error", "command_orphaned",
})  # fmt: skip
ACCOUNT_KEYS = frozenset({"acc_id", "card_num", "uni_card_num"})
DIGEST_EXAMPLES = 3


def _details(d: dict[str, Any]) -> str:
    return "\n".join(f"  {k}: {mask_account(v) if k in ACCOUNT_KEYS else v}" for k, v in d.items())


@dataclass
class _Window:
    first: datetime
    count: int
    order: Order


class Composer:
    def __init__(self, cfg: EmailCfg, env: Env):
        self._cfg = cfg
        self._env = env
        self._seen: dict[str, int] = {}  # cid → 已计入的累计成交量
        self._windows: dict[str, _Window] = {}
        self._digest: Counter[str] = Counter()
        self._examples: dict[str, list[str]] = {}
        self._digest_start: datetime | None = None

    def _subject(self, text: str) -> str:
        return f"[{self._env.value}] {text}"

    # ------------------------------------------------------------------ 成交

    def on_order(self, order: Order, now: datetime) -> list[Email]:
        cid = order.client_order_id
        if order.cum_qty > self._seen.get(cid, 0):
            self._seen[cid] = order.cum_qty
            w = self._windows.get(cid)
            if w is None:
                self._windows[cid] = _Window(now, 1, order)
            else:
                w.count += 1
                w.order = order
        elif cid in self._windows:
            self._windows[cid].order = order
        if order.status in TERMINAL and cid in self._windows:
            return [self._flush_fill(cid)]
        return []

    def _flush_fill(self, cid: str) -> Email:
        w = self._windows.pop(cid)
        o = w.order
        return Email(
            "fill",
            self._subject(f"成交 {o.symbol} {o.side.value} {o.cum_qty}/{o.qty}"),
            f"订单 {cid}（券商 {o.broker_order_id or '-'}）\n"
            f"本次合并 {w.count} 次成交，累计 {o.cum_qty}/{o.qty}，均价 {o.avg_px}\n"
            f"状态 {o.status.value}（窗口自 {w.first.isoformat()}）",
        )

    # ------------------------------------------------------------------ 立即发

    def on_risk_rejected(self, e: RiskRejected, now: datetime) -> list[Email]:
        return [Email("risk_rejected", self._subject(f"拒单 {e.rule} {e.symbol or ''}".rstrip()),
                      f"{e.reason}\n动作 {e.action}，来源 {e.source}，发起 {e.actor}，订单 {e.client_order_id or '-'}\n时间 {now.isoformat()}")]  # fmt: skip

    def on_halt(self, e: HaltChanged, now: datetime) -> list[Email]:
        what = "HALT" if e.halted else "RESUME"
        return [Email("halt", self._subject(f"{what}：{e.reason}"), f"{what} by {e.actor}\n原因 {e.reason}\n时间 {now.isoformat()}")]

    def on_alert(self, a: Alert, now: datetime) -> list[Email]:
        if a.kind.startswith("email_"):
            return []
        if a.kind in CRITICAL_ALERTS:
            body = f"{a.message}\n订单 {a.client_order_id or '-'}\n时间 {now.isoformat()}"
            if a.details:
                body += "\n" + _details(a.details)
            return [Email("alert", self._subject(f"报警 {a.kind}"), body)]
        if self._digest_start is None:
            self._digest_start = now
        self._digest[a.kind] += 1
        ex = self._examples.setdefault(a.kind, [])
        if len(ex) < DIGEST_EXAMPLES:
            ex.append(a.message)
        return []

    # ------------------------------------------------------------------ 定时

    def tick(self, now: datetime) -> list[Email]:
        out: list[Email] = []
        window = timedelta(seconds=self._cfg.merge_partial_fills_seconds)
        for cid in [c for c, w in self._windows.items() if now >= w.first + window]:
            out.append(self._flush_fill(cid))
        if self._digest_start is not None and now >= self._digest_start + timedelta(minutes=self._cfg.alert_digest_minutes):
            out.append(self._flush_digest(now))
        return out

    def flush_all(self, now: datetime) -> list[Email]:
        """停止时调用：把未到时间的合并窗口与汇总全部发出（交给 outbox，下次启动补发）。"""
        out = [self._flush_fill(cid) for cid in list(self._windows)]
        if self._digest_start is not None:
            out.append(self._flush_digest(now))
        return out

    def _flush_digest(self, now: datetime) -> Email:
        total = sum(self._digest.values())
        lines = [f"{self._digest_start.isoformat() if self._digest_start else ''} ～ {now.isoformat()} 共 {total} 条报警："]
        for kind, n in self._digest.most_common():
            lines.append(f"{kind} × {n}")
            lines.extend(f"    例：{m}" for m in self._examples.get(kind, []))
        self._digest.clear()
        self._examples.clear()
        self._digest_start = None
        return Email("digest", self._subject(f"报警汇总 {total} 条"), "\n".join(lines))
