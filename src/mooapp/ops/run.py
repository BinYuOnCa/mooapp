"""常驻进程 `mooapp run` 的组装与生命周期（SPEC 3.4；M4 计划设计要点；M4 验收草稿使用 build_runtime）。

- 只有常驻进程构造 RiskGate、OMS、Executor 和券商连接，并持有单实例锁（PostgreSQL advisory lock，按 schema 区分）
- 启动：拿锁 → 事件线程 → 恢复规则（M6b）→ OMS 启动恢复（完成前闸门拒绝新单）→ 轮询 → 遗留命令标 error
  → 心跳线程（每秒，独立于命令处理）→ 命令线程（LISTEN mooapp_commands 唤醒，另有 1 秒轮询兜底；LISTEN 出错也只报警、继续）
- 停止：停命令线程、轮询、事件线程 → 上锁（ADR-0015，模拟盘下无操作）→ 关券商连接 → 释放单实例锁 → 关数据库连接
- 本模块只负责组装：不调用 place_order / cancel_order，也不调用 Executor 的写方法（tests/unit/test_write_path.py）
- REAL：M4 仍关闭（确认点 4），build_runtime 直接拒绝
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any
from dataclasses import dataclass, field
from pathlib import Path

from mooapp.broker.port import BrokerError, BrokerPort, MarketDataPort
from mooapp.clock import Clock, SystemClock
from mooapp.common import Env, MooappError
from mooapp.oms.events import Alert, EventBus
from mooapp.oms.executor import Executor
from mooapp.oms.journal import Journal, JournaledStore
from mooapp.oms.oms import OMS
from mooapp.oms.poller import Poller
from mooapp.oms.pump import EventPump
from mooapp.oms.store import Store
from mooapp.ops.commands import HEARTBEAT_KEY, CommandProcessor
from mooapp.risk.gate import RiskGate

log = logging.getLogger(__name__)
SCHEMA = {Env.SIMULATE: "simulate", Env.REAL: "real"}
COMMAND_WAKE_SECONDS = 1.0
HEARTBEAT_SECONDS = 1.0
HEARTBEAT_ALERT_EVERY = 60.0  # 心跳写失败时最多每 60 秒报警一次


class RealDisabled(MooappError):
    """M4：REAL 尚未开放。"""


class RunnerAlreadyRunning(MooappError):
    """另一个常驻进程已持有单实例锁。"""


@dataclass
class Runtime:
    env: Env
    clock: Clock
    broker: BrokerPort
    md: MarketDataPort
    store: Store  # 数据库本身（命令行客户端、读状态用）；OMS 和闸门用的是带 journal 的包装
    bus: EventBus
    oms: OMS
    pump: EventPump
    poller: Poller
    executor: Executor
    gate: RiskGate
    processor: CommandProcessor
    engine: Any = None  # M6b：strategy.engine.RuleEngine（启动时先恢复规则，再做 OMS 启动恢复）
    listen: Callable[[float], bool] | None = None  # 等待新命令通知，最多等 timeout 秒；None 时只靠轮询
    closers: list[Callable[[], None]] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _hb_thread: threading.Thread | None = None
    _hb_last_alert: float | None = None
    _locked: bool = False

    def start(self) -> None:
        if not self.store.try_runner_lock():
            raise RunnerAlreadyRunning("另一个 mooapp run 正在运行（单实例锁被占用）")
        self._locked = True
        self.pump.start()
        if self.engine is not None:
            self.engine.start()  # 规则动作线程：闸门调用不在事件线程上（M6b 审查修复 #1）
            self.engine.recover()  # 先恢复规则：OMS 启动恢复产生的 OrderChanged 会补喂停机期间的成交
        self.oms.recover()
        self.poller.start()
        self.processor.recover_orphans()  # 已持有单实例锁：running 的命令只可能是上一个进程留下的
        self._beat()
        self._stop.clear()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, name="mooapp-heartbeat", daemon=True)
        self._hb_thread.start()
        self._thread = threading.Thread(target=self._command_loop, name="mooapp-commands", daemon=True)
        self._thread.start()
        log.info("mooapp run 已启动（%s）", self.env.value)

    def _beat(self) -> None:
        # 直接写数据库（不经 journal）：库不可用时命令行本来就连不上，心跳写进 journal 只会每秒堆一条
        with self.store.transaction():
            self.store.set_kv(HEARTBEAT_KEY, self.clock.now().isoformat())

    def _heartbeat_loop(self) -> None:
        """M4 审查 #9：心跳独立于命令处理，慢命令不会让命令行误判常驻进程不在。"""
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                self._beat()
            except Exception as e:  # 记日志；报警限频；下一秒再试
                log.exception("写心跳失败")
                now = self.clock.monotonic()
                if self._hb_last_alert is None or now - self._hb_last_alert >= HEARTBEAT_ALERT_EVERY:
                    self._hb_last_alert = now
                    self.bus.publish(Alert("heartbeat_failed", f"{type(e).__name__}：{e}"))

    def _command_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.engine is not None:
                    self.engine.poll_stale_quotes()  # 行情兜底（只排队，由事件线程执行）
                while not self._stop.is_set() and self.processor.process_one():
                    pass
                if self.listen is not None:
                    self.listen(COMMAND_WAKE_SECONDS)  # M4 审查 #6：放在 try 里，LISTEN 断开不让命令线程退出
                else:
                    self._stop.wait(COMMAND_WAKE_SECONDS)
            except Exception as e:  # 取命令或 LISTEN 失败（例如数据库断开）：记日志、报警，下一秒再试
                log.exception("命令线程出错")
                self.bus.publish(Alert("runner_error", f"{type(e).__name__}：{e}"))
                self._stop.wait(COMMAND_WAKE_SECONDS)

    def stop(self) -> None:
        """依次停止；每一步出错都记日志，最后把第一个错误原样抛出（不吞）。"""
        first: BaseException | None = None

        def step(name: str, fn: Callable[[], object]) -> None:
            nonlocal first
            try:
                fn()
            except BaseException as e:
                log.exception("停止时 %s 出错", name)
                first = first or e

        self._stop.set()
        if self._thread is not None:
            step("命令线程", lambda: self._thread.join(10.0) if self._thread else None)
        if self._hb_thread is not None:
            step("心跳线程", lambda: self._hb_thread.join(5.0) if self._hb_thread else None)
        step("轮询", self.poller.stop)
        if self.engine is not None:
            step("规则动作线程", self.engine.stop)
        step("事件线程", self.pump.stop)
        step("上锁", self._relock)
        step("券商连接", self.broker.close)
        if self._locked:
            step("单实例锁", self.store.release_runner_lock)
            self._locked = False
        for c in self.closers:
            step("数据库连接", c)
        if first is not None:
            raise first

    def _relock(self) -> None:
        try:
            self.broker.lock()  # ADR-0015：退出时重新上锁（模拟盘下无操作）
        except BrokerError as e:
            self.bus.publish(Alert("lock_failed", f"退出时重新上锁失败：{e}"))
            raise


def build_runtime(env: Env, config_dir: Path, record_dir: Path | None = None, journal_dir: Path = Path("var/journal")) -> Runtime:
    """按配置组装常驻进程的全部部件（连接 OpenD 与 PostgreSQL）。只支持 SIMULATE（M4 确认点 4）。"""
    if env is not Env.SIMULATE:
        raise RealDisabled("REAL 尚未开放：M4 模拟盘验收通过后另行计划")
    import psycopg

    from mooapp.broker.moomoo import open_broker
    from mooapp.config.loader import dsn_for, limits_for, load_broker_cfg, load_db_cfg, load_db_local, load_settings
    from mooapp.oms.pg_store import PostgresStore

    settings = load_settings(config_dir / "settings.toml")
    limits = limits_for(settings, env)  # 限额锁：只读
    dsn = dsn_for(env, load_db_cfg(config_dir / "db.toml"), load_db_local(config_dir / "db.local.toml"), None)
    clock = SystemClock()
    store = PostgresStore(dsn, schema=SCHEMA[env], other_schema=SCHEMA[Env.REAL])
    closers: list[Callable[[], None]] = [store.close]
    broker: BrokerPort | None = None
    try:
        journal = Journal(journal_dir, env)
        jstore = JournaledStore(store, journal, clock.now)
        broker = open_broker(env, load_broker_cfg(config_dir / "broker.toml"), clock, record_dir=record_dir)
        bus = EventBus()
        # ADR-0035：版本闸门（SIMULATE 报警继续，REAL 拒绝）；数据形状漂移报警
        from mooapp.broker.opend import sdk_version
        from mooapp.config.loader import load_verified_versions
        from mooapp.ops.versions import version_diffs, version_gate

        version_gate(env, version_diffs(broker.server_version(), sdk_version(), load_verified_versions(config_dir / "verified_versions.toml")), bus)
        broker.on_drift(lambda d: bus.publish(Alert(d.kind, d.message, details={"api": d.api, "name": d.name, "value": d.value})))
        oms = OMS(store=jstore, broker=broker, bus=bus, clock=clock, env=env, journal=journal)  # type: ignore[arg-type]
        pump = EventPump(oms, threaded=True)
        pump.attach(broker)
        def background(fn: Callable[[], object]) -> None:
            """确认后的自动撤单在独立线程里调用券商，不占事件线程（M4 审查小问题）。"""

            def go() -> None:
                try:
                    fn()
                except Exception as e:  # 记日志并报警，不吞
                    log.exception("后台撤单出错")
                    bus.publish(Alert("background_error", f"{type(e).__name__}：{e}"))

            threading.Thread(target=go, name="mooapp-background", daemon=True).start()

        executor = Executor(oms=oms, broker=broker, pump=pump, clock=clock, bus=bus, background=background)
        oms.set_executor(executor)
        poller = Poller(broker=broker, pump=pump, env=env, bus=bus, interval=float(settings.quote.poll_fallback_seconds))
        gate = RiskGate(
            env=env, limits=limits, oms=oms, executor=executor, md=broker, store=jstore, bus=bus, clock=clock,  # type: ignore[arg-type]
            stale_after_seconds=float(settings.quote.stale_after_seconds),
        )  # fmt: skip
        from mooapp.strategy.engine import RuleEngine

        engine = RuleEngine(
            gate=gate, oms=oms, store=jstore, md=broker, bus=bus, clock=clock, pump=pump,  # type: ignore[arg-type]
            poll_fallback_seconds=float(settings.quote.poll_fallback_seconds), threaded_actions=True,
        )  # fmt: skip
        processor = CommandProcessor(gate=gate, store=jstore, clock=clock, bus=bus, engine=engine, resume_key=_resume_key())  # type: ignore[arg-type]

        listen_conn = psycopg.connect(dsn, autocommit=True)
        closers.append(listen_conn.close)
        listen_conn.execute("LISTEN mooapp_commands")  # db/migrations/0002_m4.sql 的触发器在新命令时 NOTIFY

        def listen(timeout: float) -> bool:
            return any(True for _ in listen_conn.notifies(timeout=timeout, stop_after=1))

    except BaseException:
        if broker is not None:
            broker.close()
        for c in reversed(closers):
            c()
        raise
    return Runtime(
        env=env, clock=clock, broker=broker, md=broker, store=store, bus=bus, oms=oms, pump=pump, poller=poller,  # type: ignore[arg-type]
        executor=executor, gate=gate, processor=processor, engine=engine, listen=listen, closers=closers,
    )  # fmt: skip


def _resume_key() -> bytes | None:
    """ADR-0029：只读 secrets 的 [resume] key；读不到或没配时返回 None（常驻进程照常运行，但拒绝一切 resume）。"""
    from mooapp.config.loader import ConfigError, load_secrets

    try:
        key = load_secrets().resume_key
    except ConfigError as e:
        log.warning("读不到 secrets 的 [resume] key（%s）：本进程将拒绝 resume", e)
        return None
    if key is None or not key.get_secret_value():
        log.warning("secrets 里没有 [resume] key：本进程将拒绝 resume")
        return None
    try:
        return bytes.fromhex(key.get_secret_value())
    except ValueError:
        log.warning("[resume] key 不是十六进制：本进程将拒绝 resume")
        return None
