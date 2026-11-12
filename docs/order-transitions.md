> 来源：`~/Downloads/TRANSITIONS.md`（2026-11-01），与 `docs/reference/order_state.py`（来自 `~/Downloads/order_state.py`）配套，原样复制。
> 矩阵按 FIX 语义编写；映射到 Moomoo 订单状态是 M3 的工作（docs/status-mapping.md）。

# 订单状态转换矩阵

与 `order_state.py` 一一对应：54 条转换（T01–T54）加 3 条忽略规则（I01–I03），覆盖全部 57 个合法的（状态，事件）组合。

## 记号

- `qty` 订单数量，`cum` 已成交量，`q` 本次成交量，`new_qty` 改单后的数量，`base` 撤/改在途前的状态，`resolved` 对账结论。
- Guard 以"上游："开头的，由风控、调度或行情模块判定后才产生事件，状态机本身不校验；其余 Guard 由 `apply()` 校验。

## 通用规则（先于表格生效）

1. **去重：** 带 `exec_id` 的事件如果已经处理过，直接忽略，不产生任何 Emits。
2. **表外组合：** 抛 `InvalidTransition`，订单不变。用户请求返回错误；交易所回报另发 `ALERT.UnexpectedReport`。
3. **守卫不满足：** 成交量超过剩余量抛 `Overfill`；改单数量不大于已成交、对账结论与已记账成交不符、改单确认的新数量低于已成交，抛 `BadRequest`。订单不变；来自交易所的另发 `ALERT`。
4. **审计：** 每次成功转换写审计日志，表中不逐行列出。



## Event List
  
    # OMS 内部
    RISK_REJECT          # 风控不通过（下单时，或券商侧持有单放行前复核）
    SUBMIT               # 风控通过，发 NewOrderSingle (35=D)
    QUEUE_OFF_HOURS     # 非交易时段，进入待开盘队列
    PARK_FOR_TRIGGER    # 止损类订单，进入待触发
    SESSION_OPEN        # 时段切换，放行待开盘订单
    TRIGGER_HIT         # 行情满足触发价
    ACK_TIMEOUT         # 等确认超时 / 会话断开
    STATUS_RESOLVED      # 对账得出结论（OrderStatusRequest 35=H / drop copy）
   
    # 用户请求
    CANCEL_REQ                # OrderCancelRequest (35=F)
    REPLACE_REQ              # OrderCancelReplaceRequest (35=G)
   
    # 交易所回报
    ACK                           # ExecType=0 New
    EXCH_REJECT              # ExecType=8 Rejected
    FILL                            # ExecType=F Trade
    CANCEL_ACK                # ExecType=4 Canceled（应答撤单请求）
    CANCEL_REJECT          # 35=9, CxlRejResponseTo=1
    REPLACE_ACK              # ExecType=5 Replaced
    REPLACE_REJECT        # 35=9, CxlRejResponseTo=2
    EXPIRE                        # ExecType=C Expired（DAY 收盘、GTD 到期）
    UNSOLICITED_CANCEL    # 非用户发起的撤销：IOC 余量、熔断、kill switch



## Emits 词表

| Emit | 含义 |
|---|---|
| `EX.New` / `EX.Cancel` / `EX.Replace` / `EX.Status` | 发往交易所：NewOrderSingle (35=D) / OrderCancelRequest (35=F) / OrderCancelReplaceRequest (35=G) / OrderStatusRequest (35=H) |
| `HOLD.Freeze` | 冻结：买单冻资金（qty × 限价，市价按估价加缓冲），卖单冻持仓 |
| `HOLD.Adjust` | 重算冻结额 |
| `HOLD.Release` | 释放冻结：剩余部分或成交后的余额 |
| `LEDGER.Fill` | 成交入账：持仓、资金、均价；对应的冻结转为实扣 |
| `PUSH` | 向用户和 API 订阅者推送订单更新（括号内为附带信息） |
| `TIMER.AckStart` / `TIMER.AckStop` | 启动 / 停止确认超时计时器 |
| `ALERT.*` | 运维告警 |

Emits 是给资金冻结层、推送层的接口约定；当前 `order_state.py` 只做状态转换，还没有实现发出。

## 矩阵

| ID | From_Status | Event | Guard | To_Status | Emits |
|---|---|---|---|---|---|
| T01 | CREATED | RISK_REJECT | 上游：风控不通过 | REJECTED | PUSH(原因) |
| T02 | CREATED | SUBMIT | 上游：风控通过；交易时段内；非触发类订单 | PENDING_NEW | HOLD.Freeze, EX.New, TIMER.AckStart, PUSH |
| T03 | CREATED | QUEUE_OFF_HOURS | 上游：风控通过；非交易时段 | WAITING_SUBMIT | HOLD.Freeze, PUSH |
| T04 | CREATED | PARK_FOR_TRIGGER | 上游：风控通过；止损 / 跟踪止损类 | PENDING_TRIGGER | HOLD.Freeze, PUSH |
| T05 | WAITING_SUBMIT | SESSION_OPEN | 上游：可交易时段开始；复核风控通过 | PENDING_NEW | EX.New, TIMER.AckStart, PUSH |
| T06 | WAITING_SUBMIT | RISK_REJECT | 上游：放行前复核不通过 | REJECTED | HOLD.Release, PUSH(原因) |
| T07 | WAITING_SUBMIT | CANCEL_REQ | —（未到交易所，本地完成） | CANCELED | HOLD.Release, PUSH |
| T08 | WAITING_SUBMIT | EXPIRE | 上游：放行前有效期结束 | EXPIRED | HOLD.Release, PUSH |
| T09 | PENDING_TRIGGER | TRIGGER_HIT | 上游：行情越过触发价且通过坏价过滤；复核风控通过 | PENDING_NEW | HOLD.Adjust(按触发后估价), EX.New, TIMER.AckStart, PUSH |
| T10 | PENDING_TRIGGER | RISK_REJECT | 上游：触发后复核不通过 | REJECTED | HOLD.Release, PUSH(原因) |
| T11 | PENDING_TRIGGER | CANCEL_REQ | —（未到交易所，本地完成） | CANCELED | HOLD.Release, PUSH |
| T12 | PENDING_TRIGGER | EXPIRE | 上游：未触发且有效期结束 | EXPIRED | HOLD.Release, PUSH |
| T13 | PENDING_NEW | ACK | — | NEW | TIMER.AckStop, PUSH |
| T14 | PENDING_NEW | EXCH_REJECT | — | REJECTED | TIMER.AckStop, HOLD.Release, PUSH(原因) |
| T15 | PENDING_NEW | ACK_TIMEOUT | 上游：确认计时器到期或会话断开 | UNKNOWN | EX.Status, ALERT.AckTimeout |
| T16 | PENDING_NEW | FILL | cum+q < qty（确认未到先成交） | PARTIALLY_FILLED | TIMER.AckStop, LEDGER.Fill, PUSH |
| T17 | PENDING_NEW | FILL | cum+q = qty | FILLED | TIMER.AckStop, LEDGER.Fill, HOLD.Release(余额), PUSH |
| T18 | UNKNOWN | ACK | — | NEW | ALERT.Resolved, PUSH |
| T19 | UNKNOWN | EXCH_REJECT | — | REJECTED | ALERT.Resolved, HOLD.Release, PUSH(原因) |
| T20 | UNKNOWN | FILL | cum+q < qty | PARTIALLY_FILLED | ALERT.Resolved, LEDGER.Fill, PUSH |
| T21 | UNKNOWN | FILL | cum+q = qty | FILLED | ALERT.Resolved, LEDGER.Fill, HOLD.Release(余额), PUSH |
| T22 | UNKNOWN | STATUS_RESOLVED | resolved=NEW 且 cum=0（有成交须先按 T20/T21 补入） | NEW | ALERT.Resolved, PUSH |
| T23 | UNKNOWN | STATUS_RESOLVED | resolved=REJECTED（交易所查无此单） | REJECTED | ALERT.Resolved, HOLD.Release, PUSH(原因) |
| T24 | UNKNOWN | STATUS_RESOLVED | resolved=CANCELED | CANCELED | ALERT.Resolved, HOLD.Release, PUSH |
| T25 | NEW | FILL | cum+q < qty | PARTIALLY_FILLED | LEDGER.Fill, PUSH |
| T26 | NEW | FILL | cum+q = qty | FILLED | LEDGER.Fill, HOLD.Release(余额), PUSH |
| T27 | NEW | CANCEL_REQ | — | PENDING_CANCEL | EX.Cancel, PUSH |
| T28 | NEW | REPLACE_REQ | new_qty > cum；数量或价格有变化；带新 ClOrdID | PENDING_REPLACE | HOLD.Adjust(取新旧较大), EX.Replace |
| T29 | NEW | EXPIRE | — | EXPIRED | HOLD.Release(剩余), PUSH |
| T30 | NEW | UNSOLICITED_CANCEL | — | CANCELED | HOLD.Release(剩余), PUSH(原因) |
| T31 | PARTIALLY_FILLED | FILL | cum+q < qty | PARTIALLY_FILLED | LEDGER.Fill, PUSH |
| T32 | PARTIALLY_FILLED | FILL | cum+q = qty | FILLED | LEDGER.Fill, HOLD.Release(余额), PUSH |
| T33 | PARTIALLY_FILLED | CANCEL_REQ | — | PENDING_CANCEL | EX.Cancel, PUSH |
| T34 | PARTIALLY_FILLED | REPLACE_REQ | new_qty > cum；数量或价格有变化；带新 ClOrdID | PENDING_REPLACE | HOLD.Adjust(取新旧较大), EX.Replace |
| T35 | PARTIALLY_FILLED | EXPIRE | — | EXPIRED | HOLD.Release(剩余), PUSH |
| T36 | PARTIALLY_FILLED | UNSOLICITED_CANCEL | — | CANCELED | HOLD.Release(剩余), PUSH(原因) |
| T37 | PENDING_CANCEL | CANCEL_ACK | — | CANCELED | HOLD.Release(剩余), PUSH |
| T38 | PENDING_CANCEL | CANCEL_REJECT | base=NEW（cum=0） | NEW | PUSH(原因) |
| T39 | PENDING_CANCEL | CANCEL_REJECT | base=PARTIALLY_FILLED（cum>0） | PARTIALLY_FILLED | PUSH(原因) |
| T40 | PENDING_CANCEL | FILL | cum+q < qty | PENDING_CANCEL（base→PARTIALLY_FILLED） | LEDGER.Fill, PUSH |
| T41 | PENDING_CANCEL | FILL | cum+q = qty | FILLED（撤单请求作废） | LEDGER.Fill, HOLD.Release(余额), PUSH |
| T42 | PENDING_CANCEL | EXPIRE | — | EXPIRED | HOLD.Release(剩余), PUSH |
| T43 | PENDING_CANCEL | UNSOLICITED_CANCEL | — | CANCELED | HOLD.Release(剩余), PUSH(原因) |
| T44 | PENDING_REPLACE | REPLACE_ACK | cum = 0 | NEW（新数量、价格、ClOrdID 生效） | HOLD.Adjust(按新单), PUSH |
| T45 | PENDING_REPLACE | REPLACE_ACK | 0 < cum < new_qty | PARTIALLY_FILLED（同上） | HOLD.Adjust(按新单), PUSH |
| T46 | PENDING_REPLACE | REPLACE_ACK | cum = new_qty（在途成交恰好补满新数量） | FILLED | HOLD.Release(余额), PUSH |
| T47 | PENDING_REPLACE | REPLACE_REJECT | base=NEW（cum=0） | NEW | HOLD.Adjust(回到原单), PUSH(原因) |
| T48 | PENDING_REPLACE | REPLACE_REJECT | base=PARTIALLY_FILLED（cum>0） | PARTIALLY_FILLED | HOLD.Adjust(回到原单), PUSH(原因) |
| T49 | PENDING_REPLACE | FILL | cum+q < qty（按原数量） | PENDING_REPLACE（base→PARTIALLY_FILLED） | LEDGER.Fill, PUSH |
| T50 | PENDING_REPLACE | FILL | cum+q = qty（按原数量） | FILLED（改单请求作废） | LEDGER.Fill, HOLD.Release(余额，含改单多冻部分), PUSH |
| T51 | PENDING_REPLACE | EXPIRE | — | EXPIRED | HOLD.Release(剩余), PUSH |
| T52 | PENDING_REPLACE | UNSOLICITED_CANCEL | — | CANCELED | HOLD.Release(剩余), PUSH(原因) |
| T53 | CANCELED | FILL | cum+q ≤ qty（已撤后迟到的成交） | CANCELED（不变，cum 增加） | LEDGER.Fill, ALERT.LateFill, PUSH |
| T54 | EXPIRED | FILL | cum+q ≤ qty（已过期后迟到的成交） | EXPIRED（不变，cum 增加） | LEDGER.Fill, ALERT.LateFill, PUSH |
| I01 | NEW, PARTIALLY_FILLED, PENDING_CANCEL, PENDING_REPLACE, FILLED, CANCELED, EXPIRED | ACK | —（迟到或重复的确认） | 不变（忽略） | — |
| I02 | FILLED, CANCELED, EXPIRED, REJECTED | CANCEL_REJECT | —（撤单太晚，订单已结束） | 不变（忽略） | — |
| I03 | FILLED, CANCELED, EXPIRED, REJECTED | REPLACE_REJECT | —（改单太晚，订单已结束） | 不变（忽略） | — |
