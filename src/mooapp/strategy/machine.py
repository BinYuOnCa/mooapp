"""止损止盈规则状态机（纯逻辑：输入事件，输出动作；不调用闸门、OMS、券商）。

状态：PENDING（等入场成交）→ ARMED（价格已冻结，监控中）→ TRIGGERED（卖单在途）→ CLOSED；另有 VOID、ERROR。
依据：ADR-0005（首次成交时算一次价格）、0007（立即市价卖出；冻结时已越过就立即触发）、0008（数量 = 已买未卖）、
0009（止损向上、止盈向下取整）、0021（卖单带 protective；halt 时暂停）、0023（入场单过期）、0027（边界与执行细节）。

动作由调用方执行（M6b 接闸门与 OMS；dry-run 直接模拟）：
- PlaceProtectiveSell：经闸门发市价卖单，Intent(source=RULE, protective=True)
- CancelEntry：经闸门撤入场单余量（ADR-0027 第 3 条）
- RuleNote：给人看的说明或告警（kind 见各处）
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from mooapp.strategy.expr import ExprError
from mooapp.strategy.spec import RuleSpec
from mooapp.strategy.ticks import round_tick

TriggerSide = Literal["stop", "take"]
SIDE_CN = {"stop": "止损", "take": "止盈"}


class RuleStatus(StrEnum):
    PENDING = "PENDING"
    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    CLOSED = "CLOSED"
    VOID = "VOID"
    ERROR = "ERROR"
    CANCELED = "CANCELED"  # M6b：mooapp rule cancel


ACTIVE_STATUSES = frozenset({RuleStatus.PENDING, RuleStatus.ARMED, RuleStatus.TRIGGERED})


@dataclass(frozen=True)
class PlaceProtectiveSell:
    rule_id: str
    symbol: str
    qty: int
    side: TriggerSide
    trigger_price: Decimal
    observed_price: Decimal  # 触发时看到的价格（逐笔为 last；K 线按触发价估计）
    attempt: int  # 第几次发卖单（失败累计，ADR-0027 第 4 条）


@dataclass(frozen=True)
class CancelEntry:
    rule_id: str


@dataclass(frozen=True)
class RuleNote:
    kind: str  # awaiting_open / frozen / triggered / bar_both_sides / sell_rejected / closed / void / error / unprotected_fill / cancelled
    message: str


Action = PlaceProtectiveSell | CancelEntry | RuleNote


class RuleMachine:
    def __init__(self, spec: RuleSpec, max_sell_attempts: int = 3):
        self.spec = spec
        self.max_sell_attempts = max_sell_attempts
        self.status = RuleStatus.PENDING
        self.open: Decimal | None = None
        self.first_fill_price: Decimal | None = None
        self.stop_price: Decimal | None = None
        self.take_price: Decimal | None = None
        self.bought = 0
        self.sold = 0
        self.entry_done = False
        self.paused = False
        self.side: TriggerSide | None = None
        self.failures = 0
        self.retry_pending = False
        self._last_observed: Decimal | None = None

    @property
    def held(self) -> int:
        """ADR-0008：已买入且未卖出的数量。"""
        return self.bought - self.sold

    # ------------------------------------------------------------------ 输入事件

    def on_open(self, price: Decimal) -> list[Action]:
        """当天常规时段开盘价（ADR-0005）。首次成交时还没拿到开盘价的，在这里冻结。"""
        self.open = price
        if self.status is RuleStatus.PENDING and self.first_fill_price is not None:
            return self._freeze(last=None)
        return []

    def on_entry_fill(self, qty: int, price: Decimal, last: Decimal | None = None) -> list[Action]:
        self.bought += qty
        if self.status is RuleStatus.PENDING:
            if self.first_fill_price is None:
                self.first_fill_price = price  # 首次成交时 avg_cost 就是首次成交价（ADR-0027 第 5 条）
            if self.open is None:
                return [RuleNote("awaiting_open", "已成交但还没拿到开盘价，暂不计算止损止盈价")]
            return self._freeze(last if last is not None else price)
        if self.status in (RuleStatus.TRIGGERED, RuleStatus.CLOSED):
            # 撤入场单余量生效之前又成交：补发卖单（ADR-0027 第 3 条）
            self.status = RuleStatus.TRIGGERED
            observed = self._last_observed if self._last_observed is not None else price
            return [self._sell(qty, observed)]
        if self.status in (RuleStatus.ERROR, RuleStatus.VOID, RuleStatus.CANCELED):
            return [RuleNote("unprotected_fill", f"规则处于 {self.status.value}，新成交的 {qty} 股没有止损止盈保护")]
        return []  # ARMED：数量增加，价格不重算（ADR-0005）

    def on_entry_terminal(self) -> list[Action]:
        """入场单结束（全部成交、过期、被撤或被拒）。"""
        self.entry_done = True
        if self.status is RuleStatus.PENDING and self.bought == 0:
            self.status = RuleStatus.VOID
            return [RuleNote("void", "入场单一股未成交就结束，规则作废（ADR-0023）")]
        if self.status is RuleStatus.TRIGGERED:
            return self._maybe_close()
        return []

    def on_tick(self, last: Decimal) -> list[Action]:
        if self.paused:
            return []
        self._last_observed = last
        return self._on_quote(stop_hit=self._le_stop(last), take_hit=self._ge_take(last), stop_obs=last, take_obs=last)

    def on_bar(self, low: Decimal, high: Decimal) -> list[Action]:
        if self.paused or self.stop_price is None or self.take_price is None:
            return []
        # K 线内的成交价无法知道，按触发价估计
        return self._on_quote(
            stop_hit=low <= self.stop_price,
            take_hit=high >= self.take_price,
            stop_obs=self.stop_price,
            take_obs=self.take_price,
            bar=True,
        )

    def on_sell_rejected(self, reason: str) -> list[Action]:
        if self.status is not RuleStatus.TRIGGERED:
            return []  # 规则已结束或已取消：迟到的拒单不再处理
        self.failures += 1
        if self.failures >= self.max_sell_attempts:
            self.status = RuleStatus.ERROR
            self.retry_pending = False
            return [RuleNote("error", f"卖单已失败 {self.failures} 次（最后一次：{reason}），规则停止，请人工处理（ADR-0027 第 4 条）")]
        self.retry_pending = True
        return []

    def on_sell_filled(self, qty: int, price: Decimal) -> list[Action]:
        self.sold += qty
        return self._maybe_close()

    def cancel(self) -> list[Action]:
        """M6b：mooapp rule cancel。活动规则才能取消：撤入场单余量、停止监控；还有持仓时提示不再受保护。"""
        if self.status not in ACTIVE_STATUSES:
            return []
        acts: list[Action] = []
        if not self.entry_done:
            acts.append(CancelEntry(self.spec.id))
        self.status = RuleStatus.CANCELED
        msg = "规则已取消"
        if self.held > 0:
            msg += f"；持仓 {self.held} 股不再受止损止盈保护"
        acts.insert(0, RuleNote("cancelled", msg))
        return acts

    # ------------------------------------------------------------------ 状态快照（M6b：存进 rules.state，停机重启后恢复）

    _DEC_FIELDS = ("open", "first_fill_price", "stop_price", "take_price", "_last_observed")
    _PLAIN_FIELDS = ("bought", "sold", "entry_done", "paused", "side", "failures", "retry_pending")

    def to_state(self) -> dict[str, object]:
        st: dict[str, object] = {"status": self.status.value}
        for f in self._DEC_FIELDS:
            v = getattr(self, f)
            st[f.lstrip("_")] = str(v) if v is not None else None
        for f in self._PLAIN_FIELDS:
            st[f] = getattr(self, f)
        return st

    @classmethod
    def from_state(cls, spec: RuleSpec, state: dict[str, object], max_sell_attempts: int = 3) -> RuleMachine:
        m = cls(spec, max_sell_attempts)
        m.status = RuleStatus(str(state["status"]))
        for f in cls._DEC_FIELDS:
            v = state.get(f.lstrip("_"))
            setattr(m, f, Decimal(str(v)) if v is not None else None)
        for f in cls._PLAIN_FIELDS:
            if f in state:
                setattr(m, f, state[f])
        return m

    def pause(self) -> None:
        """halt 时暂停（ADR-0021）。"""
        self.paused = True

    def resume(self) -> None:
        """恢复后的下一笔行情照常判断（ADR-0027 第 8 条）。"""
        self.paused = False

    # ------------------------------------------------------------------ 内部

    def _le_stop(self, price: Decimal) -> bool:
        return self.stop_price is not None and price <= self.stop_price

    def _ge_take(self, price: Decimal) -> bool:
        return self.take_price is not None and price >= self.take_price

    def _freeze(self, last: Decimal | None) -> list[Action]:
        assert self.open is not None and self.first_fill_price is not None
        env = {"open": self.open, "avg_cost": self.first_fill_price, "last": last if last is not None else self.first_fill_price}
        try:
            stop = round_tick(self.spec.stop(env), "up")
            take = round_tick(self.spec.take(env), "down")
        except (ExprError, ValueError) as e:
            self.status = RuleStatus.ERROR
            return [RuleNote("error", f"止损止盈价算不出来：{e}")]
        if not (0 < stop < take):
            self.status = RuleStatus.ERROR
            return [RuleNote("error", f"止损价 {stop} 必须为正且小于止盈价 {take}（ADR-0027 第 6 条），规则不监控")]
        self.stop_price, self.take_price = stop, take
        self.status = RuleStatus.ARMED
        acts: list[Action] = [
            RuleNote("frozen", f"止损价 {stop}，止盈价 {take}（开盘价 {self.open}，首次成交价 {self.first_fill_price}）")
        ]
        if last is not None and not self.paused:
            self._last_observed = last
            acts += self._on_quote(self._le_stop(last), self._ge_take(last), last, last)  # ADR-0007：已越过就立即触发
        return acts

    def _on_quote(self, stop_hit: bool, take_hit: bool, stop_obs: Decimal, take_obs: Decimal, bar: bool = False) -> list[Action]:
        if self.status is RuleStatus.TRIGGERED and self.retry_pending:
            assert self.side is not None
            still = stop_hit if self.side == "stop" else take_hit
            if still:
                self.retry_pending = False
                return [self._sell(self.held, stop_obs if self.side == "stop" else take_obs)]
            # 价格回到两个触发价之间（或到了另一边）：重新监控两边（ADR-0027 第 4 条）
            self.status, self.side, self.retry_pending = RuleStatus.ARMED, None, False
        if self.status is not RuleStatus.ARMED:
            return []
        acts: list[Action] = []
        if stop_hit and take_hit:
            side: TriggerSide = "take"  # ADR-0027 第 2 条（你的决定）
            acts.append(RuleNote("bar_both_sides", "同一时段两边都碰到，时段内先后顺序不明，按止盈计（ADR-0027）"))
        elif take_hit:
            side = "take"
        elif stop_hit:
            side = "stop"
        else:
            return []
        return acts + self._trigger(side, take_obs if side == "take" else stop_obs, bar)

    def _trigger(self, side: TriggerSide, observed: Decimal, bar: bool) -> list[Action]:
        self.status, self.side = RuleStatus.TRIGGERED, side
        trigger = self.stop_price if side == "stop" else self.take_price
        assert trigger is not None
        other = SIDE_CN["take" if side == "stop" else "stop"]
        how = "（K 线，成交价按触发价估计）" if bar else ""
        # 先说明，再撤入场余量、再卖：调用方按顺序执行，输出也按发生顺序
        acts: list[Action] = [RuleNote("triggered", f"{SIDE_CN[side]}触发 @{trigger}{how}，看到 {observed}，卖 {self.held} 股；{other}作废")]
        if not self.entry_done:
            acts.append(CancelEntry(self.spec.id))  # ADR-0027 第 3 条：先撤入场单余量
        acts.append(self._sell(self.held, observed))
        return acts

    def _sell(self, qty: int, observed: Decimal) -> PlaceProtectiveSell:
        assert self.side is not None
        trigger = self.stop_price if self.side == "stop" else self.take_price
        assert trigger is not None
        return PlaceProtectiveSell(self.spec.id, self.spec.symbol, qty, self.side, trigger, observed, self.failures + 1)

    def _maybe_close(self) -> list[Action]:
        if self.status is RuleStatus.TRIGGERED and self.held == 0 and self.entry_done:
            self.status = RuleStatus.CLOSED
            return [RuleNote("closed", f"已全部卖出（买 {self.bought}、卖 {self.sold}），规则结束")]
        return []
