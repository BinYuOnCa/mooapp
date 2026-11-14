"""人锁（CLAUDE.md 实盘三把锁；SPEC 2.2、5.10）。

- require_human_for_real：命令行选择 REAL 时、建立任何 OpenD 连接之前调用。非 TTY 直接拒绝；
  M4 确认点 4：REAL 仍关闭，TTY 下也拒绝，不调用 getpass（M4 模拟盘验收通过后另行计划开放）。
- unlock_real_interactively：M4 写好、用假对象测试的完整流程（TTY → getpass 输入交易密码 → broker.unlock →
  显示随机确认码、用户原样输入）。口令只在局部变量里，不打印、不记录、不返回；确认码错误时重新上锁（ADR-0015）。
  目前没有任何入口调用它。
- confirm_interactively：resume 等危险操作的交互确认（TTY + 确认码，Q10：输错一次即放弃）。
- new_confirm_code：Q10，6 位字母数字，排除 0/O、1/I/l；默认用 secrets 模块。
"""

from __future__ import annotations

import random
import secrets
import string
from collections.abc import Callable
from typing import Protocol, TextIO

from mooapp.broker.port import BrokerError
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
    # M4 确认点 4：REAL 仍关闭，不调用 getpass
    raise HumanLockError("人锁：REAL 尚未开放（M4 模拟盘验收通过后另行计划），已退出")


# ------------------------------------------------------------------ M4：确认码与交互确认（Q10）

CONFIRM_ALPHABET = "".join(c for c in string.ascii_uppercase + string.ascii_lowercase + string.digits if c not in "0O1Il")


def new_confirm_code(n: int = 6, rng: random.Random | None = None) -> str:
    r = rng if rng is not None else secrets.SystemRandom()
    return "".join(r.choice(CONFIRM_ALPHABET) for _ in range(n))


def _require_tty(stdin: TextIO, stdout: TextIO, what: str) -> None:
    if not (_is_tty(stdin) and _is_tty(stdout)):
        raise HumanLockError(f"人锁：{what}只能在交互终端进行（stdin 与 stdout 都必须是 TTY），已拒绝")


def confirm_interactively(
    stdin: TextIO,
    stdout: TextIO,
    input_fn: Callable[[str], str],
    rng: random.Random | None,
    purpose: str,
) -> None:
    """显示随机确认码，用户原样输入才放行；输错一次即放弃（Q10）。"""
    _require_tty(stdin, stdout, purpose)
    code = new_confirm_code(rng=rng)
    stdout.write(f"确认码：{code}\n")
    stdout.flush()
    typed = input_fn(f"请原样输入确认码以{purpose}：").strip()
    if typed != code:
        raise HumanLockError(f"人锁：确认码不符，已放弃{purpose}")


class _Unlockable(Protocol):
    def unlock(self, password: str) -> None: ...
    def lock(self) -> None: ...


def unlock_real_interactively(
    stdin: TextIO,
    stdout: TextIO,
    getpass_fn: Callable[[str], str],
    input_fn: Callable[[str], str],
    rng: random.Random | None,
    broker: _Unlockable,
) -> None:
    """REAL 启动的人锁完整流程（M4 只写代码、用假对象测试；没有入口调用）。"""
    _require_tty(stdin, stdout, "进入 REAL ")
    password = getpass_fn("交易密码（不回显）：")
    try:
        broker.unlock(password)  # vendor § Unlock Trade：作用于整个 OpenD（ADR-0015）
    except BrokerError as e:  # 换成人锁错误再抛（保留异常链）；消息来自券商，不含口令
        raise HumanLockError(f"人锁：解锁失败（{type(e).__name__}：{e}）") from e
    finally:
        del password
    try:
        confirm_interactively(stdin, stdout, input_fn, rng, "进入 REAL")
    except HumanLockError:
        broker.lock()  # 确认失败：已解锁的立即重新上锁（ADR-0015）
        raise
