"""M2 探针（SPEC 7.1 R1–R9、7.3 P3、P5、P7）。只在 SIMULATE 跑；须在美股常规时段由你用 `!` 运行：

    ! uv run python scripts/probe_m2.py

要回答的问题：
- P3：订单 create_time / updated_time 的时区。文档只说期货参见 OpenD 配置（vendor § Place Orders 返回
  字段表），美股未写明。下单前后记录本机 UTC，与 create_time 分别按美东、UTC 解释后比较。
- 撤单终态：M1 发现 modify_order(CANCEL) 返回成功后立即查询仍是 SUBMITTED（learnings「M1 › 其他实测
  发现」）。本探针撤单后每 0.5 秒查一次，直到终态或 30 秒，记录耗时与推送。
- P5：模拟账户类型（accinfo_query）；当日买入的股票 can_sell_qty 是否立即可卖（position_list_query），
  再实际卖出验证。vendor § Get Positions：can_sell_qty = 持仓 − 冻结；average_cost 对证券模拟账户无效。
- P7：order_fee_query 在模拟盘的表现。vendor § Get Order Fee › Interface Limitations 写明不支持模拟账户，
  这里只记录实际报错原文。
- P4（部分）：deal_list_query 在模拟盘的表现。vendor § Get Today's Deals 写明只支持实盘，记录实际返回。
  部分成交见 scripts/probe_m2_partial_fill.py。

副作用（R6），标的 US.SPY（在 [risk.simulate] 白名单内）：
1. 限价买 1 股，价格 = 最新价 × 0.9（向下取到 0.01），不期望成交；随后撤单（P3、撤单终态）。
2. 限价买 1 股，价格 = 卖一价 × 1.002（向上取到 0.01），期望立即成交（P5）。需要成交，所以不满足
   「离最新价 ≥ 10%」，这是本步的目的。
3. 市价卖 1 股，把第 2 步买入的卖掉，恢复原持仓（同时验证当日买入可卖）。
remark 以 probe: 开头；finally 里撤销本次运行所有未到终态的订单并打印结果（R6、R7）。
非常规时段直接退出、不下单：撤单终态和成交都需要开市。

记录（R4、R5）：recordings/probes/probe_m2_<UTC>.jsonl；第一行是 SDK、OpenD 版本和本文件的 git 提交号；
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
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from mooapp.config.loader import load_broker_cfg, load_settings

REPO = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).stem
SYMBOL = "US.SPY"
QTY = 1
FAR_FACTOR = Decimal("0.9")
MARKETABLE_FACTOR = Decimal("1.002")
TICK = Decimal("0.01")  # 美股 ≥ $1 的最小变动单位（vendor § Place Orders › price）
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

    def poll_until_terminal(self, oid: str, label: str) -> dict | None:
        """每 POLL_INTERVAL 秒查一次，直到终态或 POLL_TIMEOUT；记录耗时。"""
        t0 = time.monotonic()
        row = None
        while time.monotonic() - t0 < POLL_TIMEOUT:
            row = self.query_order(oid)
            if row is not None and str(row["order_status"]) in TERMINAL:
                self.terminal.add(oid)
                break
            time.sleep(POLL_INTERVAL)
        elapsed = round(time.monotonic() - t0, 3)
        status = None if row is None else str(row["order_status"])
        self.rec.log(f"poll.{label}", {"order_id": oid, "timeout": POLL_TIMEOUT}, status, {"elapsed_seconds": elapsed})
        if status not in TERMINAL:
            self.failures.append(f"{label}: {POLL_TIMEOUT:g} 秒内未到终态（最后状态 {status}）")
        return row

    def place(self, label: str, price: float, side: str, order_type: str) -> str | None:
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
        after = utc_now()
        params = {
            "price": price, "qty": QTY, "code": SYMBOL, "trd_side": side, "order_type": order_type,
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

        # P5：账户类型与资金。vendor § Get Account Funds：trd_env 默认 REAL，必须显式传 SIMULATE
        ret, acc = self.trd_ctx.accinfo_query(trd_env=TrdEnv.SIMULATE, acc_id=self.acc_id, currency=ft.Currency.USD)
        self.log("accinfo_query", {"trd_env": "SIMULATE", "acc_id": self.acc_id, "currency": "USD"}, ret, acc)
        self.positions("before")

        # vendor § Get Market Snapshot
        ret, snap = self.quote_ctx.get_market_snapshot([SYMBOL])
        if not self.log("get_market_snapshot", {"code_list": [SYMBOL]}, ret, snap):
            return 1
        row = snap.to_dict("records")[0]
        last = Decimal(str(row["last_price"]))
        ask = Decimal(str(row["ask_price"]))

        # 1. P3 + 撤单终态：远离市价的限价买单，随即撤单
        far = float((last * FAR_FACTOR).quantize(TICK, rounding=ROUND_DOWN))
        oid = self.place("far", far, "BUY", "NORMAL")
        if oid is not None:
            self.cancel(oid, "far")
            self.poll_until_terminal(oid, "far.cancel")

        # 2. P5：可成交的限价买 1 股
        buy_px = float((ask * MARKETABLE_FACTOR).quantize(TICK, rounding=ROUND_UP))
        buy = self.place("buy", buy_px, "BUY", "NORMAL")
        bought = False
        if buy is not None:
            r = self.poll_until_terminal(buy, "buy.fill")
            bought = r is not None and str(r["order_status"]) == "FILLED_ALL"
        rows = self.positions("after_buy")
        if rows:
            p = rows[0]
            self.rec.log("p5.can_sell", {}, "info", {"qty": p.get("qty"), "can_sell_qty": p.get("can_sell_qty")})

        # 3. 市价卖 1 股，恢复原持仓并验证当日买入可卖。vendor § Place Orders：市价单 price 可传任意值
        sell = None
        if bought:
            sell = self.place("sell", float(last), "SELL", "MARKET")
            if sell is not None:
                self.poll_until_terminal(sell, "sell.fill")
        self.positions("after_sell")

        # P7：vendor § Get Order Fee：trd_env 默认 REAL，必须显式传 SIMULATE
        fee_ids = [o for o in (buy, sell) if o is not None]
        if fee_ids:
            ret, fee = self.trd_ctx.order_fee_query(order_id_list=fee_ids, trd_env=TrdEnv.SIMULATE, acc_id=self.acc_id)
            self.rec.log("order_fee_query", {"order_id_list": fee_ids, "trd_env": "SIMULATE", "acc_id": self.acc_id}, ret, raw(fee))

        # P4（部分）：vendor § Get Today's Deals：trd_env 默认 REAL，必须显式传 SIMULATE
        ret, deals = self.trd_ctx.deal_list_query(code=SYMBOL, trd_env=TrdEnv.SIMULATE, acc_id=self.acc_id)
        self.rec.log("deal_list_query", {"code": SYMBOL, "trd_env": "SIMULATE", "acc_id": self.acc_id}, ret, raw(deals))
        # P7、P4 文档说模拟盘不支持：报错属预期，只记录原文，不计入失败

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
