"""定时轮询兜底（M3 计划第 6 节）：推送为主，轮询结果进入同一个 EventPump，origin=poll。

- 每次：query_orders(open_only=False)（vendor § Get open Orders：未完成 + 24 小时内；走 OpenD 缓存）
- REAL 另外查 query_deals（vendor § Get Today's Deals，只支持实盘）；SIMULATE 的成交由快照增量合成（P4）
- 收到 DealPollRequested（REAL 快照领先于本地成交）时标记，下一次轮询必查成交
- 默认 5 秒一次：30 秒 6 次，低于该接口 30 秒 10 次的限频
- 后台线程里单次轮询出错：记 ERROR 日志并发 Alert("poll_error")，下个周期继续
"""

from __future__ import annotations

import logging
import threading

from mooapp.broker.port import BrokerPort
from mooapp.common import Env
from mooapp.oms.events import Alert, DealPollRequested, EventBus
from mooapp.oms.pump import EventPump

log = logging.getLogger(__name__)


class Poller:
    def __init__(self, broker: BrokerPort, pump: EventPump, env: Env, bus: EventBus, interval: float = 5.0):
        self.broker = broker
        self.pump = pump
        self.env = env
        self.bus = bus
        self.interval = interval
        self.deal_poll_pending = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        bus.subscribe(DealPollRequested, self._on_deal_poll_requested)

    def _on_deal_poll_requested(self, _e: DealPollRequested) -> None:
        self.deal_poll_pending = True

    def poll_once(self) -> None:
        for s in self.broker.query_orders(open_only=False):
            self.pump.submit_snapshot(s, "poll")
        if self.env is Env.REAL or self.deal_poll_pending:
            self.deal_poll_pending = False
            for d in self.broker.query_deals():
                self.pump.submit_fill(d, "poll")

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Poller 已启动")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mooapp-poller", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll_once()
            except Exception as e:
                log.exception("轮询出错")
                self.bus.publish(Alert("poll_error", f"{type(e).__name__}：{e}"))
