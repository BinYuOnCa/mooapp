"""M1 验收 1：只读 OpenD 会话连模拟盘（-m sim）。

sandbox 不放行 127.0.0.1:11111（ADR-0015 第 3 条），由你用 `!` 运行：
    ! uv run pytest -m sim
不下单、不解锁、不读 secrets。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from mooapp.broker import opend
from mooapp.common import Env
from mooapp.config.loader import load_broker_cfg

pytestmark = pytest.mark.sim

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def broker_cfg():
    return load_broker_cfg(REPO / "config" / "broker.toml")


@pytest.fixture(scope="module")
def session(broker_cfg):
    s = opend.connect(broker_cfg, timeout=5.0)
    yield s
    s.close()


def test_global_state(session):
    gs = session.global_state()
    assert gs.server_ver


def test_simulate_account_listed(session, broker_cfg):
    sims = [a for a in session.accounts() if a.trd_env == "SIMULATE" and "US" in a.markets]
    assert broker_cfg.simulate_acc_id in {a.acc_id for a in sims}


def test_funds_simulate(session, broker_cfg):
    f = session.funds(broker_cfg.simulate_acc_id, Env.SIMULATE)
    assert isinstance(f.total_assets, Decimal)
    assert f.total_assets > 0


def test_funds_real_refused(session, broker_cfg):
    """只读会话在 M1 不接受 REAL（REAL 在 M4，经人锁）。"""
    with pytest.raises(opend.BrokerError):
        session.funds(broker_cfg.simulate_acc_id, Env.REAL)


def test_snapshot_spy(session):
    [snap] = session.snapshot(["US.SPY"])
    assert snap.code == "US.SPY"
    assert snap.last_price > 0
    assert snap.update_time


def test_market_states(session):
    states = session.market_states(["US.SPY"])
    assert isinstance(states["US.SPY"], str) and states["US.SPY"]


def test_connect_timeout_is_bounded():
    """连一个没人监听的本机端口：5 秒左右抛 BrokerUnavailable，不无限重试（learnings「阶段 0 实测」第 3 条）。"""
    import time

    from mooapp.config.models import BrokerCfg

    cfg = BrokerCfg(host="127.0.0.1", port=1, filter_trdmarket="US", simulate_acc_id=1)
    t0 = time.monotonic()
    with pytest.raises(opend.BrokerUnavailable):
        opend.connect(cfg, timeout=2.0)
    assert time.monotonic() - t0 < 6.0
