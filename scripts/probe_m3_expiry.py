"""M3 探针 P9（SPEC 7.1 R1–R9）。只在 SIMULATE 跑；由你用 `!` 运行：

    ! uv run python scripts/probe_m3_expiry.py                 # 美东 15:45–16:00 之间运行，观察到 16:10
    ! uv run python scripts/probe_m3_expiry.py --check <order_id> [<order_id> ...]   # 任何时候；只查询，不下单

要回答的问题（docs/status-mapping.md 第 10、11 行，目前走「映射不了，报警」）：
- P9a：DAY 限价单收盘时还没成交，Moomoo 显示什么状态（DISABLED？CANCELLED_ALL？还是一直是 SUBMITTED）？
  有没有推送？什么时候变？vendor § Trading Definitions › Order Status 只写了 DISABLED = "Deactivated"，
  没说和 DAY 单过期的关系。
- P9b：第二天 order_list_query（vendor § Get open Orders：「24 小时内成交或撤销的订单」）
  和 history_order_list_query（vendor § Get Historical Orders）还能不能查到这张单，状态是什么。
  影响 M3 启动恢复能看到多久以前的订单。
- P9c：DELETED。请你在 App 里删一张远离市价的单（App 是否允许删、和撤单有什么区别，都请记下），
  然后用 --check 查它的状态。vendor 原文："Deleted, only unfilled orders can be deleted
  (The status after you actively delete the order.)"

副作用（R6）：
- 默认模式只下 1 张单：US.SPY（在 [risk.simulate] 白名单内）限价买 1 股，价格 = 最新价 × 0.9（向下取到 0.01），
  不期望成交；remark 以 probe: 开头。
- **故意不撤（违反 R6「结束时撤销本次挂出的全部订单」）**：本探针要观察的就是未成交的 DAY 单收盘后怎样，
  撤掉就测不到了。风险有限：1 股、离市价 10%、DAY 单收盘后不会再成交。
- 兜底：如果在美东 16:00 之前就因为任何原因结束（出错、Ctrl-C），finally 会撤单并轮询到终态（R6、R7）；
  16:00 之后订单仍不是终态，只记录，不撤（这本身就是要记录的结论）。
- 默认模式只在常规时段（get_market_state = AFTERNOON）且美东 15:45 之后下单，否则不下单直接退出。
- --check 模式不下单、不撤单。

记录（R4、R5）：recordings/probes/probe_m3_expiry_<UTC>.jsonl；第一行是 SDK、OpenD 版本和本文件的 git 提交号；
之后每次 SDK 调用、每条订单推送一行。账号字段只保留后 4 位，其余原样。
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from mooapp.config.loader import load_broker_cfg, load_settings

REPO = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).stem
SYMBOL = "US.SPY"
QTY = 1
FAR_FACTOR = Decimal("0.9")
TICK = Decimal("0.01")  # 美股 ≥ $1 的最小变动单位（vendor § Place Orders › price）
CONNECT_TIMEOUT = 5.0
ET = ZoneInfo("America/New_York")
EARLIEST_PLACE = (15, 45)  # 美东；太早挂单要等很久
CLOSE = (16, 0)
OBSERVE_UNTIL = (16, 10)
OBSERVE_POLL = 30.0  # 每 30 秒查一次：order_list_query 限频 30 秒 10 次（vendor § Get open Orders）
CANCEL_POLL = 0.5
CANCEL_TIMEOUT = 30.0
HISTORY_DAYS = 5
ACCOUNT_KEYS = {"acc_id", "card_num", "uni_card_num"}
# vendor § Trading Definitions › Order Status：不会再变化的状态
TERMINAL = {"FILLED_ALL", "CANCELLED_PART", "CANCELLED_ALL", "FAILED", "DISABLED", "DELETED"}
# vendor § Trading Definitions › Market State：AFTERNOON = 美股常规时段
RTH_STATES = {"AFTERNOON"}


def utc_now() -> datetime:
    return datetime.now(UTC)


def et_now() -> datetime:
    return utc_now().astimezone(ET)


def et_at(hm: tuple[int, int]) -> datetime:
    return et_now().replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


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
        ["git", "status", "--porcelain", "--", f"scripts/{PROBE}.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return head.stdout.strip() + ("（探针文件有未提交改动）" if dirty.stdout.strip() else "")


class Recorder:
    """先缓存，拿到 OpenD 版本后写出首行元数据，之后逐行写入并 flush（R4）。推送回调在 SDK 线程里调用 log，所以加锁。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode
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
                    "mode": self.mode,
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
    def __init__(self, mode: str) -> None:
        self.rec = Recorder(mode)
        self.broker = load_broker_cfg(REPO / "config" / "broker.toml")
        self.whitelist = load_settings(REPO / "config" / "settings.toml").risk_simulate.whitelist
        self.acc_id = self.broker.simulate_acc_id
        self.placed: str | None = None
        self.last_status: str | None = None
        self.failures: list[str] = []
        self.ft = None
        self.quote_ctx = None
        self.trd_ctx = None
        self.accs = None

    # ---------------------------------------------------------------- 通用

    def log(self, name: str, params: dict, ret: object, data: object) -> bool:
        """原样记录；ret != RET_OK 记为失败但不抛，让 finally 能兜底撤单（R7）。"""
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

    def query_open(self, oid: str, label: str, refresh: bool = False) -> dict | None:
        # vendor § Get open Orders：trd_env 默认 REAL，必须显式传 SIMULATE；refresh_cache=True 绕过 OpenD 缓存
        from moomoo import TrdEnv  # 写成字面量 TrdEnv.SIMULATE，供静态检查（R1）
        ret, data = self.trd_ctx.order_list_query(
            order_id=oid, trd_env=TrdEnv.SIMULATE, acc_id=self.acc_id, refresh_cache=refresh
        )
        params = {"order_id": oid, "trd_env": "SIMULATE", "acc_id": self.acc_id, "refresh_cache": refresh}
        if not self.log(f"order_list_query.{label}", params, ret, data):
            return None
        rows = data.to_dict("records")
        return rows[0] if rows else None

    def query_history(self, oids: list[str], label: str) -> None:
        # vendor § Get Historical Orders：start/end 格式 YYYY-MM-DD HH:MM:SS；美股时区文档未写明，按美东取宽窗口
        from moomoo import TrdEnv  # 写成字面量 TrdEnv.SIMULATE，供静态检查（R1）
        end = et_now() + timedelta(days=1)
        start = end - timedelta(days=HISTORY_DAYS + 1)
        params = {
            "code": SYMBOL,
            "start": f"{start:%Y-%m-%d %H:%M:%S}",
            "end": f"{end:%Y-%m-%d %H:%M:%S}",
            "trd_env": "SIMULATE",
            "acc_id": self.acc_id,
        }
        ret, data = self.trd_ctx.history_order_list_query(
            code=SYMBOL, start=params["start"], end=params["end"], trd_env=TrdEnv.SIMULATE, acc_id=self.acc_id
        )
        if not self.log(f"history_order_list_query.{label}", params, ret, data):
            return
        rows = data.to_dict("records")
        found = {oid: [r for r in rows if str(r.get("order_id")) == oid] for oid in oids}
        summary = {
            oid: (None if not rs else {"order_status": rs[0].get("order_status"), "updated_time": rs[0].get("updated_time")})
            for oid, rs in found.items()
        }
        self.rec.log(f"p9.history_lookup.{label}", {"order_ids": oids}, "info", summary)

    def cancel(self, oid: str, label: str) -> None:
        ft = self.ft
        from moomoo import TrdEnv  # 写成字面量 TrdEnv.SIMULATE，供静态检查（R1）
        # vendor § Modify or Cancel Orders：trd_env 默认 REAL，必须显式传 SIMULATE；CANCEL 时 qty、price 被忽略
        ret, data = self.trd_ctx.modify_order(
            modify_order_op=ft.ModifyOrderOp.CANCEL,
            order_id=oid,
            qty=0,
            price=0,
            trd_env=TrdEnv.SIMULATE,
            acc_id=self.acc_id,
        )
        params = {"modify_order_op": "CANCEL", "order_id": oid, "qty": 0, "price": 0, "trd_env": "SIMULATE", "acc_id": self.acc_id}
        self.log(f"modify_order.{label}", params, ret, data)

    # ---------------------------------------------------------------- 连接

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
        # 异步连接 + 限时等待（同步构造失败时无限重试，learnings「阶段 0 实测」第 3 条）
        self.quote_ctx = ft.OpenQuoteContext(host=broker.host, port=broker.port, is_async_connect=True)
        deadline = time.monotonic() + CONNECT_TIMEOUT
        while self.quote_ctx.status != ft.ContextStatus.READY and time.monotonic() < deadline:
            time.sleep(0.1)
        status = str(self.quote_ctx.status)
        self.rec.log("OpenQuoteContext", {"host": broker.host, "port": broker.port, "is_async_connect": True}, status, None)
        if self.quote_ctx.status != ft.ContextStatus.READY:
            self.failures.append(f"OpenD {CONNECT_TIMEOUT:g} 秒内未就绪（{status}）")
            return False
        # vendor § Get global market status
        ret, gs = self.quote_ctx.get_global_state()
        self.log("get_global_state", {}, ret, gs)
        self.rec.open(gs.get("server_ver") if ret == ft.RET_OK else None)

        self.trd_ctx = ft.OpenSecTradeContext(filter_trdmarket=ft.TrdMarket.US, host=broker.host, port=broker.port)
        probe = self

        class OrderPush(ft.TradeOrderHandlerBase):
            """vendor § Orders Push Callback：原样记录每条订单推送（R4）。"""

            def on_recv_rsp(self, rsp_pb):
                ret, content = super().on_recv_rsp(rsp_pb)
                probe.rec.log("push.order", {"et_now": et_now().isoformat()}, ret, raw(content))
                if ret == ft.RET_OK and probe.placed is not None:
                    for r in content.to_dict("records"):
                        if str(r.get("order_id")) == probe.placed:
                            probe.last_status = str(r.get("order_status"))
                return ret, content

        self.trd_ctx.set_handler(OrderPush())
        # vendor § Get the List of Trading Accounts
        ret, self.accs = self.trd_ctx.get_acc_list()
        if not self.log("get_acc_list", {}, ret, self.accs):
            return False
        self.assert_simulate_account()
        return True

    # ---------------------------------------------------------------- 默认模式：挂单并观察到收盘后

    def run_place(self) -> int:
        if not self.connect():
            return 1
        ft = self.ft
        from moomoo import TrdEnv  # 写成字面量 TrdEnv.SIMULATE，供静态检查（R1）

        # vendor § Get Market Status of Securities：时段以它为准（learnings「M1 › 其他实测发现」）
        ret, ms = self.quote_ctx.get_market_state([SYMBOL])
        if not self.log("get_market_state", {"code_list": [SYMBOL]}, ret, ms):
            return 1
        state = str(ms.to_dict("records")[0]["market_state"])
        now = et_now()
        if state not in RTH_STATES or not (et_at(EARLIEST_PLACE) <= now < et_at(CLOSE)):
            print(f"# 当前时段 {state}、美东 {now:%H:%M}；须在常规时段 15:45–16:00 之间运行。未下单，退出", flush=True)
            self.failures.append(f"时机不对（{state}，美东 {now:%H:%M}）")
            return 1

        # vendor § Get Market Snapshot
        ret, snap = self.quote_ctx.get_market_snapshot([SYMBOL])
        if not self.log("get_market_snapshot", {"code_list": [SYMBOL]}, ret, snap):
            return 1
        last = Decimal(str(snap.to_dict("records")[0]["last_price"]))
        price = float((last * FAR_FACTOR).quantize(TICK, rounding=ROUND_DOWN))

        if SYMBOL not in self.whitelist:
            raise SystemExit(f"{SYMBOL} 不在 [risk.simulate] 白名单内，退出（R6）")
        self.assert_simulate_account()  # R1：下单前再断言一次
        remark = f"probe:m3:expiry:{utc_now():%H%M%S}"
        # vendor § Place Orders：trd_env 默认 REAL，必须显式传 SIMULATE；用 acc_id；不解锁（P1）；
        # adjust_limit=0 不让 OpenD 改价（SPEC 5.3）；DAY + RTH（ADR-0003、0004）
        ret, data = self.trd_ctx.place_order(
            price=price,
            qty=QTY,
            code=SYMBOL,
            trd_side=ft.TrdSide.BUY,
            order_type=ft.OrderType.NORMAL,
            adjust_limit=0,
            trd_env=TrdEnv.SIMULATE,
            acc_id=self.acc_id,
            remark=remark,
            time_in_force=ft.TimeInForce.DAY,
            session=ft.Session.RTH,
        )
        params = {
            "price": price, "qty": QTY, "code": SYMBOL, "trd_side": "BUY", "order_type": "NORMAL",
            "adjust_limit": 0, "trd_env": "SIMULATE", "acc_id": self.acc_id, "remark": remark,
            "time_in_force": "DAY", "session": "RTH",
        }
        if not self.log("place_order", params, ret, data):
            return 1
        self.placed = str(data["order_id"].tolist()[0])
        print(f"# 已挂单 {self.placed}，观察到美东 {OBSERVE_UNTIL[0]}:{OBSERVE_UNTIL[1]:02d}；不要中途退出", flush=True)

        # 观察：每 OBSERVE_POLL 秒查一次（缓存、刷新缓存交替），推送由 OrderPush 记录
        first_terminal_at: str | None = None
        n = 0
        while et_now() < et_at(OBSERVE_UNTIL):
            row = self.query_open(self.placed, "observe", refresh=(n % 2 == 1))
            n += 1
            status = None if row is None else str(row["order_status"])
            if status is not None:
                self.last_status = status
            if status in TERMINAL and first_terminal_at is None:
                first_terminal_at = et_now().isoformat()
            time.sleep(OBSERVE_POLL)

        self.query_open(self.placed, "final.cache", refresh=False)
        final = self.query_open(self.placed, "final.refresh", refresh=True)
        self.query_history([self.placed], "same_day")
        self.rec.log(
            "p9.summary",
            {"order_id": self.placed},
            "info",
            {
                "final_status": None if final is None else final.get("order_status"),
                "final_updated_time": None if final is None else final.get("updated_time"),
                "first_terminal_seen_et": first_terminal_at,
                "last_status_any_source": self.last_status,
                "next_step": f"明天运行 --check {self.placed}",
            },
        )
        print(f"# 完成。明天请运行：uv run python scripts/{PROBE}.py --check {self.placed}", flush=True)
        return 0

    # ---------------------------------------------------------------- --check 模式：只查询

    def run_check(self, oids: list[str]) -> int:
        if not self.connect():
            return 1
        for oid in oids:
            self.query_open(oid, "check.cache", refresh=False)
            self.query_open(oid, "check.refresh", refresh=True)
        self.query_history(oids, "check")
        return 0

    def cleanup(self) -> None:
        """R6、R7 兜底：美东 16:00 之前结束且订单未到终态 → 撤单并轮询到终态；之后只记录，不撤（见 docstring）。"""
        if self.trd_ctx is not None and self.placed is not None and self.last_status not in TERMINAL:
            if et_now() < et_at(CLOSE):
                self.cancel(self.placed, "cleanup")
                t0 = time.monotonic()
                status = None
                while time.monotonic() - t0 < CANCEL_TIMEOUT:
                    row = self.query_open(self.placed, "cleanup.poll")
                    status = None if row is None else str(row["order_status"])
                    if status in TERMINAL:
                        break
                    time.sleep(CANCEL_POLL)
                if status not in TERMINAL:
                    self.failures.append(f"兜底撤单 {CANCEL_TIMEOUT:g} 秒内未到终态（最后状态 {status}）")
            else:
                self.rec.log(
                    "p9.left_open", {"order_id": self.placed}, "info", {"last_status": self.last_status, "note": "收盘后未撤，按设计保留"}
                )
        for ctx in (self.trd_ctx, self.quote_ctx):
            if ctx is not None:
                ctx.close()
        self.rec.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="M3 探针 P9：DAY 单过期与 DELETED 状态（只 SIMULATE）")
    ap.add_argument("--check", nargs="+", metavar="ORDER_ID", help="只查询这些订单的状态，不下单")
    args = ap.parse_args()
    probe = Probe("check" if args.check else "place")
    try:
        rc = probe.run_check(args.check) if args.check else probe.run_place()
    finally:
        probe.cleanup()
    if probe.failures:
        print("# 失败：" + "；".join(probe.failures), file=sys.stderr)
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
