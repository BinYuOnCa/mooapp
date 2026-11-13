"""Moomoo 订单状态 → 状态机事件（M3 计划第 1 节；docs/status-mapping.md）。

映射不了的状态必须标记 unmapped（由 OMS 报警），不得静默忽略。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from mooapp.broker.port import BrokerOrder
from mooapp.common import OrderKind, Side
from mooapp.oms.mapping import NO_EVENT_STATUSES, UNMAPPED_STATUSES, VENDOR_STATUSES, map_snapshot
from mooapp.oms.state import Event, EventType, Order, OrdStatus, apply

S, E = OrdStatus, EventType
REPO = Path(__file__).resolve().parents[2]
VENDOR = REPO / "docs" / "vendor" / "moomoo-API-Doc-en-Python.md"
DOC = REPO / "docs" / "status-mapping.md"
CID = "b" * 32
BID = "900001"
T = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)


def order(*events: Event, qty: int = 100) -> Order:
    o = Order(client_order_id=CID, symbol="US.SPY", side=Side.BUY, kind=OrderKind.LIMIT, qty=qty, price=Decimal("10"))
    o.broker_order_id = BID
    for e in (Event(E.SUBMIT), *events):
        apply(o, e)
    return o


def snap(raw_status: str, dealt: int = 0, avg: str = "0", qty: int = 100, err: str = "") -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=BID,
        client_order_id=CID,
        symbol="US.SPY",
        side=Side.BUY,
        kind=OrderKind.LIMIT,
        qty=qty,
        price=Decimal("10"),
        raw_status=raw_status,
        dealt_qty=dealt,
        dealt_avg_price=Decimal(avg),
        last_err_msg=err,
        created_at=T,
        updated_at=T,
        remark="moo:" + CID,
    )


def types(result) -> list[EventType]:
    return [e.type for e in result.events]


ACK = Event(E.ACK)


def test_vendor_statuses_parsed_from_doc():
    text = VENDOR.read_text(encoding="utf-8")
    section = text[text.index("## Order Status") : text.index("## Order Type")]
    found = set(re.findall(r"^\* `([A-Z_]+)`", section, flags=re.M))
    assert found == set(VENDOR_STATUSES)
    assert len(found) == 11  # NONE 加 10 个（vendor § Order Status）


@pytest.mark.parametrize("raw", sorted(NO_EVENT_STATUSES))
def test_waiting_and_submitting_produce_no_event(raw):
    r = map_snapshot(order(), snap(raw))
    assert r.events == [] and not r.unmapped


def test_submitted_is_ack():
    assert types(map_snapshot(order(), snap("SUBMITTED"))) == [E.ACK]


def test_filled_part_synthesizes_fill_with_deterministic_id():
    r = map_snapshot(order(ACK), snap("FILLED_PART", dealt=40, avg="10"))
    assert types(r) == [E.FILL]
    f = r.events[0]
    assert (f.qty, f.price, f.exec_id) == (40, Decimal("10"), f"{BID}:40")


def test_fill_delta_against_local_cum_and_price_backed_out():
    o = order(ACK, Event(E.FILL, exec_id=f"{BID}:40", qty=40, price=Decimal("10")))
    r = map_snapshot(o, snap("FILLED_ALL", dealt=100, avg="10.6"))
    assert types(r) == [E.FILL]
    assert r.events[0].qty == 60 and r.events[0].price == Decimal("11")
    assert r.events[0].exec_id == f"{BID}:100"


def test_stale_snapshot_produces_no_fill():
    o = order(ACK, Event(E.FILL, exec_id="f", qty=60, price=Decimal("10")))
    r = map_snapshot(o, snap("FILLED_PART", dealt=40, avg="10"))
    assert r.events == []


def test_cancelled_after_local_cancel_request_is_cancel_ack():
    o = order(ACK, Event(E.CANCEL_REQ))
    assert types(map_snapshot(o, snap("CANCELLED_ALL"))) == [E.CANCEL_ACK]


def test_cancelled_without_request_is_unsolicited():
    assert types(map_snapshot(order(ACK), snap("CANCELLED_ALL"))) == [E.UNSOLICITED_CANCEL]


def test_cancelled_part_fills_first_then_cancel():
    r = map_snapshot(order(ACK), snap("CANCELLED_PART", dealt=40, avg="10"))
    assert types(r) == [E.FILL, E.UNSOLICITED_CANCEL]


def test_failed_is_exch_reject_with_reason():
    r = map_snapshot(order(), snap("FAILED", err="insufficient funds"))
    assert types(r) == [E.EXCH_REJECT]
    assert r.events[0].reason == "insufficient funds"


@pytest.mark.parametrize("raw", ["NONE", "DISABLED", "DELETED", "SOMETHING_NEW"])
def test_unmapped_statuses_flagged(raw):
    r = map_snapshot(order(ACK), snap(raw))
    assert r.unmapped is True
    assert r.events == []


def test_unmapped_status_still_books_fill():
    r = map_snapshot(order(ACK), snap("DISABLED", dealt=10, avg="10"))
    assert r.unmapped is True
    assert types(r) == [E.FILL]


def test_unmapped_set_matches_plan():
    assert UNMAPPED_STATUSES == frozenset({"NONE", "DISABLED", "DELETED"})
    assert NO_EVENT_STATUSES == frozenset({"WAITING_SUBMIT", "SUBMITTING"})


def test_every_vendor_status_classified():
    for raw in VENDOR_STATUSES:
        r = map_snapshot(order(ACK), snap(raw, dealt=0))
        assert r.unmapped == (raw in UNMAPPED_STATUSES), raw


# ------------------------------------------------------------------ REAL：成交只来自成交记录


def test_real_snapshot_never_synthesizes_fill_and_requests_deal_poll():
    r = map_snapshot(order(ACK), snap("FILLED_PART", dealt=40, avg="10"), fills_from_deals=True)
    assert r.events == [] and r.deal_poll_needed


def test_real_terminal_waits_for_fills():
    r = map_snapshot(order(ACK), snap("FILLED_ALL", dealt=100, avg="10"), fills_from_deals=True)
    assert r.events == [] and r.deal_poll_needed


def test_real_cancel_part_after_fills_caught_up():
    o = order(ACK, Event(E.FILL, exec_id="D1", qty=40, price=Decimal("10")))
    r = map_snapshot(o, snap("CANCELLED_PART", dealt=40, avg="10"), fills_from_deals=True)
    assert types(r) == [E.UNSOLICITED_CANCEL] and not r.deal_poll_needed


def test_real_cancel_part_before_fills_waits():
    r = map_snapshot(order(ACK), snap("CANCELLED_PART", dealt=40, avg="10"), fills_from_deals=True)
    assert r.events == [] and r.deal_poll_needed


def test_real_submitted_still_ack():
    assert types(map_snapshot(order(), snap("SUBMITTED"), fills_from_deals=True)) == [E.ACK]


# ------------------------------------------------------------------ 文档


def doc_rows() -> list[list[str]]:
    rows = []
    in_table = False
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if line.startswith("| # | Moomoo"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            if line.startswith("|---"):
                continue
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


def test_doc_has_row_with_source_for_every_vendor_status():
    rows = doc_rows()
    by_status = {}
    for r in rows:
        m = re.match(r"`([A-Z_]+)`", r[1])
        if m:
            by_status[m.group(1)] = r
    assert set(by_status) == set(VENDOR_STATUSES)
    for st, r in by_status.items():
        assert r[-1] and r[-1] != "—", f"{st} 缺少出处"
        assert "vendor" in r[-1], f"{st} 的出处应引用 vendor 章节"


def test_doc_unmapped_rows_match_code():
    rows = doc_rows()
    unmapped = set()
    for r in rows:
        m = re.match(r"`([A-Z_]+)`", r[1])
        if m and "映射不了" in r[3]:
            unmapped.add(m.group(1))
    assert unmapped == set(UNMAPPED_STATUSES)
