# PROGRESS

**当前**：M2 第 1 步完成（2026-11-13）：两个探针已在常规时段运行，结论写入 learnings「M2 券商适配层：探针结论」。P3 时间为美东时间；P4、P7 模拟盘查不到成交记录和费用；P5 当日买入可卖；**模拟盘整单一次成交，录不到部分成交**
**下一步**：等你决定 M2 验收第 3 条怎么办，再进入第 2 步（写测试、贴验收测试草稿）

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
- OpenD 拒绝连接时 SDK 进入 `WAIT_RECONNECT` 而不报错，doctor 要等满 5 秒才提示
- sandbox 内 doctor 的 secrets 项：改为区分「无权读取」后未在 sandbox 内复验（Claude 不得访问 `~/.config/mooapp/`）
- SDK 日志打印到控制台；`set_all_thread_daemon(True)` 触发 SDK 内部的 DeprecationWarning（Python 3.15）

## M1 前置条件（全部满足）

- SPEC.md 已定并提交
- `docs/adr/` 全部已定
- OpenD 在线，`config/broker.toml` 含 `simulate_acc_id`
- PostgreSQL 17 已启动，库、角色、schema、密码已就绪
