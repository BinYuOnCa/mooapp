"""配置模型（SPEC 5.2）。

- 字段路径与 TOML 的段名一致（用 AliasPath），报错时能直接对上文件里的位置。
- 口令一律 SecretStr：repr / str 只显示 **********。
- 账号不进 repr（实盘 acc_id 属于 secrets）。
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AliasPath, BaseModel, ConfigDict, Field, SecretStr, field_validator

_SYMBOL = re.compile(r"^US\.[A-Z0-9][A-Z0-9.\-]*$")

NonNegInt = Annotated[int, Field(ge=0)]
NonNegDec = Annotated[Decimal, Field(ge=0)]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RiskLimits(_Strict):
    """风控限额（M4 使用）。数值由你决定，Claude Code 不得修改（CLAUDE.md）。"""

    whitelist: tuple[str, ...] = Field(min_length=1)  # 保持文件顺序：doctor 取第一只
    max_order_qty: NonNegInt
    max_order_value: NonNegDec
    max_position_qty: NonNegInt
    max_daily_orders: NonNegInt
    max_daily_loss: NonNegDec
    halt_on_daily_loss: bool
    max_limit_deviation_pct: NonNegDec
    regular_hours_only: bool

    @field_validator("whitelist")
    @classmethod
    def _us_only(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        # ADR-0001：只做美股
        bad = [s for s in v if not _SYMBOL.match(s)]
        if bad:
            raise ValueError(f"白名单只收 US.* 代码（ADR-0001），不合法：{', '.join(bad)}")
        return v


class QuoteCfg(_Strict):
    stale_after_seconds: Annotated[int, Field(gt=0)]
    poll_fallback_seconds: Annotated[int, Field(gt=0)]


class EmailCfg(_Strict):
    enabled: bool
    recipients: tuple[str, ...]
    merge_partial_fills_seconds: NonNegInt
    # M5 决定 5：开盘前多少分钟发心跳，5–25（美东 9:05–9:25）
    heartbeat_minutes_before_open: Annotated[int, Field(ge=5, le=25)]
    # M5 决定 4：普通报警每多少分钟汇总一封，15–120
    alert_digest_minutes: Annotated[int, Field(ge=15, le=120)] = 30


class AgentCfg(_Strict):
    auto_approve_simulate: bool = False  # SPEC Q9；不是风控限额


class Settings(BaseModel):
    """config/settings.toml"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    risk_simulate: RiskLimits = Field(validation_alias=AliasPath("risk", "simulate"))
    risk_real: RiskLimits = Field(validation_alias=AliasPath("risk", "real"))
    quote: QuoteCfg
    notify_email: EmailCfg = Field(validation_alias=AliasPath("notify", "email"))
    agent: AgentCfg = AgentCfg()


class BrokerCfg(BaseModel):
    """config/broker.toml 的 [opend] 段（SPEC 4 节、Q14）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    port: Annotated[int, Field(ge=1, le=65535)]
    filter_trdmarket: Literal["US"]
    simulate_acc_id: Annotated[int, Field(gt=0, repr=False)]

    @field_validator("host")
    @classmethod
    def _loopback(cls, v: str) -> str:
        # ADR-0014：OpenD 与程序在同一台机器，只用本机端口
        if v != "127.0.0.1":
            raise ValueError(f"OpenD host 只能是 127.0.0.1（ADR-0014），当前为 {v}")
        return v


class DbCfg(BaseModel):
    """config/db.toml 的 [postgres] 段（Q20）。host 的合法性在 dsn_for 里检查。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = "/tmp"
    port: Annotated[int, Field(ge=1, le=65535)] = 5432
    dbname: str = "mooapp"
    sim_role: str = "mooapp_sim"
    real_role: str = "mooapp_real"


class DbLocal(BaseModel):
    """config/db.local.toml（不进 Git，sandbox 可读；低等级凭据，Q20）。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    sim_password: SecretStr = Field(validation_alias=AliasPath("postgres", "sim_password"), min_length=1)


class SmtpSecrets(_Strict):
    host: str = Field(min_length=1)
    port: Annotated[int, Field(ge=1, le=65535)]
    username: str = Field(min_length=1)
    app_password: SecretStr = Field(min_length=1)


class Secrets(BaseModel):
    """~/.config/mooapp/secrets.toml（权限 600）。只有 REAL 需要 real_acc_id 与 real_password。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    smtp: SmtpSecrets
    opend_token: SecretStr = Field(default=SecretStr(""), validation_alias=AliasPath("opend", "token"))
    real_acc_id: int | None = Field(
        default=None, validation_alias=AliasPath("accounts", "real_acc_id"), repr=False
    )
    real_password: SecretStr | None = Field(
        default=None, validation_alias=AliasPath("postgres", "real_password")
    )
    # ADR-0029：resume 命令签名密钥（hex）。SIMULATE 下的常驻进程和 mooapp resume 也只读这一项
    resume_key: SecretStr | None = Field(default=None, validation_alias=AliasPath("resume", "key"))

    @field_validator("real_acc_id", mode="before")
    @classmethod
    def _placeholder_is_none(cls, v: object) -> object:
        # 模板 secrets.example.toml 里 real_acc_id = 0 是占位值，视为未填
        return None if v == 0 else v


class VerifiedVersions(_Strict):
    """config/verified_versions.toml（ADR-0035）：已验证可用的 OpenD 与 SDK 版本。由你在重跑契约测试与探针后更新。"""

    opend: str = Field(min_length=1)
    sdk: str = Field(min_length=1)
    verified_at: date
    evidence: str = Field(min_length=1)
