"""M1 验收 1、2、3：mooapp doctor（SPEC 5.10、8 节 M1 实现说明）。

全部用假 OpenD 会话，不连 OpenD、不连数据库、不读 ~/.config/mooapp/。
"""

from __future__ import annotations

import errno
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mooapp.broker.opend import (
    AccountInfo,
    BrokerUnavailable,
    Funds,
    GlobalState,
    Snapshot,
)
from mooapp.common import Env
from mooapp.config.loader import load_broker_cfg, load_db_cfg, load_settings
from mooapp.ops import cli, doctor
from mooapp.ops.doctor import CheckResult, Status

SIM_ACC = 2964165
REAL_ACC = 281756470009876
UNREACHABLE_LINE = "无法连接 OpenD 127.0.0.1:11111（5 秒超时），请确认 OpenD 已启动"


class FakeSession:
    def __init__(self, accounts: list[AccountInfo] | None = None):
        self.calls: list[tuple] = []
        self.closed = False
        self._accounts = accounts if accounts is not None else [
            AccountInfo(acc_id=REAL_ACC, trd_env="REAL", markets=("US",)),
            AccountInfo(acc_id=SIM_ACC, trd_env="SIMULATE", markets=("US",)),
        ]

    def global_state(self) -> GlobalState:
        self.calls.append(("global_state",))
        return GlobalState(server_ver="9.6.5618", market_us="AFTER_HOURS_END")

    def accounts(self) -> list[AccountInfo]:
        self.calls.append(("accounts",))
        return self._accounts

    def funds(self, acc_id: int, env: Env) -> Funds:
        self.calls.append(("funds", acc_id, env))
        return Funds(
            currency="N/A",
            total_assets=Decimal("1000000"),
            cash=Decimal("1000000"),
            power=Decimal("2000000"),
        )

    def snapshot(self, codes: list[str]) -> list[Snapshot]:
        self.calls.append(("snapshot", tuple(codes)))
        return [Snapshot(code=c, last_price=Decimal("600.12"), update_time="2026-11-11 16:00:00") for c in codes]

    def market_states(self, codes: list[str]) -> dict[str, str]:
        self.calls.append(("market_states", tuple(codes)))
        return {c: "AFTER_HOURS_END" for c in codes}

    def close(self) -> None:
        self.closed = True


class Recorder:
    def __init__(self, result: list[CheckResult] | None = None):
        self.called = 0
        self.result = result if result is not None else [CheckResult("stub", Status.PASS, "ok")]

    def __call__(self, *args, **kwargs) -> list[CheckResult]:
        self.called += 1
        return self.result


@pytest.fixture
def cfgs(cfg_dir: Path):
    return load_settings(cfg_dir / "settings.toml"), load_broker_cfg(cfg_dir / "broker.toml")


def run(cfgs, factory, secrets_path: Path, db=None, exposure=None):
    settings, broker_cfg = cfgs
    return doctor.run_doctor(
        settings,
        broker_cfg,
        opend_factory=factory,
        secrets_path=secrets_path,
        db_check=db or Recorder(),
        exposure_check=exposure or Recorder(),
    )


def by_status(results: list[CheckResult], status: Status) -> list[CheckResult]:
    return [r for r in results if r.status == status]


# ------------------------------------------------------------------ 正常路径（验收 1）


def test_five_opend_items_and_masked(cfgs, cfg_dir: Path):
    sess = FakeSession()
    seen: dict = {}

    def factory(broker_cfg, timeout):
        seen["timeout"] = timeout
        seen["cfg"] = broker_cfg
        return sess

    results = run(cfgs, factory, cfg_dir / "secrets.toml")
    out = doctor.render(results)

    assert seen["timeout"] == 5.0  # 你确认的第 3 点
    assert by_status(results, Status.FAIL) == []
    assert doctor.exit_code(results) == 0

    # 1 版本
    assert "9.6.5618" in out
    # 2 账户列表只显示后 4 位
    assert "****4165" in out and "****9876" in out
    assert str(SIM_ACC) not in out and str(REAL_ACC) not in out
    # 3 资金：显式 SIMULATE 与 simulate_acc_id
    assert ("funds", SIM_ACC, Env.SIMULATE) in sess.calls
    assert "1000000" in out.replace(",", "")
    # 4 白名单第一只的快照
    assert ("snapshot", ("US.SPY",)) in sess.calls
    assert "US.SPY" in out and "600.12" in out
    # 5 市场状态
    assert any(c[0] == "market_states" for c in sess.calls)
    assert "AFTER_HOURS_END" in out

    assert sess.closed


def test_simulate_acc_not_in_simulate_accounts_fails(cfgs, cfg_dir: Path):
    """simulate_acc_id 只出现在 REAL 账户里，必须报错（SPEC 第 4 节）。"""
    sess = FakeSession(accounts=[AccountInfo(acc_id=SIM_ACC, trd_env="REAL", markets=("US",))])
    results = run(cfgs, lambda c, t: sess, cfg_dir / "secrets.toml")
    assert by_status(results, Status.FAIL)
    assert doctor.exit_code(results) != 0
    assert str(SIM_ACC) not in doctor.render(results)
    # 账户核对失败时不得用这个 acc_id 查资金
    assert not any(c[0] == "funds" for c in sess.calls)


def test_each_line_is_single_line(cfgs, cfg_dir: Path):
    results = run(cfgs, lambda c, t: FakeSession(), cfg_dir / "secrets.toml")
    for r in results:
        assert "\n" not in r.detail, r


# ------------------------------------------------------------------ OpenD 不可达（验收 3）


def test_opend_unreachable_one_line(cfgs, cfg_dir: Path):
    def factory(broker_cfg, timeout):
        raise BrokerUnavailable("connect timeout")

    db, exposure = Recorder(), Recorder()
    results = run(cfgs, factory, cfg_dir / "secrets.toml", db=db, exposure=exposure)
    out = doctor.render(results)

    assert UNREACHABLE_LINE in out
    assert out.count(UNREACHABLE_LINE) == 1
    assert "Traceback" not in out
    assert doctor.exit_code(results) != 0
    # 1–5 项跳过，6–8 项照常
    assert db.called == 1 and exposure.called == 1
    assert any("secrets" in r.name for r in results)


def test_cli_opend_unreachable(cfg_dir: Path, monkeypatch):
    def factory(broker_cfg, timeout):
        raise BrokerUnavailable("connect timeout")

    monkeypatch.setattr(
        cli,
        "deps",
        replace(
            cli.deps,
            config_dir=cfg_dir,
            secrets_path=cfg_dir / "secrets.toml",
            opend_factory=factory,
            db_check=Recorder(),
            exposure_check=Recorder(),
        ),
    )
    r = CliRunner().invoke(cli.app, ["doctor"])
    assert r.exit_code != 0
    assert UNREACHABLE_LINE in r.output
    assert "Traceback" not in r.output
    assert not isinstance(r.exception, BrokerUnavailable)


def test_cli_default_env_is_simulate(cfg_dir: Path, monkeypatch):
    sess = FakeSession()
    monkeypatch.setattr(
        cli,
        "deps",
        replace(
            cli.deps,
            config_dir=cfg_dir,
            secrets_path=cfg_dir / "secrets.toml",
            opend_factory=lambda c, t: sess,
            db_check=Recorder(),
            exposure_check=Recorder(),
        ),
    )
    r = CliRunner().invoke(cli.app, ["doctor"])
    assert r.exit_code == 0, r.output
    assert ("funds", SIM_ACC, Env.SIMULATE) in sess.calls


# ------------------------------------------------------------------ secrets（验收 2）


def test_secrets_missing_only_that_item_fails(cfgs, tmp_path: Path):
    missing = tmp_path / "no-such-secrets.toml"
    results = run(cfgs, lambda c, t: FakeSession(), missing)
    fails = by_status(results, Status.FAIL)
    assert len(fails) == 1
    assert "secrets" in fails[0].name
    assert str(missing) in fails[0].detail
    assert doctor.exit_code(results) != 0
    # 其余照常：OpenD 五项仍然输出
    out = doctor.render(results)
    assert "9.6.5618" in out and "US.SPY" in out
    assert "Traceback" not in out


def test_secrets_bad_permission(cfgs, cfg_dir: Path):
    p = cfg_dir / "secrets.toml"
    p.chmod(0o644)
    r = doctor.check_secrets(p)
    assert r.status == Status.FAIL
    assert "600" in r.detail


def test_secrets_unreadable_shows_no_permission(tmp_path: Path):
    """sandbox 内读 ~/.config/mooapp/ 会被拒：显示「无权读取」。这里用 chmod 000 模拟。"""
    p = tmp_path / "secrets.toml"
    p.write_text("x = 1")
    p.chmod(0o000)
    r = doctor.check_secrets(p)
    assert r.status != Status.PASS
    assert "无权读取" in r.detail


def test_secrets_ok(cfg_dir: Path):
    r = doctor.check_secrets(cfg_dir / "secrets.toml")
    assert r.status == Status.PASS
    assert "281756470009876" not in r.detail


# ------------------------------------------------------------------ 第 8 项：端口未暴露


@pytest.fixture
def db_broker(cfg_dir: Path):
    return load_db_cfg(cfg_dir / "db.toml"), load_broker_cfg(cfg_dir / "broker.toml")


def test_exposure_refused_is_pass(db_broker):
    def connect(host, port, timeout):
        raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")

    results = doctor.check_exposure(*db_broker, addresses=["192.168.1.20"], connect=connect)
    assert results and all(r.status == Status.PASS for r in results)


def test_exposure_timeout_is_pass(db_broker):
    def connect(host, port, timeout):
        raise TimeoutError("timed out")

    results = doctor.check_exposure(*db_broker, addresses=["192.168.1.20"], connect=connect)
    assert results and all(r.status == Status.PASS for r in results)


def test_exposure_connected_is_fail(db_broker):
    tried = []

    def connect(host, port, timeout):
        tried.append((host, port))  # 能连上 = 暴露

    results = doctor.check_exposure(*db_broker, addresses=["192.168.1.20"], connect=connect)
    assert ("192.168.1.20", 5432) in tried and ("192.168.1.20", 11111) in tried
    assert by_status(results, Status.FAIL)
    assert doctor.exit_code(results) != 0


def test_exposure_sandbox_cannot_check(db_broker):
    def connect(host, port, timeout):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    results = doctor.check_exposure(*db_broker, addresses=["192.168.1.20"], connect=connect)
    assert not by_status(results, Status.PASS)
    assert not by_status(results, Status.FAIL)
    assert any("无法检查" in r.detail for r in results)
