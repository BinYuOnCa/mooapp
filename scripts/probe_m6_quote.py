"""M6 探针 P11（SPEC 7.1 R1–R9）。只读行情，不下单、不连交易接口；由你用 `!` 运行：

    ! uv run python scripts/probe_m6_quote.py              # 建议：常规时段跑一次；盘前或盘后再跑一次作对照
    ! uv run python scripts/probe_m6_quote.py --seconds 120

要回答的问题（M6b 规则引擎取开盘价、判断触发都依赖这些；vendor § Real-time Quote Callback 没写明）：
- P11a：模拟盘账户订阅 US.SPY 的 QUOTE 后，推送多久一条（是否每笔成交都推）
- P11b：data_date / data_time 的时区（vendor：美股为美东时间）与本机 UTC 的差
- P11c：open_price 在开盘前、开盘后分别是什么（0？昨天的？今天的？）
- P11d：盘前、盘后（及夜盘）推送的 last_price 是常规时段价格还是延长时段价格；同时间快照的 last_price 对照

做法：get_global_state → get_market_state → get_market_snapshot → subscribe([US.SPY], [QUOTE]) → 录推送 N 秒
→ 再取一次快照 → 至少订阅满 1 分钟后 unsubscribe（vendor § Subscribe and Unsubscribe：订阅后至少 1 分钟才能退订）。
副作用：占用 1 个订阅额度（vendor § Subscription Quota），结束时退订。不涉及账户，没有任何交易调用（R1、R3、R6）。

记录（R4、R5）：recordings/probes/probe_m6_quote_<UTC>.jsonl；第一行是 SDK、OpenD 版本和本文件的 git 提交号；
之后每次 SDK 调用、每条推送一行（带本机接收时刻）。行情里没有账号字段。
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
from datetime import UTC, datetime
from pathlib import Path

from mooapp.config.loader import load_broker_cfg

REPO = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).stem
SYMBOL = "US.SPY"
CONNECT_TIMEOUT = 5.0
MIN_SUBSCRIBED = 61.0  # 秒：订阅满 1 分钟才能退订


def utc_now() -> datetime:
    return datetime.now(UTC)


def raw(data: object) -> object:
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
    def __init__(self) -> None:
        self.started = utc_now()
        self.path = REPO / "recordings" / "probes" / f"{PROBE}_{self.started:%Y%m%dT%H%M%SZ}.jsonl"
        self.pending: list[dict] = []
        self.fh = None
        self.lock = threading.Lock()
        self.push_count = 0

    def log(self, call: str, params: dict, ret: object, data: object) -> None:
        rec = {"ts": utc_now().isoformat(), "call": call, "params": params, "ret": ret, "data": data}
        line = json.dumps(rec, ensure_ascii=False, default=json_default)
        with self.lock:
            print(line, flush=True)
            if self.fh is None:
                self.pending.append(rec)
            else:
                self.fh.write(line + "\n")
                self.fh.flush()

    def open(self, opend_ver: str | None) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = self.path.open("w", encoding="utf-8")
            meta = {
                "meta": True, "probe": f"scripts/{PROBE}.py", "started": self.started.isoformat(),
                "sdk": importlib.metadata.version("moomoo-api"), "opend": opend_ver, "git": git_commit(),
                "python": sys.version.split()[0],
            }  # fmt: skip
            self.fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
            for rec in self.pending:
                self.fh.write(json.dumps(rec, ensure_ascii=False, default=json_default) + "\n")
            self.pending.clear()
            self.fh.flush()
        print(f"# 记录文件：{self.path}", flush=True)

    def close(self) -> None:
        if self.fh is None:
            self.open(None)
        with self.lock:
            assert self.fh is not None
            self.fh.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="M6 探针 P11：报价推送（只读）")
    ap.add_argument("--seconds", type=float, default=60.0, help="录推送的秒数（至少 61 秒才退订）")
    args = ap.parse_args()
    rec = Recorder()
    failures: list[str] = []
    cfg = load_broker_cfg(REPO / "config" / "broker.toml")
    quote_ctx = None
    subscribed_at: float | None = None
    try:
        try:
            import moomoo as ft
        except PermissionError as e:
            rec.log("import moomoo", {}, "PermissionError", {"errno": e.errno, "strerror": e.strerror, "filename": e.filename})
            failures.append("无法加载 SDK（在 sandbox 内属预期，见 P6）")
            return 1
        ft.SysConfig.enable_console_log(False)  # vendor § Enable console logging of OpenD connection status
        ft.SysConfig.set_all_thread_daemon(True)  # vendor § Basic Functions › Set Thread Mode
        quote_ctx = ft.OpenQuoteContext(host=cfg.host, port=cfg.port, is_async_connect=True)
        deadline = time.monotonic() + CONNECT_TIMEOUT
        while quote_ctx.status != ft.ContextStatus.READY and time.monotonic() < deadline:
            time.sleep(0.1)
        rec.log("OpenQuoteContext", {"host": cfg.host, "port": cfg.port, "is_async_connect": True}, str(quote_ctx.status), None)
        if quote_ctx.status != ft.ContextStatus.READY:
            failures.append(f"OpenD {CONNECT_TIMEOUT:g} 秒内未就绪")
            return 1
        ret, gs = quote_ctx.get_global_state()  # vendor § Get global market status
        rec.log("get_global_state", {}, ret, raw(gs))
        rec.open(gs.get("server_ver") if ret == ft.RET_OK else None)
        ret, ms = quote_ctx.get_market_state([SYMBOL])  # vendor § Get Market Status of Securities
        rec.log("get_market_state", {"code_list": [SYMBOL]}, ret, raw(ms))
        ret, snap = quote_ctx.get_market_snapshot([SYMBOL])  # vendor § Get Market Snapshot
        rec.log("get_market_snapshot.before", {"code_list": [SYMBOL]}, ret, raw(snap))

        class QuotePush(ft.StockQuoteHandlerBase):
            """vendor § Real-time Quote Callback：原样记录每条推送和本机接收时刻（R4）。"""

            def on_recv_rsp(self, rsp_pb):
                ret, content = super().on_recv_rsp(rsp_pb)
                rec.push_count += 1
                rec.log("push.quote", {"local_utc": utc_now().isoformat()}, ret, raw(content))
                return ret, content

        quote_ctx.set_handler(QuotePush())
        # vendor § Subscribe and Unsubscribe：QUOTE 一种类型占 1 个额度；subscribe_push=True 才会回调
        ret, err = quote_ctx.subscribe([SYMBOL], [ft.SubType.QUOTE], is_first_push=True, subscribe_push=True)
        rec.log("subscribe", {"code_list": [SYMBOL], "subtype_list": ["QUOTE"], "is_first_push": True, "subscribe_push": True}, ret, err)
        if ret != ft.RET_OK:
            failures.append(f"subscribe 失败：{err}")
            return 1
        subscribed_at = time.monotonic()
        time.sleep(max(args.seconds, 1.0))
        ret, snap = quote_ctx.get_market_snapshot([SYMBOL])
        rec.log("get_market_snapshot.after", {"code_list": [SYMBOL]}, ret, raw(snap))
        rec.log("p11.summary", {}, "info", {"seconds": args.seconds, "push_count": rec.push_count})
        return 0
    finally:
        if quote_ctx is not None:
            if subscribed_at is not None:
                wait = MIN_SUBSCRIBED - (time.monotonic() - subscribed_at)
                if wait > 0:
                    time.sleep(wait)
                import moomoo as ft  # noqa: F811（已加载）

                ret, err = quote_ctx.unsubscribe_all()  # vendor § Subscribe and Unsubscribe
                rec.log("unsubscribe_all", {}, ret, err)
                if ret != ft.RET_OK:
                    failures.append(f"unsubscribe_all 失败：{err}")
            quote_ctx.close()
        rec.close()
        if failures:
            print("# 失败：" + "；".join(failures), file=sys.stderr)
            FAILED.append(True)


FAILED: list[bool] = []  # finally 里的失败（例如退订失败）也要让退出码非 0（R7）


if __name__ == "__main__":
    rc = main()
    sys.exit(1 if FAILED else rc)
