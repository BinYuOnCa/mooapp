"""dry-run：用一串价格回放规则，打印何时触发什么（手册 M6 第 4 点；SPEC 5.6 dry_run）。纯本地，不连 OpenD、不连数据库。

价格文件 CSV，表头固定为 ts,type,a,b：
- open,<开盘价>             当天常规时段开盘价（ADR-0005）
- fill,<数量>,<价格>         入场单成交；累计数量达到规则的 entry.qty 即视为入场单结束
- tick,<最新价>
- bar,<最低价>,<最高价>      一个时段（两边都碰到时按止盈算，ADR-0027 第 2 条）
- reject_next_sell          让下一张卖单被拒（演练 ADR-0027 第 4 条的重试）
- expire                    入场单到期（ADR-0023）

模拟约定：卖单按触发时看到的价格立即成交（K 线按触发价估计）；撤入场单余量立即生效。
滑点 = |触发价 − 成交价|。
"""

from __future__ import annotations

import csv
import io
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from mooapp.common import MooappError
from mooapp.strategy.machine import Action, CancelEntry, PlaceProtectiveSell, RuleMachine, RuleNote, RuleStatus
from mooapp.strategy.spec import RuleSpec

HEADER = ["ts", "type", "a", "b"]
_NEEDS = {"open": (True, False), "fill": (True, True), "tick": (True, False), "bar": (True, True), "reject_next_sell": (False, False), "expire": (False, False)}


class DryRunError(MooappError, ValueError):
    """价格文件不合法。消息里带行号。"""


@dataclass(frozen=True)
class PriceRow:
    line: int
    ts: str
    type: str
    a: Decimal | None
    b: Decimal | None


@dataclass(frozen=True)
class DryRunEvent:
    ts: str
    kind: str
    detail: str


@dataclass(frozen=True)
class SellRecord:
    ts: str
    side: str
    qty: int
    price: Decimal
    slippage: Decimal
    attempt: int


@dataclass
class DryRunResult:
    events: list[DryRunEvent] = field(default_factory=list)
    sells: list[SellRecord] = field(default_factory=list)
    final_status: RuleStatus = RuleStatus.PENDING
    stop_price: Decimal | None = None
    take_price: Decimal | None = None


def _num(text: str, line: int, what: str) -> Decimal:
    try:
        d = Decimal(text)
    except InvalidOperation:
        raise DryRunError(f"第 {line} 行：{what} 不是数字：{text!r}") from None
    if not d.is_finite():
        raise DryRunError(f"第 {line} 行：{what} 不是有限数字：{text!r}")
    return d


def parse_prices(text: str) -> list[PriceRow]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or [c.strip() for c in rows[0]] != HEADER:
        raise DryRunError(f"第 1 行：表头必须是 {','.join(HEADER)}")
    out: list[PriceRow] = []
    for i, raw in enumerate(rows[1:], start=2):
        if not raw or all(not c.strip() for c in raw):
            continue
        cells = [c.strip() for c in raw] + [""] * (4 - len(raw))
        ts, typ, a_txt, b_txt = cells[:4]
        if typ not in _NEEDS:
            raise DryRunError(f"第 {i} 行：未知的 type {typ!r}（只支持 {', '.join(_NEEDS)}）")
        need_a, need_b = _NEEDS[typ]
        if need_a and not a_txt:
            raise DryRunError(f"第 {i} 行：{typ} 需要 a 列")
        if need_b and not b_txt:
            raise DryRunError(f"第 {i} 行：{typ} 需要 b 列")
        a = _num(a_txt, i, "a") if a_txt else None
        b = _num(b_txt, i, "b") if b_txt else None
        if typ == "fill" and (a is None or a != a.to_integral_value() or a <= 0):
            raise DryRunError(f"第 {i} 行：fill 的数量必须是正整数，收到 {a_txt!r}")
        if typ == "bar" and a is not None and b is not None and a > b:
            raise DryRunError(f"第 {i} 行：bar 的最低价 {a} 高于最高价 {b}")
        out.append(PriceRow(i, ts, typ, a, b))
    return out


def dry_run(spec: RuleSpec, rows: list[PriceRow]) -> DryRunResult:
    m = RuleMachine(spec)
    r = DryRunResult()
    filled = 0
    reject_next = False
    last: Decimal | None = None

    def ev(ts: str, kind: str, detail: str) -> None:
        r.events.append(DryRunEvent(ts, kind, detail))

    def handle(ts: str, actions: list[Action]) -> None:
        nonlocal reject_next
        for act in actions:
            if isinstance(act, RuleNote):
                ev(ts, act.kind, act.message)
            elif isinstance(act, CancelEntry):
                ev(ts, "cancel_entry", f"撤入场单余量 {spec.entry.qty - filled} 股（立即生效）")
                handle(ts, m.on_entry_terminal())
            elif isinstance(act, PlaceProtectiveSell):
                if reject_next:
                    reject_next = False
                    ev(ts, "sell_rejected", f"第 {act.attempt} 次卖单被拒（模拟）：卖 {act.qty} 股")
                    handle(ts, m.on_sell_rejected("dry-run 模拟拒单"))
                    continue
                slip = abs(act.trigger_price - act.observed_price)
                r.sells.append(SellRecord(ts, act.side, act.qty, act.observed_price, slip, act.attempt))
                ev(ts, "sell", f"市价卖 {act.qty} 股成交 @{act.observed_price}，滑点 {slip}（第 {act.attempt} 次）")
                handle(ts, m.on_sell_filled(act.qty, act.observed_price))

    for row in rows:
        if row.type == "open":
            assert row.a is not None
            ev(row.ts, "open", f"开盘价 {row.a}")
            handle(row.ts, m.on_open(row.a))
        elif row.type == "fill":
            assert row.a is not None and row.b is not None
            qty = int(row.a)
            if m.entry_done:
                ev(row.ts, "ignored", f"入场单已结束，忽略这笔成交（第 {row.line} 行）")
                continue
            filled += qty
            ev(row.ts, "fill", f"入场成交 {qty} 股 @{row.b}（累计 {filled}/{spec.entry.qty}）")
            handle(row.ts, m.on_entry_fill(qty, row.b, last=last))
            if filled >= spec.entry.qty and not m.entry_done:
                handle(row.ts, m.on_entry_terminal())
        elif row.type == "tick":
            assert row.a is not None
            last = row.a
            handle(row.ts, m.on_tick(row.a))
        elif row.type == "bar":
            assert row.a is not None and row.b is not None
            handle(row.ts, m.on_bar(row.a, row.b))
        elif row.type == "reject_next_sell":
            reject_next = True
            ev(row.ts, "reject_next_sell", "下一张卖单将被拒（模拟）")
        elif row.type == "expire":
            ev(row.ts, "expire", "入场单到期")
            if not m.entry_done:
                handle(row.ts, m.on_entry_terminal())

    r.final_status, r.stop_price, r.take_price = m.status, m.stop_price, m.take_price
    return r


_KIND_CN = {
    "open": "开盘", "fill": "入场成交", "awaiting_open": "等开盘价", "frozen": "价格冻结", "triggered": "触发",
    "bar_both_sides": "两边都碰到", "cancel_entry": "撤入场余量", "sell": "卖出", "sell_rejected": "卖单被拒",
    "reject_next_sell": "模拟拒单", "closed": "结束", "void": "作废", "expire": "入场到期", "error": "错误",
    "unprotected_fill": "无保护成交", "ignored": "忽略",
}  # fmt: skip


def _display_width(s: str) -> int:
    """终端显示宽度：中日韩全角字符占 2 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _display_width(s))


def format_events(r: DryRunResult) -> str:
    lines = [f"止损价 {r.stop_price or '-'}　止盈价 {r.take_price or '-'}　最终状态 {r.final_status.value}", ""]
    ts_w = max((_display_width(e.ts) for e in r.events), default=2)
    kind_w = max((_display_width(_KIND_CN.get(e.kind, e.kind)) for e in r.events), default=4)
    for e in r.events:
        lines.append(f"{_pad(e.ts, ts_w)}  {_pad(_KIND_CN.get(e.kind, e.kind), kind_w)}  {e.detail}")
    return "\n".join(lines)
