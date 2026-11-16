"""M4 探针 P12（SPEC 7.1 R1–R9）：OpenD 缓存的持仓何时反映已确认的卖单。只在 SIMULATE 跑；须在美股常规时段由你用 `!` 运行：

    ! uv run python scripts/probe_m4_can_sell_cache.py

要回答的问题（ADR-0031；2026-11-16 模拟盘测试发现）：
- P12a：挂一张已确认（SUBMITTED）的卖单后，position_list_query(refresh_cache=False) 的 can_sell_qty 多久才扣掉它（还是一直不扣）？
  同时对照 refresh_cache=True 的结果（P10d：刷新查询会扣）
- P12b：撤掉这张卖单后，缓存的 can_sell_qty 多久恢复
- 背景：闸门（MoomooBroker.query_positions）用缓存查询避开限频；实测已确认卖单 10 秒后缓存仍未扣减。
  结论决定 learnings / ADR-0031 怎么写：原来的「重复扣减」在缓存路径上是否真的会发生

副作用（R6），标的 US.SPY（在 [risk.simulate] 白名单内）：
1. 限价买 1 股，价格 = 卖一价 × 1.002（向上取到 0.01），期望立即成交（需要持仓才能挂卖单）
2. 限价卖 1 股，价格 = 最新价 × 1.10（向上取到 0.01），远高于市价、不会成交；等到 SUBMITTED
3. 每 2 秒用缓存查一次持仓，共 60 秒；第 0、30、60 秒另各用刷新查询一次（刷新查询受限频，vendor § Get Positions）
4. 撤单，等到终态；再每 2 秒缓存查询 30 秒，第 0、30 秒各刷新查询一次
5. 市价卖 1 股，恢复原持仓
remark 以 probe: 开头；finally 里撤销本次运行所有未到终态的订单并打印结果（R6、R7）。非常规时段直接退出、不下单。
注意限频按账户共用：不要和 pytest -m sim 同时运行，前后间隔 31 秒。

记录（R4、R5）：recordings/probes/probe_m4_can_sell_cache_<UTC>.jsonl；第一行是 SDK、OpenD 版本和本文件的 git 提交号；
之后每次 SDK 调用、每条订单推送一行。账号字段只保留后 4 位，其余原样。
"""

from __future__ import annotations

import importlib.metadata
import json
import math
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal
from pathlib import Path

from mooapp.config.loader import load_broker_cfg, load_settings

REPO = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).stem
SYMBOL = "US.SPY"
QTY = 1
MARKETABLE_FACTOR = Decimal("1.002")
FAR_SELL_FACTOR = Decimal("1.10")  # 远高于市价的卖单，不会成交
POLL_POSITIONS = 2.0  # 秒：缓存持仓查询间隔（缓存查询不受限频，vendor § Get Positions）
WATCH_OPEN = 60.0
REFRESH_OPEN = (0.0, 30.0, 60.0)  # 刷新查询受限频：整次运行合计不超过 8 次
WATCH_AFTER_CANCEL = 30.0
REFRESH_AFTER_CANCEL = (0.0, 30.0)
TICK = Decimal("0.01")  # 美股 ≥ $1 的最小变动单位（vendor § Place Orders › price）
CONNECT_TIMEOUT = 5.0
POLL_INTERVAL = 1.0
FILL_TIMEOUT = 60.0  # 模拟盘可成交单 6–28 秒才成交（learnings M2）
CANCEL_TIMEOUT = 30.0
SETTLE = 3.0  # 成交后等持仓刷新
ACCOUNT_KEYS = {"acc_id", "card_num", "uni_card_num"}
# vendor § Trading Definitions › Order Status：不会再变化的状态
TERMINAL = {"FILLED_ALL", "CANCELLED_PART", "CANCELLED_ALL", "FAILED", "DISABLED", "DELETED"}
# vendor § Trading Definitions › Market State：AFTERNOON = 美股常规时段（learnings M1）
RTH_STATES = {"AFTERNOON"}


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
        self.whitelist = load_settings(REPO / "config" / "settings.toml").risk_simulate.whitelist
        self.acc_id = self.broker.simulate_acc_id
        self.placed: list[str] = []
        self.terminal: set[str] = set()
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

    def query_order(self, oid: str) -> dict | None:
        # vendor § Get open Orders：trd_env 默认 REAL，必须显式传 SIMULATE
        from moomoo import TrdEnv  # 写成字面量 TrdEnv.SIMULATE，供静态检查（R1）
        ret, data = self.trd_ctx.order_list_query(order_id=oid, trd_env=TrdEnv.SIMULATE, acc_id=self.acc_id)
        self.log("order_list_query", {"order_id": oid, "trd_env": "SIMULATE", "acc_id": self.acc_id}, ret, data)
        if ret != self.ft.RET_OK:
            return None
        rows = data.to_dict("records")
        return rows[0] if rows else None

    def poll_until_terminal(self, oid: str, label: str, timeout: float) -> dict | None:
        t0 = time.monotonic()
        row = None
        while time.monotonic() - t0 < timeout:
            row = self.query_order(oid)
            if row is not None and str(row["order_status"]) in TERMINAL:
                self.terminal.add(oid)
                break
            time.sleep(POLL_INTERVAL)
        status = None if row is None else str(row["order_status"])
        self.rec.log(f"poll.{label}", {"order_id": oid, "timeout": timeout}, status, {"elapsed_seconds": round(time.monotonic() - t0, 3)})
        if status not in TERMINAL:
            self.failures.append(f"{label}: {timeout:g} 秒内未到终态（最后状态 {status}）")
        return row

    def place(self, label: str, price: float, side: str, order_type: str) -> str | None:
        ft = self.ft
        from moomoo import TrdEnv  # 写成字面量 TrdEnv.SIMULATE，供静态检查（R1）
        if SYMBOL not in self.whitelist:
            raise SystemExit(f"{SYMBOL} 不在 [risk.simulate] 白名单内，退出（R6）")
        self.assert_simulate_account()  # R1：每次下单前再断言一次
        remark = f"probe:m4c:{label}:{utc_now():%H%M%S}"
        # vendor § Place Orders：trd_env 默认 REAL，必须显式传 SIMULATE；用 acc_id；不解锁（P1）；adjust_limit=0；DAY + RTH
        ret, data = self.trd_ctx.place_order(
            price=price,
            qty=QTY,
            code=SYMBOL,
            trd_side=getattr(ft.TrdSide, side),
            order_type=getattr(ft.OrderType, order_type),
            adjust_limit=0,
            trd_env=TrdEnv.SIMULATE,
            acc_id=self.acc_id,
            remark=remark,
            time_in_force=ft.TimeInForce.DAY,
            session=ft.Session.RTH,
        )
        params = {"price": price, "qty": QTY, "code": SYMBOL, "trd_side": side, "order_type": order_type, "adjust_limit": 0,
                  "trd_env": "SIMULATE", "acc_id": self.acc_id, "remark": remark, "time_in_force": "DAY", "session": "RTH"}  # fmt: skip
        if not self.log(f"place_order.{label}", params, ret, data):
            return None
        oid = str(data["order_id"].tolist()[0])
        self.placed.append(oid)
        return oid

    def cancel(self, oid: str, label: str) -> None:
        ft = self.ft
        from moomoo import TrdEnv  # 写成字面量 TrdEnv.SIMULATE，供静态检查（R1）
        # vendor § Modify or Cancel Orders：trd_env 默认 REAL，必须显式传 SIMULATE；CANCEL 时 qty、price 被忽略
        ret, data = self.trd_ctx.modify_order(
            modify_order_op=ft.ModifyOrderOp.CANCEL, order_id=oid, qty=0, price=0, trd_env=TrdEnv.SIMULATE, acc_id=self.acc_id
        )
        params = {"modify_order_op": "CANCEL", "order_id": oid, "qty": 0, "price": 0, "trd_env": "SIMULATE", "acc_id": self.acc_id}
        self.log(f"modify_order.{label}", params, ret, data)

    def positions(self, label: str, refresh: bool) -> dict | None:
        """vendor § Get Positions：返回 US.SPY 那一行（没有则 None）；原样记录整个返回（R4）。"""
        from moomoo import TrdEnv  # 写成字面量 TrdEnv.SIMULATE，供静态检查（R1）
        ret, data = self.trd_ctx.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=self.acc_id, refresh_cache=refresh)
        self.log(f"position_list_query.{label}", {"trd_env": "SIMULATE", "acc_id": self.acc_id, "refresh_cache": refresh}, ret, data)
        rows = data.to_dict("records") if ret == self.ft.RET_OK else []
        row = next((r for r in rows if r.get("code") == SYMBOL), None)
        self.rec.log(f"p12.{label}", {"refresh_cache": refresh}, "info",
                     None if row is None else {"qty": row.get("qty"), "can_sell_qty": row.get("can_sell_qty")})  # fmt: skip
        return row

    def watch(self, phase: str, seconds: float, refresh_at: tuple[float, ...]) -> None:
        """每 POLL_POSITIONS 秒缓存查询一次；在 refresh_at 指定的秒数附近另做一次刷新查询。"""
        t0 = time.monotonic()
        pending = list(refresh_at)
        while True:
            el = time.monotonic() - t0
            self.positions(f"{phase}.cache.t{el:.0f}", refresh=False)
            if pending and el >= pending[0]:
                pending.pop(0)
                self.positions(f"{phase}.refresh.t{el:.0f}", refresh=True)
            if el >= seconds:
                break
            time.sleep(POLL_POSITIONS)

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

    def run(self) -> int:
        if not self.connect():
            return 1
        ret, ms = self.quote_ctx.get_market_state([SYMBOL])  # vendor § Get Market Status of Securities
        if not self.log("get_market_state", {"code_list": [SYMBOL]}, ret, ms):
            return 1
        state = str(ms.to_dict("records")[0]["market_state"])
        if state not in RTH_STATES:
            print(f"# 当前时段 {state}，不是常规时段；本探针需要开市，未下单，退出", flush=True)
            self.failures.append(f"非常规时段（{state}）")
            return 1

        self.positions("before.refresh", refresh=True)
        ret, snap = self.quote_ctx.get_market_snapshot([SYMBOL])  # vendor § Get Market Snapshot
        if not self.log("get_market_snapshot", {"code_list": [SYMBOL]}, ret, snap):
            return 1
        row = snap.to_dict("records")[0]
        ask = Decimal(str(row["ask_price"]))
        last = Decimal(str(row["last_price"]))

        buy = self.place("buy", float((ask * MARKETABLE_FACTOR).quantize(TICK, rounding=ROUND_UP)), "BUY", "NORMAL")
        if buy is None:
            return 1
        r = self.poll_until_terminal(buy, "buy.fill", FILL_TIMEOUT)
        if r is None or str(r["order_status"]) != "FILLED_ALL":
            return 1
        time.sleep(SETTLE)
        self.positions("after_buy.cache", refresh=False)
        self.positions("after_buy.refresh", refresh=True)

        far = self.place("far_sell", float((last * FAR_SELL_FACTOR).quantize(TICK, rounding=ROUND_UP)), "SELL", "NORMAL")
        if far is None:
            return 1
        self.poll_until_status(far, "far_sell.ack", {"SUBMITTED"}, CANCEL_TIMEOUT)
        self.watch("open_sell", WATCH_OPEN, REFRESH_OPEN)

        self.cancel(far, "far_sell")
        self.poll_until_terminal(far, "far_sell.cancel", CANCEL_TIMEOUT)
        self.watch("after_cancel", WATCH_AFTER_CANCEL, REFRESH_AFTER_CANCEL)

        sell = self.place("sell", float(last), "SELL", "MARKET")  # vendor § Place Orders：市价单 price 可传任意值
        if sell is not None:
            self.poll_until_terminal(sell, "sell.fill", FILL_TIMEOUT)
        time.sleep(SETTLE)
        self.positions("after_sell.refresh", refresh=True)
        return 0

    def poll_until_status(self, oid: str, label: str, statuses: set[str], timeout: float) -> dict | None:
        t0 = time.monotonic()
        row = None
        while time.monotonic() - t0 < timeout:
            row = self.query_order(oid)
            if row is not None and str(row["order_status"]) in statuses | TERMINAL:
                break
            time.sleep(POLL_INTERVAL)
        status = None if row is None else str(row["order_status"])
        self.rec.log(f"poll.{label}", {"order_id": oid, "timeout": timeout}, status, {"elapsed_seconds": round(time.monotonic() - t0, 3)})
        if status not in statuses:
            self.failures.append(f"{label}: {timeout:g} 秒内未到 {sorted(statuses)}（最后状态 {status}）")
        return row

    def cleanup(self) -> None:
        """R6、R7：撤销本次挂出、未到终态的订单并轮询到终态；失败原样记录，计入退出码。"""
        if self.trd_ctx is not None:
            for oid in self.placed:
                if oid in self.terminal:
                    continue
                self.cancel(oid, "cleanup")
                self.poll_until_terminal(oid, "cleanup", CANCEL_TIMEOUT)
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
