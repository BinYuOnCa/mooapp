"""mooapp doctor：连通与配置检查（SPEC 5.10、8 节 M1 实现说明）。

检查项返回「名称、状态、详情」；有 FAIL 时退出码非 0。每项详情一行，账号只显示后 4 位。

| # | 检查 |
| 1 | OpenD 版本、SDK 版本 |
| 2 | 账户列表；simulate_acc_id 必须在 SIMULATE 且有 US 权限的账户中 |
| 3 | 模拟账户资金（显式 SIMULATE + acc_id） |
| 4 | 白名单第一只的快照 |
| 5 | 市场状态 |
| 6 | secrets：存在、可读、权限 600、可解析、必填项齐全 |
| 7 | 数据库：能连上；当前角色访问不了 real；listen_addresses 只含回环地址 |
| 8 | 本机非回环地址连 PostgreSQL、OpenD 端口必须失败 |

OpenD 不可达时只输出一行，跳过 1–5，6–8 照常。
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import psycopg
from psycopg import errors as pg_errors

from mooapp.broker.opend import BrokerError, BrokerUnavailable, OpenDSession, sdk_version
from mooapp.common import Env, mask_account
from mooapp.config.loader import (
    ConfigError,
    ConfigUnreadable,
    describe_db,
    dsn_for,
    load_db_local,
    load_secrets,
)
from mooapp.config.models import BrokerCfg, DbCfg, Settings

OPEND_TIMEOUT = 5.0  # 你确认的第 3 点
DB_CONNECT_TIMEOUT = 5
EXPOSURE_TIMEOUT = 1.0
LOOPBACK_LISTEN = {"localhost", "127.0.0.1", "::1"}


class Status(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str


OpenDFactory = Callable[[BrokerCfg, float], OpenDSession]


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


# ------------------------------------------------------------------ 1–5：OpenD


def _opend_checks(sess: OpenDSession, settings: Settings, broker_cfg: BrokerCfg) -> list[CheckResult]:
    out: list[CheckResult] = []
    acc_id = broker_cfg.simulate_acc_id

    # 1 版本（vendor § Get global market status：server_ver）
    try:
        gs = sess.global_state()
        out.append(CheckResult("版本", Status.PASS, f"OpenD {gs.server_ver}，SDK {sdk_version()}"))
    except BrokerError as e:
        out.append(CheckResult("版本", Status.FAIL, f"{_one_line(e)}；SDK {sdk_version()}"))

    # 2 账户列表（vendor § Get the List of Trading Accounts）
    acc_ok = False
    try:
        accs = sess.accounts()
        listing = "；".join(f"{a.trd_env} {mask_account(a.acc_id)} [{','.join(a.markets)}]" for a in accs)
        acc_ok = any(a.acc_id == acc_id and a.trd_env == Env.SIMULATE.value and "US" in a.markets for a in accs)
        if acc_ok:
            out.append(CheckResult("账户列表", Status.PASS, f"{listing}；模拟账户 {mask_account(acc_id)} 已核对"))
        else:
            out.append(
                CheckResult(
                    "账户列表",
                    Status.FAIL,
                    f"{listing or '无账户'}；broker.toml 的 simulate_acc_id {mask_account(acc_id)} "
                    "不在 SIMULATE 且有 US 权限的账户中",
                )
            )
    except BrokerError as e:
        out.append(CheckResult("账户列表", Status.FAIL, _one_line(e)))

    # 3 模拟账户资金（vendor § Get Account Funds）
    if not acc_ok:
        out.append(CheckResult("模拟账户资金", Status.SKIP, "账户核对未通过，不查询"))
    else:
        try:
            f = sess.funds(acc_id, Env.SIMULATE)
            out.append(
                CheckResult(
                    "模拟账户资金",
                    Status.PASS,
                    f"{mask_account(acc_id)} 总资产 {f.total_assets} 现金 {f.cash} 购买力 {f.power}"
                    f"（currency={f.currency}）",
                )
            )
        except BrokerError as e:
            out.append(CheckResult("模拟账户资金", Status.FAIL, _one_line(e)))

    code = settings.risk_simulate.whitelist[0]

    # 4 快照（vendor § Get Market Snapshot；美股 update_time 为美东时间）
    try:
        snaps = sess.snapshot([code])
        if not snaps:
            out.append(CheckResult("快照", Status.FAIL, f"{code} 无数据"))
        else:
            s = snaps[0]
            out.append(CheckResult("快照", Status.PASS, f"{s.code} 最新价 {s.last_price}，更新时间 {s.update_time}（美东）"))
    except BrokerError as e:
        out.append(CheckResult("快照", Status.FAIL, _one_line(e)))

    # 5 市场状态（vendor § Get Market Status of Securities）
    try:
        states = sess.market_states([code])
        if code in states:
            out.append(CheckResult("市场状态", Status.PASS, f"{code} {states[code]}"))
        else:
            out.append(CheckResult("市场状态", Status.FAIL, f"{code} 未返回市场状态"))
    except BrokerError as e:
        out.append(CheckResult("市场状态", Status.FAIL, _one_line(e)))

    return out


# ------------------------------------------------------------------ 6：secrets


def check_secrets(path: Path) -> CheckResult:
    try:
        sec = load_secrets(path)
    except ConfigUnreadable:
        return CheckResult("secrets", Status.WARN, f"无权读取 {path}（在 Claude Code 的 sandbox 内属预期）")
    except ConfigError as e:
        return CheckResult("secrets", Status.FAIL, _one_line(e))
    real = "已填" if sec.real_acc_id is not None else "未填（REAL 之前需要）"
    return CheckResult("secrets", Status.PASS, f"{path}：权限 600，可解析，必填项齐全；real_acc_id {real}")


# ------------------------------------------------------------------ 7：数据库


def check_database(db_cfg: DbCfg, db_local_path: Path) -> list[CheckResult]:
    """以 SIMULATE 角色连接（只读 db.local.toml，不读 secrets）。"""
    where = describe_db(db_cfg, Env.SIMULATE)
    try:
        dsn = dsn_for(Env.SIMULATE, db_cfg, load_db_local(db_local_path), None)
    except ConfigError as e:
        return [CheckResult("数据库连接", Status.FAIL, _one_line(e))]
    try:
        conn = psycopg.connect(dsn, connect_timeout=DB_CONNECT_TIMEOUT, autocommit=True)
    except psycopg.OperationalError as e:
        return [CheckResult("数据库连接", Status.FAIL, f"无法连接 {where}：{_one_line(e)}")]

    out: list[CheckResult] = []
    with conn:
        row = conn.execute("SELECT current_setting('server_version'), current_user").fetchone()
        out.append(CheckResult("数据库连接", Status.PASS, f"{where}，PostgreSQL {row[0]}，当前角色 {row[1]}"))

        # ④ 当前角色不得访问 real schema（Q20 数据库层面的第二道隔离）
        try:
            real_usage = conn.execute("SELECT has_schema_privilege('real', 'USAGE')").fetchone()[0]
            own_usage = conn.execute("SELECT has_schema_privilege('simulate', 'USAGE')").fetchone()[0]
        except pg_errors.InvalidSchemaName as e:
            out.append(CheckResult("schema 隔离", Status.FAIL, f"schema simulate 或 real 不存在：{_one_line(e)}"))
        else:
            if real_usage:
                out.append(CheckResult("schema 隔离", Status.FAIL, f"{row[1]} 对 schema real 有 USAGE 权限"))
            elif not own_usage:
                out.append(CheckResult("schema 隔离", Status.FAIL, f"{row[1]} 对 schema simulate 没有 USAGE 权限"))
            else:
                try:
                    conn.execute("SELECT 1 FROM real.schema_migrations LIMIT 1")
                    out.append(CheckResult("schema 隔离", Status.FAIL, f"{row[1]} 能查询 schema real"))
                except pg_errors.InsufficientPrivilege:
                    out.append(
                        CheckResult("schema 隔离", Status.PASS, f"{row[1]} 查询 schema real 被拒（permission denied）")
                    )
                except pg_errors.UndefinedTable:
                    # 无 USAGE 时 PostgreSQL 先报权限错；走到这里说明能看见 schema 但表还没建
                    out.append(CheckResult("schema 隔离", Status.FAIL, f"{row[1]} 能看见 schema real"))

        # ⑤ listen_addresses 只含回环地址
        try:
            listen = conn.execute("SHOW listen_addresses").fetchone()[0]
        except pg_errors.InsufficientPrivilege:
            out.append(CheckResult("listen_addresses", Status.SKIP, f"{row[1]} 无权读取 listen_addresses，跳过"))
        else:
            addrs = {a.strip() for a in listen.split(",") if a.strip()}
            bad = sorted(addrs - LOOPBACK_LISTEN)
            if bad or not addrs:
                out.append(CheckResult("listen_addresses", Status.FAIL, f"{listen!r} 含非回环地址：{', '.join(bad) or '空'}"))
            else:
                out.append(CheckResult("listen_addresses", Status.PASS, f"{listen!r}，只含回环地址"))
    return out


# ------------------------------------------------------------------ 8：端口未暴露


class _CannotCheck(Exception):
    """sandbox 禁止网络操作，无法完成检查。"""


def lan_addresses() -> tuple[list[str], list[str]]:
    """本机非回环 IPv4 地址，以及枚举过程中遇到的问题（写进检查详情，不吞）。

    用 UDP「connect」取出口地址（不发包），再加上主机名解析到的地址。只能确认端口是否绑定在
    网卡上；从外网能否访问取决于路由器，不在检查范围内（SPEC 8 节）。
    """
    found: set[str] = set()
    notes: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1，不会真的发出数据
            found.add(s.getsockname()[0])
    except PermissionError as e:
        raise _CannotCheck(f"无法枚举本机地址（{e.strerror}）") from e
    except OSError as e:
        notes.append(f"取出口地址失败（{e.strerror or e}），可能没有默认路由")
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(str(info[4][0]))
    except socket.gaierror as e:
        notes.append(f"主机名解析失败（{e.strerror or e}）")
    addrs = sorted(a for a in found if not a.startswith("127.") and a != "0.0.0.0")
    return addrs, notes


def _tcp_connect(host: str, port: int, timeout: float) -> None:
    with socket.create_connection((host, port), timeout=timeout):
        return


def check_exposure(
    db_cfg: DbCfg,
    broker_cfg: BrokerCfg,
    *,
    addresses: Iterable[str] | None = None,
    connect: Callable[[str, int, float], None] | None = None,
) -> list[CheckResult]:
    connect = connect or _tcp_connect
    ports = [("PostgreSQL", db_cfg.port), ("OpenD", broker_cfg.port)]
    notes: list[str] = []
    try:
        if addresses is not None:
            addrs = list(addresses)
        else:
            addrs, notes = lan_addresses()
    except _CannotCheck as e:
        return [CheckResult(f"{n} 端口 {p} 未暴露", Status.SKIP, f"无法检查：{e}（sandbox 内属预期）") for n, p in ports]

    out: list[CheckResult] = []
    for name, port in ports:
        check = f"{name} 端口 {port} 未暴露"
        if not addrs:
            why = "；".join(notes) or "本机没有非回环 IPv4 地址"
            out.append(CheckResult(check, Status.SKIP, f"未找到本机非回环地址，无法检查（{why}）"))
            continue
        exposed, cannot, closed = [], [], []
        for addr in addrs:
            try:
                connect(addr, port, EXPOSURE_TIMEOUT)
            except PermissionError as e:  # sandbox 禁止 TCP（ADR-0015 第 3 条）
                cannot.append(f"{addr}（{e.strerror or e}）")
            except OSError:  # 拒绝、超时、不可达：都说明没有暴露
                closed.append(addr)
            else:
                exposed.append(addr)
        if exposed:
            out.append(
                CheckResult(check, Status.FAIL, f"能从 {', '.join(exposed)} 连上 {port}：监听了非回环地址，请改为只监听 127.0.0.1")
            )
        elif cannot:
            out.append(CheckResult(check, Status.SKIP, f"无法检查：{'; '.join(cannot)}（sandbox 内属预期）"))
        else:
            out.append(CheckResult(check, Status.PASS, f"{', '.join(closed)} 连 {port} 失败"))
    return out


# ------------------------------------------------------------------ 汇总


def run_doctor(
    settings: Settings,
    broker_cfg: BrokerCfg,
    *,
    opend_factory: OpenDFactory,
    secrets_path: Path,
    db_check: Callable[[], list[CheckResult]],
    exposure_check: Callable[[], list[CheckResult]],
    timeout: float = OPEND_TIMEOUT,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    try:
        sess = opend_factory(broker_cfg, timeout)
    except BrokerUnavailable as e:
        results.append(
            CheckResult(
                "OpenD",
                Status.FAIL,
                f"无法连接 OpenD {broker_cfg.host}:{broker_cfg.port}（{timeout:g} 秒超时），请确认 OpenD 已启动"
                f"；原因：{_one_line(e)}",
            )
        )
    else:
        try:
            results += _opend_checks(sess, settings, broker_cfg)
        finally:
            sess.close()
    results.append(check_secrets(secrets_path))
    results += db_check()
    results += exposure_check()
    return results


def render(results: list[CheckResult]) -> str:
    return "\n".join(f"[{r.status}] {r.name}：{r.detail}" for r in results)


def exit_code(results: list[CheckResult]) -> int:
    return 1 if any(r.status is Status.FAIL for r in results) else 0
