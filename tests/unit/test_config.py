"""M1 验收 2 相关：配置加载与校验（SPEC 5.2、Q14、Q20）。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict

from conftest import FAKE_REAL_PW, FAKE_SIM_PW, FAKE_SMTP_PW, write
from mooapp.common import Env, MooappError, mask_account
from mooapp.config.loader import (
    ConfigError,
    acc_id_for,
    dsn_for,
    limits_for,
    load_broker_cfg,
    load_db_cfg,
    load_db_local,
    load_secrets,
    load_settings,
)
from mooapp.config.models import DbCfg

REPO = Path(__file__).resolve().parents[2]


def assert_readable(err: ConfigError) -> None:
    msg = str(err)
    assert msg.strip(), "报错信息不能为空"
    assert "\n" not in msg, f"报错应为一行：{msg!r}"
    assert "Traceback" not in msg


# ------------------------------------------------------------------ 共享


def test_mask_account_keeps_last_four():
    assert mask_account(2964165) == "****4165"
    assert mask_account("281756470009876") == "****9876"
    assert "2964165" not in mask_account(2964165)


def test_config_error_is_mooapp_error():
    assert issubclass(ConfigError, MooappError)


def test_env_values():
    assert Env.SIMULATE.value == "SIMULATE"
    assert Env.REAL.value == "REAL"


# ------------------------------------------------------------------ 正常加载


def test_load_all_ok(cfg_dir: Path):
    s = load_settings(cfg_dir / "settings.toml")
    b = load_broker_cfg(cfg_dir / "broker.toml")
    db = load_db_cfg(cfg_dir / "db.toml")
    local = load_db_local(cfg_dir / "db.local.toml")
    sec = load_secrets(cfg_dir / "secrets.toml")

    assert s.risk_simulate.whitelist == ("US.SPY", "US.QQQ")  # 保持文件顺序：doctor 取第一只
    assert s.risk_real.max_order_value == Decimal("100.0")
    assert s.agent.auto_approve_simulate is False
    assert b.host == "127.0.0.1" and b.port == 11111 and b.simulate_acc_id == 2964165
    assert db.host == "/tmp" and db.sim_role == "mooapp_sim"
    assert local.sim_password.get_secret_value() == FAKE_SIM_PW
    assert sec.real_acc_id == 281756470009876
    assert sec.real_password.get_secret_value() == FAKE_REAL_PW


def test_repo_config_files_load():
    """仓库里可提交的配置本身必须合法。"""
    load_settings(REPO / "config/settings.toml")
    load_broker_cfg(REPO / "config/broker.toml")
    load_db_cfg(REPO / "config/db.toml")


def test_limits_for(cfg_dir: Path):
    s = load_settings(cfg_dir / "settings.toml")
    assert limits_for(s, Env.SIMULATE).max_order_qty == 100
    assert limits_for(s, Env.REAL).max_order_qty == 1
    assert limits_for(s, Env.REAL).whitelist == ("US.SCHD",)


# ------------------------------------------------------------------ 可读报错


def test_missing_file_readable(tmp_path: Path):
    p = tmp_path / "nope.toml"
    with pytest.raises(ConfigError) as ei:
        load_settings(p)
    assert_readable(ei.value)
    assert str(p) in str(ei.value)


def test_bad_toml_readable(tmp_path: Path):
    p = write(tmp_path / "settings.toml", "[risk.simulate\nx = ")
    with pytest.raises(ConfigError) as ei:
        load_settings(p)
    assert_readable(ei.value)
    assert str(p) in str(ei.value)


def test_missing_key_readable(cfg_dir: Path):
    p = cfg_dir / "settings.toml"
    write(p, p.read_text().replace("max_daily_orders = 10\n", ""))
    with pytest.raises(ConfigError) as ei:
        load_settings(p)
    assert_readable(ei.value)
    assert "max_daily_orders" in str(ei.value)


def test_negative_value_readable(cfg_dir: Path):
    p = cfg_dir / "settings.toml"
    write(p, p.read_text().replace("max_order_qty = 100", "max_order_qty = -5"))
    with pytest.raises(ConfigError) as ei:
        load_settings(p)
    assert_readable(ei.value)
    assert "max_order_qty" in str(ei.value)


def test_non_us_symbol_rejected(cfg_dir: Path):
    """ADR-0001：白名单只收 US.*"""
    p = cfg_dir / "settings.toml"
    write(p, p.read_text().replace('"US.QQQ"', '"HK.00700"'))
    with pytest.raises(ConfigError) as ei:
        load_settings(p)
    assert_readable(ei.value)


def test_broker_host_must_be_loopback(cfg_dir: Path):
    """ADR-0014：OpenD 只用本机端口。"""
    p = cfg_dir / "broker.toml"
    write(p, p.read_text().replace("127.0.0.1", "192.168.1.20"))
    with pytest.raises(ConfigError) as ei:
        load_broker_cfg(p)
    assert_readable(ei.value)


def test_broker_missing_simulate_acc_id(cfg_dir: Path):
    p = cfg_dir / "broker.toml"
    write(p, p.read_text().replace("simulate_acc_id = 2964165\n", ""))
    with pytest.raises(ConfigError) as ei:
        load_broker_cfg(p)
    assert_readable(ei.value)
    assert "simulate_acc_id" in str(ei.value)


# ------------------------------------------------------------------ secrets


def test_secrets_permission_must_be_600(cfg_dir: Path):
    p = cfg_dir / "secrets.toml"
    p.chmod(0o644)
    with pytest.raises(ConfigError) as ei:
        load_secrets(p)
    assert_readable(ei.value)
    assert "600" in str(ei.value)


def test_secrets_missing_smtp_section(cfg_dir: Path):
    p = cfg_dir / "secrets.toml"
    text = p.read_text()
    write(p, text[text.index("[opend]"):], 0o600)
    with pytest.raises(ConfigError) as ei:
        load_secrets(p)
    assert_readable(ei.value)
    assert "smtp" in str(ei.value)


def test_secrets_error_does_not_leak_values(cfg_dir: Path):
    """校验失败时 pydantic 会带上 input_value；报错里不得出现口令。"""
    p = cfg_dir / "secrets.toml"
    write(p, p.read_text().replace("port = 465", f'port = "{FAKE_SMTP_PW}"'), 0o600)
    with pytest.raises(ConfigError) as ei:
        load_secrets(p)
    assert_readable(ei.value)
    assert FAKE_SMTP_PW not in str(ei.value)


def test_repr_does_not_leak_passwords(cfg_dir: Path):
    local = load_db_local(cfg_dir / "db.local.toml")
    sec = load_secrets(cfg_dir / "secrets.toml")
    for obj in (local, sec):
        for text in (repr(obj), str(obj)):
            for pw in (FAKE_SIM_PW, FAKE_REAL_PW, FAKE_SMTP_PW):
                assert pw not in text


def test_secrets_placeholder_real_acc_id_is_none(cfg_dir: Path):
    """模板里 real_acc_id = 0 是占位值，视为未填。"""
    p = cfg_dir / "secrets.toml"
    write(p, p.read_text().replace("real_acc_id = 281756470009876", "real_acc_id = 0"), 0o600)
    assert load_secrets(p).real_acc_id is None


# ------------------------------------------------------------------ acc_id_for（Q14）


def test_acc_id_simulate_does_not_need_secrets(cfg_dir: Path):
    b = load_broker_cfg(cfg_dir / "broker.toml")
    assert acc_id_for(Env.SIMULATE, b, None) == 2964165


def test_acc_id_simulate_ignores_secrets(cfg_dir: Path):
    b = load_broker_cfg(cfg_dir / "broker.toml")
    sec = load_secrets(cfg_dir / "secrets.toml")
    assert acc_id_for(Env.SIMULATE, b, sec) == 2964165


def test_acc_id_real_from_secrets(cfg_dir: Path):
    b = load_broker_cfg(cfg_dir / "broker.toml")
    sec = load_secrets(cfg_dir / "secrets.toml")
    assert acc_id_for(Env.REAL, b, sec) == 281756470009876


def test_acc_id_real_without_secrets_errors(cfg_dir: Path):
    b = load_broker_cfg(cfg_dir / "broker.toml")
    with pytest.raises(ConfigError) as ei:
        acc_id_for(Env.REAL, b, None)
    assert_readable(ei.value)


def test_acc_id_real_missing_real_acc_id_errors(cfg_dir: Path):
    p = cfg_dir / "secrets.toml"
    write(p, p.read_text().replace("real_acc_id = 281756470009876", "real_acc_id = 0"), 0o600)
    b = load_broker_cfg(cfg_dir / "broker.toml")
    with pytest.raises(ConfigError) as ei:
        acc_id_for(Env.REAL, b, load_secrets(p))
    assert_readable(ei.value)
    assert "real_acc_id" in str(ei.value)


# ------------------------------------------------------------------ dsn_for（Q20、ADR-0014）


def test_dsn_simulate_uses_local_not_secrets(cfg_dir: Path):
    db = load_db_cfg(cfg_dir / "db.toml")
    local = load_db_local(cfg_dir / "db.local.toml")
    d = conninfo_to_dict(dsn_for(Env.SIMULATE, db, local, None))
    assert d["host"] == "/tmp"
    assert d["user"] == "mooapp_sim"
    assert d["dbname"] == "mooapp"
    assert d["password"] == FAKE_SIM_PW


def test_dsn_simulate_without_local_errors(cfg_dir: Path):
    db = load_db_cfg(cfg_dir / "db.toml")
    with pytest.raises(ConfigError) as ei:
        dsn_for(Env.SIMULATE, db, None, None)
    assert_readable(ei.value)


def test_dsn_real_uses_secrets(cfg_dir: Path):
    db = load_db_cfg(cfg_dir / "db.toml")
    sec = load_secrets(cfg_dir / "secrets.toml")
    d = conninfo_to_dict(dsn_for(Env.REAL, db, None, sec))
    assert d["user"] == "mooapp_real"
    assert d["password"] == FAKE_REAL_PW


def test_dsn_real_without_secrets_errors(cfg_dir: Path):
    db = load_db_cfg(cfg_dir / "db.toml")
    local = load_db_local(cfg_dir / "db.local.toml")
    with pytest.raises(ConfigError) as ei:
        dsn_for(Env.REAL, db, local, None)
    assert_readable(ei.value)


@pytest.mark.parametrize("host", ["/tmp", "/var/run/postgresql", "127.0.0.1"])
def test_dsn_host_allowed(cfg_dir: Path, host: str):
    db = DbCfg(host=host)
    local = load_db_local(cfg_dir / "db.local.toml")
    assert conninfo_to_dict(dsn_for(Env.SIMULATE, db, local, None))["host"] == host


@pytest.mark.parametrize("host", ["localhost", "192.168.1.20", "db.example.com", "0.0.0.0", "tmp"])
def test_dsn_host_rejected(cfg_dir: Path, host: str):
    db = DbCfg(host=host)
    local = load_db_local(cfg_dir / "db.local.toml")
    with pytest.raises(ConfigError) as ei:
        dsn_for(Env.SIMULATE, db, local, None)
    assert_readable(ei.value)
