"""人锁（CLAUDE.md 实盘三把锁；SPEC 2.2、5.10）。

M1 只实现 TTY 检查与拒绝（你确认的第 1 点）：
- stdin、stdout 任一不是交互终端 → HumanLockError；
- 都是交互终端 → 仍然 HumanLockError，提示「REAL 在 M4 实现」，不调用 getpass。
人锁在建立任何 OpenD 连接之前执行；失败时调用方不得构造 BrokerPort。
M4 补全：getpass 输入交易密码 → broker.unlock → 随机确认码（Q10）。
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import TextIO

from mooapp.common import MooappError


class HumanLockError(MooappError):
    """人锁拒绝进入 REAL。"""


def _is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except ValueError:
        # 已关闭的流调用 isatty() 抛 ValueError：按非交互终端处理（即拒绝），不是吞错
        return False


def require_human_for_real(
    stdin: TextIO,
    stdout: TextIO,
    getpass_fn: Callable[[str], str],
    rng: random.Random | None = None,
) -> None:
    """任一检查失败抛 HumanLockError。M1 中本函数总是抛出。"""
    if not (_is_tty(stdin) and _is_tty(stdout)):
        raise HumanLockError("人锁：REAL 只能在交互终端启动（stdin 与 stdout 都必须是 TTY），已拒绝")
    # getpass_fn 与 rng 在 M4 使用；M1 不调用 getpass
    raise HumanLockError("人锁：REAL 在 M4 实现，M1 不支持 REAL，已退出")
