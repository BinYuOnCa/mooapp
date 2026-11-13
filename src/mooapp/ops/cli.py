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
