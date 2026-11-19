"""版本闸门（ADR-0035 第 1 条）：实际的 OpenD / SDK 版本与 config/verified_versions.toml 比较。

SIMULATE：不一致只报警 api_version_changed，继续运行（OpenD 会自动升级，不打断模拟盘）。
REAL：不一致拒绝启动（REAL 目前关闭，开放时生效）。
"""

from __future__ import annotations

from mooapp.common import Env, MooappError
from mooapp.config.models import VerifiedVersions
from mooapp.oms.events import Alert, EventBus


class VersionNotVerified(MooappError):
    """REAL 下 OpenD 或 SDK 版本未经验证。"""


def version_diffs(opend: str, sdk: str, verified: VerifiedVersions) -> list[str]:
    out = []
    if opend != verified.opend:
        out.append(f"OpenD {verified.opend} → {opend}")
    if sdk != verified.sdk:
        out.append(f"SDK {verified.sdk} → {sdk}")
    return out


def version_gate(env: Env, diffs: list[str], bus: EventBus) -> None:
    if not diffs:
        return
    text = "；".join(diffs)
    if env is Env.REAL:
        raise VersionNotVerified(f"版本未经验证：{text}。先重跑契约测试与探针，再更新 config/verified_versions.toml（ADR-0035）")
    bus.publish(Alert("api_version_changed", f"版本与已验证的不同：{text}；模拟盘继续运行，请重跑契约测试与探针后更新 verified_versions.toml（ADR-0035）"))
