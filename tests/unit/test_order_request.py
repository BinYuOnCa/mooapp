"""OrderRequest 构造时校验、remark 编解码、最小变动单位（M2 计划第 4、6 点；ADR-0004、0009、0019）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from mooapp.broker.port import (
    REMARK_PREFIX,
    InvalidOrder,
    OrderRequest,
    encode_remark,
    is_on_tick,
    new_client_order_id,
    parse_remark,
    tick_size,
)
from mooapp.common import MooappError, OrderKind, Side

CID = "0123456789abcdef0123456789abcdef"


def req(**kw) -> OrderRequest:
    base = dict(
        client_order_id=CID,
        symbol="US.SPY",
        side=Side.BUY,
        qty=1,
        kind=OrderKind.LIMIT,
        limit_price=Decimal("683.61"),
    )
    base.update(kw)
    return OrderRequest(**base)


# ------------------------------------------------------------------ 最小变动单位（vendor § Place Orders › price；FAQ Q14）


@pytest.mark.parametrize(
    "price,tick",
    [
        (Decimal("0.5"), Decimal("0.0001")),
        (Decimal("0.9999"), Decimal("0.0001")),
        (Decimal("1"), Decimal("0.01")),
        (Decimal("759.58"), Decimal("0.01")),
    ],
)
def test_tick_size(price, tick):
    assert tick_size(price) == tick


@pytest.mark.parametrize(
    "price,ok",
    [
        (Decimal("759.58"), True),
        (Decimal("759.585"), False),
        (Decimal("1.00"), True),
        (Decimal("0.1234"), True),
        (Decimal("0.12345"), False),
    ],
)
def test_is_on_tick(price, ok):
    assert is_on_tick(price) is ok


# ------------------------------------------------------------------ OrderRequest


def test_valid_limit_and_market():
    assert req().limit_price == Decimal("683.61")
    m = req(kind=OrderKind.MARKET, limit_price=None)
    assert m.limit_price is None


def test_invalid_order_is_mooapp_error():
    assert issubclass(InvalidOrder, MooappError)
    assert issubclass(InvalidOrder, ValueError)


@pytest.mark.parametrize(
    "kw",
    [
        dict(kind=OrderKind.LIMIT, limit_price=None),  # 限价单必须有价格
        dict(kind=OrderKind.MARKET, limit_price=Decimal("1.00")),  # 市价单不能有价格
        dict(limit_price=Decimal("759.585")),  # 不在最小变动单位上，不自动取整（ADR-0009）
        dict(limit_price=Decimal("0")),
        dict(limit_price=Decimal("-1.00")),
        dict(qty=0),
        dict(qty=-1),
        dict(symbol="HK.00700"),  # 只做美股（ADR-0001）
        dict(symbol="SPY"),
        dict(client_order_id="not-hex"),
        dict(client_order_id=CID[:-1]),
    ],
    ids=lambda kw: ",".join(f"{k}={v}" for k, v in kw.items()),
)
def test_invalid_requests(kw):
    with pytest.raises(InvalidOrder):
        req(**kw)


def test_qty_must_be_int_not_float_or_bool():
    with pytest.raises(InvalidOrder):
        req(qty=1.0)
    with pytest.raises(InvalidOrder):
        req(qty=True)


def test_limit_price_must_be_decimal():
    with pytest.raises(InvalidOrder):
        req(limit_price=683.61)


def test_remark_property_fits_64_bytes():
    r = req()
    assert r.remark == REMARK_PREFIX + CID
    assert len(r.remark.encode("utf-8")) <= 64  # vendor § Place Orders › remark


# ------------------------------------------------------------------ remark（ADR-0019：只有 moo: 开头的是本程序订单）


def test_new_client_order_id_format():
    a, b = new_client_order_id(), new_client_order_id()
    assert a != b
    assert len(a) == 32 and all(c in "0123456789abcdef" for c in a)


def test_remark_roundtrip():
    assert REMARK_PREFIX == "moo:"
    assert parse_remark(encode_remark(CID)) == CID


@pytest.mark.parametrize(
    "remark",
    [
        "",
        None,
        "probe:m2:buy:143132",  # 探针单是外部订单
        "moo:",
        "moo:xyz",
        "moo:" + CID + "x",
        "MOO:" + CID,
        "手动下单",
    ],
)
def test_external_remarks_parse_to_none(remark):
    assert parse_remark(remark) is None


def test_encode_rejects_bad_id():
    with pytest.raises(InvalidOrder):
        encode_remark("abc")
