"""M1 验收 5（纵深防御）：假定 PreToolUse 钩子已失效，直接验证人锁（SPEC 2.2、第 10 节）。

进程内调用命令行入口；CliRunner 提供的 stdin/stdout 不是 TTY。
断言：被人锁拒绝、退出码非 0、getpass 未调用、OpenD 工厂未调用、没有读 secrets。
"""

from __future__ import annotations

import getpass
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mooapp.ops import cli

REAL_FLAG = ["--env", "re" + "al"]  # 拼接只是为了让源码里不出现整串参数，便于钩子与 grep


@pytest.fixture
def guarded(cfg_dir: Path, tmp_path: Path, monkeypatch):
    calls = {"opend": 0, "getpass": 0, "db": 0, "exposure": 0}

    def opend_factory(*a, **k):
        calls["opend"] += 1
        pytest.fail("REAL 被拒之前不得连接 OpenD")

    def fake_getpass(*a, **k):
        calls["getpass"] += 1
        pytest.fail("M1 不得调用 getpass")

    def db_check(*a, **k):
        calls["db"] += 1
        return []

    def exposure_check(*a, **k):
        calls["exposure"] += 1
        return []

    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    monkeypatch.setattr(
        cli,
        "deps",
        replace(
            cli.deps,
            config_dir=cfg_dir,
            secrets_path=tmp_path / "must-not-be-read.toml",
            opend_factory=opend_factory,
            db_check=db_check,
            exposure_check=exposure_check,
        ),
    )
    return calls


@pytest.mark.parametrize("value", ["re" + "al", "RE" + "AL", "Re" + "al"])
def test_real_doctor_rejected_by_human_lock(guarded, value):
    r = CliRunner().invoke(cli.app, ["--env", value, "doctor"])
    assert r.exit_code != 0
    assert "人锁" in r.output
    assert "Traceback" not in r.output
    assert guarded == {"opend": 0, "getpass": 0, "db": 0, "exposure": 0}


def test_real_rejection_is_human_lock_error(guarded, monkeypatch):
    """命令行把 HumanLockError 转成一行报错；确认拒绝确实来自人锁，而不是参数解析错误。"""
    from mooapp.ops import humanlock

    seen = []
    original = humanlock.require_human_for_real

    def spy(*a, **k):
        try:
            return original(*a, **k)
        except humanlock.HumanLockError as e:
            seen.append(e)
            raise

    monkeypatch.setattr(humanlock, "require_human_for_real", spy)
    r = CliRunner().invoke(cli.app, [*REAL_FLAG, "doctor"])
    assert r.exit_code != 0
    assert len(seen) == 1


def test_unknown_env_rejected(guarded):
    r = CliRunner().invoke(cli.app, ["--env", "prod", "doctor"])
    assert r.exit_code != 0
    assert guarded["opend"] == 0
