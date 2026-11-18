"""读取并校验配置文件（SPEC 5.2、4 节、Q14、Q20）。

所有失败都抛 ConfigError，信息为一行、可读、含文件路径与字段名；
不带 pydantic 的 input_value，避免口令出现在报错里。
"""

from __future__ import annotations

import stat
import tomllib
from pathlib import Path
from typing import TypeVar

from psycopg.conninfo import make_conninfo
from pydantic import BaseModel, ValidationError

from mooapp.common import Env, MooappError
from mooapp.config.models import (
    BrokerCfg,
    DbCfg,
    DbLocal,
    RiskLimits,
    Secrets,
    Settings,
    VerifiedVersions,
)

DEFAULT_SETTINGS = Path("config/settings.toml")
DEFAULT_BROKER = Path("config/broker.toml")
DEFAULT_DB = Path("config/db.toml")
DEFAULT_DB_LOCAL = Path("config/db.local.toml")
DEFAULT_VERIFIED = Path("config/verified_versions.toml")
DEFAULT_SECRETS = Path.home() / ".config" / "mooapp" / "secrets.toml"

LOOPBACK_TCP_HOST = "127.0.0.1"

M = TypeVar("M", bound=BaseModel)


class ConfigError(MooappError):
    """缺文件、缺项、数值非法时抛出；信息一行、可读。"""


class ConfigUnreadable(ConfigError):
    """文件存在但无权读取（secrets 在 sandbox 内属预期，SPEC 8 节 M1 实现说明）。"""


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _read_toml(path: Path) -> dict:
    # 不用 path.exists()：它把 PermissionError 也当成「不存在」返回 False，
    # 在 sandbox 里会把「无权读取」误报成「不存在」。直接读，按异常类型区分。
    try:
        raw = path.read_bytes()
    except FileNotFoundError as e:
        raise ConfigError(f"配置文件不存在：{path}") from e
    except PermissionError as e:
        raise ConfigUnreadable(f"无权读取 {path}（{e.strerror}）") from e
    except OSError as e:
        raise ConfigError(f"无法读取 {path}：{e.strerror or e}") from e
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}：TOML 解析失败：{_one_line(e)}") from e


def _validate(model: type[M], data: dict, path: Path, prefix: str = "") -> M:
    try:
        return model.model_validate(data)
    except ValidationError as e:
        parts = []
        for err in e.errors(include_input=False, include_url=False):
            loc = ".".join(str(x) for x in (prefix, *err["loc"]) if x != "")
            parts.append(f"{loc}：{_one_line(err['msg'])}" if loc else _one_line(err["msg"]))
        raise ConfigError(f"{path}：" + "；".join(parts)) from None


def load_settings(path: Path = DEFAULT_SETTINGS) -> Settings:
    return _validate(Settings, _read_toml(path), path)


def load_verified_versions(path: Path = DEFAULT_VERIFIED) -> VerifiedVersions:
    """ADR-0035：已验证的 OpenD 与 SDK 版本。"""
    return _validate(VerifiedVersions, _read_toml(path), path)


def load_broker_cfg(path: Path = DEFAULT_BROKER) -> BrokerCfg:
    raw = _read_toml(path)
    if not isinstance(raw.get("opend"), dict):
        raise ConfigError(f"{path}：缺少 [opend] 段")
    return _validate(BrokerCfg, raw["opend"], path, prefix="opend")


def load_db_cfg(path: Path = DEFAULT_DB) -> DbCfg:
    raw = _read_toml(path)
    if not isinstance(raw.get("postgres"), dict):
        raise ConfigError(f"{path}：缺少 [postgres] 段")
    return _validate(DbCfg, raw["postgres"], path, prefix="postgres")


def load_db_local(path: Path = DEFAULT_DB_LOCAL) -> DbLocal:
    return _validate(DbLocal, _read_toml(path), path)


def load_secrets(path: Path = DEFAULT_SECRETS) -> Secrets:
    """检查：存在、可读、权限 600（组和其他用户无任何权限）、可解析、必填项齐全。"""
    raw = _read_toml(path)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(f"{path}：权限为 {mode:o}，必须是 600（运行 chmod 600 {path}）")
    return _validate(Secrets, raw, path)


def limits_for(settings: Settings, env: Env) -> RiskLimits:
    """限额锁：REAL 用更严的 [risk.real]（CLAUDE.md 实盘三把锁）。"""
    return settings.risk_real if env is Env.REAL else settings.risk_simulate


def acc_id_for(env: Env, broker: BrokerCfg, secrets: Secrets | None) -> int:
    """SIMULATE 只用 broker.toml，不读 secrets；REAL 只用 secrets（Q14）。"""
    if env is Env.SIMULATE:
        return broker.simulate_acc_id
    if secrets is None:
        raise ConfigError("REAL 需要 secrets 文件中的 [accounts] real_acc_id，但未加载 secrets")
    if secrets.real_acc_id is None:
        raise ConfigError("secrets 文件缺少 [accounts] real_acc_id（0 视为未填）")
    return secrets.real_acc_id


def check_db_host(host: str) -> None:
    """ADR-0014、Q20：只能是 unix socket 目录（绝对路径）或 127.0.0.1。"""
    if host == LOOPBACK_TCP_HOST or host.startswith("/"):
        return
    raise ConfigError(
        f"数据库 host 只能是 unix socket 目录（绝对路径）或 {LOOPBACK_TCP_HOST}，当前为 {host!r}（ADR-0014）"
    )


def dsn_for(env: Env, db: DbCfg, local: DbLocal | None, secrets: Secrets | None) -> str:
    """SIMULATE 只读 db.local.toml，REAL 只读 secrets。DSN 含密码，不得写日志。"""
    check_db_host(db.host)
    if env is Env.SIMULATE:
        if local is None:
            raise ConfigError("SIMULATE 需要 config/db.local.toml 中的 [postgres] sim_password")
        role, password = db.sim_role, local.sim_password
    else:
        if secrets is None or secrets.real_password is None:
            raise ConfigError("REAL 需要 secrets 文件中的 [postgres] real_password")
        role, password = db.real_role, secrets.real_password
    return make_conninfo(
        host=db.host,
        port=str(db.port),
        dbname=db.dbname,
        user=role,
        password=password.get_secret_value(),
    )


def describe_db(db: DbCfg, env: Env) -> str:
    """可写日志的数据库描述：只有 host、port、库名、角色。"""
    role = db.real_role if env is Env.REAL else db.sim_role
    return f"{db.host}:{db.port}/{db.dbname}（角色 {role}）"


__all__ = [
    "ConfigError",
    "ConfigUnreadable",
    "acc_id_for",
    "check_db_host",
    "describe_db",
    "dsn_for",
    "limits_for",
    "load_broker_cfg",
    "load_db_cfg",
    "load_db_local",
    "load_secrets",
    "load_settings",
]
