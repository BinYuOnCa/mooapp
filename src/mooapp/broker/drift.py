"""API 数据形状漂移检测（ADR-0035 第 2 条）。

字段基线：探针与推送录制里各接口实际返回的字段集合（recordings/probes/*.jsonl、recordings/pushes/*.jsonl，
2026-11-12 ～ 11-16，SDK 10.12.7208、OpenD 1012）。枚举基线：vendor 文档 § Trading Definitions / § Quote 的取值。

只报警，不改变任何行为：新字段 api_new_field、基线字段在本次返回的所有行里都没有 api_field_missing、
关键枚举出现文档里没有的值 api_new_enum。每个进程每项只报一次。必需字段缺失、类型不对仍由 convert.py 直接报错。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_ORDER = frozenset({
    "amount", "aux_price", "code", "combo_legs", "create_time", "currency", "dealt_avg_price", "dealt_qty", "expire_time",
    "fill_outside_rth", "jp_acc_type", "last_err_msg", "order_id", "order_market", "order_status", "order_type", "price", "qty",
    "remark", "session", "stock_name", "strategy_type", "time_in_force", "trail_spread", "trail_type", "trail_value", "trd_side",
    "updated_time",
})  # fmt: skip

FIELDS: dict[str, frozenset[str]] = {
    "order_list_query": _ORDER,  # vendor § Get open Orders
    "history_order_list_query": _ORDER,  # vendor § Get Historical Orders
    "place_order": frozenset({
        "aux_price", "code", "create_time", "currency", "dealt_avg_price", "dealt_qty", "fill_outside_rth", "jp_acc_type",
        "last_err_msg", "order_id", "order_status", "order_type", "price", "qty", "remark", "session", "stock_name", "time_in_force",
        "trail_spread", "trail_type", "trail_value", "trd_side", "updated_time",
    }),  # fmt: skip  vendor § Place Orders
    "push.order": (_ORDER - {"order_market"}) | {"trd_env", "trd_market"},  # vendor § Orders Push Callback
    "position_list_query": frozenset({
        "acc_id", "average_cost", "can_sell_qty", "code", "combo_id", "cost_price", "cost_price_valid", "currency", "diluted_cost",
        "jp_acc_type", "market_val", "nominal_price", "payout_if_win", "pl_ratio", "pl_ratio_avg_cost", "pl_ratio_valid", "pl_val",
        "pl_val_valid", "position_id", "position_market", "position_side", "position_type", "qty", "realized_pl", "stock_name",
        "strategy_type", "today_buy_qty", "today_buy_val", "today_pl_val", "today_sell_qty", "today_sell_val", "today_trd_val",
        "unrealized_pl",
    }),  # fmt: skip  vendor § Get Positions
    "accinfo_query": frozenset({
        "au_avl_withdrawal_cash", "au_cash", "aud_assets", "aud_net_cash_power", "available_funds", "avl_withdrawal_cash",
        "beginning_dtbp", "bond_assets", "ca_avl_withdrawal_cash", "ca_cash", "cad_assets", "cad_net_cash_power", "cash",
        "cn_avl_withdrawal_cash", "cn_cash", "cnh_assets", "cnh_net_cash_power", "crypto_mv", "currency", "dt_call_amount",
        "dt_status", "exposure_level", "exposure_limit", "frozen_cash", "fund_assets", "hk_avl_withdrawal_cash", "hk_cash",
        "hkd_assets", "hkd_net_cash_power", "initial_margin", "interest_charged_amount", "is_pdt", "jp_avl_withdrawal_cash",
        "jp_cash", "jpy_assets", "jpy_net_cash_power", "long_mv", "maintenance_margin", "margin_call_margin", "market_val",
        "max_power_short", "max_withdrawal", "my_avl_withdrawal_cash", "my_cash", "myr_assets", "myr_net_cash_power",
        "net_cash_power", "pdt_seq", "pending_asset", "power", "realized_pl", "remaining_dtbp", "remaining_limit", "risk_level",
        "risk_status", "securities_assets", "sg_avl_withdrawal_cash", "sg_cash", "sgd_assets", "sgd_net_cash_power", "short_mv",
        "total_assets", "unrealized_pl", "us_avl_withdrawal_cash", "us_cash", "usd_assets", "usd_net_cash_power", "used_limit",
    }),  # fmt: skip  vendor § Get Account Funds
    "get_market_snapshot": frozenset({
        "after_amplitude", "after_change_rate", "after_change_val", "after_high_price", "after_low_price", "after_price",
        "after_turnover", "after_volume", "amplitude", "ask_price", "ask_vol", "avg_price", "bid_ask_ratio", "bid_price", "bid_vol",
        "circular_market_val", "close_price_5min", "code", "dividend_lfy", "dividend_lfy_ratio", "dividend_ratio_ttm", "dividend_ttm",
        "earning_per_share", "enable_margin", "enable_short_sell", "equity_valid", "ey_ratio", "future_last_settle_price",
        "future_last_trade_time", "future_main_contract", "future_position", "future_position_change", "future_valid", "high_price",
        "highest52weeks_price", "highest_history_price", "index_equal_count", "index_fall_count", "index_option_type",
        "index_raise_count", "index_valid", "issued_shares", "last_price", "listing_date", "long_margin_initial_ratio", "lot_size",
        "low_price", "lowest52weeks_price", "lowest_history_price", "mortgage_ratio", "name", "net_asset", "net_asset_per_share",
        "net_profit", "open_price", "option_area_type", "option_contract_multiplier", "option_contract_nominal_value",
        "option_contract_size", "option_delta", "option_expiry_date_distance", "option_gamma", "option_implied_volatility",
        "option_net_open_interest", "option_open_interest", "option_owner_lot_multiplier", "option_premium", "option_rho",
        "option_strike_price", "option_theta", "option_type", "option_valid", "option_vega", "outstanding_shares",
        "overnight_amplitude", "overnight_change_rate", "overnight_change_val", "overnight_high_price", "overnight_low_price",
        "overnight_price", "overnight_turnover", "overnight_volume", "pb_ratio", "pe_ratio", "pe_ttm_ratio", "plate_equal_count",
        "plate_fall_count", "plate_raise_count", "plate_valid", "pre_amplitude", "pre_change_rate", "pre_change_val", "pre_high_price",
        "pre_low_price", "pre_price", "pre_turnover", "pre_volume", "prev_close_price", "price_spread", "sec_status",
        "short_available_volume", "short_margin_initial_ratio", "short_sell_rate", "stock_owner", "strike_time", "suspension",
        "total_market_val", "trust_assetClass", "trust_aum", "trust_dividend_yield", "trust_netAssetValue", "trust_outstanding_units",
        "trust_premium", "trust_valid", "turnover", "turnover_rate", "update_time", "volume", "volume_ratio", "wrt_break_even_point",
        "wrt_conversion_price", "wrt_conversion_ratio", "wrt_delta", "wrt_end_trade", "wrt_implied_volatility",
        "wrt_inline_price_status", "wrt_ipop", "wrt_issue_vol", "wrt_issuer_code", "wrt_leverage", "wrt_lower_strike_price",
        "wrt_maturity_date", "wrt_premium", "wrt_price_recovery_ratio", "wrt_recovery_price", "wrt_score", "wrt_street_ratio",
        "wrt_street_vol", "wrt_strike_price", "wrt_type", "wrt_upper_strike_price", "wrt_valid",
    }),  # fmt: skip  vendor § Get Market Snapshot
    "push.quote": frozenset({
        "after_amplitude", "after_change_rate", "after_change_val", "after_high_price", "after_low_price", "after_price",
        "after_turnover", "after_volume", "amplitude", "code", "contract_multiplier", "contract_nominal_value", "contract_size",
        "dark_status", "data_date", "data_time", "delta", "expiry_date_distance", "gamma", "high_price", "implied_volatility",
        "index_option_type", "last_price", "last_settle_price", "listing_date", "low_price", "name", "net_open_interest",
        "open_interest", "open_price", "option_area_type", "overnight_amplitude", "overnight_change_rate", "overnight_change_val",
        "overnight_high_price", "overnight_low_price", "overnight_price", "overnight_turnover", "overnight_volume",
        "owner_lot_multiplier", "position", "position_change", "pre_amplitude", "pre_change_rate", "pre_change_val", "pre_high_price",
        "pre_low_price", "pre_price", "pre_turnover", "pre_volume", "premium", "prev_close_price", "price_spread", "rho", "sec_status",
        "strike_price", "suspension", "theta", "turnover", "turnover_rate", "vega", "volume",
    }),  # fmt: skip  vendor § Real-time Quote Callback
    "get_market_state": frozenset({"code", "market_state", "stock_name"}),  # vendor § Get Market Status of Securities
    "request_trading_days": frozenset({"time", "trade_date_type"}),  # vendor § Get Trading Calendar
}

ENUMS: dict[str, frozenset[str]] = {
    # vendor § Trading Definitions › Order Status（11 个，与 oms/mapping.py VENDOR_STATUSES 一致）
    "order_status": frozenset({"NONE", "WAITING_SUBMIT", "SUBMITTING", "SUBMITTED", "FILLED_PART", "FILLED_ALL", "CANCELLED_PART",
                               "CANCELLED_ALL", "FAILED", "DISABLED", "DELETED"}),  # fmt: skip
    "trd_side": frozenset({"NONE", "BUY", "SELL", "SELL_SHORT", "BUY_BACK"}),  # § Transaction Direction
    "order_type": frozenset({"NONE", "NORMAL", "MARKET", "ABSOLUTE_LIMIT", "AUCTION", "AUCTION_LIMIT", "SPECIAL_LIMIT",
                             "SPECIAL_LIMIT_ALL", "STOP", "STOP_LIMIT", "MARKET_IF_TOUCHED", "LIMIT_IF_TOUCHED", "TRAILING_STOP",
                             "TRAILING_STOP_LIMIT", "TWAP"}),  # fmt: skip  § Order Type
    "market_state": frozenset({
        "NONE", "AUCTION", "WAITING_OPEN", "MORNING", "REST", "AFTERNOON", "CLOSED", "PRE_MARKET_BEGIN", "PRE_MARKET_END",
        "AFTER_HOURS_BEGIN", "AFTER_HOURS_END", "OVERNIGHT", "NIGHT_OPEN", "NIGHT_END", "NIGHT", "TRADE_AT_LAST", "FUTURE_DAY_OPEN",
        "FUTURE_DAY_BREAK", "FUTURE_DAY_CLOSE", "FUTURE_DAY_WAIT_OPEN", "HK_CAS", "FUTURE_NIGHT_WAIT", "FUTURE_AFTERNOON",
        "FUTURE_SWITCH_DATE", "FUTURE_OPEN", "FUTURE_BREAK", "FUTURE_BREAK_OVER", "FUTURE_CLOSE", "STIB_AFTER_HOURS_WAIT",
        "ASHARE_AFTER_HOURS_WAIT", "STIB_AFTER_HOURS_BEGIN", "ASHARE_AFTER_HOURS_BEGIN", "STIB_AFTER_HOURS_END",
        "ASHARE_AFTER_HOURS_END",
    }),  # fmt: skip  § Market State
    "trade_date_type": frozenset({"WHOLE", "MORNING", "AFTERNOON"}),  # § TradeDateType（P8：半日市为 MORNING）
}
_PLACEHOLDERS = frozenset({"", "N/A"})


@dataclass(frozen=True)
class Drift:
    kind: str  # api_new_field / api_field_missing / api_new_enum
    api: str
    name: str  # 字段名或枚举字段名
    value: str | None  # 新枚举值
    message: str


class DriftDetector:
    def __init__(self, report: Callable[[Drift], None] | None = None):
        self._reports: list[Callable[[Drift], None]] = [report] if report is not None else []
        self._seen: set[tuple[str, str, str, str | None]] = set()
        self._lock = threading.Lock()

    def add_report(self, cb: Callable[[Drift], None]) -> None:
        with self._lock:
            self._reports.append(cb)

    def check(self, api: str, rows: Sequence[Mapping[str, Any]]) -> list[Drift]:
        """返回本次新发现的漂移（之前报过的不再返回），并逐个交给 report 回调。未登记的接口和空结果不检查。"""
        base = FIELDS.get(api)
        if base is None or not rows:
            return []
        keys: set[str] = set()
        for r in rows:
            keys.update(r.keys())
        found: list[Drift] = []
        for f in sorted(keys - base):
            found.append(Drift("api_new_field", api, f, None, f"{api} 返回了基线里没有的字段 {f!r}（ADR-0035）"))
        for f in sorted(base - keys):
            found.append(Drift("api_field_missing", api, f, None, f"{api} 的返回里没有基线字段 {f!r}（ADR-0035）"))
        for field, allowed in ENUMS.items():
            if field not in keys:
                continue
            for r in rows:
                v = r.get(field)
                if v is None or (isinstance(v, float) and v != v):
                    continue
                s = str(v)
                if s not in allowed and s not in _PLACEHOLDERS:
                    found.append(Drift("api_new_enum", api, field, s, f"{api} 的 {field} 出现 vendor 文档里没有的值 {s!r}（ADR-0035）"))
        new: list[Drift] = []
        with self._lock:
            for d in found:
                key = (d.kind, d.api, d.name, d.value)
                if key not in self._seen:
                    self._seen.add(key)
                    new.append(d)
            reports = list(self._reports)
        for d in new:
            for cb in reports:
                cb(d)
        return new
