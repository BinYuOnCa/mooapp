"""EventPump：推送、轮询、本地动作进入 OMS 的唯一通道（SPEC 3.4；M3 计划第 6 节）。

- threaded=True（常驻进程）：一个队列 + 一个消费线程，依次调用 OMS；SDK 推送回调只负责入队
- threaded=False（测试）：入队后不处理，测试显式 drain()，便于构造重复、乱序、丢失
- call(fn)：本地动作（建单、SUBMIT、撤单请求等）排在已入队事件之后执行并返回结果。
  同步模式下先 drain 再执行（drain 进行中则直接执行，不嵌套）；线程模式下入队并等待；消费线程未启动或就在消费线程里时直接执行
- submit_task(fn)：排队执行、不等结果（ADR-0026 的自动撤单）
- 消费线程处理某条事件出错：记 ERROR 日志（含堆栈）并发 Alert("pump_error")，继续处理下一条——
  不吞错，也不让一条坏事件卡死整条通道
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, TypeVar

from mooapp.broker.port import BrokerFill, BrokerOrder, BrokerPort
from mooapp.oms.events import Alert
from mooapp.oms.oms import OMS

log = logging.getLogger(__name__)
T = TypeVar("T")
_STOP = object()


class EventPump:
    def __init__(self, oms: OMS, threaded: bool = False):
        self.oms = oms
        self.threaded = threaded
        self._q: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._draining = False  # 同步模式：drain 期间被处理的事件再调用 call()，不嵌套 drain

    # ------------------------------------------------------------------ 入队

    def submit_snapshot(self, snap: BrokerOrder, origin: str) -> None:
        self._q.put(("snapshot", snap, origin))

    def submit_fill(self, fill: BrokerFill, origin: str) -> None:
        self._q.put(("fill", fill, origin))

    def submit_task(self, fn: Callable[[], object]) -> None:
        """排队执行一个本地动作，不等结果（例如 ADR-0026 确认后自动撤单：在 OMS 持锁时发布，处理方不能阻塞）。"""
        self._q.put(("task", fn))

    def attach(self, broker: BrokerPort) -> None:
        """订阅券商推送（vendor § Orders Push Callback、§ Deals Push Callback）。合成成交不入队：OMS 自己按快照算。"""
        broker.on_order_update(lambda s: self.submit_snapshot(s, "push"))
        broker.on_fill(lambda f: None if f.synthetic else self.submit_fill(f, "push"))

    # ------------------------------------------------------------------ 处理

    def _process(self, item: tuple[Any, ...]) -> None:
        kind = item[0]
        if kind == "snapshot":
            self.oms.ingest_snapshot(item[1], item[2])
        elif kind == "fill":
            self.oms.ingest_fill(item[1], item[2])
        elif kind == "task":
            item[1]()
        else:
            raise ValueError(f"未知的队列项 {kind!r}")

    def drain(self) -> None:
        if self.threaded:
            self._q.join()
            return
        if self._draining:
            return
        self._draining = True
        try:
            while True:
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    return
                try:
                    self._process(item)
                finally:
                    self._q.task_done()
        finally:
            self._draining = False

    def call(self, fn: Callable[[], T]) -> T:
        running = self._thread is not None and self._thread.is_alive()
        if self.threaded and running and threading.current_thread() is not self._thread:
            fut: Future[T] = Future()
            self._q.put(("call", fn, fut))
            return fut.result()
        if not self.threaded:
            self.drain()
        return fn()

    # ------------------------------------------------------------------ 消费线程

    def start(self) -> None:
        if not self.threaded:
            raise RuntimeError("同步模式的 EventPump 不启动线程")
        if self._thread is not None:
            raise RuntimeError("EventPump 已启动")
        self._thread = threading.Thread(target=self._loop, name="mooapp-event-pump", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        self._q.put(_STOP)
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise RuntimeError(f"EventPump {timeout:g} 秒内未停止")
        self._thread = None

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            try:
                if item is _STOP:
                    return
                if item[0] == "call":
                    _, fn, fut = item
                    try:
                        fut.set_result(fn())
                    except BaseException as e:  # 交给等待中的调用者原样抛出
                        fut.set_exception(e)
                    continue
                try:
                    self._process(item)
                except Exception as e:
                    log.exception("事件处理出错：%r", item[:2])
                    self.oms.bus.publish(Alert("pump_error", f"{type(e).__name__}：{e}", details={"item": repr(item[:2])}))
            finally:
                self._q.task_done()
