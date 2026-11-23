"""命令行入口 `mooapp`（SPEC 5.10）。

配置锁：环境只来自命令行参数，默认 SIMULATE；不读配置文件或环境变量（SPEC 2.2）。
人锁：选择 REAL 时，在执行任何子命令、建立任何 OpenD 连接之前调用 require_human_for_real。
依赖经 `deps` 注入，测试用 dataclasses.replace 换成假的。
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import typer

from mooapp.broker import opend
from mooapp.common import Env
from mooapp.config.loader import (
    DEFAULT_SECRETS,
    ConfigError,
    load_broker_cfg,
    load_db_cfg,
    load_settings,
)
from mooapp.config.models import BrokerCfg, DbCfg
from mooapp.ops import doctor as doctor_mod
from mooapp.ops import humanlock

EXIT_CONFIG = 2
EXIT_HUMAN_LOCK = 3


@dataclass(frozen=True)
class Deps:
    opend_factory: doctor_mod.OpenDFactory = opend.connect
    secrets_path: Path = DEFAULT_SECRETS
    config_dir: Path = field(default_factory=lambda: Path("config"))
    db_check: Callable[[DbCfg, Path], list[doctor_mod.CheckResult]] = doctor_mod.check_database
    exposure_check: Callable[[DbCfg, BrokerCfg], list[doctor_mod.CheckResult]] = doctor_mod.check_exposure


deps = Deps()

app = typer.Typer(
    name="mooapp",
    help="mooapp：Moomoo OpenAPI 学习项目。默认 SIMULATE。",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    add_completion=False,
)


@app.callback()
def main(
    ctx: typer.Context,
    env: Annotated[
        Env,
        typer.Option("--env", case_sensitive=False, help="交易环境；默认 SIMULATE。REAL 需要通过人锁。"),
    ] = Env.SIMULATE,
) -> None:
    if env is Env.REAL:
        try:
            # 经模块属性调用，便于测试替换；getpass.getpass 在调用时取，M1 不会真的调用它
            humanlock.require_human_for_real(sys.stdin, sys.stdout, getpass.getpass)
        except humanlock.HumanLockError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(EXIT_HUMAN_LOCK) from None
    ctx.obj = env


@app.command()
def doctor(ctx: typer.Context) -> None:
    """连通检查：OpenD 与 SDK 版本、账户（后 4 位）、模拟资金、快照、市场状态、secrets、数据库、端口。"""
    d = deps
    try:
        settings = load_settings(d.config_dir / "settings.toml")
        broker_cfg = load_broker_cfg(d.config_dir / "broker.toml")
        db_cfg = load_db_cfg(d.config_dir / "db.toml")
    except ConfigError as e:
        typer.echo(f"配置错误：{e}", err=True)
        raise typer.Exit(EXIT_CONFIG) from None

    results = doctor_mod.run_doctor(
        settings,
        broker_cfg,
        opend_factory=d.opend_factory,
        secrets_path=d.secrets_path,
        db_check=lambda: d.db_check(db_cfg, d.config_dir / "db.local.toml"),
        exposure_check=lambda: d.exposure_check(db_cfg, broker_cfg),
        verified=_verified_or_none(d.config_dir / "verified_versions.toml"),
    )
    typer.echo(doctor_mod.render(results))
    raise typer.Exit(doctor_mod.exit_code(results))


# ------------------------------------------------------------------ M3：数据库与订单（只读）

db_app = typer.Typer(help="数据库初始化（SPEC 第 4 节）。", no_args_is_help=True)
order_app = typer.Typer(help="订单：list 只读；buy / sell / cancel 写入命令表，由 mooapp run 经闸门执行（SPEC 3.4）。", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(order_app, name="order")

_SCHEMA = {Env.SIMULATE: "simulate", Env.REAL: "real"}


def _dsn(env: Env) -> str:
    """SIMULATE 只读 db.local.toml；REAL 只读 secrets（dsn_for）。DSN 含密码，不输出。"""
    from mooapp.config.loader import dsn_for, load_db_local, load_secrets

    d = deps
    db = load_db_cfg(d.config_dir / "db.toml")
    if env is Env.SIMULATE:
        return dsn_for(env, db, load_db_local(d.config_dir / "db.local.toml"), None)
    return dsn_for(env, db, None, load_secrets(d.secrets_path))


@db_app.command("migrate")
def db_migrate(ctx: typer.Context) -> None:
    """在当前环境的 schema 里执行 db/ 下的迁移、视图、函数、授权（只执行未执行过的迁移）。"""
    import psycopg

    from mooapp.config.loader import describe_db
    from mooapp.oms.migrate import migrate

    env: Env = ctx.obj
    try:
        dsn = _dsn(env)
        target = describe_db(load_db_cfg(deps.config_dir / "db.toml"), env)
    except ConfigError as e:
        typer.echo(f"配置错误：{e}", err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    with psycopg.connect(dsn, autocommit=True) as conn:
        applied = migrate(conn, _SCHEMA[env], Path("db"))
    typer.echo(f"{target} schema {_SCHEMA[env]}：新执行迁移 {applied or '无'}")


@order_app.command("list")
def order_list(ctx: typer.Context, all_: Annotated[bool, typer.Option("--all", help="包含已结束的订单")] = False) -> None:
    """列出本地订单；Moomoo 状态映射不了的标出「⚠ 映射不了」（docs/status-mapping.md）。"""
    from mooapp.oms.mapping import UNMAPPED_STATUSES, VENDOR_STATUSES
    from mooapp.oms.pg_store import PostgresStore

    env: Env = ctx.obj
    try:
        dsn = _dsn(env)
    except ConfigError as e:
        typer.echo(f"配置错误：{e}", err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    other = _SCHEMA[Env.REAL if env is Env.SIMULATE else Env.SIMULATE]
    store = PostgresStore(dsn, schema=_SCHEMA[env], other_schema=other)
    try:
        orders = store.list_orders(open_only=not all_)
    finally:
        store.close()
    if not orders:
        typer.echo("（没有订单）")
        return
    typer.echo("client_order_id  broker_id  symbol    side  qty   status            cum   avg        moomoo")
    for o in orders:
        flag = ""
        if o.raw_status is not None and (o.raw_status in UNMAPPED_STATUSES or o.raw_status not in VENDOR_STATUSES):
            flag = "  ⚠ 映射不了"
        typer.echo(
            f"{o.client_order_id[:14]}…  {o.broker_order_id or '-':9}  {o.symbol:8}  {o.side.value:4}  {o.qty:<4}  "
            f"{o.status.value:16}  {o.cum_qty:<4}  {o.avg_px!s:9}  {o.raw_status or '-'}{flag}"
        )


# ------------------------------------------------------------------ M4：命令表与常驻进程（SPEC 3.4）

EXIT_COMMAND = 4
EXIT_BROKER = 6  # mooapp run 连不上 OpenD（5 留给 M7 的 EXIT_BREAK）
COMMAND_TIMEOUT = 30.0


def _client(env: Env):  # type: ignore[no-untyped-def]
    """命令行进程只连数据库、写命令表；不构造闸门、OMS，也不连 OpenD。"""
    from mooapp.clock import SystemClock
    from mooapp.oms.pg_store import PostgresStore
    from mooapp.ops.commands import CommandClient

    other = _SCHEMA[Env.REAL if env is Env.SIMULATE else Env.SIMULATE]
    store = PostgresStore(_dsn(env), schema=_SCHEMA[env], other_schema=other)
    return store, CommandClient(store=store, clock=SystemClock(), timeout=COMMAND_TIMEOUT)


def _submit(ctx: typer.Context, kind: str, payload: dict, actor: str = "cli") -> dict:  # type: ignore[type-arg]
    from mooapp.ops.commands import CommandTimeout, RunnerNotRunning

    env: Env = ctx.obj
    try:
        store, client = _client(env)
    except ConfigError as e:
        typer.echo(f"配置错误：{e}", err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    try:
        return client.submit(kind, payload, actor=actor)
    except (RunnerNotRunning, CommandTimeout, RuntimeError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(EXIT_COMMAND) from None
    finally:
        store.close()


def _exit_if_expired(r: dict) -> None:  # type: ignore[type-arg]
    """常驻进程取出命令时已过了命令行的截止时间（M4 审查 #7、ADR-0030）：没有执行，不是闸门拒绝。"""
    if r.get("rule") == "expired":
        typer.echo(f"命令已过期，未执行：{r.get('reason')}。请查询后再决定是否重发", err=True)
        raise typer.Exit(EXIT_COMMAND)


def _echo_decision(r: dict) -> None:  # type: ignore[type-arg]
    _exit_if_expired(r)
    if r.get("allowed"):
        typer.echo(f"已受理：{r.get('client_order_id')}（{r.get('status') or r.get('reason')}）")
    else:
        typer.echo(f"闸门拒绝（{r.get('rule')}）：{r.get('reason')}", err=True)
        raise typer.Exit(EXIT_COMMAND)


def _place(ctx: typer.Context, side: str, symbol: str, qty: int, limit: str | None) -> None:
    from decimal import Decimal, InvalidOperation

    from mooapp.ops.commands import CommandClient

    try:
        price = Decimal(limit) if limit is not None else None
    except InvalidOperation:
        typer.echo(f"限价不是数字：{limit}", err=True)
        raise typer.Exit(EXIT_COMMAND) from None
    _echo_decision(_submit(ctx, "place", CommandClient.place_payload(symbol, side, qty, price)))


@order_app.command("buy")
def order_buy(
    ctx: typer.Context,
    symbol: str,
    qty: int,
    limit: Annotated[str | None, typer.Option("--limit", help="限价；不给则为市价单")] = None,
) -> None:
    """买入（经闸门；常驻进程 mooapp run 必须在运行）。"""
    _place(ctx, "BUY", symbol, qty, limit)


@order_app.command("sell")
def order_sell(
    ctx: typer.Context,
    symbol: str,
    qty: int,
    limit: Annotated[str | None, typer.Option("--limit", help="限价；不给则为市价单")] = None,
) -> None:
    """卖出（经闸门；常驻进程 mooapp run 必须在运行）。"""
    _place(ctx, "SELL", symbol, qty, limit)


@order_app.command("cancel")
def order_cancel(ctx: typer.Context, client_order_id: str) -> None:
    """撤单（经闸门；不受限额和 halt 限制，ADR-0016）。"""
    _echo_decision(_submit(ctx, "cancel", {"client_order_id": client_order_id}))


@app.command()
def halt(ctx: typer.Context, reason: Annotated[str, typer.Option("--reason", help="halt 原因")] = "手动 halt") -> None:
    """Kill switch：拒绝一切新单并撤销本程序全部挂单；状态跨重启保留（ADR-0022）。"""
    r = _submit(ctx, "halt", {"reason": reason})
    typer.echo(f"已 halt。已发撤单 {len(r['cancel_sent'])} 张，待确认后撤 {len(r['deferred'])} 张，撤单失败 {len(r['failed'])} 张")
    for cid, err in r["failed"].items():
        typer.echo(f"  撤单失败 {cid}：{err}", err=True)
    if r["failed"]:
        raise typer.Exit(EXIT_COMMAND)


@app.command()
def resume(ctx: typer.Context) -> None:
    """解除 halt：只能在交互终端，原样输入随机确认码（ADR-0022、Q10）。"""
    try:
        humanlock.confirm_interactively(sys.stdin, sys.stdout, input, None, "解除 halt")
    except humanlock.HumanLockError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(EXIT_HUMAN_LOCK) from None
    from datetime import UTC, datetime

    from mooapp.config.loader import load_secrets
    from mooapp.ops.resume_auth import resume_payload

    try:
        key = load_secrets(deps.secrets_path).resume_key
    except ConfigError as e:
        typer.echo(f"读不到 secrets：{e}", err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    if key is None or not key.get_secret_value():
        typer.echo("secrets 里没有 [resume] key（ADR-0029），无法签名 resume", err=True)
        raise typer.Exit(EXIT_CONFIG)
    try:
        raw = bytes.fromhex(key.get_secret_value())
    except ValueError:
        typer.echo("[resume] key 不是十六进制字符串", err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    _submit(ctx, "resume", resume_payload(raw, datetime.now(UTC)))
    typer.echo("已解除 halt")


@app.command()
def run(ctx: typer.Context) -> None:
    """常驻进程：持有闸门、OMS 和券商连接，处理命令表（SPEC 3.4）。Ctrl-C 退出。"""
    import signal
    import threading

    from mooapp.broker.port import BrokerUnavailable
    from mooapp.ops.run import RealDisabled, RunnerAlreadyRunning, build_runtime

    env: Env = ctx.obj
    try:
        rt = build_runtime(env, deps.config_dir, record_dir=Path("recordings/pushes"))
    except (ConfigError, RealDisabled) as e:
        typer.echo(f"无法启动：{e}", err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    except BrokerUnavailable as e:  # OpenD 未启动或未就绪：一行说明即可，调用栈没有信息量（由外层调度重试）
        typer.echo(f"无法连接 OpenD：{e}", err=True)
        raise typer.Exit(EXIT_BROKER) from None
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    try:
        try:
            rt.start()
        except RunnerAlreadyRunning as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(EXIT_COMMAND) from None
        typer.echo(f"mooapp run 已启动（{env.value}）；halt={'是' if rt.gate.halted else '否'}。Ctrl-C 退出")
        try:
            while not stopped.wait(1.0):
                pass
        except KeyboardInterrupt:
            typer.echo("收到 Ctrl-C，正在退出")
    finally:
        rt.stop()


# ------------------------------------------------------------------ M6a：规则文件检查与 dry-run（纯本地，不连 OpenD、不连数据库）

rule_app = typer.Typer(
    help="止损止盈规则（M6）：check 校验；dry-run 回放；add / cancel 经常驻进程执行；list 只读。", no_args_is_help=True
)
app.add_typer(rule_app, name="rule")


@rule_app.command("check")
def rule_check(path: Path) -> None:
    """校验规则文件（YAML + JSON Schema + 表达式白名单），不建规则。"""
    from mooapp.strategy.spec import RuleSpecError, load_rule_file

    try:
        spec = load_rule_file(path)
    except RuleSpecError as e:
        typer.echo(f"规则不合法：{e}", err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    typer.echo(
        f"规则 {spec.id} 合法：{spec.symbol} 限价买 {spec.entry.qty} 股 @{spec.entry.limit_price}；"
        f"止损 {spec.stop_src}；止盈 {spec.take_src}"
    )


@rule_app.command("dry-run")
def rule_dry_run(path: Path, prices: Annotated[Path, typer.Option("--prices", help="价格文件 CSV：ts,type,a,b")]) -> None:
    """用价格文件回放规则，打印何时触发什么（ADR-0005、0007、0008、0009、0027）。"""
    from mooapp.strategy.dryrun import DryRunError, dry_run, format_events, parse_prices
    from mooapp.strategy.spec import RuleSpecError, load_rule_file

    try:
        spec = load_rule_file(path)
        rows = parse_prices(prices.read_text(encoding="utf-8"))
    except (RuleSpecError, DryRunError) as e:
        typer.echo(f"输入不合法：{e}", err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    except OSError as e:
        typer.echo(f"读不了价格文件 {prices}：{e.strerror or e}", err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    typer.echo(format_events(dry_run(spec, rows)))


@rule_app.command("add")
def rule_add(ctx: typer.Context, path: Path) -> None:
    """添加规则：先在本地校验，再写入命令表，由 mooapp run 下入场单并开始监控（M6b）。"""
    from mooapp.strategy.spec import RuleSpecError, load_rule

    try:
        text = path.read_text(encoding="utf-8")
        load_rule(text)
    except RuleSpecError as e:
        typer.echo(f"规则不合法：{e}", err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    except OSError as e:
        typer.echo(f"读不了规则文件 {path}：{e.strerror or e}", err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    r = _submit(ctx, "rule_add", {"yaml": text})
    _exit_if_expired(r)
    if r.get("allowed"):
        typer.echo(f"规则 {r['rule_id']} 已添加，入场单 {r['entry_cid']}")
    else:
        typer.echo(f"规则未添加：{r.get('reason')}", err=True)
        raise typer.Exit(EXIT_COMMAND)


@rule_app.command("cancel")
def rule_cancel(ctx: typer.Context, rule_id: str) -> None:
    """取消规则：撤入场单余量、停止监控；还有持仓时提示不再受保护（M6b 默认做法 3）。"""
    r = _submit(ctx, "rule_cancel", {"rule_id": rule_id})
    _exit_if_expired(r)
    typer.echo(f"规则 {rule_id}：{r.get('status')}")
    if r.get("unprotected_qty"):
        typer.echo(f"注意：持仓 {r['unprotected_qty']} 股不再受止损止盈保护", err=True)


@rule_app.command("list")
def rule_list(ctx: typer.Context, all_: Annotated[bool, typer.Option("--all", help="包含已结束的规则")] = False) -> None:
    """列出规则（只读，直接读数据库）。"""
    from mooapp.oms.pg_store import PostgresStore

    env: Env = ctx.obj
    try:
        dsn = _dsn(env)
    except ConfigError as e:
        typer.echo(f"配置错误：{e}", err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    other = _SCHEMA[Env.REAL if env is Env.SIMULATE else Env.SIMULATE]
    store = PostgresStore(dsn, schema=_SCHEMA[env], other_schema=other)
    try:
        rules = store.list_rules(active_only=not all_)
    finally:
        store.close()
    if not rules:
        typer.echo("（没有规则）")
        return
    for r in rules:
        st = r.state
        typer.echo(
            f"{r.id:<20} {r.symbol:<8} {r.status:<9} 止损 {st.get('stop_price') or '-':<8} 止盈 {st.get('take_price') or '-':<8} "
            f"买 {st.get('bought', 0)} 卖 {st.get('sold', 0)}"
        )


def _verified_or_none(path: Path):  # type: ignore[no-untyped-def]
    """ADR-0035：读不到 verified_versions.toml 时返回 None（doctor 显示 WARN，不失败）。"""
    from mooapp.config.loader import load_verified_versions

    try:
        return load_verified_versions(path)
    except ConfigError:
        return None


# ------------------------------------------------------------------ M7：收盘 P&L 与对账（只读；PROGRESS「M7 计划」）

EXIT_BREAK = 5
REPORTS_DIR = Path("reports")  # 已被 .gitignore 忽略（Q15）


def _eod_runtime(env: Env):  # type: ignore[no-untyped-def]
    """M7 决定 1：命令行进程直接连库、连 OpenD 做只读查询，不经常驻进程和闸门。返回 (store, broker, clock, closers)。"""
    from mooapp.broker.moomoo import open_broker
    from mooapp.clock import SystemClock
    from mooapp.oms.pg_store import PostgresStore

    other = _SCHEMA[Env.REAL if env is Env.SIMULATE else Env.SIMULATE]
    store = PostgresStore(_dsn(env), schema=_SCHEMA[env], other_schema=other)
    clock = SystemClock()
    try:
        broker = open_broker(env, load_broker_cfg(deps.config_dir / "broker.toml"), clock)
    except BaseException:
        store.close()
        raise
    return store, broker, clock, [broker.close, store.close]


@app.command()
def eod(
    ctx: typer.Context,
    date_: Annotated[str | None, typer.Option("--date", help="交易日 YYYY-MM-DD（美东）；默认今天，只支持最近两个已完成的交易日")] = None,
) -> None:
    """收盘 P&L 与三方对账：收盘后 30 分钟起运行；写 reports/YYYY-MM-DD.csv 和 eod_reports；有差异时退出码非 0。"""
    from datetime import date as _date

    from mooapp.broker.port import BrokerError
    from mooapp.eod.report import render_text, write_csv
    from mooapp.eod.run import EodError, run_eod, save_report

    env: Env = ctx.obj
    try:
        requested = _date.fromisoformat(date_) if date_ else None
    except ValueError:
        typer.echo(f"日期格式不对：{date_!r}（应为 YYYY-MM-DD）", err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    try:
        store, broker, clock, closers = _eod_runtime(env)
    except (ConfigError, BrokerError) as e:
        typer.echo(f"无法连接：{e}", err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    try:
        try:
            report = run_eod(requested, store=store, broker=broker, md=broker, clock=clock, env=env)
        except EodError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(EXIT_CONFIG) from None
        path = write_csv(report, REPORTS_DIR)
        save_report(store, report)
        typer.echo(render_text(report))
        typer.echo(f"报告：{path}")
    finally:
        first: BaseException | None = None
        for c in closers:
            try:
                c()
            except BaseException as e:  # 关闭出错也要把其余连接关掉；最后原样抛出第一个错误（不吞）
                first = first or e
        if first is not None:
            raise first
    if not report.ok:
        raise typer.Exit(EXIT_BREAK)
