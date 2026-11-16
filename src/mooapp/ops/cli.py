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
    )
    typer.echo(doctor_mod.render(results))
    raise typer.Exit(doctor_mod.exit_code(results))


# ------------------------------------------------------------------ M3：数据库与订单（只读）

db_app = typer.Typer(help="数据库初始化（SPEC 第 4 节）。", no_args_is_help=True)
order_app = typer.Typer(help="订单（M3 只读；下单、撤单在 M4 经闸门）。", no_args_is_help=True)
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
