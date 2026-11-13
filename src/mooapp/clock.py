"""时钟（SPEC 5.1）。业务代码只通过 Clock 取时间，测试注入 FakeClock。

- now()：带时区的 UTC 时间（ADR-0011）
- monotonic()：单调秒数，用于限流、超时
- sleep()：FakeClock 的 sleep 立即返回并推进两种时间
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock:
    """测试用：时间只在 sleep / advance 时前进。线程安全。"""

    def __init__(self, start: datetime):
        if start.tzinfo is None or start.utcoffset() != timedelta(0):
            raise ValueError(f"FakeClock 需要带时区的 UTC 时间，收到 {start!r}")
        self._now = start.astimezone(UTC)
        self._mono = 0.0
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def monotonic(self) -> float:
        with self._lock:
            return self._mono

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"时间不能倒退：{seconds}")
        with self._lock:
            self._mono += seconds
            self._now += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)
