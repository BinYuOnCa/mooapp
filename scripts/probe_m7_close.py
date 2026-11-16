"""M7 探针 P13（SPEC 7.1 R1–R9）：收盘前后账户数据的样子。**只读：不下单、不撤单、不改任何东西。**
只在 SIMULATE 跑；建议美东 15:55、16:05、16:25 各运行一次（每次运行拍一张快照），由你用 `!` 运行：

    ! uv run python scripts/probe_m7_close.py

要回答的问题（M7 收盘 P&L 与对账的设计依据；vendor 文档没写明）：
- P13a：收盘后 today_pl_val、pl_val、持仓在 16:00 前后、收盘后 30 分钟内是否变化（M7「收盘 P&L」取哪个时点）
- P13b：收盘后 order_list_query（当日列表，缓存 / 刷新）与 history_order_list_query 里今天的订单是否都还查得到、
  成交量与均价是否完整（M7 三方对账的数据来源；模拟盘没有成交记录接口，P4）
- P13c：资金 accinfo_query 收盘前后是否变化
- P8：request_trading_days 怎么标记美股半日市（窗口含 2026-11-27 感恩节次日、2026-12-24 平安夜；vendor § Get Trading Calendar）
- 同时记录快照（last_price、after_price、update_time）与市场状态，便于对照 P11

副作用（R6）：无。只调用查询接口；刷新查询每次运行合计 3 次（持仓、订单、资金各 1 次），远低于限频。
注意限频按账户共用：不要和 pytest -m sim 同时运行。

记录（R4、R5）：recordings/probes/probe_m7_close_<UTC>.jsonl；第一行是 SDK、OpenD 版本和本文件的 git 提交号；
之后每次 SDK 调用一行。账号字段只保留后 4 位，其余原样。
"""

from __future__ import annotations

import importlib.metadata
import json
import math
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from mooapp.config.loader import load_broker_cfg

REPO = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).stem
SYMBOL = "US.SPY"
MARKET_TZ = ZoneInfo("America/New_York")
CAL_START = "2026-11-20"
CAL_END = "2026-12-31"  # 含 2026-11-27（感恩节次日）、2026-12-24（平安夜）
CONNECT_TIMEOUT = 5.0
ACCOUNT_KEYS = {"acc_id", "card_num", "uni_card_num"}


def utc_now() -> datetime:
    return datetime.now(UTC)


def mask(value: object) -> str:
    """与 mooapp.common.mask_account 一致：只保留后 4 位（R5）。探针按 R2 不 import mooapp.common。"""
    return "****" + str(value)[-4:]


def scrub(obj: object) -> object:
    """只脱敏账号字段；其余值原样（R5）。"""
    if isinstance(obj, dict):
        return {k: (mask(v) if k in ACCOUNT_KEYS and v not in (None, "", "N/A") else scrub(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub(v) for v in obj]
    return obj


def raw(data: object) -> object:
    """DataFrame 按行转成 dict，不做取舍（R4）；其他原样。"""
    if hasattr(data, "to_dict"):
        return data.to_dict("records")
    return data


def json_default(o: object) -> object:
    if isinstance(o, float) and math.isnan(o):
        return "NaN"
    return str(o)


def git_commit() -> str:
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True)
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", f"scripts/{PROBE}.py"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return head.stdout.strip() + ("（探针文件有未提交改动）" if dirty.stdout.strip() else "")


class Recorder:
    """先缓存，拿到 OpenD 版本后写出首行元数据，之后逐行写入并 flush（R4）。推送回调在 SDK 线程里调用 log，所以加锁。"""

    def __init__(self) -> None:
        self.started = utc_now()
        self.path = REPO / "recordings" / "probes" / f"{PROBE}_{self.started:%Y%m%dT%H%M%SZ}.jsonl"
        self.pending: list[dict] = []
        self.fh = None
        self.lock = threading.Lock()

    def log(self, call: str, params: dict, ret: object, data: object) -> None:
        rec = scrub({"ts": utc_now().isoformat(), "call": call, "params": params, "ret": ret, "data": data})
        line = json.dumps(rec, ensure_ascii=False, default=json_default)
        with self.lock:
            print(line, flush=True)
            if self.fh is None:
                self.pending.append(rec)
            else:
                self._write(rec)

    def open(self, opend_ver: str | None) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = self.path.open("w", encoding="utf-8")
            self._write(
                {
                    "meta": True,
                    "probe": f"scripts/{PROBE}.py",
                    "started": self.started.isoformat(),
                    "sdk": importlib.metadata.version("moomoo-api"),
                    "opend": opend_ver,
                    "git": git_commit(),
                    "python": sys.version.split()[0],
                }
            )
            for rec in self.pending:
                self._write(rec)
            self.pending.clear()
        print(f"# 记录文件：{self.path}", flush=True)

    def _write(self, rec: dict) -> None:
        assert self.fh is not None
        self.fh.write(json.dumps(rec, ensure_ascii=False, default=json_default) + "\n")
        self.fh.flush()

    def close(self) -> None:
        if self.fh is None:
            self.open(None)
        with self.lock:
            assert self.fh is not None
            self.fh.close()


class Probe:
    def __init__(self) -> None:
        self.rec = Recorder()
        self.broker = load_broker_cfg(REPO / "config" / "broker.toml")
        self.acc_id = self.broker.simulate_acc_id
        self.failures: list[str] = []
        self.ft = None
        self.quote_ctx = None
        self.trd_ctx = None
        self.accs = None

    def log(self, name: str, params: dict, ret: object, data: object) -> bool:
        """原样记录；ret != RET_OK 记为失败但不抛，让 finally 能撤单（R7）。"""
        self.rec.log(name, params, ret, raw(data))
        ok = ret == self.ft.RET_OK
        if not ok:
            self.failures.append(f"{name}: {data}")
        return ok

    def assert_simulate_account(self) -> None:
        # R1：acc_id 必须属于 trd_env == SIMULATE 且有 US 权限的账户
        ok = any(
            int(r["acc_id"]) == self.acc_id
            and str(r["trd_env"]) == "SIMULATE"
            and "US" in [str(m) for m in r["trdmarket_auth"]]
            for r in self.accs.to_dict("records")
        )
        if not ok:
            raise SystemExit(f"simulate_acc_id {mask(self.acc_id)} 不在 SIMULATE/US 账户中，退出（R1）")

    def connect(self) -> bool:
        broker = self.broker
        try:
            import moomoo as ft
        except PermissionError as e:
            self.rec.log("import moomoo", {}, "PermissionError", {"errno": e.errno, "strerror": e.strerror, "filename": e.filename})
            self.failures.append("无法加载 SDK（在 sandbox 内属预期，见 P6）")
            return False
        self.ft = ft
        ft.SysConfig.enable_console_log(False)  # vendor § Enable console logging of OpenD connection status
        ft.SysConfig.set_all_thread_daemon(True)  # vendor § Basic Functions › Set Thread Mode
        self.quote_ctx = ft.OpenQuoteContext(host=broker.host, port=broker.port, is_async_connect=True)
        deadline = time.monotonic() + CONNECT_TIMEOUT
        while self.quote_ctx.status != ft.ContextStatus.READY and time.monotonic() < deadline:
            time.sleep(0.1)
        status = str(self.quote_ctx.status)
        self.rec.log("OpenQuoteContext", {"host": broker.host, "port": broker.port, "is_async_connect": True}, status, None)
        if self.quote_ctx.status != ft.ContextStatus.READY:
            self.failures.append(f"OpenD {CONNECT_TIMEOUT:g} 秒内未就绪（{status}）")
            return False
        ret, gs = self.quote_ctx.get_global_state()  # vendor § Get global market status
        self.log("get_global_state", {}, ret, gs)
        self.rec.open(gs.get("server_ver") if ret == ft.RET_OK else None)
        self.trd_ctx = ft.OpenSecTradeContext(filter_trdmarket=ft.TrdMarket.US, host=broker.host, port=broker.port)
        rec = self.rec

        class OrderPush(ft.TradeOrderHandlerBase):
            """vendor § Orders Push Callback：原样记录每条订单推送（R4）。"""

            def on_recv_rsp(self, rsp_pb):
                ret, content = super().on_recv_rsp(rsp_pb)
                rec.log("push.order", {}, ret, raw(content))
                return ret, content

        self.trd_ctx.set_handler(OrderPush())
        ret, self.accs = self.trd_ctx.get_acc_list()  # vendor § Get the List of Trading Accounts
        if not self.log("get_acc_list", {}, ret, self.accs):
            return False
        self.assert_simulate_account()
        return True

    def query(self, name: str, params: dict, fn, **kw):
        """调用一个查询接口，原样记录（R4）；返回 records 列表（失败返回 None，并记为失败但不抛，R7）。"""
        ret, data = fn(**kw)
        ok = self.log(name, params, ret, data)
        return data.to_dict("records") if ok and hasattr(data, "to_dict") else (data if ok else None)

    def run(self) -> int:
        if not self.connect():
            return 1
        from moomoo import TrdEnv  # 写成字面量 TrdEnv.SIMULATE，供静态检查（R1）
        ft = self.ft
        now = utc_now()
        et = now.astimezone(MARKET_TZ)
        acc = {"trd_env": "SIMULATE", "acc_id": self.acc_id}

        ms = self.query("get_market_state", {"code_list": [SYMBOL]}, self.quote_ctx.get_market_state, code_list=[SYMBOL])
        snap = self.query("get_market_snapshot", {"code_list": [SYMBOL]}, self.quote_ctx.get_market_snapshot, code_list=[SYMBOL])

        pos = {}
        for refresh in (False, True):
            # vendor § Get Positions：不过滤 code，看全部行（含 qty=0）
            pos[refresh] = self.query(f"position_list_query.{'refresh' if refresh else 'cache'}", {**acc, "refresh_cache": refresh},
                                      self.trd_ctx.position_list_query, trd_env=TrdEnv.SIMULATE, acc_id=self.acc_id, refresh_cache=refresh)  # fmt: skip
        orders = {}
        for refresh in (False, True):
            # vendor § Get open Orders：未完成 + 24 小时内的订单
            orders[refresh] = self.query(f"order_list_query.{'refresh' if refresh else 'cache'}", {**acc, "refresh_cache": refresh},
                                         self.trd_ctx.order_list_query, trd_env=TrdEnv.SIMULATE, acc_id=self.acc_id, refresh_cache=refresh)  # fmt: skip
        day_start = et.strftime("%Y-%m-%d 00:00:00")
        day_end = (et + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
        # vendor § Get Historical Orders：start / end 按美东传（与 create_time 一致；时区文档未写明）
        hist = self.query("history_order_list_query", {**acc, "start": day_start, "end": day_end},
                          self.trd_ctx.history_order_list_query, start=day_start, end=day_end, trd_env=TrdEnv.SIMULATE, acc_id=self.acc_id)  # fmt: skip
        cash = {}
        for refresh in (False, True):
            # vendor § Get Account Funds：currency 默认 HKD，显式传 USD
            cash[refresh] = self.query(f"accinfo_query.{'refresh' if refresh else 'cache'}", {**acc, "refresh_cache": refresh, "currency": "USD"},
                                       self.trd_ctx.accinfo_query, trd_env=TrdEnv.SIMULATE, acc_id=self.acc_id, refresh_cache=refresh,
                                       currency=ft.Currency.USD)  # fmt: skip
        # vendor § Get Trading Calendar（P8）
        cal = self.query("request_trading_days", {"market": "US", "start": CAL_START, "end": CAL_END},
                         self.quote_ctx.request_trading_days, market=ft.TradeDateMarket.US, start=CAL_START, end=CAL_END)  # fmt: skip

        def pnl(rows):
            if rows is None:
                return None
            try:
                return sum(float(r.get("today_pl_val")) for r in rows)
            except (TypeError, ValueError) as e:
                return f"无法求和：{type(e).__name__}：{e}"

        def statuses(rows):
            if rows is None:
                return None
            out: dict[str, int] = {}
            for r in rows:
                if str(r.get("create_time", "")).startswith(et.strftime("%Y-%m-%d")):
                    out[str(r.get("order_status"))] = out.get(str(r.get("order_status")), 0) + 1
            return out

        def first(rows, *keys):
            return None if not rows else {k: rows[0].get(k) for k in keys}

        summary = {
            "et_time": et.strftime("%Y-%m-%d %H:%M:%S"),
            "market_state": None if not ms else ms[0].get("market_state"),
            "snapshot": first(snap, "last_price", "open_price", "prev_close_price", "after_price", "update_time"),
            "sum_today_pl_val": {"cache": pnl(pos[False]), "refresh": pnl(pos[True])},
            "positions_rows": {"cache": None if pos[False] is None else len(pos[False]), "refresh": None if pos[True] is None else len(pos[True])},
            "today_orders_by_status": {"cache": statuses(orders[False]), "refresh": statuses(orders[True]), "history": statuses(hist)},
            "cash": {"cache": first(cash[False], "cash", "total_assets", "power"), "refresh": first(cash[True], "cash", "total_assets", "power")},
            "half_days": None if cal is None else [r for r in cal if str(r.get("trade_date_type")) not in ("WHOLE", "N/A")],
        }
        self.rec.log("p13.summary", {}, "info", summary)
        return 0

    def cleanup(self) -> None:
        """只读探针：没有订单要撤（R6），只关闭连接和记录文件。"""
        for ctx in (self.trd_ctx, self.quote_ctx):
            if ctx is not None:
                ctx.close()
        self.rec.close()


def main() -> int:
    probe = Probe()
    try:
        rc = probe.run()
    finally:
        probe.cleanup()
    if probe.failures:
        print("# 失败：" + "；".join(probe.failures), file=sys.stderr)
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
