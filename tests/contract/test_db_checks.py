"""doctor 第 7 项：数据库检查（SPEC 8 节 M1 实现说明 ②④⑤）。

连本机 PostgreSQL（经 unix socket），读仓库里的 config/db.toml 与 config/db.local.toml
（后者被 .gitignore 忽略、sandbox 可读，Q20）。不读 secrets。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import write
from mooapp.config.loader import load_db_cfg
from mooapp.ops import doctor
from mooapp.ops.doctor import Status

pytestmark = pytest.mark.pg

REPO = Path(__file__).resolve().parents[2]
DB_TOML = REPO / "config" / "db.toml"
DB_LOCAL = REPO / "config" / "db.local.toml"


def test_database_checks_pass():
    results = doctor.check_database(load_db_cfg(DB_TOML), DB_LOCAL)
    out = doctor.render(results)
    assert results, "应至少有一项结果"
    assert not [r for r in results if r.status == Status.FAIL], out
    assert "real" in out  # ④ 已检查 mooapp_sim 访问 real schema
    assert "listen_addresses" in out  # ⑤ 检查过，或注明无权限跳过
    for r in results:
        assert "\n" not in r.detail


def test_wrong_password_fails_without_leaking(tmp_path: Path):
    bad_pw = "definitely-wrong-password"
    local = write(tmp_path / "db.local.toml", f'[postgres]\nsim_password = "{bad_pw}"\n', 0o600)
    results = doctor.check_database(load_db_cfg(DB_TOML), local)
    out = doctor.render(results)
    assert [r for r in results if r.status == Status.FAIL], out
    assert bad_pw not in out
    assert "Traceback" not in out


def test_missing_db_local_fails_readably(tmp_path: Path):
    missing = tmp_path / "db.local.toml"
    results = doctor.check_database(load_db_cfg(DB_TOML), missing)
    fails = [r for r in results if r.status == Status.FAIL]
    assert fails
    assert str(missing) in fails[0].detail
