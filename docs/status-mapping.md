# Moomoo 订单状态 → OMS 状态机事件

> M3（2026-11-13）。状态机见 `docs/order-transitions.md`（T01–T55、I01–I03），代码在 `src/mooapp/oms/mapping.py`。
> 出处里的「vendor」指 `docs/vendor/moomoo-API-Doc-en-Python.md`；「learnings」指 `docs/learnings.md`。

## 映射方式

Moomoo 只给出订单的**快照**（`BrokerOrder`：`raw_status`、`dealt_qty`、`dealt_avg_price`），而状态机要的是**事件**。
所以映射的输入是「本地订单 + 券商快照」，输出是一串事件，顺序固定：

1. **先补成交**：快照的 `dealt_qty` 大于本地 `cum_qty` 时，按差额生成一条 `FILL`。
   - `exec_id = "<broker_order_id>:<dealt_qty>"`，和 `broker/convert.py` 的 `FillSynthesizer` 格式相同。这个 id 是确定的，重启后也能去重。
   - 成交价由均价反推：`(dealt_avg_price × dealt_qty − 本地 avg × 本地 cum) / 差额`。
   - 只有 SIMULATE 这样做。REAL 的成交只从成交记录来（成交推送和 `query_deals`），快照的 `dealt_qty` 只用于一致性检查，见下文「REAL 的成交」。
   - 快照的 `dealt_qty` 小于本地 `cum_qty`，说明是乱序到达的旧推送：不产生成交，也不回退本地数量。
2. **再处理状态**：按下表把 `raw_status` 翻译成 0 或 1 个事件。补充两条规则：
   - 本地还在 PENDING_NEW 或 UNKNOWN（没看到确认），快照却已是 `CANCELLED_*`：先补一个 `ACK`，再发撤销事件。撤销说明这张单曾被接受；不补的话，PENDING_NEW 加撤销在矩阵里没有对应转换。这种情况出现在推送丢失、轮询直接看到终态时。
   - 本地已是同一结局（CANCELED 再收到撤销，REJECTED 再收到 `FAILED`）：不再发事件，避免重复快照被当成非法回报而误报警。

## 映射表

vendor § Trading Definitions › Order Status（第 26023 行起）共 11 个取值（`NONE` 加 10 个）；第 12 行是兜底。

| # | Moomoo `OrderStatus` | vendor 原文 | 事件 | 说明 | 出处 |
|---|---|---|---|---|---|
| 1 | `NONE` | Unknown status | **映射不了，报警** | 文档只写了「未知」 | vendor § Order Status |
| 2 | `WAITING_SUBMIT` | Queued (Futu server has received your order and is preparing to submit it to the exchange.) | 不产生事件，记录 | 券商已收到但还没送交易所，本地保持 PENDING_NEW。它和本地的 `WAITING_SUBMIT`（OMS 自己持有的非交易时段队列）不是一回事；v1 由闸门判断时段，不使用本地 `WAITING_SUBMIT` | vendor § Order Status |
| 3 | `SUBMITTING` | Submitting (Futu server has sent your order to the exchange, and the exchange is processing the order.) | 不产生事件，记录 | 交易所还没确认 | vendor § Order Status；learnings「M2 › 推送顺序」：每张单依次推送 `SUBMITTING` → `SUBMITTED` → 终态 |
| 4 | `SUBMITTED` | Working (Order has been successfully submitted to the exchange.) | `ACK` | PENDING_NEW 或 UNKNOWN 时走 T13、T18；已经过了确认阶段时按 I01 忽略（迟到或重复的确认） | vendor § Order Status；`recordings/pushes/SIMULATE_20261113T184058Z.jsonl` |
| 5 | `FILLED_PART` | Partially filled | 只补 `FILL`（第 1 步） | 状态由数量决定（T16、T20、T25、T31、T40、T49）。模拟盘录不到部分成交，由 FakeBroker 注入 | vendor § Order Status；learnings「M2 › 模拟盘不会部分成交」 |
| 6 | `FILLED_ALL` | Fully filled | 只补 `FILL`（第 1 步） | 补齐后状态机走到 FILLED（T17、T21、T26、T32、T41、T50）。补完成交后本地 `cum_qty` 仍不等于 `qty`：报警（`ALERT.UnexpectedReport`） | vendor § Order Status；M2 录制 |
| 7 | `CANCELLED_PART` | Partially cancelled | 先补 `FILL`，再发：本地是 PENDING_CANCEL 时发 `CANCEL_ACK`（T37），否则发 `UNSOLICITED_CANCEL`（T30、T36、T43、T52） | 部分成交后撤销，`cum_qty > 0` | vendor § Order Status |
| 8 | `CANCELLED_ALL` | Fully cancelled | 同上 | `cum_qty = 0` | vendor § Order Status；M2 录制（21 张单）；learnings「M2 › 撤单到终态约 0.05 秒」 |
| 9 | `FAILED` | Failed. Rejected by server. | `EXCH_REJECT`，原因取 `last_err_msg` | T14、T19 | vendor § Order Status |
| 10 | `DISABLED` | Deactivated (Actively operate a disabled order, this will not be submitted to the exchange.) | **暂时映射不了，报警** | 不知道它和「DAY 单收盘过期」是什么关系。`order_state.py` 的 `display_status` 把 EXPIRED 显示为 DISABLED，但它自己注明「未对照 Futu 的实际行为」。等探针 P9 实测 | vendor § Order Status；`docs/reference/order_state.py` `display_status` 注释 |
| 11 | `DELETED` | Deleted, only unfilled orders can be deleted (The status after you actively delete the order.) | **暂时映射不了，报警** | 不知道 App 里的「删除」和「撤单」是不是两回事、会不会经过交易所。等探针 P9 和你在 App 里实测 | vendor § Order Status |
| 12 | 上表以外的字符串 | — | **映射不了，报警** | 防止 SDK 升级后出现新状态 | — |

终态集合沿用 `broker/port.py` 的 `TERMINAL_STATUSES`（`FILLED_ALL`、`CANCELLED_PART`、`CANCELLED_ALL`、`FAILED`、`DISABLED`、`DELETED`）。

## 映射不了的状态怎么处理

不静默忽略：

- 写一条 `order_events`：`outcome = 'unmapped'`，`raw_status` 是原文，`payload` 是整个快照；
- 发 `ALERT.UnmappedStatus`；
- 订单状态和数量都不变，第 1 步的成交照样补。（成交要按 vendor 定义的 `dealt_qty` 记账，状态不明并不影响数量）；
- `mooapp order list` 里标出这张单。

## 本地动作产生的事件（不来自快照）

| 时机 | 事件 | 出处 |
|---|---|---|
| 写库后、调用 `place_order` 之前 | `SUBMIT`（T02） | SPEC 5.4；本文件 |
| `place_order` 超时或抛 `BrokerUnavailable` | `ACK_TIMEOUT`（T15） | order-transitions.md |
| UNKNOWN 时，用刷新缓存的查询确认券商端没有这个 remark，且 attempts < 3 | `RESUBMIT`（T55，M3 新增），用同一个 client_order_id 重发 | order-transitions.md T55 |
| UNKNOWN 时重发次数已用完，仍然查不到 | `STATUS_RESOLVED(REJECTED)`（T23），报警 | order-transitions.md |
| `cancel_order` 返回成功 | `CANCEL_REQ`（T27、T33）。撤单是异步的，要等快照变成 `CANCELLED_*` 才发 `CANCEL_ACK` | learnings「M1 › 撤单返回成功 ≠ 已撤销」 |
| `cancel_order` 返回 ret≠OK | `CANCEL_REJECT`（T38、T39；终态时按 I02 忽略） | vendor § Modify or Cancel Orders |

Moomoo 没有「撤单中」状态（vendor § Order Status 的 11 个取值里没有），PENDING_CANCEL 只存在于本地。

## REAL 的成交

vendor § Deals Push Callback、§ Get Today's Deals：只有实盘有成交推送和成交查询。

- 成交只从成交记录来，`exec_id = deal_id`。
- 订单快照的 `dealt_qty` 大于本地 `cum_qty` 时，不从快照合成成交，而是立刻触发一次 `query_deals` 轮询。
- 快照已经是终态（如 `FILLED_ALL`），但本地 `cum_qty` 还没追上：先不发状态事件，等成交补齐后由下一条快照或下一次轮询推进。超过一个轮询周期仍对不上就报警。

## remark 与 client_order_id

- vendor § Place Orders › remark：「The maximum length after converting to utf8 is 64 bytes」。
- 格式 `moo:<32 位小写 hex>`，共 36 字节（`broker/port.py` `encode_remark`）；M2 计划第 4 点。
- 实测：M2 模拟盘契约测试的 23 张单，remark 都在推送里原样带回（learnings「M2 实现 › 模拟盘契约测试」）。
- 快照按 remark 里的 client_order_id 关联本地订单，所以推送比 `place_order` 返回先到时也能关联上（vendor § Place Orders 提示；learnings「M2 › 推送顺序」实测早约 3 毫秒）。
- remark 不以 `moo:` 开头（包括 `probe:`）就是外部订单：只告警，不处理（ADR-0019）。以 `moo:` 开头但本地没有这个 cid：写 `recon_events` 并报警，不自动接管。
