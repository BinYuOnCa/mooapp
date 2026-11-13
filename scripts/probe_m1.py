"""M1 探针（SPEC 7.1 R1–R9、7.3 P1、P2、P6）。只在 SIMULATE 跑；由你用 `!` 运行：

    ! uv run python scripts/probe_m1.py

要回答的问题：
- P1：模拟盘下单是否需要解锁交易。本探针**不解锁**，直接下单：成功即说明不需要。
  出处：vendor § Unlock Trade、§ Place Orders（文档说模拟盘不需要，这里实测）。
- P2：快照 update_time（vendor § Get Market Snapshot：美股为美东时间）与本机 UTC 相差多少，
  判断是实时还是延时行情。休市时差值没有意义，如实记录。
- P6：在 Claude Code 的 sandbox 内运行时，连 127.0.0.1:11111 应被拦（ADR-0015 第 3 条）。
  所以第一步先用标准库 socket 试连，再 import SDK（SDK 在 sandbox 内导入就会失败）。

副作用（R6）：US.SPY 限价买 1 股，价格 = 最新价 × 0.9（向下取到 0.01），离最新价 ≥ 10%，
不期望成交；remark 以 probe: 开头；结束时在 finally 里撤单并打印结果。

记录（R4、R5）：recordings/probes/probe_m1_<UTC>.jsonl；第一行是 SDK、OpenD 版本和本文件的
git 提交号；之后每次 SDK 调用一行（入参、ret、原始返回）。账号字段只保留后 4 位，其余原样。
"""

from __future__ import annotations

import importlib.metadata
import json
import math
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from mooapp.config.loader import load_broker_cfg, load_settings

REPO = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).stem
SYMBOL = "US.SPY"
QTY = 1
PRICE_FACTOR = Decimal("0.9")
CONNECT_TIMEOUT = 5.0
ACCOUNT_KEYS = {"acc_id", "card_num", "uni_card_num"}
ET = ZoneInfo("America/New_York")


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


class Recorder:
    """先缓存，拿到 OpenD 版本后写出首行元数据，之后逐行写入并 flush（R4）。"""

    def __init__(self) -> None:
        self.started = utc_now()
        self.path = REPO / "recordings" / "probes" / f"{PROBE}_{self.started:%Y%m%dT%H%M%SZ}.jsonl"
        self.pending: list[dict] = []
        self.fh = None
        self.failed = False

    def log(self, call: str, params: dict, ret: object, data: object) -> None:
        rec = scrub({"ts": utc_now().isoformat(), "call": call, "params": params, "ret": ret, "data": data})
        print(json.dumps(rec, ensure_ascii=False, default=json_default))
        if self.fh is None:
            self.pending.append(rec)
        else:
            self._write(rec)

    def open(self, opend_ver: str | None) -> None:
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
        print(f"# 记录文件：{self.path}")

    def _write(self, rec: dict) -> None:
        assert self.fh is not None
        self.fh.write(json.dumps(rec, ensure_ascii=False, default=json_default) + "\n")
        self.fh.flush()

    def close(self) -> None:
        if self.fh is None:
            self.open(None)
        assert self.fh is not None
        self.fh.close()


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


def p6_socket(rec: Recorder, host: str, port: int) -> None:
    """P6：标准库直连 OpenD 端口，记录结果（sandbox 内应为 PermissionError）。"""
    params = {"host": host, "port": port, "timeout": 2.0}
    try:
        with socket.create_connection((host, port), timeout=2.0):
            rec.log("p6.socket.create_connection", params, "connected", None)
    except OSError as e:
        rec.log("p6.socket.create_connection", params, type(e).__name__, {"errno": e.errno, "strerror": e.strerror})


class Probe:
    def __init__(self) -> None:
        self.rec = Recorder()
        self.broker = load_broker_cfg(REPO / "config" / "broker.toml")
        self.whitelist = load_settings(REPO / "config" / "settings.toml").risk_simulate.whitelist
        self.acc_id = self.broker.simulate_acc_id
        self.placed: list[str] = []
        self.failures: list[str] = []
        self.quote_ctx = None
        self.trd_ctx = None
        self.accs = None

    def log(self, name: str, params: dict, ret: object, data: object, ok: object) -> None:
        """原样记录；ret != RET_OK 记为失败但不抛，让 finally 能撤单（R7）。"""
        self.rec.log(name, params, ret, raw(data))
        if ret != ok:
            self.failures.append(f"{name}: {data}")

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

    def run(self) -> int:
        rec, broker, acc_id = self.rec, self.broker, self.acc_id
        p6_socket(rec, broker.host, broker.port)
        try:
            from moomoo import (
                RET_OK,
                ContextStatus,
                OpenQuoteContext,
                OpenSecTradeContext,
                OrderType,
                SysConfig,
                TimeInForce,
                TrdEnv,
                TrdMarket,
                TrdSide,
            )
        except PermissionError as e:
            rec.log("import moomoo", {}, "PermissionError", {"errno": e.errno, "strerror": e.strerror, "filename": e.filename})
            self.failures.append("无法加载 SDK（在 sandbox 内属预期，见 P6）")
            return 1

        SysConfig.set_all_thread_daemon(True)  # vendor § Basic Functions › Set Thread Mode
        # 异步连接 + 限时等待（同步构造失败时无限重试，learnings「阶段 0 实测」第 3 条）
        self.quote_ctx = OpenQuoteContext(host=broker.host, port=broker.port, is_async_connect=True)
        deadline = time.monotonic() + CONNECT_TIMEOUT
        while self.quote_ctx.status != ContextStatus.READY and time.monotonic() < deadline:
            time.sleep(0.1)
        status = str(self.quote_ctx.status)
        rec.log("OpenQuoteContext", {"host": broker.host, "port": broker.port, "is_async_connect": True}, status, None)
        if self.quote_ctx.status != ContextStatus.READY:
            self.failures.append(f"OpenD {CONNECT_TIMEOUT:g} 秒内未就绪（{status}）")
            return 1

        # vendor § Get global market status
        ret, gs = self.quote_ctx.get_global_state()
        self.log("get_global_state", {}, ret, gs, RET_OK)
        rec.open(gs.get("server_ver") if ret == RET_OK else None)

        # vendor § Get the List of Trading Accounts
        self.trd_ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=broker.host, port=broker.port)
        ret, self.accs = self.trd_ctx.get_acc_list()
        self.log("get_acc_list", {}, ret, self.accs, RET_OK)
        if ret != RET_OK:
            return 1
        self.assert_simulate_account()

        # vendor § Get Market Snapshot（P2）
        ret, snap = self.quote_ctx.get_market_snapshot([SYMBOL])
        self.log("get_market_snapshot", {"code_list": [SYMBOL]}, ret, snap, RET_OK)
        if ret != RET_OK:
            return 1
        row = snap.to_dict("records")[0]
        now = utc_now()
        upd_et = datetime.strptime(str(row["update_time"]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
        rec.log(
            "p2.compare",
            {"update_time_raw": row["update_time"], "assumed_tz": "America/New_York"},
            "info",
            {
                "update_time_utc": upd_et.astimezone(UTC).isoformat(),
                "local_utc": now.isoformat(),
                "lag_seconds": (now - upd_et.astimezone(UTC)).total_seconds(),
            },
        )

        # R6：只交易白名单内标的；限价离最新价 ≥ 10%
        if SYMBOL not in self.whitelist:
            raise SystemExit(f"{SYMBOL} 不在 [risk.simulate] 白名单内，退出（R6）")
        last = Decimal(str(row["last_price"]))
        price = float((last * PRICE_FACTOR).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        remark = f"probe:m1:{now:%Y%m%dT%H%M%SZ}"

        self.assert_simulate_account()  # R1：每次下单前再断言一次
        # vendor § Place Orders：trd_env 默认 REAL，必须显式传 SIMULATE；用 acc_id；不解锁（P1）
        ret, order = self.trd_ctx.place_order(
            price=price,
            qty=QTY,
            code=SYMBOL,
            trd_side=TrdSide.BUY,
            order_type=OrderType.NORMAL,
            trd_env=TrdEnv.SIMULATE,
            acc_id=acc_id,
            remark=remark,
            time_in_force=TimeInForce.DAY,
        )
        params = {
            "price": price, "qty": QTY, "code": SYMBOL, "trd_side": "BUY", "order_type": "NORMAL",
            "trd_env": "SIMULATE", "acc_id": acc_id, "remark": remark, "time_in_force": "DAY",
        }
        self.log("place_order", params, ret, order, RET_OK)
        if ret == RET_OK:
            self.placed.extend(str(o) for o in order["order_id"].tolist())
            print(f"# P1：未解锁下单成功 → 模拟盘下单不需要解锁；order_id={self.placed}")
            return 0
        print(f"# P1：未解锁下单失败，原文：{order}（判断是否与解锁有关，写进 learnings）")
        return 1

    def cleanup(self) -> None:
        """R6、R7：撤销本次挂出的全部订单并打印结果；失败原样记录，计入退出码。"""
        if self.placed:
            from moomoo import RET_OK, ModifyOrderOp, TrdEnv

            for oid in self.placed:
                # vendor § Modify or Cancel Orders：trd_env 默认 REAL，必须显式传 SIMULATE
                ret, data = self.trd_ctx.modify_order(
                    modify_order_op=ModifyOrderOp.CANCEL,
                    order_id=oid,
                    qty=0,
                    price=0,
                    trd_env=TrdEnv.SIMULATE,
                    acc_id=self.acc_id,
                )
                params = {"modify_order_op": "CANCEL", "order_id": oid, "qty": 0, "price": 0, "trd_env": "SIMULATE", "acc_id": self.acc_id}
                self.log("modify_order", params, ret, data, RET_OK)
                # vendor § Get Order List：撤单后的最终状态
                ret, data = self.trd_ctx.order_list_query(order_id=oid, trd_env=TrdEnv.SIMULATE, acc_id=self.acc_id)
                params = {"order_id": oid, "trd_env": "SIMULATE", "acc_id": self.acc_id}
                self.log("order_list_query", params, ret, data, RET_OK)
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
