"""共享基础类型（SPEC 5.1）。M1 只需要 Env、MooappError、mask_account。"""

from __future__ import annotations

from enum import StrEnum


class Env(StrEnum):
    SIMULATE = "SIMULATE"
    REAL = "REAL"


class MooappError(Exception):
    """所有自定义异常的基类。"""


def mask_account(acc_id: int | str) -> str:
    """账号只保留后 4 位；日志、邮件、doctor、录制统一用它（SPEC 5.2、4 节）。"""
    s = str(acc_id)
    return "****" + s[-4:]
