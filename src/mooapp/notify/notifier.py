"""通知器（M5；SPEC 5.7；PROGRESS「M5 计划」决定 2、3、5、6、7）。

- 订阅 EventBus：回调只把事件放进队列，不阻塞交易
- tick()：处理队列（Composer）→ 定时合并与汇总 → 心跳 → 写 outbox → 投递到期邮件
- outbox 在数据库里（重启后补发）；写不进库时邮件先留在内存，下一轮再写
- 投递失败按 BACKOFF_SECONDS 退避（最后一档一直重复），报警 email_send_failed（不进邮件）
- 心跳：交易日开盘前 heartbeat_minutes_before_open 分钟发；晚于该时刻 LATE_GRACE 以上才第一次检查到 → 标「迟到」；
  每天一封，记在 kv（重启不重发）
- threaded=True 时后台线程每 poll_seconds 跑一次 tick；threaded=False 由调用方（测试）显式调用
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from mooapp.broker.port import MarketDataPort
from mooapp.clock import Clock
from mooapp.common import Env, et_str
from mooapp.config.models import EmailCfg
from mooapp.notify.compose import Composer
from mooapp.notify.mailer import Email, Mailer, MailError
from mooapp.oms.events import Alert, EventBus, OrderChanged
from mooapp.oms.store import Store, StoreUnavailable
from mooapp.risk.gate import HaltChanged, RiskRejected

log = logging.getLogger(__name__)
MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
BACKOFF_SECONDS = (60, 120, 300, 900, 1800)
LATE_GRACE = timedelta(minutes=2)
HEARTBEAT_KEY = "heartbeat_email:"


def _backoff(attempts_before: int) -> timedelta:
    return timedelta(seconds=BACKOFF_SECONDS[min(attempts_before, len(BACKOFF_SECONDS) - 1)])


def deliver_or_enqueue(email: Email, recipients: Sequence[str], mailer: Mailer, store: Store, clock: Clock) -> bool:
    """M5 决定 7（mooapp eod 用）：当场发送；失败则写进 outbox 由常驻进程重试。返回是否已当场发出。"""
    try:
        mailer.send(email, recipients)
        return True
    except MailError as e:
        log.error("邮件当场发送失败，转入 outbox 由常驻进程重试：%s", e)
        with store.transaction():
            store.enqueue_email(email.kind, email.subject, email.body, list(recipients), clock.now())
        return False


class Notifier:
    def __init__(
        self,
        bus: EventBus,
        store: Store,
        mailer: Mailer,
        cfg: EmailCfg,
        clock: Clock,
        env: Env,
        md: MarketDataPort | None,
        threaded: bool = True,
        status: Callable[[], dict[str, Any]] | None = None,
        poll_seconds: float = 1.0,
    ):
        self._bus = bus
        self._store = store
        self._mailer = mailer
        self._cfg = cfg
        self._clock = clock
        self._env = env
        self._md = md
        self._threaded = threaded
        self._status = status or (lambda: {})
        self._poll = poll_seconds
        self._composer = Composer(cfg, env)
        self._q: queue.Queue[object] = queue.Queue()
        self._unsaved: list[Email] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._first_hb_check = True
        self._trading_day: tuple[date, bool] | None = None

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        for kind in (OrderChanged, RiskRejected, HaltChanged, Alert):
            self._bus.subscribe(kind, self._q.put)  # 只排队（发布方可能持有 OMS 锁）
        if self._threaded:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="mooapp-notifier", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """停止后台线程；队列里的事件、未到时间的合并窗口与汇总写进 outbox；未发出的邮件留在 outbox，下次启动补发。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(30.0)
            self._thread = None
        now = self._clock.now()
        emails: list[Email] = []
        while True:
            try:
                emails.extend(self._compose(self._q.get_nowait(), now))
            except queue.Empty:
                break
        emails.extend(self._composer.flush_all(now))
        self._save([*self._unsaved, *emails], None, now)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # 记日志并报警，下一轮再试；不让通知线程退出，也不影响交易
                log.exception("通知器出错")
                self._bus.publish(Alert("email_notifier_error", f"{type(e).__name__}：{e}"))
            self._stop.wait(self._poll)

    # ------------------------------------------------------------------ 一轮

    def tick(self) -> None:
        now = self._clock.now()
        emails: list[Email] = []
        while True:
            try:
                e = self._q.get_nowait()
            except queue.Empty:
                break
            emails.extend(self._compose(e, now))
        emails.extend(self._composer.tick(now))
        hb = self._heartbeat(now)
        self._save([*self._unsaved, *emails], hb, now)
        self.deliver_due(now)

    def _compose(self, e: object, now: datetime) -> list[Email]:
        if isinstance(e, OrderChanged):
            return self._composer.on_order(e.order, now)
        if isinstance(e, RiskRejected):
            return self._composer.on_risk_rejected(e, now)
        if isinstance(e, HaltChanged):
            return self._composer.on_halt(e, now)
        if isinstance(e, Alert):
            return self._composer.on_alert(e, now)
        return []

    def _save(self, emails: list[Email], hb: tuple[Email, str] | None, now: datetime) -> None:
        pending = list(emails)
        if hb is not None:
            pending.append(hb[0])
        if not pending:
            return
        try:
            with self._store.transaction():
                for e in pending:
                    self._store.enqueue_email(e.kind, e.subject, e.body, list(self._cfg.recipients), now)
                if hb is not None:
                    self._store.set_kv(hb[1], now.isoformat())
            self._unsaved = []
        except StoreUnavailable as e:
            log.error("outbox 写不进数据库，%d 封邮件先留在内存：%s", len(pending), e)
            self._unsaved = [m for m in pending if hb is None or m is not hb[0]]
            if hb is not None:
                self._first_hb_check = False  # 下一轮重新生成心跳（kv 没写成功）

    def deliver_due(self, now: datetime) -> int:
        sent = 0
        try:
            due = self._store.due_emails(now)
        except StoreUnavailable as e:
            log.error("读不到 outbox：%s", e)
            return 0
        for rec in due:
            if self._threaded and self._stop.is_set():
                break
            try:
                self._mailer.send(Email(rec.kind, rec.subject, rec.body), rec.recipients)
            except MailError as e:
                nxt = now + _backoff(rec.attempts)
                log.error("邮件 %s 发送失败（第 %d 次），%s 重试：%s", rec.id, rec.attempts + 1, et_str(nxt), e)
                with self._store.transaction():
                    self._store.mark_email_failed(rec.id, str(e), nxt, now)
                self._bus.publish(Alert("email_send_failed", f"邮件 {rec.id}（{rec.kind}）发送失败：{e}"))
                continue
            with self._store.transaction():
                self._store.mark_email_sent(rec.id, now)
            sent += 1
        return sent

    # ------------------------------------------------------------------ 心跳

    def _is_trading_day(self, d: date) -> bool:
        if self._trading_day is None or self._trading_day[0] != d:
            assert self._md is not None
            self._trading_day = (d, self._md.is_trading_day(d))  # vendor § Get Trading Calendar；每天只查一次
        return self._trading_day[1]

    def _heartbeat(self, now: datetime) -> tuple[Email, str] | None:
        if self._md is None:
            return None
        first, self._first_hb_check = self._first_hb_check, False
        today = now.astimezone(MARKET_TZ).date()
        key = HEARTBEAT_KEY + today.isoformat()
        due = datetime.combine(today, MARKET_OPEN, MARKET_TZ) - timedelta(minutes=self._cfg.heartbeat_minutes_before_open)
        if now < due or not self._is_trading_day(today) or self._store.get_kv(key) is not None:
            return None
        late = first and now - due > LATE_GRACE
        status = self._status()
        lines = [f"环境 {self._env.value}", f"时间 {et_str(now)}", *(f"{k}: {v}" for k, v in status.items())]
        subject = f"[{self._env.value}] 系统正常 {today.isoformat()}" + ("（迟到）" if late else "")
        return Email("heartbeat", subject, "\n".join(lines)), key
