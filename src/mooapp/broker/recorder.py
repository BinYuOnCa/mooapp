"""推送录制（recordings/pushes/，M3 回放用）。

每次运行一个文件：{env}_{UTC 启动时刻}.jsonl。首行 meta；之后每条推送一行 {"ts", "kind", "row"}。
与探针规则 R5 一致：原样记录，只脱敏账号（字段名含 acc_id 的值用 mask_account）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mooapp.clock import Clock
from mooapp.common import Env, mask_account


def _mask(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: (mask_account(v) if "acc_id" in k and v is not None else v) for k, v in row.items()}


class PushRecorder:
    def __init__(self, directory: Path, env: Env, clock: Clock, meta: Mapping[str, Any] | None = None):
        self._clock = clock
        started = clock.now()
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{env.value}_{started:%Y%m%dT%H%M%SZ}.jsonl"
        self._lock = threading.Lock()
        self._fh = self.path.open("x", encoding="utf-8")  # 不覆盖已有录制
        self._write({"meta": True, "env": env.value, "started": started.isoformat(), **_mask(meta or {})})

    def _write(self, obj: dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False, default=str)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def record(self, kind: str, row: Mapping[str, Any] | str) -> None:
        """row 为推送的一行；推送失败（ret != RET_OK）时为错误原文。"""
        payload = _mask(row) if isinstance(row, Mapping) else row
        self._write({"ts": self._clock.now().isoformat(), "kind": kind, "row": payload})

    def close(self) -> None:
        with self._lock:
            self._fh.close()
