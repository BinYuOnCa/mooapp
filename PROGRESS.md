# PROGRESS

**当前（M6b，分支 `dev/milestone_6`）**：2026-11-14 审查修复完成（见「M6b 审查修复」），`pytest` 727 passed，`pytest -m pg` 42 passed；死锁测试和常驻进程测试连跑 5 次都通过。待开盘：`mooapp db migrate`（0003）→ 探针 P11 → 验收 `tests/acceptance/test_m6_rule_oco.py`。下一步 `/milestone-git commit 6`
**M4 审查修复已合并进本分支**（2026-11-14，fb1580e、4edf086）：解决了 7 个文件的冲突（规则引擎和 resume 签名共用 `CommandProcessor`；`poll_stale_quotes` 留在命令线程，心跳改为独立线程）。`pytest` 755 passed，`pytest -m pg` 43 passed；常驻进程和规则引擎的线程测试连跑 5 次都通过
**ADR-0030**（2026-11-14，你选 C）：`rule_add`、`rule_cancel` 也随命令行超时过期；halt、resume 不过期。`pytest` 759 passed，`pytest -m pg` 43 passed。命令行拿到 expired 结果时提示「命令已过期，未执行」（下单、撤单、规则命令都是），`pytest` 763 passed
**M6a**：2026-11-14 实现完成并提交（279598f、d69b278），`pytest` 667 passed，`pytest -m pg` 35 passed。验收 1（5 条价格路径）和验收 2（非法表达式在加载时被拒）由 `test_rule_machine.py`、`test_dryrun.py`、`test_rule_spec.py` 覆盖；可以手动试 `uv run mooapp rule dry-run examples/rules/spy-oco.yaml --prices examples/prices/path1_stop_first.csv`。下一步 `/milestone-git commit 6`；M6b 另行计划
**M4（分支 `dev/milestone_4`）**：第 3 步完成（2026-11-14）：闸门完整规则、halt 与 resume、ADR-0026 待撤、按账户共用限流器、commands 表与 `mooapp run`、人锁代码（实盘仍关闭）。`pytest` 550 passed，`pytest -m pg` 35 passed（Claude 在 sandbox 内运行）。M3 实现已完成，等周一常规时段验收
**下一步**：周一（2026-11-16）按下面「周一操作清单」逐步执行。**进度（美东约 10:30）**：第 0 步、0.1–0.5、第 1 步完成；M3 两份验收通过（App 已核对）；0.6 契约测试通过；P10 完成（today_pl_val 有效；can_sell_qty 已扣挂单）；P11 常规时段完成（约每秒 6 条推送，美东时间，open_price 为当天开盘价；16:00 后再跑一次对照）；M4 验收通过（第二次；验收价格系数 0.9 → 0.96）；第 5 步完成；**M6 验收通过**（take 触发）。**今天的验收全部完成**：M3（验收 3、4）、M4（验收 3）、M6（验收 3）；待办见「验收后待办」P9 已有结论（见下方「P9 结论」）


## M5 计划（已确认 2026-11-16；分支 `dev/milestone_5`，worktree `.claude/worktrees/m5`）

目标：SPEC 8 节 M5 三条验收（SMTP 密码填错时交易照常、改正后补发；一笔单分 5 次成交只收 1 封合并邮件；连续 3 个交易日收到心跳）。与 M7（主目录 `dev/milestone_7`）并行开发。

**状态（2026-11-16 晚）**：实现完成，`pytest` 840 passed，`pytest -m pg` 44 passed；通知器与常驻进程的线程测试连跑 3 次通过。新增 `src/mooapp/notify/`（mailer、compose、notifier）、迁移 `0005_m5.sql`（outbox）、`EmailCfg.alert_digest_minutes`；`settings.toml` 心跳改为 15 分钟、汇总 30 分钟；`tests/conftest.py` 的测试配置同步改为 15。待你：在 M5 目录 `uv run mooapp db migrate`（0005）→ 验收 1–3 的草稿复制进 `tests/acceptance/`

### 你的决定（2026-11-16）

1. 常驻进程与 `mooapp eod` 读 secrets 的 `[smtp]` 段：ADR-0032（扩展 ADR-0029 的例外）
2. 发送队列：数据库 **outbox 表**（迁移 `0005_m5.sql`，避开 M7 的 0004），重启后补发；常驻进程统一投递
3. 每次重试前重新读 `[smtp]`（改正密码不用重启）；重启后从 outbox 补发
4. 关键事件立即发（成交、RiskRejected、HaltChanged、`daily_loss_halt` / `halt_cancel_failed` / `resume_rejected` 等）；其余报警**每 30 分钟汇总一封**，可配置 15–120 分钟（`alert_digest_minutes`），没有新报警不发；启动恢复期间的报警并入汇总
5. 心跳：交易日**开盘前 15 分钟（美东 9:15）**，可配置 5–25 分钟（`heartbeat_minutes_before_open`，即 9:05–9:25）；常驻进程晚于该时刻启动则启动后补发一封并标「迟到」；每天一封
6. 重试间隔 1、2、5、15 分钟，之后每 30 分钟，直到发出
7. `mooapp eod` 结束后**由 eod 进程直接发**汇总邮件，不等常驻进程；直接发送失败则写进 outbox，由常驻进程重试（不丢）。eod 在 M7 分支，M5 提供「同步发送、失败转 outbox」的接口，两个分支都合并后再接上

### 设计要点

- `src/mooapp/notify/`：`mailer.py`（Mailer 协议；SmtpMailer：465 用 SSL、587 用 STARTTLS，标准库 smtplib，不加依赖；测试用 FakeMailer 可模拟认证失败、网络错误）、`compose.py`（事件 → 邮件：成交按订单在 `merge_partial_fills_seconds` 窗口内合并，订单终态立即发出；报警汇总；账号只显示后 4 位）、`notifier.py`（订阅 EventBus，只排队不阻塞；后台线程从 outbox 投递、退避重试）、`heartbeat.py`（交易日 9:15 心跳、迟到补发、每天一封）
- `oms/store.py` / `oms/pg_store.py`：outbox（enqueue、取到期待发、标记已发 / 失败与下次重试时间）；内存与 PG 契约测试
- `config/models.py`：`EmailCfg` 增加 `alert_digest_minutes`（15–120）；`heartbeat_minutes_before_open` 改为 5–25；`settings.toml` 默认 15 和 30
- `ops/run.py`：常驻进程启动通知器与心跳；停止时把队列里的邮件留在 outbox
- 测试先写、先失败；验收 1–3 的草稿贴在聊天里，由你复制进 `tests/acceptance/`

## 修复计划（已确认 2026-11-16，分支 `fix/Post_Milestone_3`）

周一验收中发现的两个问题。你的决定：计划 1 选 A（`min` 公式 + ADR-0031）；计划 2 照做；SPEC 由 Claude 改；**先做计划 2**。每份先提交失败的测试，再提交实现。

### 计划 2：启动恢复改查历史订单（P9）

**状态（2026-11-16）**：实现完成，`pytest` 778 passed，`pytest -m pg` 43 passed；新增测试 15 条（实现前 14 条失败，另 1 条是防回归）。已提交。**模拟盘验证通过**（2026-11-16 美东 13:39）：启动 `mooapp run`，841874、841875 经历史订单 NEW → CANCELED（UNSOLICITED_CANCEL，origin poll），不再报 `missing_at_broker`；`mooapp order list` 无未完成订单。同时出现的 `external_order`（P10 探针单）、`unknown_client_order_id`（0.6 契约测试单）是 24 小时列表里的预期报警

- 问题：恢复只用 `order_list_query`（当日列表）。前一天的单（841874、841875）查不到，只报 `missing_at_broker`，本地一直是 NEW
- `broker/port.py`：BrokerPort 新增只读 `query_order_history(start, end) -> list[BrokerOrder]`
- `broker/moomoo.py`：`history_order_list_query` 实现（vendor § Get Historical Orders），时间按美东传
- `broker/throttle.py`：新规则 `history_order_list_query` 30 秒 10 次（vendor § Get Historical Orders › Interface Limitations），按账户共用
- `oms/oms.py` recover：本地未完成、当日列表查不到（CREATED 除外）的订单，**只查一次**历史，窗口 = 最早下单时间前 1 天 ～ 现在后 1 天（vendor 没写清时区，用宽窗口 + 订单号精确匹配，不猜时区）；命中的交给 `ingest_snapshot`（origin=poll）；查不到的照旧 `missing_at_broker`；历史查询出错报警 `history_query_failed`、照旧处理、不让恢复失败
- `broker/fake.py`：`roll_day()` 模拟跨日（未完成单按 DAY 过期为 CANCELLED_ALL，全部移入历史，当日列表清空）；`query_order_history`
- SPEC：BrokerPort 接口列表加一行
- 测试（先写、先失败）：过期 → CANCELED 且不报 missing；历史显示成交 → FILLED 并记成交；历史也查不到 → 照旧 missing；历史查询出错 → 报警、恢复完成；只调一次历史；无缺失时不调；限流规则；Moomoo 适配器传参
- 模拟盘验证：启动一次 `mooapp run`，841874、841875 变为 CANCELED，`missing_at_broker` 消失（不下单）
- 不在本次：常驻进程跨过 20:00 时挂单是否及时结束（取决于推送，未观察）

### 计划 1：闸门可卖数量不再重复扣挂单（P10d）

**模拟盘验证（2026-11-16）**：
- 第一次运行 `tests/contract/test_gate_can_sell_sim.py` 失败：断言「卖单确认后 10 秒内缓存 can_sell 会扣减」不成立 → 写探针 P12 实测：OpenD 缓存不随下单、撤单自动更新，只有刷新查询才更新（learnings「M4 探针 P12」；ADR-0031 补「实测补充」与已知局限）
- 改断言后第二次运行 `1 passed`（美东约 14:20）：缓存 qty 2、can_sell 2（未扣）；第 2 张卖单放行；第 3 张被拒，理由 `min(2 − 0, 2 − 2) = 0`。这次是缓存未刷新的情形，验证的是「不卖超」；「已刷新时不重复扣」由 FakeBroker 测试覆盖（`test_can_sell_adr31.py`）
- 回归第 2 步 `pytest -m sim tests/contract`：17 passed（5 分 33 秒，无限频报错）；P10d/P12 契约打印「qty 1 → 1，can_sell_qty 1 → 1」（缓存未扣，符合 P12）；闸门端到端测试再次通过
- 回归第 3–7 步（美东约 14:30–14:40，修复分支上）：M3 幂等 `1 passed`；M3 重启恢复 `1 passed`（843601，本地 NEW / 券商 SUBMITTED）；M4 halt `1 passed`；`mooapp resume` 解除；M6 OCO `1 passed`（这次 **stop 触发**，卖单 843612 @740.02；上午是 take 触发，两边都在模拟盘验证过）
- **计划 1、计划 2 全部完成，回归全部通过**；下一步：合并 `fix/Post_Milestone_3` 进 main

**状态（2026-11-16）**：实现完成，`pytest` 788 passed，`pytest -m pg` 43 passed；规则引擎和常驻进程的线程测试连跑 3 次都通过。新增测试 10 条（实现前 8 条失败；另 2 条闸门测试在旧 FakeBroker 上本来就通过，改了 FakeBroker 后才真正测到 bug）。`test_gate_m4.py::test_sell_counts_open_sell_orders` 测的是未确认卖单，新规则下结论不变，未改写

- 新规则（ADR-0031，替代 M4 确认点 3 的卖出部分）：`可卖 = min(can_sell_qty − 本程序未确认卖单剩余量, 持仓 qty − 本程序全部未完成卖单剩余量)`；未确认 = PENDING_NEW、UNKNOWN
- 文件：`docs/adr/0031-can-sell-from-broker.md`、`risk/rules.py`、`oms/oms.py`（`open_leaves_both` 同时给出未确认卖单剩余量，仍只扫一次）、`risk/gate.py`、`broker/fake.py`（`can_sell_qty` 扣已确认挂单，照 P10d）
- 测试：四种情形各「刚好够卖 / 差一股」；`test_gate_m4.py::test_sell_counts_open_sell_orders` 按新语义改写
- 不碰 `tests/acceptance/`，不改风控限额

## 周一操作清单（2026-11-16）

### 先确认的重点

1. ~~**M4 验收的 halt 会撤掉 841874、841875**~~（已过时：P9 查明它们周五 20:01 已被券商结束，见「P9 结论」）：这两张在本地数据库里、状态 NEW，halt 撤本程序所有挂单；DAY 过期样本只剩 841876–841883（不在本地库，halt 不碰）。你已确认接受（2026-11-14，选 A），8 张样本够用
2. **两份验收会自己启动常驻进程并占单实例锁**：跑验收时 `mooapp run` 必须是停着的；只有解除 halt（第 5 步）时才开
3. **`mooapp run` 和 `mooapp resume` 在你自己的终端窗口里运行**，不用 `!`：前者是常驻进程；后者要求交互终端输入确认码
4. 每个连 OpenD 的步骤之间等满 **31 秒**（限频按账户算）；全程不用 `--env real`
5. 两份 M3 验收分开跑；M2 契约测试不在 M3 分支跑（M3 的限流器按实例计数），放到 dev/milestone_6 上（0.6）
6. M3 验收的 `finally` 撤单失败会盖掉断言结果；要加处理由你本人改 `tests/acceptance/`。真失败时先看报错是不是 `high frequency`

### 时间安排（美东）

- 9:30 前：第 0 步 doctor、0.1、0.2
- 9:30 后：0.3 → 0.4 → 0.5 → 第 1 步 → 第 2 步 → 第 3 步 → 0.6 → 第 4 步 → 第 5 步 → 第 6 步（每步间隔 31 秒，合计约 30 分钟；第 6 步不要晚于 15:45 开始）
- 16:00 后：P9 `--check`

### 第 0 步：开盘前检查

```
! git branch --show-current
! uv run mooapp doctor
```
- [x] doctor 各项通过、OpenD 在线；没有在运行的 `mooapp run`

### 第 0 步之 M3（dev/milestone_3）

**0.1 切换分支（开盘前）**
```
! git status --short                 # 应为空
! git switch dev/milestone_3
! git log --oneline -1               # 应为 a54bbc7
```
- [x] 工作区干净，在 dev/milestone_3

**0.2 数据库迁移（不连 OpenD）**
```
! uv run mooapp db migrate
```
- [x] 只涉及 `0001_oms.sql`（多半已执行，输出「没有要执行的迁移」）；0002、0003 留到第 1 步

**0.3 M3 验收 4：同一 client_order_id 重试 3 次（常规时段）**
```
! uv run pytest -m sim tests/acceptance/test_m3_idempotent_retry.py -s
```
- 过程：SPY 限价买 1 股 @ 最新价 × 0.9（不会成交），同一 cid 提交 3 次，最后撤单
- [x] `1 passed`（订单 842592）
- [x] App 里只有 **1 张**新的 SPY 买单，已撤销
- 报 `high frequency`：额度被占（App 同时操作、别的进程连着 OpenD），等 31 秒单独重跑

（等 31 秒）

**0.4 M3 验收 3：下单后杀进程、重启恢复（常规时段）**
```
! uv run pytest -m sim tests/acceptance/test_m3_restart_recovery.py -s
```
- 过程：子进程下单后 `os._exit(9)`；父进程重建 OMS、启动恢复、与券商对比，最后撤单
- [x] `1 passed`（订单 842718，本地 NEW / 券商 SUBMITTED）
- [x] 输出「请在 App 里核对订单 <号>：本地 NEW，券商 SUBMITTED」（或 PENDING_NEW/SUBMITTING、UNKNOWN/SUBMITTING）
- [x] **App 目视核对**：这张单存在、只有 1 张、随后被撤销（验收 3 要求你亲自对照）
- 预期有 `unknown_client_order_id` 报警（那 8 张没有本地记录的单），不影响结果

（等 31 秒）

**0.5 切回 dev/milestone_6**
```
! git switch dev/milestone_6
! git log --oneline -1
```
- [x] 切换成功，工作区干净

**0.6 M2 契约测试（dev/milestone_6，常规时段；排在第 3 步之后、第 4 步之前）**
```
! uv run pytest -m sim tests/contract
```
- [x] 全部通过（2026-11-16：15 passed，用时 4 分 42 秒，无限频报错；按账户共用的限流器生效。实际排在第 1 步之后、P10 之前跑）；其中限流验收连下 20 单再全撤，之后**等满 31 秒**再做下一步
- 失败且是 `high frequency`：按账户共用的限流器还有漏洞，把输出发给 Claude

### 第 1 步：数据库迁移（不连 OpenD）

```
! uv run mooapp db migrate
```
- [x] 执行了 `0002_m4.sql` 和 `0003_m6.sql`；再跑一次应没有新迁移
- 后面所有步骤都依赖这一步建的 commands、audit、rules 表

### 第 2 步：探针 P10（常规时段）

```
! uv run python scripts/probe_m4_pnl.py
```
- 副作用：买 1 股 SPY（期望立即成交）→ 挂最新价 × 1.10 的卖单，查持仓后撤掉 → 市价卖 1 股，恢复原持仓
- [x] 退出码 0，stderr 没有 `# 失败：`
- [x] `p10.before`、`after_buy`、`after_sell` 的 `today_pl_val` 都是**数值**，`sum_today_pl_val` 不是「无法求和」
- [x] `p10.with_open_sell`：SPY 的 `can_sell_qty` 是否比 `qty` 少 1 → **是**（qty 1、can_sell 0）：闸门会重复扣，验收后出修复计划（learnings「M4 探针 P10」）
- [x] App 里没有残留的 probe 挂单
- `today_pl_val` 无效：闸门会拒绝所有买单，第 4、6 步必然失败 → **停下**，把结果发给 Claude
- `can_sell_qty` 已扣掉挂着的卖单：闸门的可卖数量规则会重复扣，需要修，但不影响今天的验收
- 把 `recordings/probes/probe_m4_pnl_<UTC>.jsonl` 的路径发给 Claude（按 7.2 格式写进 learnings）

（等 31 秒）

### 第 3 步：探针 P11（常规时段）

```
! uv run python scripts/probe_m6_quote.py
```
- [x] 退出码 0，记录文件已生成
- 要回答：报价推送频率、时间戳时区、`open_price` 取值；可选：16:00 后再跑一次，对照盘后 `last_price`
- 把记录文件路径发给 Claude

（等 31 秒）→ 0.6 契约测试 →（等 31 秒）

### 第 4 步：M4 验收（常规时段，`mooapp run` 停着）

```
! uv run pytest -m sim tests/acceptance/test_m4_halt.py
```
- 过程：挂 3 张 SPY 买单 @ 最新价 × 0.9 → 全部确认后 halt → 3 张 CANCELED（券商 CANCELLED_ALL）→ 再下一单被 `halt` 拒绝
- [x] `1 passed`（2026-11-16 第二次运行。第一次被闸门以 `max_limit_deviation_pct` 拒绝：草稿用最新价 × 0.9，偏离 10% 超过模拟盘 5% 上限；你把系数改为 0.96 后通过）
- [x] App 里 3 张 SPY 单已撤销
- 预期：halt 结果里有 **2 张撤单失败**（841874、841875：本地还是 NEW，券商端周五已结束，撤单被拒），并报警 `halt_cancel_failed`；验收只看自己的 3 张，照样通过。启动时这两张还会报 `missing_at_broker`
- 开头报「开始前不应处于 halt」：先做第 5 步再回来跑
- 结束后 halt 保留，必须做第 5 步

### 第 5 步：解除 halt（你自己的终端）

窗口 A：
```
cd ~/GitRepos/mooapp && uv run mooapp run
```
- [x] 日志有「mooapp run 已启动（simulate）」
- [x] **没有**「没有 [resume] key」「读不到 secrets」之类的警告（有就是密钥没读到）
- 8 条 `unknown_client_order_id`、2 条 `missing_at_broker`（841874、841875）、若干 `external_order`（P10、契约测试的单不在本地库）是预期的

窗口 B：
```
cd ~/GitRepos/mooapp && uv run mooapp resume
```
- [x] 原样输入随机确认码后输出「已解除 halt」（2026-11-16：你运行了几轮 run/resume；journal 显示 halt 16:35:12 UTC 已持久化，16:40:27 的第一次签名 resume（命令 7）解除，之后命令 8、9 是重复 resume）
- 报「签名不对」或「已过期」：检查两个窗口读的是不是同一份 secrets
- [x] 窗口 A 按 **Ctrl-C** 停掉 `mooapp run`，确认正常退出（uv 打印 `Failed to get PID of child process: ESRCH`：子进程已先退出，无影响；`pgrep -fl "mooapp run"` 无输出）

（等 31 秒）

### 第 6 步：M6 验收（常规时段，不处于 halt，`mooapp run` 停着）

```
! uv run pytest -m sim tests/acceptance/test_m6_rule_oco.py -s
```
- 过程：SPY 限价买 1 股 @ 卖一价 × 1.0005；止损 = 成本 − 0.05，止盈 = 成本 + 0.05；最长等 10 分钟
- [x] `1 passed`（2026-11-16，用时 20.64 秒；规则 accept-m6-1794847616）
- [x] 规则 CLOSED，只有 1 张卖单且已成交
- [x] 按输出的触发方向、卖单号、成交价到 App 核对；持仓恢复原状（take 触发：买入 11:46:57 成交 745.37，止盈线 745.42；卖单 843037 11:47:10 市价成交 745.49；App 两张均已成交）

### P9 结论（2026-11-16 美东 10:29 `--check`，收盘后和周二那次不用再跑）

- 841874–841883 全部 `CANCELLED_ALL`，`updated_time` 2026-11-13 20:01:00（周五盘后时段结束时过期）；周五「排队到下一交易日」的推测是错的（learnings 已更正）
- 前一天的订单 `order_list_query`（缓存、刷新都）查不到，只有 `history_order_list_query` 能查到；结束近 3 天仍可查
- 发现的缺口：启动恢复只查当日订单列表，本地仍为 NEW 的 841874、841875 不会被结束（只报 `missing_at_broker`）。验收跑完后由 Claude 出修复计划（缺失订单再查历史订单）
- 记录：`recordings/probes/probe_m3_expiry_20261116T152905Z.jsonl`

### 验收后待办（2026-11-16 新增）

- [x] 提交今天的文档和你改的 `tests/acceptance/test_m4_halt.py`（ad15cd7、e23a395；cherry-pick 到 dev/milestone_4 为 1bc1233）
- [x] 打 tag：`accept/milestone_3`（d238e28）、`accept/milestone_4`（d5c1f57）、`accept/milestone_6`（a9cf104），已推送
- [x] 修复计划 1：闸门可卖数量重复扣挂单（P10d）→ ADR-0031，已实现（见「修复计划」）
- [x] 修复计划 2：启动恢复改查历史订单（P9）→ 已实现并在模拟盘验证（见「修复计划」）
- [ ] 可选：美东 16:00 后再跑一次 P11 `uv run python scripts/probe_m6_quote.py`，看盘后 `last_price`、`open_price`
- [x] M3 打 tag 之后：两份 M3 验收的 `build()` 改用 `build_runtime`（你本人修改）（2026-11-16 完成：分支 `fix/Post_Milestone_3` 提交 ae47027；价格系数 0.9 → 0.96，`build()` 等单实例锁释放；两条模拟盘重跑均 `1 passed`，App 核对 13:16:56 @712.48、13:18:35 @712.22 两张均已撤销）

### 结果发给 Claude

- 0.3、0.4、0.6、第 2–6 步的结果行（passed / failed），失败附关键报错；0.4 的订单号和 App 核对结果
- P10、P11 的记录文件路径
- 通过后：`/milestone-git finish 3`（可当天，也可等 M4、M6 一起）；之后你本人把两份 M3 验收的 `build()` 改用 `build_runtime`

## M6b 审查修复（已确认 2026-11-14，分支 `dev/milestone_6`）

`/code-review high` 有 10 条发现，加上附带的 2 条小问题，逐条核实都成立；你选择全部修复。每条先写能复现问题的测试，确认失败后再修：

1. 死锁、事件线程被阻塞：规则引擎不再在事件线程上调用闸门。下单、撤单、时段查询、快照交给专门的动作线程，结果排回事件线程；`rule add` 和 `rule cancel` 在调用方线程上调用闸门
2. 保护性卖单被撤销或过期（例如被 halt 撤掉）时，按被拒处理：剩余数量在下一笔行情重试
3. 调用闸门出错时，按卖单失败计入；`rule add` 出错时规则作废，不留下活动规则
4. 恢复时按闸门当前的 halt 状态，明确设置暂停或恢复
5. OMS 就绪（`OmsReady` 事件）之前，状态机的动作先排队，就绪后再执行
6. 同一标的只订阅一次；不再有活动规则、且订阅满 1 分钟的标的退订
7. 时段缓存跨过美东 9:30 和 16:00 时立即失效；查询失败也缓存几秒
8. 快照兜底失败时，同一标的 1 分钟内只报警一次
9. 加载失败的规则在库里标为 ERROR；`rule cancel` 对没加载成功的规则直接把库里标为 CANCELED
10. `_persist` 改用深拷贝；CANCELED 状态下的入场成交也报警「不受保护」

## M6b 计划（已确认 2026-11-14，分支 `dev/milestone_6`）

目标：SPEC 8 节 M6 验收 3（模拟盘买 1 股，设很窄的止损止盈，观察一边触发后另一边作废）。代码和测试周末用 FakeBroker 加 PostgreSQL 完成；探针 P11 和验收 3 在开盘时由你运行。

### 你确认的点（2026-11-14，包括默认做法 4 条）

1. 只在常规时段判断触发：`market_state` 不是 AFTERNOON 时忽略行情，结果缓存 30 秒。隔夜跳空的情况，开盘后第一笔行情立即触发（ADR-0003、0007）
2. 停机重启后，规则从数据库恢复并继续监控，不需要重新 `rule add`
3. 增加 `mooapp rule cancel <id>`：撤入场单余量、停止监控；还有持仓时提示「N 股不再受保护」
4. 你手动卖掉规则管理的持仓时不额外处理：触发时卖单被闸门以可卖数量不足拒绝，3 次后进入 ERROR 并报警

### 设计要点

- `strategy/engine.py` RuleEngine，跑在事件线程上：
  - add：检查 ADR-0020 → 写 rules 表（PENDING）→ 经闸门下入场单（来源为规则引擎，带 rule_id）→ 订阅行情
  - OMS 新发 `OrderChanged` 事件，引擎按 client_order_id 找到规则，把成交、结束、被拒喂给状态机
  - 卖出经闸门发市价单并带 protective 标记；被拒时调用 `on_sell_rejected`，下一笔行情重试
  - 每次状态变化写回状态快照和 `rule_events`；halt 时暂停
- 成交按订单累计量算增量，并记录已计入的数量和金额：推送、恢复补喂重复到达也不会重复计
- 行情：BrokerPort 补上 `subscribe_quotes`（Moomoo 订阅 `SubType.QUOTE`，vendor § Subscribe、§ Real-time Quote Callback；FakeBroker 加 `push_quote`）；推送只排进事件队列；某个标的超过 `poll_fallback_seconds` 没有推送就用快照补
- 数据库 `0003_m6.sql`：`rules`、`rule_orders`、只追加的 `rule_events`；`active_rule_for` 返回真实结果
- 闸门：Intent 增加 `rule_id`，ADR-0020 的检查不拦规则自己的入场单
- 命令：`rule add <yaml>`（命令行先本地校验，再把 YAML 原文写进命令表）、`rule cancel <id>`、`rule list`（只读）
- 启动恢复：引擎先从快照恢复规则，再由 OMS 做启动恢复；停机期间的成交经 `OrderChanged` 补喂；重新订阅行情
- 探针 P11 `scripts/probe_m6_quote.py`：推送频率、`data_time` 的时区、`open_price` 什么时候有值、盘前盘后的 `last_price` 是不是常规时段的价格

### 文件

- 新增：`strategy/engine.py`、`db/migrations/0003_m6.sql`、`scripts/probe_m6_quote.py`；测试 `test_rule_engine.py`、`test_rule_recovery.py`、`test_quote_feed.py`
- 修改：`strategy/machine.py`（状态快照、CANCELED）、`oms/oms.py`（OrderChanged）、Store 两个实现、broker 层（subscribe_quotes）、`risk/gate.py`、`risk/rules.py`、`ops/commands.py`、`ops/run.py`、`ops/cli.py`；补充 `test_commands`、`test_store_contract`、`test_gate_m4`、`test_rule_machine`、`test_cli_m4`；收尾时改 learnings 和 PROGRESS
- 不改：风控限额、`tests/acceptance/`、`.claude/`；没有新依赖

### 执行顺序

1. 计划写进 PROGRESS（已完成）
2. 写测试，跑一遍确认失败，原样贴出结果，同时贴验收 3 的草稿（做法 C）；**停下等你确认**
3. 写实现，直到 `pytest` 和 `pytest -m pg` 都通过
4. 开盘时你依次运行：探针 P11 → 验收 3（与其他 sim 测试间隔 31 秒）
5. `/milestone-git commit 6`

## M6a 计划（已确认 2026-11-14，分支 `dev/milestone_6`，从 `dev/milestone_4` 拉出）

目标：SPEC 8 节 M6 的验收 1（5 条价格路径与你手算一致）和验收 2（非法表达式在加载时被拒）。只做纯逻辑部分，不需要实时行情和数据库。验收 3（模拟盘一边触发后另一边作废）放到 M6b。M5 跳过，M6 不依赖它。

### 你确认的点

1. K 线时段里两边都碰到时，按止盈算（ADR-0027 第 2 条）
2. 触发时先撤掉入场单余量，再卖出已成交的数量；撤单前又成交的部分补卖（ADR-0027 第 3 条）
3. 卖单被拒绝：下一笔行情时同一边条件仍成立就重发；价格回到两个触发价之间就重新监控；累计失败 3 次进入 ERROR（ADR-0027 第 4 条）
4. 依赖：PyYAML 6.0.3、jsonschema 4.26.0（SPEC Q13 已批准，2026-11-14 安装）

### 设计要点

- 规则文件：YAML 加 JSON Schema，字段为 `id`、`symbol`、`entry{side: BUY, qty, limit_price}`、`stop`、`take`；不允许多余字段，限价用字符串写；加载时编译两个表达式
- 表达式：基于 `ast` 白名单，不使用 eval；只允许 `open`、`avg_cost`、`last`、`min`、`max`、`round_tick(x, "up"|"down")`、`+ - * /`、负号、数字；全部用 Decimal；长度不超过 200 个字符
- 取整：复用 `broker/port.py` 的最小变动单位；止损价向上、止盈价向下（ADR-0009）
- 状态机（纯逻辑，输入事件、输出动作）：PENDING → ARMED → TRIGGERED → CLOSED，另有 VOID、ERROR；首次成交时冻结价格（ADR-0005、0027 第 5 条）；冻结时已越过就立即触发（ADR-0007）；入场单过期按 ADR-0023；halt 期间暂停
- dry-run：`mooapp rule dry-run <yaml> --prices <csv>`，CSV 列为 `ts,type,a,b`，type 取 `open | fill | tick | bar | reject_next_sell | expire`；卖单默认按触发时看到的价格成交，并打印滑点。另有 `mooapp rule check <yaml>`（只校验）

### 价格路径（入场买 10 股 @100.21，开盘价 99.99，首次成交价 100.21）

- 止损价 = min(99.99, 100.21) × 0.95 = 94.9905，向上取整为 95.00；止盈价 = max(99.99, 100.21) × 1.05 = 105.2205，向下取整为 105.22
- 1 先止损（tick 99.00 → 95.01 → 95.00）；2 先止盈（103.00 → 105.21 → 105.22）；3 跳空越过止损（98.00 → 92.50，滑点 2.50）；4 一根 K 线两边都碰到（最低 94.00、最高 106.00，按止盈）；5a 分两次成交 4 + 6 股后在 94.80 触发、价格不重算；5b 只成交 4 股时触发：撤余量、卖 4 股，撤单前又成交 2 股则补卖 2 股

### 文件

- 新增：`src/mooapp/strategy/` 下 `expr.py`、`ticks.py`、`spec.py`、`rule.schema.json`、`machine.py`、`dryrun.py`；`docs/adr/0027-rule-engine-semantics.md`；`examples/rules/spy-oco.yaml`、`examples/prices/*.csv`；测试 `test_expr.py`、`test_rule_spec.py`、`test_rule_machine.py`、`test_dryrun.py`
- 修改：`ops/cli.py`（`rule dry-run`、`rule check`）、`pyproject.toml`、`uv.lock`；收尾时改 learnings 和 PROGRESS
- 不改：风控限额、`tests/acceptance/`、`.claude/`、闸门和 OMS

### 执行顺序

1. 安装依赖、写 ADR-0027、把计划写进 PROGRESS（已完成）
2. 写测试，跑一遍确认失败，原样贴出结果；**停下等你确认**
3. 写实现，直到 `pytest` 通过
4. `/milestone-git commit 6`

M6b（另行计划）：rules 表与 `active_rule_for`、Moomoo 的 `subscribe_quotes`、把状态机接到闸门、OMS 和 HaltChanged、`rule add` 和 `rule list`、模拟盘验收 3

## M4 审查修复（已确认 2026-11-14，分支 `dev/milestone_4`）

`/code-review high dev/milestone_4`：8 条 bug 和小问题全部修复；第 5、10 条按你的决定写 ADR-0028、ADR-0029。每条先写复现测试（`tests/unit/test_m4_review.py`）。修完后合并进 `dev/milestone_6`。

1. halt 撤单时，单张订单状态已变（成交、已确认）不中断，记入失败继续撤下一张
2. 撤单时「判断未确认、挂待撤标记」在 OMS 里一步完成，不再有竞态
3. 库不可用时 halt 照常：挂单列表改从券商查；本地写撤单请求失败也照样把撤单发给券商（ADR-0025）
4. OMS 启动恢复（回放 journal）后发 `OmsReady`，闸门据此重新读取 halt 状态（ADR-0022）
5. 当日亏损触线后只拦买单（ADR-0028）
6. 等待新命令通知出错不让命令线程退出；心跳改由独立线程每秒写
7. 下单、撤单命令带过期时间（命令行超时即过期，常驻进程不再执行）；常驻进程启动时把遗留的「处理中」命令标为 error
8. 启动恢复后，对「已确认且带待撤标记」的订单补发撤单
9. resume 必须带 HMAC 签名（ADR-0029）：`[resume] key` 放 secrets；常驻进程验签、60 秒内有效、随机数不重复
10. 小问题：确认后的自动撤单不在事件线程上调用券商；库不可用时审计里注明「上一条通过记录作废」；同一次下单只查一次挂单；第二次 halt 不覆盖第一次的原因；探针 P10 增加「挂卖单前后的 can_sell_qty」（查可卖数量是否已扣掉挂着的卖单）

**状态（2026-11-14）**：全部实现。`pytest` 578 passed，`pytest -m pg` 36 passed；test_m4_review.py 的 27 条复现测试修复前全部失败、现在全部通过。另外：`tests/unit/test_commands.py` 的 resume 测试改为带签名；`tests/store/test_store_contract.py` 新增 `fail_running_commands` 的契约测试；`broker/moomoo.py` 的持仓查询缓存 0.5 秒（下单、撤单、订单推送时清空）。待你：提交（`/milestone-git commit 4`）→ 合并进 `dev/milestone_6`

## M4 计划（已确认 2026-11-14）

目标：SPEC 8 节 M4 三条验收。第 1、2 条（每条规则「刚好不超限 / 超限」两个测试、防绕过测试）周末用 FakeBroker 和 PostgreSQL 完成；第 3 条（模拟盘挂 3 张单后 halt）周一由你用 `!` 运行。

### 你确认的点

1. PENDING_NEW 时收到的撤单，等确认（ACK，或轮询看到 SUBMITTED）后自动发出；不改状态矩阵，另写 ADR-0026
2. 当日亏损取券商持仓接口 `today_pl_val` 之和（含当天卖光、qty=0 的行），实盘再减去费用；模拟盘该字段是否有效，周一用探针 P10 实测。确认之前，字段无效时拒绝买单
3. 持仓上限与可卖数量都把本程序未成交的挂单算进去：买入时券商总持仓 + 未成交买单剩余量 + 本单 ≤ `max_position_qty`；卖出时 `can_sell_qty` − 未成交卖单剩余量 ≥ 本单
4. 人锁完整流程、限额锁、上锁的代码都写，用假对象测试；但 `open_broker` 和闸门仍拒绝 REAL。「打开 REAL」在 M4 模拟盘验收通过后另行计划，由你在自己的终端里演练
5. 默认做法 4 条：
   - 限流器进程内按 (env, acc_id) 共用
   - 所有 `order_list_query` 都限流（30 秒 10 次）
   - 常规时段里行情快照早于 `stale_after_seconds` 时，以 `quote_stale` 拒单
   - 当日下单次数按订单计：当日（美东）首次 SUBMIT 的订单数，T55 重发不重复计

### 设计要点

- 规则顺序：halt → 白名单 → 常规时段（`get_market_state` = AFTERNOON）→ 行情是否过期 → 单笔数量 → 单笔金额（限价 × 数量；市价单用最新价）→ 限价偏离 → 持仓上限（买）/ 可卖数量（卖）→ 当日下单次数 → 当日亏损。另有：
  - ADR-0020：调用 `Store.active_rule_for`，M6 之前返回 None
  - ADR-0021：带 protective 标记的卖单只有来源是规则引擎时才被承认，跳过单笔金额与当日次数；其他来源带这个标记一律拒绝并报警
  - 撤单只检查「订单属于本程序且处于活动状态」（ADR-0016）
- 审计：每次闸门判定都写进只追加表 `audit`；拒单时发 `RiskRejected` 事件。触及亏损上限时自动 halt（ADR-0017）。库不可用时拒绝新单，撤单和 halt 照常执行，审计先写 journal（ADR-0025）
- halt：状态存 kv 表（ADR-0022）；撤销本程序全部活动订单，PENDING_NEW 和 UNKNOWN 的打上「待撤」标记；不碰外部订单；REAL 时调用 `lock()`；发 `HaltChanged` 事件。resume 只能在交互终端执行，要求 TTY 加确认码（Q10）
- commands 表与常驻进程 `mooapp run`（SPEC 3.4）：`mooapp order buy|sell|cancel`、`halt`、`resume` 只写命令并同步等待结果；常驻进程持有 advisory lock，用 LISTEN/NOTIFY 加 1 秒轮询取命令；常驻进程不在时，写命令直接报错
- 探针 P10 `scripts/probe_m4_pnl.py`：模拟盘 `today_pl_val` 是否有效，卖光后 qty=0 的行是否还带当日盈亏

### 文件

- 新增：`risk/rules.py`、`risk/audit.py`、`ops/run.py`、`ops/commands.py`、`db/migrations/0002_m4.sql`、`docs/adr/0026-cancel-before-ack.md`、`scripts/probe_m4_pnl.py`，以及测试
- 修改：`risk/gate.py`；`oms/oms.py`、`oms/executor.py`、`oms/state.py`；`broker/port.py`、`broker/convert.py`、`broker/throttle.py`、`broker/fake.py`；`ops/humanlock.py`、`ops/cli.py`；收尾时改 learnings 和 PROGRESS
- 不改：风控限额数值、`tests/acceptance/`、`.claude/`；没有新依赖

### 执行顺序

1. 写 ADR-0026、探针 P10
2. 写测试，跑一遍确认全部失败，原样贴出结果，同时贴验收草稿（做法 C）；**停下等你确认**
3. 写实现，直到 `pytest` 和 `pytest -m pg` 都通过。优先级：闸门规则和 halt → commands 和 run → 人锁代码
4. 周一：你先跑 M3 验收（间隔 31 秒），再跑探针 P10、`pytest -m sim` 和 M4 验收
5. 写 learnings 和 PROGRESS，`/milestone-git commit 4`；M3 打 tag 之后，再 `/milestone-git finish 4`

**Git 命名**（2026-11-13 起）：分支 `dev/milestone_N`（合并后保留），tag `accept/milestone_N`（打在 main 的合并提交上）；旧的 m1、m2 tag 与 m2、m3 分支已改成新名字。流程见 `.claude/skills/milestone-git/SKILL.md`（`/milestone-git <start|commit|finish|push|status> <N>`）

## M3 计划（已确认 2026-11-13）

目标：SPEC 8 节 M3 四条验收。完整计划在会话中给出；要点如下。

### 你确认的点

1. 超时重发：新增 T55 `UNKNOWN + RESUBMIT → PENDING_NEW`。连续 2 次刷新缓存的查询都查不到才重发，同一 cid，最多 3 次（docs/order-transitions.md）
2. DISABLED、DELETED 在实测前按「映射不了」处理（写事件、报警、状态不动）；探针 P9 `scripts/probe_m3_expiry.py`
3. M3 先做闸门骨架 `risk/gate.py`（REAL 拒绝、未就绪、halt（内存）、白名单），Executor 只认 GateTicket；限额规则在 M4
4. journal（ADR-0025）进 M3；`commands` 表和 `mooapp run` 推到 M4

### 与计划的差异（均已在会话中说明）

- Resolver 并进 `oms/executor.py`，不单独建 `resolver.py`：对外能发单的只有 `place`、`cancel`（都要 ticket）和 `resolve_unknown`（只处理已过闸门的 UNKNOWN 单）
- `GateTicket` 放在 `oms/ticket.py`，避免 oms 反向依赖 risk
- 只有 `refresh=True` 的查询限流（30 秒 10 次）；走缓存的查询维持 M2 的做法不限流（**待你确认**）
- 迁移多一张 `journal_applied` 表（journal 按 entry_id 幂等）
- 状态映射补了两条规则：没看到确认就看到撤销时先补 ACK；本地已是同一结局时不再发事件（docs/status-mapping.md）
- Moomoo 状态共 11 个，计划里误写成 12 个，已改正
- 修改了已有测试：`test_layering.py`（放行 executor）、`test_throttle.py`、`test_moomoo_broker.py`、`tests/contract/test_broker_contract.py`（各加 refresh 用例）；第 3 步改了自己写的 `test_cancel_rejected_by_broker_goes_back_and_raises`（同步模式下要让推送真的丢失）

### 文件

- 新增：`src/mooapp/oms/` 下 `state.py`、`mapping.py`、`events.py`、`store.py`、`pg_store.py`、`journal.py`、`migrate.py`、`ticket.py`、`executor.py`、`pump.py`、`poller.py`、`oms.py`；`src/mooapp/risk/gate.py`；`db/migrations/0001_oms.sql`、`db/routines/.gitkeep`；`docs/status-mapping.md`；`scripts/probe_m3_expiry.py`；测试和 `tests/fixtures/`（两份推送 fixture，不含 acc_id，请审核）
- 修改：`docs/order-transitions.md`（T55）、`broker/port.py`、`broker/moomoo.py`、`broker/throttle.py`、`broker/fake.py`、`ops/cli.py`（`db migrate`、`order list`）、learnings、PROGRESS
- 删除：`src/mooapp/oms/.gitkeep`、`src/mooapp/risk/.gitkeep`
- 不改：风控限额、`config/*.toml`、`tests/acceptance/`（两份 M3 验收由你复制和维护）、`.claude/`；无新依赖

## 已完成

- 阶段 0：环境、仓库、官方文档 `docs/vendor/`、Python 环境与冒烟脚本
- 阶段 1 会话 1：安全护栏（deny / ask 规则、.gitignore、gitleaks pre-commit），提交 4d64df8
- 阶段 1 会话 2：护栏验证，金丝雀 4 项全部被拒；加固 sandbox（不可绕过）与 REAL 环境参数拦截钩子，提交 e44d248
- 阶段 1 会话 3：14 条 ADR、CLAUDE.md、目录骨架、order-transitions.md 与 docs/reference/order_state.py（来自 ~/Downloads）、config/settings.toml、config/secrets.example.toml、README.md
- 官方 Skills 审查（手册 1.4，2026-11-11）：MoomooOpen/moomoo-agent-hub @ 9e45546 共 11 个 skill，不安装。原因：moomoo CLI 只凭 OAuth token 即可下实盘单，绕过 risk/gate.py 与人锁。已在 `.claude/settings.json` deny `Bash(moomoo:*)`
- ADR 0001–0014 全部已定；手册第 3 节状态列已同步为「已定」，第 12 条改为 PostgreSQL
- 阶段 1 会话 4（提交 09f90ac）：
  - SPEC.md 已定：目标与非目标、架构、进程与线程模型、接口草案、vendor 要点、探针规则 R1–R9 与探针 P1–P8、M1–M9 验收标准、问题清单 Q1–Q22、测试策略
  - 新建 ADR 0015–0025；ADR-0006、0012 加注记，0012 被 0024 取代
  - 主要决定：
    - 进程模型：只有常驻进程 `mooapp run` 持有闸门、OMS 和券商连接，其他命令经 `commands` 表提交（Q18）
    - 存储：PostgreSQL 17（Homebrew，只监听回环地址）；`simulate` 和 `real` 两个 schema；库、角色、schema 由你手动创建（已完成）；初始化代码放 `db/` 进 Git；存储过程不得写 OMS 拥有的表
    - journal：写库前先写本地 journal，不加密，与日志同等对待（ADR-0025）
    - 凭据：模拟账户 `simulate_acc_id` 在 `config/broker.toml`（进 Git，已填）；`mooapp_sim` 密码在 `config/db.local.toml`（不进 Git，sandbox 可读）；`mooapp_real` 密码和实盘 `acc_id` 在 secrets
  - learnings.md 增加「阶段 1 会话 4：SPEC 审阅中发现的问题」
- sandbox 与数据库实测（2026-11-11）：sandbox 只放行 PostgreSQL socket `/tmp/.s.PGSQL.5432`；sandbox 内经 socket 连接成功，TCP 5432 和 11111 都被拦；pg_hba 已改为 scram，无密码连接被拒；两个角色互相访问不了对方的 schema
- M1 连通与配置（2026-11-11 至 11-12，待提交）：
  - 实现：`common.py`、`config/models.py`、`config/loader.py`、`broker/opend.py`（只读会话，SDK 延迟导入）、`ops/humanlock.py`、`ops/doctor.py`（8 类检查）、`ops/cli.py`、`scripts/probe_m1.py`
  - 测试：`tests/unit/` 5 个文件、`tests/contract/test_db_checks.py`（pg）、`tests/contract/test_opend_readonly.py`（sim）、`tests/conftest.py`
  - 探针：P1 模拟盘下单不需要解锁；P2 常规时段快照为实时（延迟约 2 秒）；P6 sandbox 拦住 11111，sandbox 内连 `import moomoo` 都会失败。cron 在常规时段跑了一次（10:30 ET）
  - 主要发现：撤单返回成功 ≠ 已撤销（异步）；夜盘时模拟盘照样接受常规时段的 DAY 单并排队，闸门必须自己判断时段；时段以 `get_market_state` 为准
  - 与计划的差异（均已在会话中说明）：
    - 构建后端用 `uv_build`（为了装出 `mooapp` 命令）
    - 新增 `tests/conftest.py`（假配置 fixture）
    - `ConfigUnreadable`：区分「不存在」和「无权读取」
    - 探针还读了 `settings.toml` 的白名单（R6），仍通过 `mooapp.config`

## M2 计划（已确认 2026-11-12）

目标：SPEC 8 节 M2 三条验收。不做 REAL、OMS、闸门；MoomooBroker 遇到 `Env.REAL` 直接拒绝（REAL 在 M4）。

### vendor 发现（改动原设计）

- `deal_list_query` 只支持实盘（§ Get Today's Deals）：模拟盘成交只靠 `dealt_qty` 增量，id 合成为 `order_id:累计成交量`
- `order_fee_query` 不支持模拟账户（§ Get Order Fee）：P7 由文档回答，探针只记报错原文
- `SysConfig.enable_console_log(False)`（§ Enable console logging）：关掉 SDK 控制台日志
- 模拟账户 `average_cost` 无效（§ Get Positions）：`Position.avg_cost` 为 `Decimal | None`

### 你确认的点

1. 模拟盘契约测试在 M2 直接调用 `BrokerPort.place_order`（尚无闸门；只 SIMULATE，只由你用 `-m sim` 跑）
2. 验收 2：`US.SPY` 限价 = 最新价 × 0.9、1 股、连下 20 笔后全部撤销；限流 15 次/30 秒，窗口加 1 秒余量。**验收测试走做法 C**：Claude 在聊天里贴草稿，你复制到 `tests/acceptance/` 审核、修改；以后对该目录的修改也只由你手工进行
3. 验收 3：`US.SPY` 100 股限价 = 卖一价，60 秒未到终态就撤剩余，已成交部分市价卖回；录不到部分成交就停下问你
4. remark 格式 `moo:<32 位 uuid4 hex>`；不以 `moo:` 开头的（含 `probe:`）都是外部订单
5. 模拟盘的合成成交在 broker 层生成，经 `on_fill` 送出
6. `OrderRequest` 构造时校验（价格必须在最小变动单位上，不自动取整）
7. `subscribe_quotes` 的 Moomoo 实现推到 M6；重连只依赖 SDK 自带重连，完整验收在 M9
8. `BrokerOrder.kind` 为 `OrderKind | None`（None = 非 v1 类型的外部订单）

### 文件

- 新增：`src/mooapp/clock.py`；`src/mooapp/broker/` 下 `port.py`、`convert.py`、`throttle.py`、`moomoo.py`、`recorder.py`、`fake.py`；`scripts/probe_m2.py`、`scripts/probe_m2_partial_fill.py`
- 修改：`common.py`（Side、OrderKind、Source）、`broker/opend.py`（异常改从 port 导入、关控制台日志、TCP 预检快速失败）、`ops/doctor.py`（仅导入路径）；收尾改 learnings、PROGRESS
- 测试：`tests/unit/` 下 `test_order_request.py`、`test_convert.py`、`test_throttle.py`、`test_moomoo_broker.py`，`test_layering.py` 加一条；`tests/contract/test_broker_contract.py`（Fake 默认，sim 由你跑）
- 不改：风控限额、`tests/acceptance/`、`.claude/`、`config/*.toml`；无新依赖

### 执行顺序

1. 探针（常规时段，你用 `!` 跑）→ learnings
2. 写测试，确认失败，原样贴结果；**停下等你确认**；同时贴验收测试草稿（做法 C）
3. 写实现直到 `pytest` 通过
4. 你用 `!` 跑 `pytest -m sim` 和你的验收测试；推送录在 `recordings/pushes/`
5. learnings、PROGRESS；你审 diff、提交、`git tag m2`

## M1 计划（已确认 2026-11-11，已完成 2026-11-12；保留作记录）

目标：读取配置、连上 OpenD，打印模拟账户和一只股票的快照（手册第 5 节 M1）。不实现 REAL 交易。

### 你确认的四点

1. REAL 在 M1：非 TTY 直接拒绝（`HumanLockError`）；TTY 下也只提示「REAL 在 M4 实现」后退出，不调用 getpass。人锁在任何 OpenD 连接之前执行
2. doctor 第 7、8 项（数据库、端口未暴露）在 M1 做，因此 M1 引入 `psycopg[binary]>=3.2`（Q19 已批准）
3. OpenD 连接超时 5 秒（异步连接 + 限时等待；同步构造在失败时会无限重试，见 learnings「阶段 0 实测」）
4. 探针下单：`US.SPY` 1 股，限价 = 最新价 × 0.9（取整到 0.01），BUY

### 文件

- `pyproject.toml`：依赖 `pydantic`、`typer`、`psycopg[binary]>=3.2`；开发依赖 `pytest`；`[project.scripts] mooapp = "mooapp.ops.cli:app"`；注册 `sim`、`pg` marker，默认 `-m "not sim and not pg"`
- 各包 `__init__.py`（删除对应 `.gitkeep`）
- `src/mooapp/common.py`：`Env`、`MooappError`、`mask_account()`
- `src/mooapp/config/models.py`：`RiskLimits`、`Settings`（含 `AgentCfg`）、`BrokerCfg`（含 `simulate_acc_id`）、`DbCfg`、`DbLocal`、`Secrets`；密码用 `SecretStr`
- `src/mooapp/config/loader.py`：`load_settings`、`load_broker_cfg`、`load_db_cfg`、`load_db_local`、`load_secrets`、`limits_for`、`acc_id_for`、`dsn_for`、`ConfigError`（一行可读报错，无堆栈）
- `src/mooapp/broker/opend.py`：只读 OpenD 会话（限时连接、全局状态、账户列表、资金、快照、市场状态）；全项目只有 broker/ 与 scripts/ import `moomoo`
- `src/mooapp/ops/humanlock.py`：`require_human_for_real()` 的 TTY 检查部分
- `src/mooapp/ops/doctor.py`：检查项返回「名称、状态、详情」；有 FAIL 时退出码非 0
- `src/mooapp/ops/cli.py`：typer 入口，全局环境参数默认 simulate
- `scripts/probe_m1.py`
- 收尾时：`docs/learnings.md`、`PROGRESS.md`
- 不改：风控限额、`tests/acceptance/`、`.claude/`、`config/broker.toml`

### doctor 检查项

| # | 检查 | vendor 出处 |
|---|---|---|
| 1 | OpenD 版本（`server_ver`）、SDK 版本（`importlib.metadata`） | § Get global market status |
| 2 | 账户列表（后 4 位）；核对 `simulate_acc_id` 在 SIMULATE 账户中 | § Get the List of Trading Accounts |
| 3 | 模拟账户资金（显式 `trd_env=SIMULATE`、`acc_id`） | § Get Account Funds |
| 4 | 白名单第一只（`US.SPY`）快照 | § Get Market Snapshot |
| 5 | 市场状态 | § Get Market Status of Securities |
| 6 | secrets：存在、权限 600、可解析、必填项齐全；只这一项报错，其余照常；sandbox 内显示「无权读取」 | — |
| 7 | 数据库：经 `db.toml` 连上；`mooapp_sim` 查 `real` 被拒；`listen_addresses` 只含回环地址 | — |
| 8 | 局域网 IP 连 5432、11111 必须失败；sandbox 内显示「无法检查」 | — |

OpenD 不可达：5 秒内输出一行「无法连接 OpenD 127.0.0.1:11111（5 秒超时），请确认 OpenD 已启动」，跳过 1–5 项，6–8 项照常。

### 探针 `scripts/probe_m1.py`（SPEC 7.1 R1–R9）

- P1：模拟盘下单是否需要 `unlock_trade`；P2：快照 `update_time` 与本机 UTC 比较（实时还是延时）；P6：Claude 在 sandbox 内跑一次，确认 11111 被拦
- 步骤：读 `broker.toml` → `get_acc_list` 核对模拟账户 → `US.SPY` 快照 → 限价买 1 股（最新价 × 0.9，`remark="probe:m1:<UTC>"`，显式 `trd_env=TrdEnv.SIMULATE` 与 `acc_id`，不解锁）→ `finally` 撤单并打印结果
- 出处：§ Place Orders、§ Modify or Cancel Orders、§ Unlock Trade
- 记录：`recordings/probes/probe_m1_<UTC>.jsonl`，首行记 SDK、OpenD 版本和 git 提交号
- 由你用 `!` 运行；休市时被拒也如实记录，必要时开市再跑

### 测试（先写、确认失败，再写实现）

| 文件 | 覆盖 | 谁跑 |
|---|---|---|
| `tests/unit/test_config.py`：正常加载；缺文件、缺项、负数时可读报错；`limits_for`；SIMULATE 不读 secrets；REAL 缺 `real_acc_id` 报错；`dsn_for` 只接受 socket 或 127.0.0.1；repr 不泄露密码 | 验收 2 | Claude |
| `tests/unit/test_doctor.py`：假 OpenD 会话；五项输出、无完整账号；OpenD 不可达一行报错、退出码非 0、无堆栈；secrets 缺失只该项 FAIL | 验收 1、2、3 | Claude |
| `tests/unit/test_humanlock.py`、`test_cli_real.py`：进程内调用 CLI，非 TTY 流；断言 `HumanLockError`、退出码非 0、getpass 未调用、OpenD 工厂未调用 | 验收 5 | Claude |
| `tests/unit/test_probe_rules.py`：探针 AST 扫描；src/、tests/ 不 import scripts；无 `scripts/__init__.py` | R1–R3 | Claude |
| `tests/unit/test_layering.py`：`moomoo` 只出现在 broker/ 和 scripts/ | SPEC 3.3 | Claude |
| `tests/contract/test_db_checks.py`（`-m pg`） | 第 7 项 | Claude |
| `tests/contract/test_opend_readonly.py`（`-m sim`） | 验收 1 | 你用 `!` |

### 执行顺序

1. 改 `pyproject.toml`，`uv sync`（若 sandbox 拦网络，报告被拦域名）
2. 写上表 Claude 负责的测试，跑一遍确认全部失败，原样贴结果。**停下等你确认测试**
3. 写实现，直到 `pytest`、`pytest -m pg` 通过
4. 你用 `!` 运行：`mooapp doctor`、`pytest -m sim`、`python scripts/probe_m1.py`；关掉 OpenD 再跑 doctor；移走 secrets 再跑 doctor
5. 结论写进 learnings、PROGRESS；你审 diff、提交、`git tag m1`

## 待你处理

- [ ] M6b（开盘后，在 M4 的事项之后，间隔 31 秒）：`! uv run mooapp db migrate`（执行 0003：rules、rule_orders、rule_events）
- [ ] M6b（常规时段）：`! uv run python scripts/probe_m6_quote.py`（P11：报价推送频率、时区、open_price、盘前盘后的 last_price）；盘前或盘后再跑一次作对照
- [ ] M6b（常规时段，不处于 halt）：`! uv run pytest -m sim tests/acceptance/test_m6_rule_oco.py`（最长约 10 分钟）

- [ ] M4（周一）：`! uv run mooapp db migrate`（执行迁移 0002：audit、commands、orders.cancel_pending）
- [ ] M4（周一常规时段）：`! uv run python scripts/probe_m4_pnl.py`（P10：模拟盘 today_pl_val 是否有效）。无效时闸门会拒绝所有买单，先看 P10 的结果再跑 M4 验收
- [ ] M4（周一之前）：在 secrets 里加 `[resume] key`（`python3 -c "import secrets; print(secrets.token_hex(32))"` 生成，文件权限保持 600），然后重启 `mooapp run`。没有这把密钥，常驻进程会拒绝所有 resume（ADR-0029）
- [ ] M4（周一常规时段）：`! uv run pytest -m sim tests/acceptance/test_m4_halt.py`，结束后 `! uv run mooapp resume`（halt 跨重启保留）
- [x] M3 打 tag 之后：两份 M3 验收的 `build()` 改用 `mooapp.ops.run.build_runtime`（M4 的闸门签名已变，旧写法在 dev/milestone_4 上会失败；你本人修改）（2026-11-16 完成：分支 `fix/Post_Milestone_3` 提交 ae47027；价格系数 0.9 → 0.96，`build()` 等单实例锁释放；两条模拟盘重跑均 `1 passed`，App 核对 13:16:56 @712.48、13:18:35 @712.22 两张均已撤销）

- [ ] M3：确认是否所有 `order_list_query` 都限流（目前只限 refresh=True）
- [ ] M3：跑探针 P9：`! uv run python scripts/probe_m3_expiry.py`（美东 15:45–16:00），次日 `--check <order_id>`；App 里删一张远离市价的单后再 `--check`
- [ ] M3：`! uv run mooapp db migrate`；常规时段 `! uv run pytest -m sim` 与 `! uv run pytest -m sim tests/acceptance/test_m3_idempotent_retry.py tests/acceptance/test_m3_restart_recovery.py`
- [x] M3：审 diff 与两份 fixture，提交（aff9fee、600d29e）
- [x] M3（2026-11-16）：两份 M3 验收分开跑，均通过（842592、842718，App 已核对）。原条目：两份 M3 验收与 sim 契约测试**分开跑**，中间等满 31 秒，因为限频按账户算、多个 broker 共用额度（learnings「M3 › vendor 限频按账户计算」）。建议先给验收的 `finally` 加上撤单失败的处理
- [x] M3（周一美东 16:00 后，周二再一次）：P9 `--check` 已于 2026-11-16 10:29 完成，DAY 单过期为 CANCELLED_ALL（见「周一操作清单 › P9 结论」）；DISABLED 仍无样本
- [ ] 决定：sim 测试里同一进程的多个 broker 是否共用限流器（进程级、按 acc_id）
- [ ] M3：验收通过后 `/milestone-git finish 3`（tag `accept/milestone_3`）
- [ ] 提交 `.claude/skills/milestone-git/`（你本人提交：`.claude/` 由你维护）

- [ ] SPEC 8 节 M2 验收第 3 条改口径（你手工改）：模拟盘只录「整单成交 + 撤单」，部分成交由 FakeBroker 注入的推送覆盖（learnings「M2 › 模拟盘不会部分成交」）
- [x] 提交 `docs/learnings.md`、`PROGRESS.md` 中第二次部分成交探针的结论（ef26879）
- [ ] 审 M2 实现 diff（`src/mooapp/clock.py`、`common.py`、`broker/` 下 7 个文件）、learnings、PROGRESS，提交，`git tag m2`

- [x] 核对 `config/settings.toml`：`[risk.real]` 白名单 ETF 与现价、`max_limit_deviation_pct`、`[risk.simulate]` 数值、邮件收件人
- [x] 在 secrets 里填 `[accounts] real_acc_id`（2026-11-12 doctor 显示「real_acc_id 已填」）
- [x] `config/settings.toml`（你在 M1 期间改过，Claude 未动）：两个白名单里的 `US.TLSA` 疑为 `US.TSLA` 的笔误；`[risk.real]` 白名单 7 只，其中 SPY、QQQ、NVDA、AMZN 单价远超 `max_order_value = 100`，与注释「1–2 只低价 ETF」不一致
- [x] 审 M1 diff，提交，`git tag m1`
- [ ] 待议（REAL 阶段前）：双 OpenD 分别服务 SIMULATE 和 REAL。**原前提不成立**：vendor FAQ Q17 的同机双 OpenD 用的是两个不同的登录账户，Q8 的「一个账户多处登录」说的是多台机器；你手动实验确认（2026-11-12 约 21:00 EST 复测），同一账户在同一台 Mac 上第二次登录 OpenD 会失败，提示 `User is already logged in on this device`；命令行版、可视化版无论怎么组合都一样（learnings「阶段 1 会话 4 › vendor 要点」）。SIMULATE 与 REAL 在同一个登录账户下，所以现有账户做不到。只有满足以下附加条件之一才可行，都需要你决定：
  - 再开一个 moomoo 登录账户，只用它的模拟账户跑 SIMULATE（`simulate_acc_id` 要随之更换），原账户的 OpenD 只服务 REAL；
  - REAL 用的 OpenD 放到另一台机器上（Q8 允许），但与 ADR-0014「同一台机器、只用本机端口」冲突，需要新 ADR。
  - 选定以后，再验证「一个 OpenD 解锁后，另一个是否仍锁定」；不做双 OpenD 时维持 ADR-0015 的三条对策

## 已知问题

- `unlock_trade` 作用于整个 OpenD（vendor § Unlock Trade），对策见 ADR-0015；第 3 条已落实，P6 实测确认 sandbox 拦住 11111
- 模拟盘没有成交推送，也不支持 STOP 单：成交来源按 SPEC Q2；STOP 对照实验本版不做（ADR-0006 注记）
- PATH 上的 `psql` / `pg_isready` 是 14.12 客户端（来自 `postgresql@14`），服务端是 17；工具要用 `/opt/homebrew/opt/postgresql@17/bin/` 下的版本
- Claude 连不上 OpenD：`mooapp doctor`、`pytest -m sim`、探针、模拟盘验收都由你用 `!` 运行；sandbox 内 `import moomoo` 也会失败
- 撤单是异步的：`modify_order(CANCEL)` 返回成功后立即查询仍是 `SUBMITTED`。M2 探针要轮询到终态；M3 OMS 按待撤销状态处理
- ~~OpenD 拒绝连接时 SDK 进入 `WAIT_RECONNECT` 而不报错，doctor 要等满 5 秒才提示~~：M2 已加 TCP 预检，端口拒绝时 1 秒内报错且不加载 SDK
- 订单方向为 `SELL_SHORT` / `BUY_BACK` 的外部订单会让 `query_orders` 报 BrokerError（v1 只有 BUY / SELL；是否映射属于业务决策，ADR 未覆盖，遇到再议）
- SDK 会接住推送回调里的异常、只写进 SDK 日志：`MoomooBroker` 先用 logging 记 ERROR 再抛出（learnings「M2 实现」）；常驻进程要配置 logging 落盘（M9）
- sandbox 内 doctor 的 secrets 项：改为区分「无权读取」后未在 sandbox 内复验（Claude 不得访问 `~/.config/mooapp/`）
- ~~SDK 日志打印到控制台~~（M2 已用 `enable_console_log(False)` 关掉）；`set_all_thread_daemon(True)` 触发 SDK 内部的 DeprecationWarning（Python 3.15）

## M1 前置条件（全部满足）

- SPEC.md 已定并提交
- `docs/adr/` 全部已定
- OpenD 在线，`config/broker.toml` 含 `simulate_acc_id`
- PostgreSQL 17 已启动，库、角色、schema、密码已就绪
