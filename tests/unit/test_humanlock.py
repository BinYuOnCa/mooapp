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


# ------------------------------------------------------------------ M4：确认码、resume 交互确认、REAL 解锁流程（只写代码，REAL 仍关闭）

import random  # noqa: E402

from mooapp.ops.humanlock import (  # noqa: E402
    CONFIRM_ALPHABET,
    confirm_interactively,
    new_confirm_code,
    unlock_real_interactively,
)


class FakeBroker:
    """只记录 unlock / lock；口令只放在 unlocked_with 里供断言，不打印。"""

    def __init__(self, fail_unlock: bool = False):
        self.unlocked_with: str | None = None
        self.locked = 0
        self.fail_unlock = fail_unlock

    def unlock(self, password: str) -> None:
        if self.fail_unlock:
            from mooapp.broker.port import NotUnlocked

            raise NotUnlocked("unlock_trade 失败：口令错误（测试）")
        self.unlocked_with = password

    def lock(self) -> None:
        self.locked += 1


SECRET = "s3cr3t-trade-pw"


def test_confirm_code_alphabet_and_length():
    """Q10：6 位字母数字，排除易混字符 0/O、1/I/l。"""
    for ch in "0O1Il":
        assert ch not in CONFIRM_ALPHABET
    code = new_confirm_code(rng=random.Random(1))
    assert len(code) == 6 and all(c in CONFIRM_ALPHABET for c in code)
    assert new_confirm_code(n=8, rng=random.Random(1)) != new_confirm_code(n=8, rng=random.Random(2))


def test_confirm_interactively_accepts_exact_code():
    out = TtyStream()
    rng = random.Random(7)
    expected = new_confirm_code(rng=random.Random(7))
    confirm_interactively(TtyStream(), out, input_fn=lambda prompt: expected, rng=rng, purpose="resume")
    assert expected in out.getvalue()


def test_confirm_interactively_wrong_code_rejected_once():
    calls = []

    def typed(prompt):
        calls.append(prompt)
        return "WRONG1"

    with pytest.raises(HumanLockError):
        confirm_interactively(TtyStream(), TtyStream(), input_fn=typed, rng=random.Random(7), purpose="resume")
    assert len(calls) == 1  # Q10：输错一次即放弃


def test_confirm_interactively_requires_tty():
    with pytest.raises(HumanLockError):
        confirm_interactively(NonTtyStream(), TtyStream(), input_fn=lambda p: pytest.fail("不应读输入"), rng=random.Random(1), purpose="resume")


def test_unlock_flow_success_password_never_printed():
    out = TtyStream()
    broker = FakeBroker()
    code = new_confirm_code(rng=random.Random(3))
    unlock_real_interactively(
        TtyStream(), out, getpass_fn=lambda prompt: SECRET, input_fn=lambda prompt: code, rng=random.Random(3), broker=broker
    )
    assert broker.unlocked_with == SECRET
    assert SECRET not in out.getvalue()


def test_unlock_flow_wrong_confirm_code_relocks():
    broker = FakeBroker()
    with pytest.raises(HumanLockError):
        unlock_real_interactively(
            TtyStream(), TtyStream(), getpass_fn=lambda p: SECRET, input_fn=lambda p: "NOPE22", rng=random.Random(3), broker=broker
        )
    assert broker.locked == 1  # 确认码错误：已解锁的要重新上锁（ADR-0015）


def test_unlock_flow_unlock_failure_is_human_lock_error_without_password():
    broker = FakeBroker(fail_unlock=True)
    with pytest.raises(HumanLockError) as ei:
        unlock_real_interactively(
            TtyStream(), TtyStream(), getpass_fn=lambda p: SECRET, input_fn=lambda p: "x", rng=random.Random(3), broker=broker
        )
    assert SECRET not in str(ei.value)


def test_unlock_flow_non_tty_never_asks_password():
    with pytest.raises(HumanLockError):
        unlock_real_interactively(
            NonTtyStream(), TtyStream(), getpass_fn=forbidden_getpass, input_fn=lambda p: "x", rng=random.Random(3), broker=FakeBroker()
        )


def test_real_still_disabled_in_m4_entrypoint():
    """M4 确认点 4：CLI 入口的 require_human_for_real 在 TTY 下仍拒绝（REAL 待验收后另行开放），不调用 getpass。"""
    with pytest.raises(HumanLockError) as ei:
        require_human_for_real(stdin=TtyStream(), stdout=TtyStream(), getpass_fn=forbidden_getpass)
    assert "M4" in str(ei.value) and "另行" in str(ei.value)
