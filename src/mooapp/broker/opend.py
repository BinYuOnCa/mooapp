"""只读 OpenD 会话（M1）：限时连接、全局状态、账户列表、资金、快照、市场状态。

- 全项目只有 broker/ 与 scripts/ import moomoo（SPEC 3.3 规则 1）。SDK 类型不出本模块：
  返回值都是下面的数据类。
- moomoo 只在 open_contexts() 里 import（M2 的 moomoo.open_broker 也用它）：SDK 一导入就写 ~/.com.moomoo.OpenD/Log/，在 Claude Code 的
  sandbox 里会抛 PermissionError（2026-11-11 实测）。延迟导入让单元测试能使用本模块的类型。
- SDK 的 ret != RET_OK 一律转成 BrokerError，不吞（SPEC 2.3）。
- M1 不下单、不解锁；funds() 只接受 SIMULATE（REAL 在 M4，经人锁）。
"""

from __future__ import annotations

import importlib.metadata
import socket
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from mooapp.broker.port import BrokerError, BrokerUnavailable  # M2 起定义在 port；doctor 仍从这里导入
from mooapp.common import Env
from mooapp.config.models import BrokerCfg

SDK_DIST = "moomoo-api"
_POLL_INTERVAL = 0.1
_TCP_PRECHECK_TIMEOUT = 1.0


# ------------------------------------------------------------------ 数据类


@dataclass(frozen=True)
class GlobalState:
    server_ver: str  # OpenD 版本（vendor § Get global market status）
    market_us: str


@dataclass(frozen=True)
class AccountInfo:
    acc_id: int
    trd_env: str  # "REAL" / "SIMULATE"
    markets: tuple[str, ...]  # trdmarket_auth


@dataclass(frozen=True)
class Funds:
    currency: str
    total_assets: Decimal
    cash: Decimal
    power: Decimal


@dataclass(frozen=True)
class Snapshot:
    code: str
    last_price: Decimal
    update_time: str  # 原文；美股为美东时间（vendor § Get Market Snapshot）


class OpenDSession(Protocol):
    def global_state(self) -> GlobalState: ...
    def accounts(self) -> list[AccountInfo]: ...
    def funds(self, acc_id: int, env: Env) -> Funds: ...
    def snapshot(self, codes: list[str]) -> list[Snapshot]: ...
    def market_states(self, codes: list[str]) -> dict[str, str]: ...
    def close(self) -> None: ...


def sdk_version() -> str:
    """SDK 版本，从安装元数据读取，不 import SDK。"""
    try:
        return importlib.metadata.version(SDK_DIST)
    except importlib.metadata.PackageNotFoundError:
        return "未安装"


def _dec(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as e:
        raise BrokerError(f"{field} 不是数值：{value!r}") from e


# ------------------------------------------------------------------ 实现


class MoomooReadSession:
    """持有一个行情连接和一个美股交易连接。只由 connect() 构造。"""

    def __init__(self, ft: Any, quote_ctx: Any, trade_ctx: Any):
        self._ft = ft
        self._quote = quote_ctx
        self._trade = trade_ctx

    def _ok(self, api: str, result: tuple[int, Any]) -> Any:
        ret, data = result
        if ret != self._ft.RET_OK:
            raise BrokerError(f"{api} 失败：{' '.join(str(data).split())}")
        return data

    def global_state(self) -> GlobalState:
        # vendor § Get global market status：get_global_state() -> dict（server_ver 为 OpenD 版本）
        d = self._ok("get_global_state", self._quote.get_global_state())
        return GlobalState(server_ver=str(d.get("server_ver", "")), market_us=str(d.get("market_us", "")))

    def accounts(self) -> list[AccountInfo]:
        # vendor § Get the List of Trading Accounts：get_acc_list() -> DataFrame
        df = self._ok("get_acc_list", self._trade.get_acc_list())
        return [
            AccountInfo(
                acc_id=int(row["acc_id"]),
                trd_env=str(row["trd_env"]),
                markets=tuple(str(m) for m in (row["trdmarket_auth"] or [])),
            )
            for row in df.to_dict("records")
        ]

    def funds(self, acc_id: int, env: Env) -> Funds:
        # vendor § Get Account Funds：accinfo_query 的 trd_env 默认 REAL，必须显式传（SPEC 5.3）；
        # 用 acc_id 而不用 acc_index（vendor 强烈建议）。currency 只对综合账户生效，单市场账户忽略。
        if env is not Env.SIMULATE:
            raise BrokerError("M1 只读会话只支持 SIMULATE；REAL 在 M4 经人锁实现")
        ft = self._ft
        df = self._ok(
            "accinfo_query",
            self._trade.accinfo_query(
                trd_env=ft.TrdEnv.SIMULATE,
                acc_id=acc_id,
                refresh_cache=False,
                currency=ft.Currency.USD,
            ),
        )
        rows = df.to_dict("records")
        if not rows:
            raise BrokerError("accinfo_query 未返回数据")
        r = rows[0]
        return Funds(
            currency=str(r.get("currency", "")),
            total_assets=_dec(r["total_assets"], "total_assets"),
            cash=_dec(r["cash"], "cash"),
            power=_dec(r["power"], "power"),
        )

    def snapshot(self, codes: list[str]) -> list[Snapshot]:
        # vendor § Get Market Snapshot：get_market_snapshot(code_list) -> DataFrame
        df = self._ok("get_market_snapshot", self._quote.get_market_snapshot(list(codes)))
        return [
            Snapshot(
                code=str(r["code"]),
                last_price=_dec(r["last_price"], "last_price"),
                update_time=str(r["update_time"]),
            )
            for r in df.to_dict("records")
        ]

    def market_states(self, codes: list[str]) -> dict[str, str]:
        # vendor § Get Market Status of Securities：get_market_state(code_list) -> DataFrame
        df = self._ok("get_market_state", self._quote.get_market_state(list(codes)))
        return {str(r["code"]): str(r["market_state"]) for r in df.to_dict("records")}

    def close(self) -> None:
        # vendor § Quote Object › Close Connection
        for ctx in (self._trade, self._quote):
            if ctx is not None:
                ctx.close()


def _tcp_precheck(cfg: BrokerCfg, timeout: float) -> None:
    """端口不通时快速失败，不加载 SDK（M1 已知问题：SDK 导入即写日志目录，且同步构造会无限重试）。"""
    try:
        sock = socket.create_connection((cfg.host, cfg.port), timeout=min(timeout, _TCP_PRECHECK_TIMEOUT))
    except ConnectionRefusedError as e:
        raise BrokerUnavailable(f"OpenD {cfg.host}:{cfg.port} 拒绝连接（OpenD 未启动？）") from e
    except OSError as e:
        raise BrokerUnavailable(f"无法连接 OpenD {cfg.host}:{cfg.port}：{e}") from e
    sock.close()


def open_contexts(cfg: BrokerCfg, timeout: float = 5.0) -> tuple[Any, Any, Any]:
    """限时打开行情连接和美股交易连接，返回 (moomoo 模块, quote_ctx, trade_ctx)；
    timeout 秒内未就绪抛 BrokerUnavailable。connect() 与 moomoo.open_broker() 共用。

    SDK 的同步构造在连接失败时每 6 秒重试、永不返回（learnings「阶段 0 实测」第 3 条；
    SDK 源码 common/open_context_base.py）。因此：
    0. 先做 TCP 预检，端口拒绝时 1 秒内失败，不 import SDK。
    1. 行情连接用 is_async_connect=True，轮询 status 直到 READY 或超时。
       （这两个参数 vendor 未写明，用法来自阶段 0 实测与 SDK 源码。）
    2. 交易连接没有异步选项；行情连接就绪说明 OpenD 在线且握手成功，此时再在守护线程里
       同步构造交易连接，用剩余时间限时等待。
    """
    deadline = time.monotonic() + timeout
    _tcp_precheck(cfg, timeout)
    try:
        import moomoo as ft
    except PermissionError as e:
        raise BrokerUnavailable(f"无法加载 moomoo SDK（{e.strerror}：{e.filename}）") from e

    # vendor § Basic Functions › Set Thread Mode：SDK 线程设为守护线程，超时放弃时进程仍能退出
    ft.SysConfig.set_all_thread_daemon(True)
    # vendor § Enable console logging of OpenD connection status：不往终端打印连接日志（learnings M2：实测有效）
    ft.SysConfig.enable_console_log(False)

    quote = ft.OpenQuoteContext(host=cfg.host, port=cfg.port, is_async_connect=True)
    while quote.status != ft.ContextStatus.READY:
        if time.monotonic() >= deadline:
            status = quote.status
            quote.close()
            raise BrokerUnavailable(f"行情连接 {timeout:g} 秒内未就绪（状态 {status}）")
        time.sleep(_POLL_INTERVAL)

    holder: dict[str, Any] = {}
    lock = threading.Lock()  # 保护 holder 与 abandoned：超时放弃后迟到的交易连接由线程自己关闭

    def build_trade() -> None:
        try:
            # vendor § Get the List of Trading Accounts 的示例：OpenSecTradeContext(filter_trdmarket=...)
            # security_firm 不传：SDK 自动识别（learnings「阶段 0 实测」第 4 条）
            ctx = ft.OpenSecTradeContext(filter_trdmarket=ft.TrdMarket.US, host=cfg.host, port=cfg.port)
        except BaseException as e:  # 交给主线程原样重新抛出，不吞
            with lock:
                holder["error"] = e
            return
        with lock:
            if holder.get("abandoned"):
                ctx.close()
            else:
                holder["ctx"] = ctx

    t = threading.Thread(target=build_trade, name="mooapp-opend-trade-connect", daemon=True)
    t.start()
    t.join(max(deadline - time.monotonic(), 0.0))
    with lock:
        trade = holder.get("ctx")
        error = holder.get("error")
        if trade is None:
            holder["abandoned"] = True
    if trade is None:
        quote.close()
        if error is not None:
            raise BrokerUnavailable(f"交易连接失败：{error!r}") from error
        raise BrokerUnavailable(f"交易连接 {timeout:g} 秒内未就绪")
    return ft, quote, trade


def connect(cfg: BrokerCfg, timeout: float = 5.0) -> MoomooReadSession:
    """M1 只读会话；timeout 秒内未就绪抛 BrokerUnavailable。"""
    ft, quote, trade = open_contexts(cfg, timeout)
    return MoomooReadSession(ft, quote, trade)
