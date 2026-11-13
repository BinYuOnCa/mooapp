"""M2 部分成交探针（SPEC 7.1 R1–R9、7.3 P4；M2 验收第 3 条）。只在 SIMULATE 跑；须在美股常规时段由你用 `!` 运行：

    ! uv run python scripts/probe_m2_partial_fill.py

要回答的问题：
- P4：模拟盘部分成交怎么表现——订单推送里 dealt_qty / dealt_avg_price 怎么变化、是否出现 FILLED_PART、
  撤掉剩余部分后是否为 CANCELLED_PART。vendor § Trading Definitions › Order Status 列出了这些状态，
  但模拟盘撮合规则文档未写明。
- 同时录下一份「部分成交 + 撤单」的推送（M2 验收第 3 条；M3 回放用）。

副作用（R6），标的 US.SPY（在 [risk.simulate] 白名单内）：
1. 限价买 100 股，价格 = 当前卖一价（不加价）。**数量 100 的理由**：要观察部分成交，数量需大于卖一档
   可能的成交量；100 是 [risk.simulate] 的单笔上限 max_order_qty，约 6 万模拟资金。不加价是为了让成交
   取决于盘口，增加部分成交的机会。
2. 每 0.5 秒查询一次，最多 60 秒；仍未到终态就撤掉剩余部分，再轮询到终态。
3. 已成交多少，就市价卖出多少，恢复原持仓；卖单轮询到终态。
remark 以 probe: 开头；finally 里撤销本次运行所有未到终态的订单并打印结果（R6、R7）。
模拟盘可能总是一次全部成交，那样就录不到部分成交——如实记录，不重试（由你决定下一步）。
非常规时段直接退出、不下单。

记录（R4、R5）：recordings/probes/probe_m2_partial_fill_<UTC>.jsonl；第一行是 SDK、OpenD 版本和本文件的
git 提交号；之后每次 SDK 调用、每条订单推送一行。账号字段只保留后 4 位，其余原样。
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
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from mooapp.config.loader import load_broker_cfg, load_settings

REPO = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).stem
SYMBOL = "US.SPY"
BUY_QTY = 100
FILL_WAIT = 60.0
CONNECT_TIMEOUT = 5.0
POLL_INTERVAL = 0.5
POLL_TIMEOUT = 30.0
PUSH_GRACE = 3.0
ACCOUNT_KEYS = {"acc_id", "card_num", "uni_card_num"}
ET = ZoneInfo("America/New_York")
# vendor § Trading Definitions › Order Status：不会再变化的状态
TERMINAL = {"FILLED_ALL", "CANCELLED_PART", "CANCELLED_ALL", "FAILED", "DISABLED", "DELETED"}
# vendor § Trading Definitions › Market State：AFTERNOON = 美股常规时段
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
        ["git", "status", "--porcelain", "--", f"scripts/{PROBE}.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return head.stdout.strip() + ("（探针文件有未提交改动）" if dirty.stdout.strip() else "")


class Recorder:
    """先缓存，拿到 OpenD 版本后写出首行元数据，之后逐行写入并 flush（R4）。
    推送回调在 SDK 线程里调用 log，所以加锁。"""

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

    # ---------------------------------------------------------------- 通用

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
        # vendor § Get open Orders：trd_env 默认 REAL，必须显式传 SIMULATE；refresh_cache=False 不受限频
        ft = self.ft
        from moomoo import TrdEnv  # 写成字面量 TrdEnv.SIMULATE，供静态检查（R1）
        ret, data = self.trd_ctx.order_list_query(order_id=oid, trd_env=TrdEnv.SIMULATE, acc_id=self.acc_id)
        self.log("order_list_query", {"order_id": oid, "trd_env": "SIMULATE", "acc_id": self.acc_id}, ret, data)
        if ret != ft.RET_OK:
            return None
        rows = data.to_dict("records")
        return rows[0] if rows else None

    def poll_until_terminal(self, oid: str, label: str, timeout: float = POLL_TIMEOUT, required: bool = True) -> dict | None:
        """每 POLL_INTERVAL 秒查一次，直到终态或 timeout；记录耗时。required=False 时超时不算失败。"""
        t0 = time.monotonic()
        row = None
        while time.monotonic() - t0 < timeout:
            row = self.query_order(oid)
            if row is not None and str(row["order_status"]) in TERMINAL:
                self.terminal.add(oid)
                break
            time.sleep(POLL_INTERVAL)
        elapsed = round(time.monotonic() - t0, 3)
        status = None if row is None else str(row["order_status"])
        self.rec.log(f"poll.{label}", {"order_id": oid, "timeout": timeout}, status, {"elapsed_seconds": elapsed})
        if required and status not in TERMINAL:
            self.failures.append(f"{label}: {timeout:g} 秒内未到终态（最后状态 {status}）")
        return row

    def place(self, label: str, price: float, qty: int, side: str, order_type: str) -> str | None:
        ft = self.ft
        from moomoo import TrdEnv  # 写成字面量 TrdEnv.SIMULATE，供静态检查（R1）
        if SYMBOL not in self.whitelist:
            raise SystemExit(f"{SYMBOL} 不在 [risk.simulate] 白名单内，退出（R6）")
        self.assert_simulate_account()  # R1：每次下单前再断言一次
        remark = f"probe:m2:{label}:{utc_now():%H%M%S}"
        before = utc_now()
        # vendor § Place Orders：trd_env 默认 REAL，必须显式传 SIMULATE；用 acc_id；不解锁（P1）；
        # adjust_limit=0 不让 OpenD 改价（SPEC 5.3）；session=RTH（ADR-0003）
        ret, data = self.trd_ctx.place_order(
            price=price,
            qty=qty,
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
        after = utc_now()
        params = {
            "price": price, "qty": qty, "code": SYMBOL, "trd_side": side, "order_type": order_type,
            "adjust_limit": 0, "trd_env": "SIMULATE", "acc_id": self.acc_id, "remark": remark,
            "time_in_force": "DAY", "session": "RTH",
            "local_utc_before": before.isoformat(), "local_utc_after": after.isoformat(),
        }
        if not self.log(f"place_order.{label}", params, ret, data):
            return None
        oid = str(data["order_id"].tolist()[0])
        self.placed.append(oid)
        self.p3_compare(label, data.to_dict("records")[0], before, after)
        return oid

    def p3_compare(self, label: str, row: dict, before: datetime, after: datetime) -> None:
        """P3：create_time 分别按美东、UTC 解释，与下单前后的本机 UTC 比较。"""
        out = {"local_utc_before": before.isoformat(), "local_utc_after": after.isoformat()}
        for field in ("create_time", "updated_time"):
            text = str(row.get(field, ""))
            out[f"{field}_raw"] = text
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    naive = datetime.strptime(text, fmt)
                except ValueError:
                    continue
                for tzname, tz in (("as_et", ET), ("as_utc", UTC)):
                    t = naive.replace(tzinfo=tz).astimezone(UTC)
                    out[f"{field}_{tzname}_minus_before_s"] = round((t - before).total_seconds(), 3)
                break
            else:
                out[f"{field}_parse"] = "无法按 yyyy-MM-dd HH:mm:ss[.ffffff] 解析"
        self.rec.log(f"p3.compare.{label}", {}, "info", out)

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

    def positions(self, label: str) -> list[dict]:
        ft = self.ft
        from moomoo import TrdEnv  # 写成字面量 TrdEnv.SIMULATE，供静态检查（R1）
        # vendor § Get Positions：trd_env 默认 REAL，必须显式传 SIMULATE
        ret, data = self.trd_ctx.position_list_query(code=SYMBOL, trd_env=TrdEnv.SIMULATE, acc_id=self.acc_id)
        self.log(f"position_list_query.{label}", {"code": SYMBOL, "trd_env": "SIMULATE", "acc_id": self.acc_id}, ret, data)
        return data.to_dict("records") if ret == ft.RET_OK else []

    # ---------------------------------------------------------------- 主流程

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
        rec = self.rec

        class OrderPush(ft.TradeOrderHandlerBase):
            """vendor § Orders Push Callback：原样记录每条订单推送（R4）。"""

            def on_recv_rsp(self, rsp_pb):
                ret, content = super().on_recv_rsp(rsp_pb)
                rec.log("push.order", {}, ret, raw(content))
                return ret, content

        self.trd_ctx.set_handler(OrderPush())
        # vendor § Get the List of Trading Accounts
        ret, self.accs = self.trd_ctx.get_acc_list()
        if not self.log("get_acc_list", {}, ret, self.accs):
            return False
        self.assert_simulate_account()
        return True

    def run(self) -> int:
        if not self.connect():
            return 1
        ft = self.ft
        from moomoo import TrdEnv  # 写成字面量 TrdEnv.SIMULATE，供静态检查（R1）

        # vendor § Get Market Status of Securities：时段以它为准（learnings「M1 › 其他实测发现」）
        ret, ms = self.quote_ctx.get_market_state([SYMBOL])
        if not self.log("get_market_state", {"code_list": [SYMBOL]}, ret, ms):
            return 1
        state = str(ms.to_dict("records")[0]["market_state"])
        if state not in RTH_STATES:
            print(f"# 当前时段 {state}，不是常规时段；本探针需要开市，未下单，退出", flush=True)
            self.failures.append(f"非常规时段（{state}）")
            return 1

        self.positions("before")
        # vendor § Get Market Snapshot：ask_price、ask_vol 为卖一价、卖一量
        ret, snap = self.quote_ctx.get_market_snapshot([SYMBOL])
        if not self.log("get_market_snapshot", {"code_list": [SYMBOL]}, ret, snap):
            return 1
        row = snap.to_dict("records")[0]
        last = Decimal(str(row["last_price"]))
        ask = float(Decimal(str(row["ask_price"])))  # 卖一价本身就在最小变动单位上

        # 1. 限价买 100 股 @ 卖一价
        buy = self.place("buy100", ask, BUY_QTY, "BUY", "NORMAL")
        if buy is None:
            return 1
        # 2. 最多等 FILL_WAIT 秒；未到终态就撤掉剩余部分
        r = self.poll_until_terminal(buy, "buy100.wait", timeout=FILL_WAIT, required=False)
        if buy not in self.terminal:
            self.cancel(buy, "buy100.rest")
            r = self.poll_until_terminal(buy, "buy100.cancel")
        dealt = 0 if r is None else int(float(r["dealt_qty"]))
        self.rec.log(
            "p4.summary", {}, "info",
            {"final_status": None if r is None else str(r["order_status"]), "dealt_qty": dealt, "qty": BUY_QTY},
        )

        # 3. 市价卖出已成交部分。vendor § Place Orders：市价单 price 可传任意值
        if dealt > 0:
            sell = self.place("sell_back", float(last), dealt, "SELL", "MARKET")
            if sell is not None:
                self.poll_until_terminal(sell, "sell_back.fill")
        self.positions("after")

        # P4（部分）：vendor § Get Today's Deals：trd_env 默认 REAL，必须显式传 SIMULATE；文档说模拟盘不支持，只记录
        ret, deals = self.trd_ctx.deal_list_query(code=SYMBOL, trd_env=TrdEnv.SIMULATE, acc_id=self.acc_id)
        self.rec.log("deal_list_query", {"code": SYMBOL, "trd_env": "SIMULATE", "acc_id": self.acc_id}, ret, raw(deals))

        time.sleep(PUSH_GRACE)  # 等迟到的推送
        return 0

    def cleanup(self) -> None:
        """R6、R7：撤销本次挂出、未到终态的订单并轮询到终态；失败原样记录，计入退出码。"""
        if self.trd_ctx is not None:
            for oid in self.placed:
                if oid in self.terminal:
                    continue
                self.cancel(oid, "cleanup")
                self.poll_until_terminal(oid, "cleanup")
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
