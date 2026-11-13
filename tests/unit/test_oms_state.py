"""状态机移植（M3 计划第 2 节）：T01–T55、I01–I03 逐条覆盖；矩阵文档与代码一致；不变量与异常。"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from mooapp.common import OrderKind, Side
from mooapp.oms.state import (
    DYNAMIC_EVENTS,
    IGNORED,
    LATE_FILL_STATES,
    MAX_ATTEMPTS,
    TRANSITIONS,
    BadRequest,
    Event,
    EventType,
    InvalidTransition,
    Order,
    OrdStatus,
    Overfill,
    apply,
)

S, E = OrdStatus, EventType
REPO = Path(__file__).resolve().parents[2]
MATRIX = REPO / "docs" / "order-transitions.md"
CID = "a" * 32
PX = Decimal("10")


def mk(qty: int = 100) -> Order:
    return Order(client_order_id=CID, symbol="US.SPY", side=Side.BUY, kind=OrderKind.LIMIT, qty=qty, price=PX)


_seq = [0]


def fill(q: int, px: Decimal = PX) -> Event:
    _seq[0] += 1
    return Event(E.FILL, exec_id=f"x{_seq[0]}", qty=q, price=px)


def ev(t: EventType, **kw) -> Event:
    return Event(t, **kw)


def run(*events: Event, qty: int = 100) -> Order:
    o = mk(qty)
    for e in events:
        assert apply(o, e) is True, f"{e.type} 在 {o.status} 应生效"
    return o


# 到达各状态的路径
PN = (ev(E.SUBMIT),)
UNK = PN + (ev(E.ACK_TIMEOUT),)
NEW = PN + (ev(E.ACK),)
WS = (ev(E.QUEUE_OFF_HOURS),)
PT = (ev(E.PARK_FOR_TRIGGER),)


def part() -> tuple[Event, ...]:
    return NEW + (fill(40),)


def pc_new() -> tuple[Event, ...]:
    return NEW + (ev(E.CANCEL_REQ),)


def pc_part() -> tuple[Event, ...]:
    return part() + (ev(E.CANCEL_REQ),)


def pr_new(new_qty: int = 150) -> tuple[Event, ...]:
    return NEW + (ev(E.REPLACE_REQ, qty=new_qty, cl_ord_id="r1"),)


def pr_part(new_qty: int = 150) -> tuple[Event, ...]:
    return part() + (ev(E.REPLACE_REQ, qty=new_qty, cl_ord_id="r1"),)


CASES = [
    # id, 路径, 事件, 目标状态, 额外检查
    ("T01", (), ev(E.RISK_REJECT), S.REJECTED, None),
    ("T02", (), ev(E.SUBMIT), S.PENDING_NEW, lambda o: o.attempts == 1),
    ("T03", (), ev(E.QUEUE_OFF_HOURS), S.WAITING_SUBMIT, None),
    ("T04", (), ev(E.PARK_FOR_TRIGGER), S.PENDING_TRIGGER, None),
    ("T05", WS, ev(E.SESSION_OPEN), S.PENDING_NEW, lambda o: o.attempts == 1),
    ("T06", WS, ev(E.RISK_REJECT), S.REJECTED, None),
    ("T07", WS, ev(E.CANCEL_REQ), S.CANCELED, None),
    ("T08", WS, ev(E.EXPIRE), S.EXPIRED, None),
    ("T09", PT, ev(E.TRIGGER_HIT), S.PENDING_NEW, None),
    ("T10", PT, ev(E.RISK_REJECT), S.REJECTED, None),
    ("T11", PT, ev(E.CANCEL_REQ), S.CANCELED, None),
    ("T12", PT, ev(E.EXPIRE), S.EXPIRED, None),
    ("T13", PN, ev(E.ACK), S.NEW, None),
    ("T14", PN, ev(E.EXCH_REJECT, reason="no"), S.REJECTED, lambda o: o.reason == "no"),
    ("T15", PN, ev(E.ACK_TIMEOUT), S.UNKNOWN, None),
    ("T16", PN, "fill40", S.PARTIALLY_FILLED, lambda o: o.cum_qty == 40),
    ("T17", PN, "fill100", S.FILLED, lambda o: o.cum_qty == 100),
    ("T18", UNK, ev(E.ACK), S.NEW, None),
    ("T19", UNK, ev(E.EXCH_REJECT), S.REJECTED, None),
    ("T20", UNK, "fill40", S.PARTIALLY_FILLED, None),
    ("T21", UNK, "fill100", S.FILLED, None),
    ("T22", UNK, ev(E.STATUS_RESOLVED, resolved_status=S.NEW), S.NEW, None),
    ("T23", UNK, ev(E.STATUS_RESOLVED, resolved_status=S.REJECTED), S.REJECTED, None),
    ("T24", UNK, ev(E.STATUS_RESOLVED, resolved_status=S.CANCELED), S.CANCELED, None),
    ("T25", NEW, "fill40", S.PARTIALLY_FILLED, None),
    ("T26", NEW, "fill100", S.FILLED, None),
    ("T27", NEW, ev(E.CANCEL_REQ), S.PENDING_CANCEL, lambda o: o.base_status is S.NEW),
    ("T28", NEW, ev(E.REPLACE_REQ, qty=150, cl_ord_id="r1"), S.PENDING_REPLACE, lambda o: o.pending_qty == 150),
    ("T29", NEW, ev(E.EXPIRE), S.EXPIRED, None),
    ("T30", NEW, ev(E.UNSOLICITED_CANCEL), S.CANCELED, None),
    ("T31", "part", "fill20", S.PARTIALLY_FILLED, lambda o: o.cum_qty == 60),
    ("T32", "part", "fill60", S.FILLED, None),
    ("T33", "part", ev(E.CANCEL_REQ), S.PENDING_CANCEL, lambda o: o.base_status is S.PARTIALLY_FILLED),
    ("T34", "part", ev(E.REPLACE_REQ, qty=150, cl_ord_id="r1"), S.PENDING_REPLACE, None),
    ("T35", "part", ev(E.EXPIRE), S.EXPIRED, None),
    ("T36", "part", ev(E.UNSOLICITED_CANCEL), S.CANCELED, None),
    ("T37", "pc_new", ev(E.CANCEL_ACK), S.CANCELED, lambda o: o.base_status is None),
    ("T38", "pc_new", ev(E.CANCEL_REJECT), S.NEW, None),
    ("T39", "pc_part", ev(E.CANCEL_REJECT), S.PARTIALLY_FILLED, None),
    ("T40", "pc_new", "fill40", S.PENDING_CANCEL, lambda o: o.base_status is S.PARTIALLY_FILLED),
    ("T41", "pc_new", "fill100", S.FILLED, lambda o: o.base_status is None),
    ("T42", "pc_new", ev(E.EXPIRE), S.EXPIRED, None),
    ("T43", "pc_new", ev(E.UNSOLICITED_CANCEL), S.CANCELED, None),
    ("T44", "pr_new", ev(E.REPLACE_ACK), S.NEW, lambda o: o.qty == 150 and o.cl_ord_id == "r1"),
    ("T45", "pr_part", ev(E.REPLACE_ACK), S.PARTIALLY_FILLED, lambda o: o.qty == 150),
    ("T46", "pr_part60_fill20", ev(E.REPLACE_ACK), S.FILLED, lambda o: o.qty == 60 and o.cum_qty == 60),
    ("T47", "pr_new", ev(E.REPLACE_REJECT), S.NEW, lambda o: o.qty == 100),
    ("T48", "pr_part", ev(E.REPLACE_REJECT), S.PARTIALLY_FILLED, None),
    ("T49", "pr_new", "fill40", S.PENDING_REPLACE, lambda o: o.base_status is S.PARTIALLY_FILLED),
    ("T50", "pr_new", "fill100", S.FILLED, lambda o: o.pending_qty is None),
    ("T51", "pr_new", ev(E.EXPIRE), S.EXPIRED, None),
    ("T52", "pr_new", ev(E.UNSOLICITED_CANCEL), S.CANCELED, None),
    ("T53", "canceled", "fill10", S.CANCELED, lambda o: o.cum_qty == 10 and o.anomalies),
    ("T54", "expired", "fill10", S.EXPIRED, lambda o: o.cum_qty == 10 and o.anomalies),
    ("T55", UNK, ev(E.RESUBMIT), S.PENDING_NEW, lambda o: o.attempts == 2 and o.cl_ord_id == CID),
]


def build(path) -> Order:
    named = {
        "part": part,
        "pc_new": pc_new,
        "pc_part": pc_part,
        "pr_new": pr_new,
        "pr_part": pr_part,
        "pr_part60_fill20": lambda: pr_part(60) + (fill(20),),
        "canceled": lambda: NEW + (ev(E.UNSOLICITED_CANCEL),),
        "expired": lambda: NEW + (ev(E.EXPIRE),),
    }
    return run(*(named[path]() if isinstance(path, str) else path))


def to_event(e) -> Event:
    if isinstance(e, str):
        return fill(int(e.removeprefix("fill")))
    return e


@pytest.mark.parametrize("tid,path,event,target,check", CASES, ids=[c[0] for c in CASES])
def test_transition(tid, path, event, target, check):
    o = build(path)
    assert apply(o, to_event(event)) is True
    assert o.status is target, tid
    if check is not None:
        assert check(o), f"{tid}: {o}"


def test_every_transition_id_covered():
    ids = {c[0] for c in CASES}
    assert ids == {f"T{i:02d}" for i in range(1, 56)}


@pytest.mark.parametrize(
    "path,event",
    [
        ("filled", ev(E.ACK)),  # I01
        ("canceled", ev(E.ACK)),  # I01
        ("filled", ev(E.CANCEL_REJECT)),  # I02
        ("rejected", ev(E.CANCEL_REJECT)),  # I02
        ("expired", ev(E.REPLACE_REJECT)),  # I03
    ],
)
def test_ignored_rules(path, event):
    paths = {
        "filled": NEW + (fill(100),),
        "canceled": NEW + (ev(E.UNSOLICITED_CANCEL),),
        "rejected": PN + (ev(E.EXCH_REJECT),),
        "expired": NEW + (ev(E.EXPIRE),),
    }
    o = run(*paths[path])
    before = (o.status, o.cum_qty, len(o.history))
    assert apply(o, event) is False
    assert (o.status, o.cum_qty, len(o.history)) == before


def test_invalid_transition_leaves_order_unchanged():
    o = run(*NEW)
    snapshot = (o.status, o.cum_qty, list(o.history))
    with pytest.raises(InvalidTransition):
        apply(o, ev(E.SUBMIT))
    assert (o.status, o.cum_qty, list(o.history)) == snapshot


def test_duplicate_exec_id_ignored():
    o = run(*NEW)
    f = Event(E.FILL, exec_id="dup", qty=40, price=PX)
    assert apply(o, f) is True
    assert apply(o, f) is False
    assert o.cum_qty == 40


def test_overfill_raises_and_order_unchanged():
    o = run(*part())
    with pytest.raises(Overfill):
        apply(o, fill(61))
    assert o.cum_qty == 40 and o.status is S.PARTIALLY_FILLED


def test_fill_requires_exec_id():
    o = run(*NEW)
    with pytest.raises(BadRequest):
        apply(o, Event(E.FILL, qty=1, price=PX))


def test_avg_price_weighted():
    o = run(*NEW, fill(40, Decimal("10")), fill(60, Decimal("11")))
    assert o.avg_px == Decimal("10.6")


def test_resubmit_limited_to_max_attempts():
    o = run(*UNK)
    for _ in range(MAX_ATTEMPTS - 1):
        assert apply(o, ev(E.RESUBMIT)) is True
        assert apply(o, ev(E.ACK_TIMEOUT)) is True
    assert o.attempts == MAX_ATTEMPTS
    with pytest.raises(BadRequest):
        apply(o, ev(E.RESUBMIT))
    assert o.status is S.UNKNOWN and o.attempts == MAX_ATTEMPTS


def test_resubmit_only_from_unknown():
    o = run(*NEW)
    with pytest.raises(InvalidTransition):
        apply(o, ev(E.RESUBMIT))


def test_resolved_status_must_match_fills():
    o = run(*UNK, fill(40))  # UNKNOWN + FILL → PARTIALLY_FILLED，已不是 UNKNOWN
    assert o.status is S.PARTIALLY_FILLED
    o2 = run(*UNK)
    with pytest.raises(BadRequest):
        apply(o2, ev(E.STATUS_RESOLVED, resolved_status=S.FILLED))


# ------------------------------------------------------------------ 矩阵文档与代码一致


def matrix_rows() -> list[list[str]]:
    rows = []
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if re.match(r"^\| (T\d\d|I\d\d) \|", line):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


def test_matrix_doc_matches_code():
    rows = matrix_rows()
    t_rows = [r for r in rows if r[0].startswith("T")]
    i_rows = [r for r in rows if r[0].startswith("I")]
    assert len(t_rows) == 55 and len(i_rows) == 3
    doc_pairs = set()
    problems = []
    for rid, frm, event, _guard, to, _emits in t_rows:
        s, e = S(frm), E(event)
        doc_pairs.add((s, e))
        target = re.match(r"[A-Z_]+", to).group(0)
        if (s, e) in TRANSITIONS:
            if TRANSITIONS[(s, e)] is not S(target):
                problems.append(f"{rid}: 文档 {target}，代码 {TRANSITIONS[(s, e)]}")
        elif e in DYNAMIC_EVENTS and s in DYNAMIC_EVENTS[e]:
            continue
        elif e is E.FILL and s in LATE_FILL_STATES:
            continue
        else:
            problems.append(f"{rid}: ({s}, {e}) 代码中不存在")
    for pair in TRANSITIONS:
        if pair not in doc_pairs:
            problems.append(f"代码有 {pair}，文档没有")
    doc_ignored = set()
    for rid, frm, event, *_ in i_rows:
        for s in frm.split(","):
            doc_ignored.add((S(s.strip()), E(event)))
    if doc_ignored != set(IGNORED):
        problems.append(f"IGNORED 不一致：文档多 {doc_ignored - set(IGNORED)}，代码多 {set(IGNORED) - doc_ignored}")
    assert not problems, problems
