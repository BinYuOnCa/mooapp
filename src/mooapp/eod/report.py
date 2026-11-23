"""EodReport：CSV（reports/YYYY-MM-DD.csv）、终端文本、eod_reports 行（PROGRESS「M7 计划」决定 3、4、6）。

金额只在这里按 money() 格式化（ROUND_HALF_UP 到 0.01）；均价保留 4 位小数。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from mooapp.eod.pnl import ZERO, PnlRow, money
from mooapp.eod.recon import ReconBreak
from mooapp.oms.store import EodRecord

COLUMNS = ("symbol", "qty", "avg_cost", "close", "close_source", "buy_qty", "sell_qty", "realized", "unrealized", "fees", "broker_today_pl")


@dataclass
class EodReport:
    trade_date: date
    generated_at: datetime
    env: str
    rows: dict[str, PnlRow]
    breaks: list[ReconBreak]
    info: dict[str, Any] = field(default_factory=dict)  # unknown_orders、external_qty
    close_sources: dict[str, str] = field(default_factory=dict)
    broker_today_pl: dict[str, Decimal | None] = field(default_factory=dict)  # 只作参考（含程序外持仓），不算 BREAK
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.breaks


def _avg(x: Decimal | None) -> str:
    return "" if x is None else str(x.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def row_dict(report: EodReport, r: PnlRow) -> dict[str, str]:
    pl = report.broker_today_pl.get(r.symbol)
    return {
        "symbol": r.symbol,
        "qty": str(r.qty),
        "avg_cost": _avg(r.avg_cost),
        "close": "" if r.close is None else str(r.close),
        "close_source": report.close_sources.get(r.symbol, ""),
        "buy_qty": str(r.buy_qty),
        "sell_qty": str(r.sell_qty),
        "realized": str(money(r.realized)),
        "unrealized": "" if r.unrealized is None else str(money(r.unrealized)),
        "fees": str(money(r.fees)),
        "broker_today_pl": "" if pl is None else str(pl),
    }


def totals(report: EodReport) -> dict[str, str]:
    rows = report.rows.values()
    return {
        "realized": str(money(sum((r.realized for r in rows), ZERO))),
        "unrealized": str(money(sum((r.unrealized or ZERO for r in rows), ZERO))),
        "fees": str(money(sum((r.fees for r in rows), ZERO))),
    }


def break_dict(b: ReconBreak) -> dict[str, Any]:
    return {"kind": b.kind, "symbol": b.symbol, "client_order_id": b.client_order_id, "details": b.details}


def write_csv(report: EodReport, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{report.trade_date.isoformat()}.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for r in report.rows.values():
            w.writerow(row_dict(report, r))
    if report.breaks:
        with (out_dir / f"{report.trade_date.isoformat()}.breaks.csv").open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=("kind", "symbol", "client_order_id", "details"))
            w.writeheader()
            for b in report.breaks:
                w.writerow({**break_dict(b), "details": b.details})
    return path


def to_record(report: EodReport) -> EodRecord:
    summary = {
        **totals(report),
        "close_sources": report.close_sources,
        "notes": report.notes,
        "unknown_orders": report.info.get("unknown_orders", []),
        "external_qty": report.info.get("external_qty", {}),
    }
    return EodRecord(
        trade_date=report.trade_date,
        generated_at=report.generated_at,
        env=report.env,
        ok=report.ok,
        summary=summary,
        breaks=[{**break_dict(b), "details": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in b.details.items()}} for b in report.breaks],
        rows=[row_dict(report, r) for r in report.rows.values()],
    )


def render_text(report: EodReport) -> str:
    head = f"EOD {report.trade_date.isoformat()}（{report.env}）："
    lines = [head + ("无差异" if report.ok else f"[BREAK] {len(report.breaks)} 条差异")]
    t = totals(report)
    lines.append(f"合计：已实现 {t['realized']}  未实现 {t['unrealized']}  费用 {t['fees']}")
    if report.rows:
        lines.append("  ".join(f"{c:>12}" for c in COLUMNS[:4] + COLUMNS[5:10]))
        for r in report.rows.values():
            d = row_dict(report, r)
            lines.append("  ".join(f"{d[c]:>12}" for c in COLUMNS[:4] + COLUMNS[5:10]))
    for b in report.breaks:
        lines.append(f"[BREAK] {b.kind} {b.symbol or ''} {b.client_order_id or ''} {b.details}")
    if report.info.get("external_qty"):
        lines.append(f"程序外持仓（只报告，ADR-0019）：{report.info['external_qty']}")
    if report.info.get("unknown_orders"):
        lines.append(f"券商有、本地没有的 moo: 订单（只报告）：{len(report.info['unknown_orders'])} 张")
    for s, src in report.close_sources.items():
        lines.append(f"收盘价 {s}：{src}")
    lines.extend(f"注：{n}" for n in report.notes)
    return "\n".join(lines)
