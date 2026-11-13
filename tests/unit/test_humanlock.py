"""M1 验收 5：人锁的 TTY 检查（SPEC 2.2、5.10、第 10 节）。

M1 范围（你确认的第 1 点）：非 TTY 直接拒绝；TTY 下也只提示「REAL 在 M4 实现」后拒绝，
不调用 getpass。
"""

from __future__ import annotations

import io

import pytest

from mooapp.common import MooappError
from mooapp.ops.humanlock import HumanLockError, require_human_for_real


class TtyStream(io.StringIO):
    def isatty(self) -> bool:
        return True


class NonTtyStream(io.StringIO):
    def isatty(self) -> bool:
        return False


def forbidden_getpass(prompt: str = "") -> str:
    pytest.fail("getpass 不应被调用")


def test_human_lock_error_is_mooapp_error():
    assert issubclass(HumanLockError, MooappError)


@pytest.mark.parametrize(
    "stdin, stdout",
    [
        (NonTtyStream(), NonTtyStream()),
        (TtyStream(), NonTtyStream()),
        (NonTtyStream(), TtyStream()),
    ],
    ids=["both-non-tty", "stdout-non-tty", "stdin-non-tty"],
)
def test_non_tty_rejected(stdin, stdout):
    with pytest.raises(HumanLockError):
        require_human_for_real(stdin=stdin, stdout=stdout, getpass_fn=forbidden_getpass)


def test_closed_stream_rejected():
    s = io.StringIO()
    s.close()
    with pytest.raises(HumanLockError):
        require_human_for_real(stdin=s, stdout=s, getpass_fn=forbidden_getpass)


def test_tty_in_m1_still_rejected_without_getpass():
    with pytest.raises(HumanLockError) as ei:
        require_human_for_real(stdin=TtyStream(), stdout=TtyStream(), getpass_fn=forbidden_getpass)
    assert "M4" in str(ei.value)
