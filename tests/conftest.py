"""共享 fixture：在 tmp_path 里生成假配置文件。

测试不读 ~/.config/mooapp/（SPEC 第 10 节）；这里的口令都是明显的假值。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

SETTINGS_TOML = """
[risk.simulate]
whitelist = ["US.SPY", "US.QQQ"]
max_order_qty = 100
max_order_value = 5000.0
max_position_qty = 200
max_daily_orders = 50
max_daily_loss = 500.0
halt_on_daily_loss = true
max_limit_deviation_pct = 5.0
regular_hours_only = true

[risk.real]
whitelist = ["US.SCHD"]
max_order_qty = 1
max_order_value = 100.0
max_position_qty = 2
max_daily_orders = 10
max_daily_loss = 20.0
halt_on_daily_loss = true
max_limit_deviation_pct = 2.0
regular_hours_only = true

[quote]
stale_after_seconds = 30
poll_fallback_seconds = 5

[notify.email]
enabled = true
recipients = ["someone@example.com"]
merge_partial_fills_seconds = 60
heartbeat_minutes_before_open = 15

[agent]
auto_approve_simulate = false
"""

BROKER_TOML = """
[opend]
host = "127.0.0.1"
port = 11111
filter_trdmarket = "US"
simulate_acc_id = 2964165
"""

DB_TOML = """
[postgres]
host = "/tmp"
port = 5432
dbname = "mooapp"
sim_role = "mooapp_sim"
real_role = "mooapp_real"
"""

FAKE_SIM_PW = "fake-sim-password"
FAKE_REAL_PW = "fake-real-password"
FAKE_SMTP_PW = "fake-smtp-password"

DB_LOCAL_TOML = f"""
[postgres]
sim_password = "{FAKE_SIM_PW}"
"""

SECRETS_TOML = f"""
[smtp]
host = "smtp.example.com"
port = 465
username = "someone@example.com"
app_password = "{FAKE_SMTP_PW}"

[opend]
token = ""

[accounts]
real_acc_id = 281756470009876

[postgres]
real_password = "{FAKE_REAL_PW}"
"""


def write(path: Path, text: str, mode: int = 0o644) -> Path:
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    """一套合法的假配置：settings、broker、db、db.local、secrets（600）。"""
    d = tmp_path / "config"
    d.mkdir()
    write(d / "settings.toml", SETTINGS_TOML)
    write(d / "broker.toml", BROKER_TOML)
    write(d / "db.toml", DB_TOML)
    write(d / "db.local.toml", DB_LOCAL_TOML, 0o600)
    write(d / "secrets.toml", SECRETS_TOML, 0o600)
    return d
