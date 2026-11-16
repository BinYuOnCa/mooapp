# SPEC：mooapp v1

- **状态**：已定（2026-11-11；第 9 节 Q1–Q22 已全部答复并写回正文）
- **日期**：2026-11-11
- **依据**：`docs/manual.md`、`CLAUDE.md`、`docs/adr/0001–0025`（全部「已定」，其中 0012 已被 0024 取代；0015–0025 来自第 9 节答复）、`docs/order-transitions.md`、`docs/reference/order_state.py`、`docs/vendor/moomoo-API-Doc-en-Python.md`（下文简称 vendor）
- **约定**：本文引用 vendor 时写章节标题，如「vendor § Place Orders」。正文中的 `(Qn)` 表示该处按第 9 节的答复写；`[Qn]` 表示仍待决定。

---

## 1. 目标与非目标

### 1.1 目标

1. 学懂 Moomoo OpenAPI 的订单生命周期：下单、推送、部分成交、撤单、对账，以及模拟盘与实盘的差异。
2. 搭出一条**唯一的写路径**：命令行、规则引擎、Agent 三个入口 → 风控闸门 → OMS → BrokerPort → OpenD。
3. 具备安全的实盘能力：三把锁（配置锁、人锁、限额锁）+ kill switch，实盘只做微仓。
4. 用确定性代码执行止损止盈（本地 OCO）；Agent 只能「提议」，不能直接下单。
5. 每个里程碑留下 `docs/learnings.md` 记录，这是本项目最重要的产出。

### 1.2 非目标

| 不做 | 原因 / 依据 |
| --- | --- |
| 盈利、回测、策略优化 | 本版是学习项目 |
| 美股以外的市场 | ADR-0001 |
| 融资、卖空、期权、期货、加密货币 | ADR-0002 |
| 盘前、盘后、夜盘 | ADR-0003 |
| 限价 DAY 以外的入场订单 | ADR-0004 |
| 把止损止盈挂到券商；券商 STOP 单对照实验本版也不做，记入下一版（Q3） | ADR-0006 及其注记 |
| 改单（`modify_order`）：改价一律「撤单 + 新单」，两步都经闸门（Q4） | 第 9 节 Q4 |
| 无人值守实盘；交易密码落盘 | 手册第 7 节；CLAUDE.md |
| 使用 moomoo CLI 或官方 agent-hub Skills | 2026-11-11 审查：CLI 只凭 OAuth token 就能下实盘单，绕过闸门和人锁；`.claude/settings.json` 已 deny `Bash(moomoo:*)` |
| 新闻、情绪、异动等外部数据 | 本版暂不做 |
| 网络暴露 | ADR-0014：OpenD 与程序在同一台机器，只用本机端口 |

---

## 2. 已定约束

### 2.1 ADR 摘要

| # | 决策 | 取值 | 在代码中的落点 |
| --- | --- | --- | --- |
| 1 | 市场 | 只做美股 | `TrdMarket.US`；白名单只收 `US.*` |
| 2 | 账户与产品 | 现金账户；股票与 ETF；无卖空、融资、期权 | 闸门拒绝卖出量 > 可卖持仓 |
| 3 | 时段 | 只做常规时段 | 闸门 `regular_hours_only`；`session=RTH` |
| 4 | 入场单 | 限价、DAY | `OrderRequest` 校验 |
| 5 | 开盘价 | 买入首次成交当日常规时段开盘价；止损止盈价首次成交后算一次，持仓期间不重算 | strategy |
| 6 | 止损止盈执行 | 全部本地监控，两张都不挂券商 | strategy；无 STOP 单代码路径 |
| 7 | 触发后 | 立即市价卖出；设规则时价格已越过触发价则立即触发 | strategy |
| 8 | 部分成交 | 止损止盈数量 = 已买入且未卖出数量 | strategy 从 OMS 持仓读取 |
| 9 | 价格取整 | 按最小变动单位，止损向上、止盈向下 | `round_tick()` |
| 10 | 成本与 P&L | 平均成本法；未实现盈亏用当日常规时段收盘价 | eod |
| 11 | 时间 | 内部存 UTC；时段按 `America/New_York` 判断 | 全局 `Clock` |
| 12 | 存储 | ~~SQLite 单文件~~，已被 ADR-0024 取代 | — |
| 13 | Agent 接入 | 自建工具层，以 MCP server 暴露 | agent |
| 14 | 运行位置 | OpenD 与程序同一台常开电脑 | `host=127.0.0.1` |
| 15 | REAL 解锁的作用范围（Q1） | 解锁对整个 OpenD 生效；REAL 运行期间不开 Claude Code；退出、halt、异常时重新上锁；sandbox 能拦本机端口就拦 | ops 人锁、`BrokerPort.lock()`、sandbox 配置 |
| 16 | 撤单与 halt（Q5） | 撤单经闸门，只校验「本程序的活动订单」；不受限额和 halt 限制；照样审计 | risk |
| 17 | 当日亏损口径（Q6） | 已实现 + 浮动盈亏的当日变化 − 当日费用；交易日按 `America/New_York`；触及后按 `halt_on_daily_loss` 自动 halt | oms `daily_pnl`、risk |
| 18 | 当日下单次数口径（Q7） | 只算已发往券商的新单（含被券商拒绝的）；不算闸门拒绝和撤单 | oms `orders_sent_today` |
| 19 | 程序外订单与持仓（Q8） | 只管理 remark 带 client_order_id 的订单；外部订单只告警；持仓上限按券商总持仓；规则只管本程序买入的数量 | oms、risk、strategy |
| 20 | 同一标的多次入场（Q11） | 每个标的同时最多一条活动规则；有活动规则时拒绝 rule、agent 来源的新买单；命令行买入不受限，但规则不覆盖新增部分 | risk、strategy |
| 21 | 保护性卖单（Q12） | 当日次数和单笔金额不拦保护性卖单（数量 ≤ 可卖持仓）；只有规则引擎能发；halt 时规则引擎暂停并告警 | risk、strategy |
| 22 | halt 跨重启（Q16） | 持久化；重启后仍为 halt，只有 `mooapp resume` 能解除 | risk、store |
| 23 | 入场单未成交（Q17） | 不重挂；规则随入场单过期失效；部分成交后过期的，按已成交数量继续止损止盈 | strategy |
| 24 | 存储（Q15，取代 0012） | Homebrew PostgreSQL，只监听 `127.0.0.1:5432`；SIMULATE、REAL 两个 schema，由你手动创建；建表、索引、视图、函数与存储过程、授权等初始化代码进 Git；代码只依赖 `Store` 协议 | store |
| 25 | 本地日志先行（Q22） | 每次写库前先写本地 journal 文件；写库成功后移入归档；库不可用时撤单、halt 照常，新单拒绝 | store |

其余答复属于技术方案，直接写进正文，不另建 ADR：Q2（成交来源）、Q3（ADR-0006 注记）、Q4（不改单）、Q9（自动批准开关）、Q10（确认码）、Q13（依赖）、Q14（账号位置）、Q18（进程模型，3.4 节）、Q19–Q21（数据库驱动、凭据、部署，第 4 节）。Q15 另建 ADR-0024，Q22 另建 ADR-0025。

### 2.2 实盘三把锁

| 锁 | 规则 | 实现里程碑 |
| --- | --- | --- |
| 配置锁 | 默认 `SIMULATE`；只有命令行显式传 `--env real` 才进入 `REAL`；环境不来自配置文件或环境变量 | M1 |
| 人锁 | REAL 启动时：① `sys.stdin.isatty()` 与 `sys.stdout.isatty()` 都为真；② `getpass` 输入交易密码，调用 `unlock_trade`，密码只存在于局部变量，不写日志、不落盘；③ 显示随机确认码，用户原样输入 | M1（检查与拒绝），M4（完整流程） |
| 限额锁 | REAL 使用 `[risk.real]`；SIMULATE 使用 `[risk.simulate]`；Claude Code 不得修改数值 | M4 |

补充（ADR-0015）：`unlock_trade` 作用于整个 OpenD，「只要一个连接解锁，其他连接都能调用交易接口」（vendor § Unlock Trade）。人锁因此只守住「启动」这一步，另加三条：

1. REAL 运行期间不开 Claude Code（写进 REAL 启动提示和手册演练）。
2. 程序正常退出、halt、未处理异常时都调用 `unlock_trade(is_unlock=False)` 重新上锁（`BrokerPort.lock()`）；上锁失败要告警，不得吞掉。
3. sandbox 不放行 `127.0.0.1:11111`（2026-11-11 决定）：`.claude/settings.json` 只放行 PostgreSQL 的 unix socket `/tmp/.s.PGSQL.5432`，不开 `allowLocalBinding`。因此 Claude Code 连不上 OpenD，凡是连 OpenD 的命令（`doctor`、`pytest -m sim`、探针、模拟盘验收）都由你用 `!` 运行。P6 负责确认 11111 确实被拦。

M1 最后一条验收按「纵深防御」理解：目的是防止 Claude 自己连入 REAL。PreToolUse 钩子是第一道，人锁是第二道；测试假定第一道已失效，直接验证第二道（见第 10 节）。

### 2.3 CLAUDE.md 硬性规则在设计中的体现

- 下单、改单、撤单只有一条路径：`risk.gate.RiskGate` → `oms.executor`。M4 写防绕过测试（5.4 节）。
- 禁止静默吞错：broker 层把 SDK 的 `ret != RET_OK` 一律转成自定义异常（5.3 节）。
- API 用法在代码注释里写 vendor 章节；文档不明确的写探针脚本（第 7 节列出已知的不明确点）。

---

## 3. 架构

### 3.1 写路径

```text
  命令行 (ops/cli)        规则引擎 (strategy)        Agent (agent/MCP)
        │                        │                     │ propose_order
        │                        │                     ▼
        │                        │               审批队列 approvals
        │                        │                     │ mooapp approve <id>
        ▼                        ▼                     ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ risk/gate.py  RiskGate.submit(Intent) —— 唯一入口；halt 状态   │
  └──────────────────────────────────────────────────────────────┘
        │ 通过（带 GateTicket）              │ 拒绝 → 审计日志 + RiskRejected 事件
        ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ oms  OMS：状态机 / PostgreSQL / 幂等 / 恢复 —— executor 调 broker│
  └──────────────────────────────────────────────────────────────┘
        │ place_order / cancel_order                ▲ 推送 + 轮询 → 同一事件入口
        ▼                                           │
  ┌──────────────────────────────────────────────────────────────┐
  │ broker  BrokerPort  ←  MoomooBroker（OpenD）/ FakeBroker（测试）│
  └──────────────────────────────────────────────────────────────┘
```

读路径（行情、持仓、订单查询）不经过闸门。

### 3.2 模块与职责

| 模块 | 职责 | 里程碑 |
| --- | --- | --- |
| `config/` | 读取并用 pydantic 校验 `config/settings.toml`、`config/broker.toml`、`~/.config/mooapp/secrets.toml`；决定 `Env` 和当前生效的 `RiskLimits` | M1 |
| `ops/` | 命令行入口（`mooapp ...`）、人锁、日志、单实例锁、`status`、调度 | M1、M4、M9 |
| `broker/` | `BrokerPort` 和 `MarketDataPort` 协议；`MoomooBroker`（限流、重连、错误映射）；`FakeBroker`；推送录制 | M2 |
| `oms/` | 订单状态机（移植 `docs/reference/order_state.py`）、Moomoo 状态映射、`Store` 协议与 `PostgresStore`、幂等、推送与轮询合流、启动恢复；`executor` 是唯一调用 `BrokerPort` 写方法的地方 | M3 |
| `risk/` | `RiskGate`：限额规则、halt/resume、审计；各入口只能通过它下单、撤单 | M4 |
| `notify/` | 内部事件总线订阅者；邮件异步队列、重试、部分成交合并、心跳 | M5 |
| `strategy/` | 规则引擎：YAML + JSON Schema、表达式白名单、本地 OCO 监控、`dry-run` | M6 |
| `eod/` | 收盘任务：三方对账、P&L、CSV 报告、汇总邮件 | M7 |
| `agent/` | MCP server：只读工具、提议工具、审批队列、工具调用审计 | M8 |

### 3.3 依赖方向

```text
ops ──► agent ──► risk ──► oms ──► broker ──► config
 │        │        ▲        │        ▲
 │        └──► strategy ────┘        │
 └──► eod ───────────────────────────┘
notify 只订阅事件总线，不被其他模块直接调用；events / clock / errors 为共享基础模块。
```

规则：

1. `broker` 之外不 import `moomoo`（SDK 类型不出 broker 层）。
2. `strategy`、`agent`、`ops` 不 import `oms.executor`，也不调用 `BrokerPort` 的写方法；只能调用 `RiskGate.submit`。
3. `risk` 只依赖 `oms` 的只读查询（持仓、当日订单数、当日盈亏、是否有活动规则）和 `oms.executor`；**不 import `strategy`**。ADR-0020 需要的「该标的是否有活动规则」由 `Store` 提供只读查询，规则状态由 strategy 写入同一个 `Store`。
4. 所有时间经 `Clock` 获取（便于测试与 ADR-0011）。

### 3.4 进程与线程模型

- **线程**：SDK 的推送回调在 SDK 自己的线程里执行。所有 `OmsEvent`（推送、轮询、本地）先放进一个队列，由**单个消费线程**依次调用 `OMS.handle`，因此 `OMS` 和 `Store` 不需要加锁。闸门的检查与 `Executor` 的发单在同一把锁内完成，避免两个入口同时通过「当日次数」检查。
- **进程**（Q18）：`mooapp run`、`mooapp order …`、`mooapp approve`、`mooapp agent serve` 是本项目的几个独立进程（不是 SDK 的要求）。代码里只有一条写路径还不够，运行时也只能有一个闸门实例：
  - 只有常驻进程 `mooapp run` 构造 `RiskGate`、`OMS`、`Executor` 和券商连接，并持有单实例锁（PostgreSQL advisory lock，按 schema 区分）。M3 提供最小版本，M9 加调度和开机自启。
  - 其他进程不构造这些对象。写操作（下单、撤单、halt、resume、approve）写入 `commands` 表；常驻进程按顺序取出交给闸门，把 `Decision` 写回同一行；命令行同步等待结果，超时报错。唤醒用 `LISTEN/NOTIFY`，另加 1 秒轮询兜底。
  - 常驻进程没在跑时（拿不到 advisory lock 的持有者心跳），写命令直接报错。只读命令直接读 `Store`。
  - 用库表而不是 socket：不需要另开端口，可审计，每条指令都留痕。
  - 数据库不可用时（ADR-0025）：闸门读不到当日次数、持仓、亏损，**新单一律拒绝**；撤单和 halt 照常执行，审计和事件先落在本地 journal，库恢复后补写。

---

## 4. 运行环境与数据

| 项 | 规定 |
| --- | --- |
| Python | ≥ 3.12（`pyproject.toml`） |
| SDK | `moomoo-api>=10.12.7208` |
| OpenD | 本机 `127.0.0.1:11111`，参数来自 `config/broker.toml`（阶段 0 生成） |
| 交易账户 | 用 `acc_id` 指定，不用 `acc_index`（vendor § Place Orders）。模拟账户写在 `config/broker.toml` 的 `simulate_acc_id`（会进 Git；Claude Code 在 sandbox 里也能读到，可以跑 `doctor` 和 `pytest -m sim`）；实盘账户写在 `~/.config/mooapp/secrets.toml` 的 `[accounts] real_acc_id`（Q14）。启动时用 `get_acc_list` 核对：配置的 `acc_id` 必须出现在对应 `trd_env` 的账户里，否则报错退出。所有输出中账号只显示后 4 位 |
| 存储 | PostgreSQL（ADR-0024），**视为本项目之外的基础设施**（Q19）：程序不负责安装、启停、备份数据库，也不建库、建角色、建 schema，只连接已存在的 schema 并执行 `db/` 里的初始化代码 |
| 数据库部署 | 用本机 Homebrew 安装的 `postgresql@17`，放弃 Docker。`postgresql.conf` 里 `listen_addresses` 只能是回环地址（`localhost` 即 127.0.0.1 与 ::1，或 `127.0.0.1`），`port = 5432`；`pg_hba.conf` 只保留 `local mooapp,mooapp_test all scram-sha-256` 和 `host … 127.0.0.1/32 scram-sha-256`、`::1/128 scram-sha-256`，不得有 `trust`（initdb 默认会生成 trust 行，需要你检查）。开机自启用 `brew services start`（M9）。安装、配置、启停、备份都由你负责 |
| 手动创建（你做，2026-11-11 已完成） | 库 `mooapp`、`mooapp_test`；角色 `mooapp_sim`、`mooapp_real`；`mooapp` 中 schema `simulate`（属主 `mooapp_sim`）、`real`（属主 `mooapp_real`）；`mooapp_sim` 在 `mooapp_test` 有 `CONNECT`、`CREATE`（`pytest -m pg` 每次建临时 schema、结束删除）。2026-11-11 实测：`mooapp_sim` 对 `real` 无 `USAGE`、查询报 permission denied；`mooapp_real` 对 `simulate` 无 `USAGE`；`mooapp_sim` 在 `mooapp` 库没有 `CREATE`；无密码连接被拒（pg_hba 已是 scram）；`listen_addresses = localhost` |
| 数据库凭据 | 两个角色都用 `scram-sha-256`（Q20）。`mooapp_sim` 用专用随机密码（不在别处使用），写在 `config/db.local.toml`：被 `.gitignore` 忽略、sandbox 能读，属于「低等级凭据」，处理方式同模拟账户 acc_id，但不进 Git。`mooapp_real` 的密码在 `~/.config/mooapp/secrets.toml` 的 `[postgres] real_password`。连接参数（host、port、库名、角色名）在 `config/db.toml`，可提交 |
| 初始化代码（进 Git） | 放仓库根目录 `db/`，由 `mooapp db migrate` 在当前环境的 schema 里、以该 schema 的属主角色执行（自写执行器，不引入 Alembic）：`db/migrations/NNNN_<名>.sql` 建表、索引、约束，按编号只执行一次，记在每个 schema 的 `schema_migrations` 表；`db/views/`、`db/routines/`（函数与存储过程）每次 migrate 都用 `CREATE OR REPLACE` 重新应用；`db/grants/` 授权最后执行。PostgreSQL 没有 Oracle 式的 package，按文件名前缀分组（如 `db/routines/analytics_*.sql`） |
| 存储过程的边界（已定） | 以后可以用存储过程承担一部分业务逻辑，尤其是 ad hoc 分析。约束：存储过程**不得写** OMS 拥有的表（`orders`、`order_events`、`fills`、`rules`、`approvals`、`commands`、`audit`、`kv`），这些表只能由常驻进程经 `Store` 写入（3.4 节、ADR-0025 journal）；分析类函数声明为 `STABLE`、`SECURITY INVOKER`，只读这些表，结果可以写进它们自己的 `analytics_*` 表。M3 加一条静态检查扫描 `db/routines/`。需要存储过程改动交易状态时，先停下来另建 ADR |
| 本地 journal | `var/journal/<env>/pending/`、`var/journal/<env>/archive/YYYY-MM-DD/`（ADR-0025）；与日志同等对待，不进 Git |
| 运行产物 | `var/logs/`、`var/journal/`，`recordings/`、`reports/`；`.gitignore` 已加 `var/` 与 `config/*.local.toml` |
| 密钥 | 只在 `~/.config/mooapp/secrets.toml`（SMTP、OpenD token、实盘 `acc_id`、REAL 数据库角色的密码）；交易密码不在任何文件中 |
| 新增依赖 | 已批准（Q13）：运行 `pydantic`、`typer`、`PyYAML`、`jsonschema`、`mcp`；开发 `pytest`。Q19 批准：`psycopg[binary]>=3.2` |

---

## 5. 接口草案

只列签名与数据类，方便审阅；实现时以里程碑计划为准。金额与价格一律用 `Decimal`，数量用 `int`（美股整股）。

### 5.1 共享

```python
# mooapp/common.py
class Env(StrEnum):          SIMULATE = "SIMULATE"; REAL = "REAL"
class Side(StrEnum):         BUY = "BUY"; SELL = "SELL"
class OrderKind(StrEnum):    LIMIT = "LIMIT"; MARKET = "MARKET"     # v1 只有这两种
class Source(StrEnum):       CLI = "cli"; RULE = "rule"; AGENT = "agent"; SYSTEM = "system"

class Clock(Protocol):
    def now_utc(self) -> datetime: ...                 # tz-aware UTC
    def to_market(self, t: datetime) -> datetime: ...  # America/New_York

class MooappError(Exception): ...                      # 所有自定义异常的基类
```

### 5.2 config

```python
@dataclass(frozen=True)
class RiskLimits:
    whitelist: frozenset[str]
    max_order_qty: int
    max_order_value: Decimal
    max_position_qty: int
    max_daily_orders: int
    max_daily_loss: Decimal
    halt_on_daily_loss: bool
    max_limit_deviation_pct: Decimal
    regular_hours_only: bool

class Settings(BaseModel): risk_simulate: RiskLimits; risk_real: RiskLimits; quote: QuoteCfg; notify_email: EmailCfg; agent: AgentCfg
class AgentCfg(BaseModel): auto_approve_simulate: bool = False      # Q9；不是风控限额
class DbCfg(BaseModel): host: str = "/tmp"; port: int = 5432; dbname: str = "mooapp"; sim_role: str = "mooapp_sim"; real_role: str = "mooapp_real"   # config/db.toml（Q20）
class DbLocal(BaseModel): sim_password: str                      # config/db.local.toml；repr 不输出值
class BrokerCfg(BaseModel): host: str; port: int; filter_trdmarket: Literal["US"]; simulate_acc_id: int   # Q14
class Secrets(BaseModel):  smtp: SmtpSecrets; opend_token: str = ""; real_acc_id: int | None = None   # repr 不输出值；只有 REAL 需要 real_acc_id

def load_settings(path: Path = Path("config/settings.toml")) -> Settings
def load_broker_cfg(path: Path = Path("config/broker.toml")) -> BrokerCfg
def load_secrets(path: Path = Path.home() / ".config/mooapp/secrets.toml") -> Secrets
def limits_for(settings: Settings, env: Env) -> RiskLimits
def acc_id_for(env: Env, broker: BrokerCfg, secrets: Secrets | None) -> int   # SIMULATE 不读 secrets；REAL 缺 real_acc_id 抛 ConfigError
def mask_account(acc_id: int | str) -> str    # 只保留后 4 位；日志、邮件、doctor、录制统一用它
class ConfigError(MooappError): ...   # 缺文件、缺项、数值非法（如负数）时抛出，信息可读，无堆栈刷屏
```

### 5.3 broker

```python
@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str          # 本地生成，写入 remark（≤ 64 字节 UTF-8，vendor § Place Orders）
    symbol: str                   # "US.SCHD"
    side: Side
    qty: int
    kind: OrderKind
    limit_price: Decimal | None   # LIMIT 必填；MARKET 为 None
    # time_in_force 固定 DAY，session 固定 RTH（ADR-0003/0004）

@dataclass(frozen=True)
class BrokerOrder:                # 券商视角的订单快照（查询或推送）
    broker_order_id: str
    client_order_id: str | None   # 来自 remark；外部订单为 None
    symbol: str; side: Side; kind: OrderKind
    qty: int; price: Decimal | None
    raw_status: str               # Moomoo OrderStatus 原文，映射在 oms（M3）
    dealt_qty: int; dealt_avg_price: Decimal
    last_err_msg: str
    updated_at: datetime          # UTC；时区换算规则待探针确认（第 7 节）

@dataclass(frozen=True)
class BrokerFill:
    fill_id: str                  # deal_id
    broker_order_id: str
    symbol: str; side: Side; qty: int; price: Decimal
    ts: datetime                  # UTC

@dataclass(frozen=True) class Position: symbol: str; qty: int; can_sell_qty: int; avg_cost: Decimal
@dataclass(frozen=True) class Cash:     currency: str; cash: Decimal; buying_power: Decimal
@dataclass(frozen=True) class Quote:    symbol: str; last: Decimal; open: Decimal | None; ts: datetime

class BrokerPort(Protocol):
    env: Env
    # 写（只允许 oms.executor 调用）；v1 不提供 modify_order（Q4）
    def place_order(self, req: OrderRequest) -> BrokerOrder: ...
    def cancel_order(self, broker_order_id: str) -> None: ...
    # 读
    def query_orders(self, open_only: bool = True) -> list[BrokerOrder]: ...
    def query_order_history(self, start: datetime, end: datetime) -> list[BrokerOrder]: ...   # history_order_list_query（vendor § Get Historical Orders，30 秒 10 次）；前一天的订单 query_orders 查不到，启动恢复用它结束旧单（2026-11-16 修复计划 2）
    def query_deals(self) -> list[BrokerFill]: ...              # 当日；模拟盘的成交主要靠它和订单推送的 dealt_qty 增量（Q2）
    def query_fees(self, broker_order_ids: list[str]) -> dict[str, Decimal]: ...   # order_fee_query（vendor § Get Order Fee，OpenD ≥ 8.2.4218）；模拟盘是否有值见 P7
    def query_positions(self) -> list[Position]: ...
    def query_cash(self) -> Cash: ...
    # 推送
    def on_order_update(self, cb: Callable[[BrokerOrder], None]) -> None: ...
    def on_fill(self, cb: Callable[[BrokerFill], None]) -> None: ...   # 只有实盘有成交推送（Q2）
    # 会话
    def unlock(self, password: str) -> None: ...                # 仅 REAL；只由 ops 人锁调用
    def lock(self) -> None: ...
    def close(self) -> None: ...

class MarketDataPort(Protocol):
    def snapshot(self, symbols: list[str]) -> list[Quote]: ...
    def subscribe_quotes(self, symbols: list[str], cb: Callable[[Quote], None]) -> None: ...
    def market_state(self) -> str: ...
    def is_trading_day(self, d: date) -> bool: ...

# 异常（SDK 的 ret != RET_OK 全部映射，不得吞掉）
class BrokerError(MooappError): ...
class BrokerUnavailable(BrokerError): ...    # OpenD 不可达 / 断线
class RateLimited(BrokerError): ...
class OrderRejected(BrokerError): ...
class NotUnlocked(BrokerError): ...
```

`MoomooBroker` 的强制要求：

- 每次调用 `place_order`、`modify_order`、查询类接口都**显式传 `trd_env`**。vendor § Place Orders 与 § Modify or Cancel Orders 的默认值是 `TrdEnv.REAL`，漏传即为实盘。契约测试检查这一点。
- 限流按 vendor：下单 30 秒 15 次且间隔 ≥ 0.02 秒；改单/撤单 30 秒 20 次且间隔 ≥ 0.04 秒；解锁 30 秒 10 次。
- 推送可能早于 `place_order` 返回到达（vendor § Place Orders 提示），OMS 必须能处理「未知 broker_order_id 的推送」（按 remark 里的 client_order_id 关联）。
- 成交来源（Q2）：模拟盘用订单推送里 `dealt_qty` 的增量 + 定时 `deal_list_query` 轮询；实盘以成交推送为主、轮询兜底。都进 `OMS.handle`，按 `deal_id` 去重；模拟盘若查不到 `deal_id`（P4），用 `order_id` + 累计成交量生成合成 id。
- 价格由 `round_tick` 先取整好再下单，`place_order` 不使用 `adjust_limit` 自动调价（vendor § Place Orders、FAQ Q14），避免价格被 OpenD 悄悄改动。

### 5.4 oms

```python
class OrdStatus(StrEnum): ...      # 照搬 order_state.py 的 13 个状态
class EventType(StrEnum): ...      # 照搬 order_state.py 的 20 个事件

@dataclass
class Order:
    client_order_id: str; broker_order_id: str | None
    env: Env; source: Source; symbol: str; side: Side; kind: OrderKind
    qty: int; limit_price: Decimal | None
    status: OrdStatus; cum_qty: int; avg_fill_price: Decimal
    created_at: datetime; updated_at: datetime

@dataclass(frozen=True)
class OmsEvent:
    type: EventType
    client_order_id: str
    origin: Literal["push", "poll", "local"]   # 写入 order_events.source
    exec_id: str | None = None                  # 成交去重键
    qty: int | None = None; price: Decimal | None = None
    reason: str | None = None

class Store(Protocol):                          # ADR-0024：协议；实现 PostgresStore，单元测试用 MemoryStore
    # 表：orders、order_events（只追加）、fills、rules、approvals、commands（Q18）、audit、kv（halt 状态等）、schema_migrations
    # 一个 Store 实例只对应一个 Env（一个 schema，search_path 固定），不存在跨环境查询
    # 金额列用 NUMERIC，时间列用 timestamptz（UTC，ADR-0011）
    def insert_order(self, o: Order) -> None: ...
    def append_event(self, e: OmsEvent, new_status: OrdStatus) -> None: ...
    def insert_fill_once(self, f: BrokerFill) -> bool: ...     # 按 fill_id 去重，已存在返回 False
    def active_rule_for(self, symbol: str) -> str | None: ...   # ADR-0020；供 risk 只读查询
    def get_kv(self, key: str) -> str | None: ...; def set_kv(self, key: str, value: str) -> None: ...   # halt 状态（ADR-0022）
    def transaction(self) -> ContextManager[None]: ...

class PostgresStore(Store): def __init__(self, dsn: str, schema: Literal["simulate", "real"]): ...
    # 连接后断言 current_schema() == schema，且 current_user 对另一个 schema 没有 USAGE 权限（Q20）
class MemoryStore(Store): ...                   # 仅测试用

def dsn_for(env: Env, db: DbCfg, local: DbLocal | None, secrets: Secrets | None) -> str
    # SIMULATE 只读 db.local.toml，不读 secrets；REAL 只读 secrets；host 只能是 unix socket 目录（默认 `/tmp`）或 `127.0.0.1`，否则抛 ConfigError（ADR-0014）。默认走 unix socket，因为 sandbox 只放行了 socket
    # DSN 含密码，不得写日志；日志里只记 host、port、库名、角色

class Journal:                                  # ADR-0025：本地日志先行
    def __init__(self, root: Path, env: Env): ...   # var/journal/<env>/
    def write(self, entry: JournalEntry) -> Path: ...       # pending/<entry_id>.json；写完 fsync，权限 600
    def archive(self, entry_id: str) -> None: ...           # 写库成功后移到 archive/YYYY-MM-DD/
    def pending(self) -> list[JournalEntry]: ...            # 按写入顺序
class JournaledStore(Store): ...                # 包一层：每个写事务先 Journal.write，提交成功再 archive
def replay_journal(journal: Journal, store: Store) -> ReplayReport   # 启动和库恢复时执行；按 entry_id 幂等

class OMS:
    def __init__(self, store: Store, broker: BrokerPort, bus: EventBus, clock: Clock): ...
    def handle(self, e: OmsEvent) -> Order: ...                # 推送、轮询、本地事件的唯一入口；只在单个消费线程上调用（3.4 节）
    def recover(self) -> RecoveryReport: ...                   # 启动对齐；完成前拒绝新指令
    # 只读查询（供 risk / strategy / agent / eod）
    def get(self, client_order_id: str) -> Order: ...
    def open_orders(self) -> list[Order]: ...
    def position(self, symbol: str) -> int: ...                 # 券商总持仓，含程序外部分（ADR-0019）
    def orders_sent_today(self) -> int: ...                     # ADR-0018：已发往券商的新单，含券商拒绝；不含闸门拒绝和撤单
    def daily_pnl(self) -> Decimal: ...                         # ADR-0017：已实现 + 浮动盈亏当日变化 − 当日费用；交易日按 America/New_York
    def external_orders(self) -> list[BrokerOrder]: ...         # remark 无 client_order_id 的订单，只告警不处理（ADR-0019）

# oms/executor.py —— 唯一调用 BrokerPort 写方法的模块
class Executor:
    def place(self, ticket: GateTicket, req: OrderRequest) -> Order: ...
    def cancel(self, ticket: GateTicket, client_order_id: str) -> None: ...
```

防绕过（M4 测试）：静态扫描 `src/`，`.place_order(` 和 `.cancel_order(` 只允许出现在 `oms/executor.py` 与 `broker/`；`Executor` 的方法要求一个只有 `RiskGate` 能构造的 `GateTicket`。

### 5.5 risk

```python
@dataclass(frozen=True)
class Intent:
    source: Source
    action: Literal["place", "cancel"]          # 不支持 modify（Q4）
    request: OrderRequest | None = None         # place 时必填
    client_order_id: str | None = None          # cancel 时必填
    actor: str = ""                             # "cli" / "rule:<id>" / "agent:approval:<id>"
    protective: bool = False                    # 止损止盈卖单（ADR-0021）；只有 source == RULE 且 side == SELL 时才被承认，否则闸门拒绝该 Intent

@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule: str | None                            # 命中的规则名，如 "max_order_qty"
    reason: str
    order: Order | None = None

class RiskGate:
    def __init__(self, limits: RiskLimits, oms: OMS, md: MarketDataPort,
                 executor: Executor, bus: EventBus, clock: Clock): ...
    def submit(self, intent: Intent) -> Decision: ...   # 每次拒单写审计日志并发 RiskRejected 事件
    def halt(self, reason: str, actor: str) -> None: ...  # 拒绝新单 + 撤销全部挂单 + 暂停规则引擎 + 上锁（REAL）；状态写入 Store（ADR-0022）
    def resume(self, actor: str) -> None: ...           # 只能由 ops 在交互确认后调用
    @property
    def halted(self) -> bool: ...
```

规则顺序（任一不通过即拒）：halt → 白名单 → 常规时段 → 单笔数量 → 单笔金额（限价 × 数量；市价用最新价）→ 限价偏离 → 单票持仓上限（买）/ 可卖数量（卖，ADR-0002）→ 当日下单次数 → 当日亏损上限。撤单只检查「订单属于本程序且处于活动状态」，不受限额和 halt 限制，照样写审计（ADR-0016）。

规则的例外与补充：

- **同一标的已有活动规则**（ADR-0020）：`source` 为 `RULE`（入场单）或 `AGENT` 的买单被拒；`CLI` 买单放行，但规则不覆盖新增部分。
- **保护性卖单**（ADR-0021）：`protective=True` 且 `source == RULE` 时跳过「单笔金额」和「当日下单次数」，其余照查（白名单、时段、数量 ≤ 可卖持仓）；halt 状态下规则引擎已暂停，不会产生保护性卖单。其他来源带 `protective=True` 一律拒绝并告警。
- **当日亏损**（ADR-0017）：触及 `max_daily_loss` 且 `halt_on_daily_loss` 为真时自动 `halt`。

### 5.6 strategy（规则引擎）

```python
@dataclass(frozen=True)
class RuleSpec:                    # 由 YAML 加载并经 JSON Schema 校验
    id: str; symbol: str
    entry: EntrySpec               # 限价 DAY 买入（ADR-0004）
    stop_expr: str                 # 例："min(open, avg_cost) * 0.95"
    take_expr: str                 # 例："max(open, avg_cost) * 1.05"

def compile_expr(src: str) -> Callable[[ExprEnv], Decimal]
    # 只允许变量 open, avg_cost, last 与函数 min, max, round_tick；基于 ast 白名单，禁止 eval/exec
def round_tick(price: Decimal, direction: Literal["up", "down"]) -> Decimal   # ADR-0009；美股价位：< $1 为 0.0001，≥ $1 为 0.01（vendor FAQ Q14）

class RuleEngine:
    def __init__(self, gate: RiskGate, oms: OMS, md: MarketDataPort, store: Store, clock: Clock): ...
    def add(self, spec: RuleSpec) -> None: ...      # 该标的已有活动规则时拒绝（ADR-0020）
    def on_fill(self, order: Order) -> None: ...    # 首次成交时计算并冻结止损止盈价（ADR-0005）
    def on_quote(self, q: Quote) -> None: ...       # 越过即发市价卖单（ADR-0007），数量 = 剩余持仓（ADR-0008），Intent 带 protective=True
    def on_entry_expired(self, order: Order) -> None: ...  # ADR-0023：未成交则规则失效；部分成交则按已成交数量继续
    def pause(self) -> None: ...; def resume(self) -> None: ...   # 跟随 halt / resume（ADR-0021）
    def dry_run(self, spec: RuleSpec, prices: list[PricePoint]) -> list[DryRunEvent]: ...
```

### 5.7 notify

```python
class EventBus:
    def publish(self, e: DomainEvent) -> None: ...
    def subscribe(self, kind: type[DomainEvent], cb: Callable[[DomainEvent], None]) -> None: ...
# DomainEvent：OrderPlaced, OrderCanceled, OrderFilled, RiskRejected, ReconBreak, EodPnl, Heartbeat, Alert

class EmailNotifier:   # 订阅 EventBus；异步队列 + 重试；SMTP 失败不阻塞交易；账号只显示后 4 位
    def start(self) -> None: ...
    def stop(self) -> None: ...
```

### 5.8 eod

```python
@dataclass(frozen=True)
class EodReport:
    trade_date: date
    rows: list[PnlRow]          # 逐标的：已实现、未实现、费用；券商盈亏与差异
    breaks: list[ReconBreak]    # 有差异时邮件标题加 [BREAK]
def run_eod(trade_date: date, oms: OMS, broker: BrokerPort, md: MarketDataPort, clock: Clock) -> EodReport
def write_csv(report: EodReport, out_dir: Path) -> Path   # reports/YYYY-MM-DD.csv
```

### 5.9 agent（MCP server）

| 类别 | 工具 | 说明 |
| --- | --- | --- |
| 只读 | `get_quote`、`get_positions`、`get_orders`、`get_pnl`、`explain_order` | 直接读 OMS / MarketDataPort |
| 提议 | `propose_order`、`propose_rule` | 只写入 `approvals` 表，返回 `approval_id`；不调用闸门 |
| 不提供 | 修改限额、切换环境、读取配置、批准 | — |

```python
class ApprovalQueue:
    def propose(self, kind: Literal["order", "rule"], payload: dict, caller: str) -> str: ...
    def approve(self, approval_id: str, confirm_code: str | None) -> Decision: ...  # 批准后才 gate.submit(source=AGENT)
    def reject(self, approval_id: str, reason: str) -> None: ...
```

REAL 下 `approve` 只能在交互终端由 `mooapp approve <id>` 调用并输入确认码；SIMULATE 下由 `[agent] auto_approve_simulate` 决定是否自动批准，默认 `false`（Q9）。每次工具调用记录输入、输出、时间和调用方。外部文本一律当数据。

### 5.10 ops（命令行）

| 命令 | 里程碑 | 说明 |
| --- | --- | --- |
| `mooapp doctor` | M1 | OpenD 与 SDK 版本、账户列表（后 4 位）、模拟账户资金、白名单第一只的快照、市场状态 |
| `mooapp --env real <cmd>` | M1/M4 | 触发人锁；非交互终端直接拒绝 |
| `mooapp order buy\|sell <symbol> <qty> --limit <price>` | M4 | 经闸门 |
| `mooapp order cancel <client_order_id>` | M4 | 经闸门 |
| `mooapp order list` | M3 | 只读 |
| `mooapp halt` / `mooapp resume` | M4 | resume 需交互确认 |
| `mooapp rule add\|list\|dry-run` | M6 | |
| `mooapp eod [--date]` | M7 | |
| `mooapp approve <id>` / `mooapp reject <id>` | M8 | |
| `mooapp agent serve` | M8 | 启动 MCP server |
| `mooapp run` / `mooapp status` | M9 | 常驻进程 / 一屏状态 |

```python
def require_human_for_real(stdin: TextIO, stdout: TextIO,
                           getpass_fn: Callable[[str], str],
                           rng: random.Random) -> None
    # 任一检查失败抛 HumanLockError；密码读出后立即传给 broker.unlock，不返回、不存储
    # 人锁在建立任何 OpenD 连接之前执行；失败时不得构造 BrokerPort

def new_confirm_code(n: int = 6) -> str
    # Q10：secrets 模块生成，字母数字，排除 0/O、1/I/l；人锁与 REAL approve 共用；输错一次即放弃
```

---

## 6. vendor 要点（与设计直接相关）

| 要点 | 出处 | 影响 |
| --- | --- | --- |
| `place_order` / `modify_order` 的 `trd_env` 默认 `REAL` | § Place Orders、§ Modify or Cancel Orders | 必须显式传；契约测试 |
| 模拟盘下单、改单不需要 `unlock_trade` | § Unlock Trade、§ Place Orders | M1 探针再实测确认 |
| `unlock_trade` 对整个 OpenD 生效 | § Unlock Trade | ADR-0015 |
| 模拟盘只支持 `NORMAL`（限价）与 `MARKET` | § Trading Definitions › Order Type | 券商 STOP 对照实验只能在实盘做，本版不做（Q3） |
| 模拟盘美股不支持盘前盘后、夜盘 | § Place Orders | 与 ADR-0003 一致 |
| 成交推送（Deals Push）只在实盘有，模拟盘没有 | § Deals Push Callback | 成交来源按 Q2（5.3 节） |
| `remark` 最长 64 字节（UTF-8） | § Place Orders | 存放 client_order_id |
| 推送可能早于 `place_order` 返回 | § Place Orders | OMS 处理乱序 |
| 改数量时 `qty` 传「期望的总量」 | § Modify or Cancel Orders | v1 不改单（Q4），暂不相关 |
| 市价单只在常规时段 | § Place Orders | 与 ADR-0003/0007 一致 |
| 限频：下单 15 次/30 秒，改撤 20 次/30 秒，解锁 10 次/30 秒 | 各接口 Interface Limitations | broker 限流器 |
| Moomoo 订单状态 11 个（`WAITING_SUBMIT` … `DELETED`），没有「撤单中」 | § Trading Definitions › Order Status | M3 映射到 13 个内部状态；映射不了的报警 |
| 用 `acc_id` 而不是 `acc_index` | § Place Orders | 位置见第 4 节（Q14） |
| 美股价格精度：< $1 保留 4 位小数，≥ $1 保留 2 位；价位 < $1 为 $0.0001，≥ $1 为 $0.01 | § Place Orders › price；FAQ Q14 | `round_tick`（ADR-0009）；不用 `adjust_limit` |
| `order_fee_query` 查订单费用，OpenD ≥ 8.2.4218 | § Get Order Fee | M7 P&L 的费用；模拟盘是否返回见 P7 |
| 交易日历 `request_trading_days` 返回 `trade_date_type`（`WHOLE` / `MORNING` / `AFTERNOON`） | § Get Trading Calendar；§ TradeDateType | 美股半日市怎么标记文档未写明，见 P8；M7「收盘后 30 分钟」和 M9 日程按当天实际收盘时间算 |

---

## 7. 探针

### 7.1 探针规则（已定，CLAUDE.md 硬性规则引用本节）

探针是用来在模拟盘上实测 API 行为的一次性脚本，不是产品代码，也不是测试。

| # | 规则 | 怎么做 |
| --- | --- | --- |
| R1 | **只在 SIMULATE 跑** | 所有交易调用写死 `trd_env=TrdEnv.SIMULATE`，不接受 `--env` 等任何环境参数；源码里不得出现 `TrdEnv.REAL`、`unlock_trade`、`getpass`。模拟账户用 `broker.toml` 的 `simulate_acc_id`，并用 OpenD 的 `get_acc_list` 核对：它必须属于 `trd_env == SIMULATE` 且市场为 US 的账户，否则退出。每次下单前再断言一次 |
| R2 | **不能被正式代码 import** | 文件放 `scripts/`，命名 `probe_<里程碑>[_<主题>].py`（如 `probe_m1.py`、`probe_m2_partial_fill.py`）；`scripts/` 下不放 `__init__.py`；`src/` 与 `tests/` 不得 import 或执行任何 `scripts/` 下的文件。依赖只能单向：探针只用 SDK `moomoo`、标准库，以及 `mooapp.config` 里读 `broker.toml` 的函数 |
| R3 | **不读密钥** | 不读 `~/.config/mooapp/` 下任何文件（模拟账户来自 `broker.toml`，见 R1），不连接 SMTP |
| R4 | **打印并记录原始输入输出** | 每次 SDK 调用和每条推送都原样打印，并逐行写入 `recordings/probes/<探针名>_<UTC 时间 YYYYMMDDTHHMMSSZ>.jsonl`（已被 `.gitignore` 忽略）。每行包含：UTC 时间戳、调用名或推送类型、全部入参、`ret`、原始返回（DataFrame 按行转成 dict，不做取舍）。文件第一行记录 SDK 版本、OpenD 版本、探针文件的 git 提交号 |
| R5 | **唯一的脱敏例外：账号** | 打印和记录中，凡是账户号（`acc_id`、`card_num` 等）只保留后 4 位，与 `mask_account` 一致（模拟账户虽已写入 `broker.toml`，录制里仍统一脱敏）。除此之外不改动任何原始值 |
| R6 | **副作用有界** | 只交易 `[risk.simulate]` 白名单内的标的；单笔 1 股，除非探针目的需要更多（在探针开头的 docstring 里写明数量和理由）；不需要成交的限价单离最新价至少 10%；`remark` 以 `probe:` 开头，与 OMS 订单区分；遵守 6 节的限频；结束时撤销本次运行挂出的全部订单，并打印撤单结果 |
| R7 | **不吞错** | 任何 `ret != RET_OK` 或异常都原样打印、写入记录，然后以非 0 退出码结束；`finally` 里的撤单失败同样打印并记录，不得 `pass` |
| R8 | **结论写进 learnings** | 每个探针跑完，在 `docs/learnings.md` 对应里程碑下追加一条，格式见 7.2。没有写入 learnings 的结论，不得作为实现依据 |
| R9 | **结论转为契约测试** | 探针确认的行为如果 BrokerPort 要依赖，就在 `tests/contract/` 写一条对应测试（默认跑 FakeBroker，`-m sim` 跑模拟盘），探针本身不进入 pytest |

自动检查（M1 实现，放 `tests/unit/test_probe_rules.py`；不是验收测试）：对 `scripts/probe_*.py` 做 AST 扫描，检查 R1、R3 的禁用项，并确认每个 `place_order` / `modify_order` 调用都显式带 `trd_env=TrdEnv.SIMULATE`；对 `src/` 与 `tests/` 扫描，确认没有 import `scripts`；确认 `scripts/__init__.py` 不存在。

### 7.2 learnings 条目格式

```markdown
#### <问题一句话>（P<n>）

- **结论**：……
- **依据**：实测 / 文档 / 两者
- **出处**：vendor § <章节>（文档未写明则写「文档未写明」）；`scripts/probe_<…>.py` @ <git 提交号>
- **记录**：`recordings/probes/<文件名>.jsonl`；运行时间 <UTC>；SDK <版本>，OpenD <版本>
- **影响**：涉及的模块或 SPEC 条目
```

### 7.3 需要探针实测的点（不是决策，按里程碑排）

| # | 问题 | 里程碑 | 探针 |
| --- | --- | --- | --- |
| P1 | 模拟盘是否真的不需要 `unlock_trade` | M1 | `scripts/probe_m1.py`（手册已列） |
| P2 | 你的账户在模拟盘能否拿到美股实时行情（快照与订阅），还是延时 | M1 | 同上，加打印 `get_market_snapshot` 的时间戳 |
| P3 | 订单与成交时间字符串的时区（文档只写了期货参见 OpenD 配置） | M2 | 下单后比较 `create_time` 与本机 UTC |
| P4 | 模拟盘部分成交怎么表现；`deal_list_query` 在模拟盘能否查到成交及 `deal_id` | M2 | 录制推送 |
| P5 | 模拟账户类型（现金还是融资）及 `can_sell_qty` 当日买入是否可卖 | M2 | `accinfo_query`、`position_list_query` |
| P6 | sandbox 只放行 PostgreSQL 的 unix socket `/tmp/.s.PGSQL.5432`。**2026-11-11 用 shell 命令实测（不是探针脚本）**：sandbox 内 socket 连接成功（PostgreSQL 17.11）；TCP `127.0.0.1:5432` 无响应；TCP `127.0.0.1:11111` 报 `PermissionError: Operation not permitted`。M1 的探针 `probe_m1.py` 在 sandbox 内再确认一次 11111 被拦，并按 R4 留记录 | M1 | ADR-0015 第 3 条；`pytest -m pg` 由 Claude 跑，连 OpenD 的由你用 `!` 跑 |
| P7 | 模拟盘 `order_fee_query` 是否返回费用，字段是否齐全 | M2 | 下单成交 1 股后查询 |
| P8 | 美股半日市在 `request_trading_days` 里怎么标记；`get_market_state` 在半日市 13:00 ET 后的取值 | M7 | 查询已知半日市日期（如感恩节次日） |

---

## 8. 验收标准（照抄手册第 5 节）

### M1 连通与配置

- [ ] `mooapp doctor` 输出以上五项，账号已脱敏
- [ ] 移走 secrets 文件后运行，给出清晰报错而不是一堆堆栈
- [ ] 关掉 OpenD 后运行，几秒内提示你无法连接
- [ ] learnings.md 里有 unlock 的实测结论
- [ ] 在 Claude Code 里运行 REAL 启动命令，必须被人锁拒绝

M1 实现说明（不改验收原文）：

- 第 2 条：SIMULATE 的连通检查不依赖 secrets（Q14 后模拟账户在 `broker.toml`）。`doctor` 把 secrets 作为单独一项检查（文件存在、权限 600、能解析、必填项齐全），缺失或出错时只这一项给出一行可读的报错，其余项照常输出，退出码非 0。Claude Code 在 sandbox 里读不到该文件，这一项会显示「无权读取」，属预期。
- 第 5 条：按 2.2 节的纵深防御理解，验证方法见第 10 节。
- 新增检查（Q20，不是验收原文）：`doctor` 确认 PostgreSQL 和 OpenD 都没有暴露到本机以外。① `config/db.toml` 的 host 必须是 socket 目录或 `127.0.0.1`，`broker.toml` 的 host 必须是 `127.0.0.1`；② 经 `config/db.toml` 连上 PostgreSQL；③ 对本机每个非回环地址（局域网 IP）尝试连接 5432 和 11111，必须被拒绝或超时，能连上就报错；④ `mooapp_sim` 查询 `real` schema 必须报权限错误；⑤ 如果当前角色有权读取，`SHOW listen_addresses` 必须只含回环地址（`localhost`、`127.0.0.1`、`::1`），没有权限就跳过并注明。③ 只能确认端口没有绑定到网卡上，从外网能否访问还取决于路由器，不在检查范围内。在 Claude Code 的 sandbox 里，涉及 TCP 的③和 OpenD 相关项会显示「无法检查」，这是预期结果（ADR-0015 第 3 条）；完整检查由你用 `!` 运行。

### M2 券商适配层与 FakeBroker

- [x] 契约测试在 FakeBroker 和模拟盘上都通过
- [x] 模拟盘连续快速下 20 笔单，限流器自动排队，没有收到限频错误
- [x] 至少录下一份包含整单成交和撤单的模拟盘推送记录；部分成交由 FakeBroker 注入推送覆盖
      （2026-11-13 改口径：模拟盘整单一次成交，录不到部分成交，见 learnings「M2 › 模拟盘不会部分成交」；原文"至少录下一份包含部分成交和撤单的推送记录"照抄自手册第 5 节）


### M3 OMS 核心

- [ ] 回放录制的推送，最终状态与券商查询结果一致
- [ ] FakeBroker 注入重复、乱序、丢失推送，状态仍然正确
- [ ] 模拟盘下单后立刻杀掉程序，重启后订单状态与 App 里显示一致
- [ ] 同一个 client\_order\_id 重试 3 次，券商端只有 1 张单

### M4 风控闸门与 Kill switch

- [ ] 每条规则都有「刚好不超限通过、超限被拒」两个测试
- [ ] 防绕过测试存在且通过
- [ ] 模拟盘挂 3 张单后执行 halt：全部撤销，新单被拒

### M5 邮件通知与心跳

- [ ] SMTP 密码填错时交易照常，改正后补发
- [ ] 一笔单分 5 次成交，只收到 1 封合并邮件
- [ ] 连续 3 个交易日收到心跳邮件

### M6 规则引擎

- [ ] 5 条价格路径测试全部通过，结果与你手算一致
- [ ] 非法表达式（如 `__import__`）在加载时被拒
- [ ] 模拟盘买 1 股，设很窄的止损止盈，观察一边触发后另一边作废

### M7 收盘 P&L 与对账

- [ ] 构造数据的测试与手算结果一致到分
- [ ] 往本地库手动插一条假成交，当天报告出现 \[BREAK\]
- [ ] 模拟盘连续 5 个交易日对账零差异

### M8 Trading Agent 层

- [ ] 在 Claude Code 里接入这个 MCP server，用自然语言走通：查持仓 → 提议一张模拟盘订单 → 你批准 → 收到成交通知
- [ ] REAL 环境下，Agent 提议的订单在你批准之前不会发出
- [ ] 提示注入测试通过
- [ ] Agent 把示例策略的文字翻译成 YAML，与你手写的版本一致

### M9 调度与无人值守（仅模拟盘）

- [ ] 重启电脑后，程序无人操作即恢复运行
- [ ] 关掉 OpenD 5 分钟，收到告警；恢复后自动重连
- [ ] 模拟盘连续无人值守运行 1 周

---

## 9. 问题清单（ADR 未覆盖，需要你决策）

每条给出建议；你答复后写回本文，涉及业务决策的另建 ADR。Q1–Q17 已于 2026-11-11 答复并写回：业务决策见 ADR-0015–0023（2.1 节），其余写在正文对应位置。

| #   | 问题 | 建议 | 答复                                                                                                                                                                                                                                                                                 |
|-----| --- | --- |------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Q1  | **解锁作用于整个 OpenD。** 你在终端解锁 REAL 后，本机任何连上 OpenD 的进程（包括 Claude Code 用 Bash 跑的 Python）都能下实盘单，人锁只拦得住「启动」这一步。怎么补？ | ① REAL 运行期间不开 Claude Code；② 程序退出、halt、异常时调用 `unlock_trade(is_unlock=False)` 重新上锁；③ 用探针 P6 看 sandbox 能否拦本机端口，能拦就在 sandbox 配置里禁止。三条都做 | 同意                                                                                                                                                                                                                                                                                 |
| Q2  | **模拟盘没有成交推送。** 模拟盘的成交从哪里来？ | 模拟盘：订单推送里 `dealt_qty` 的增量 + 定时 `deal_list_query` 轮询；实盘：成交推送为主、轮询兜底。两条都进 `OMS.handle`，按 `deal_id` 去重；若模拟盘查不到 `deal_id`（P4），用 `order_id + 累计成交量` 生成合成 id | 同意                                                                                                                                                                                                                                                                                 |
| Q3  | **券商 STOP 对照实验（ADR-0006 后半句）。** 模拟盘不支持 STOP，只能在实盘做。本版做不做？ | 本版不做，记入下一版；ADR-0006 加一行注记 | 同意                                                                                                                                                                                                                                                                                 |
| Q4  | **改单。** CLAUDE.md 提到改单也要经闸门，但 M2 的 BrokerPort 清单里没有 `modify_order`。v1 要不要支持改单？ | 不支持；需要改价就「撤单 + 新单」，两步都经闸门。状态机里的 REPLACE 相关转换保留但不触发 | 同意                                                                                                                                                                                                                                                                                 |
| Q5  | **撤单要不要过限额。** halt 状态下能否撤单？ | 撤单经闸门但只校验「订单属于本程序且处于活动状态」，不受限额和 halt 限制，照样写审计 | 同意                                                                                                                                                                                                                                                                                 |
| Q6  | **当日亏损口径。** `max_daily_loss` 怎么算？ | 当日已实现盈亏 + 持仓按最新价的浮动盈亏变化 − 当日费用；交易日按 America/New_York 划分；触及后按 `halt_on_daily_loss` 自动 halt | 同意                                                                                                                                                                                                                                                                                 |
| Q7  | **当日下单次数口径。** 被闸门拒绝的、被券商拒绝的、撤单，算不算？ | 只算「已发往券商的新单」（含被券商拒绝的），不算闸门拒绝和撤单 | 同意                                                                                                                                                                                                                                                                                 |
| Q8  | **程序外的持仓和订单**（App 里手动下的、启动前已有的）。 | OMS 只管理 remark 带 client_order_id 的订单；外部订单只告警、不撤不改；单票持仓上限按券商的总持仓算；规则引擎只管本程序买入的数量 | 同意                                                                                                                                                                                                                                                                                 |
| Q9  | **SIMULATE 下 Agent 提议是否自动批准**（手册说可配置）。默认值？ | 默认关闭（需要 `mooapp approve`），在 `settings.toml` 加 `[agent] auto_approve_simulate = false`。这是新配置项，不是风控限额 | 同意                                                                                                                                                                                                                                                                                 |
| Q10 | **确认码格式**（人锁和 REAL approve 共用）。 | 6 位随机字母数字，排除易混字符（0/O、1/I/l），用 `secrets` 模块生成；输错一次即放弃本次操作 | 同意                                                                                                                                                                                                                                                                                 |
| Q11 | **同一标的多次入场。** 持仓未清时再买同一标的，均价合并，和 ADR-0005「首次成交当日开盘价」冲突。 | v1 每个标的同时最多一条活动规则；有活动规则时闸门拒绝该标的的新买单（来源为 rule 和 agent 时）。命令行手动买入不受此限，但规则不覆盖新增部分 | 同意                                                                                                                                                                                                                                                                                 |
| Q12 | **保护性卖单与限额。** 止损止盈触发的市价卖单，如果当日下单次数已满或已 halt，还发不发？ | 当日次数和单笔金额不拦保护性卖单（只减仓，数量 ≤ 可卖持仓）；halt 时规则引擎一并暂停，持仓保留并发告警，由你决定是否手动平仓 | 同意                                                                                                                                                                                                                                                                                 |
| Q13 | **新增依赖。** 需要你批准。 | 运行：`pydantic`（M1 配置校验，手册指定）、`typer`（命令行）、`PyYAML` + `jsonschema`（M6）、`mcp`（M8）；开发：`pytest`。`zoneinfo` 用标准库（SQLite 已不用，见 Q15、Q19） | 同意                                                                                                                                                                                                                                                                                 |
| Q14 | **交易账户 `acc_id` 放哪里。** `config/broker.toml` 目前只有 host、port、市场；它未被 `.gitignore` 忽略。 | 模拟账户 `acc_id` 可写入 `config/broker.toml`；实盘 `acc_id` 放 `~/.config/mooapp/secrets.toml`，避免账号进仓库；`doctor` 只显示后 4 位 | 同意                                                                                                                                                                                                                                                                                 |
| Q15 | **运行产物位置**（SQLite、日志、录制、报告）。 | 放仓库下的 `var/`（db、logs）和已忽略的 `recordings/`、`reports/`；`.gitignore` 加 `var/`。SIMULATE 与 REAL 用两个不同的数据库文件 | 不做 SQLite，直接用 PostgreSQL；SIMULATE 与 REAL 在同一个库的两个 schema；库 Docker 化，数据文件不进 Git，也可不在本仓库，仓库只管 DDL（ADR-0024，第 4 节）                                                                                                                                                                    |
| Q16 | **halt 状态是否跨重启保留。** | 保留，存在数据库；重启后仍为 halt，只有 `mooapp resume` 能解除 | 同意                                                                                                                                                                                                                                                                                 |
| Q17 | **入场单未成交的处理。** DAY 单收盘过期后，规则要不要次日自动重挂？ | 不重挂；规则随入场单过期而失效，需要时重新 `rule add`。部分成交后过期的，按已成交数量继续执行止损止盈（ADR-0008） | 同意                                                                                                                                                                                                                                                                                 |
| Q18 | **进程模型。** `mooapp run`、`mooapp order …`、`mooapp approve`、`mooapp agent serve` 是本项目的独立进程。代码里只有一条写路径，但每个进程各自构造一套 `RiskGate` + `OMS` 时，运行时就有多个闸门：各自的「当日次数」「halt」判断互不知情，各自连 OpenD、各自收推送写库。 | 只有一个常驻进程（`mooapp run`，M3 起提供最小版本，M9 加调度和自启）构造 `RiskGate`、`OMS`、`Executor` 和券商连接，并持有单实例锁。其他进程不构造它们：写操作（下单、撤单、halt、resume、approve）写入 `Store` 的 `commands` 表，常驻进程依次取出、交给闸门，把 `Decision` 写回，命令行同步等待结果（超时报错）；常驻进程没在跑时，写命令直接报错。只读命令直接读 `Store`。用库表而不是 socket：不受 sandbox 对本机端口的限制，可审计，换 PostgreSQL 时不变 | 同意（写入 3.4 节）                                                                                                                                                                                                                                                                       |
| Q19 | **PostgreSQL 驱动（新增依赖）。** | `psycopg[binary]>=3.2`（psycopg 3，同步 API，支持 `LISTEN/NOTIFY`，`[binary]` 不需要本机编译 libpq）。迁移用自己写的几十行执行器，不引入 Alembic、SQLAlchemy。测试库不引入 testcontainers | 同意，视PostgreSQL为独立于本项目的Infrustructure                                                                                                                                                                                                                                               |
| Q20 | **数据库凭据与 sandbox。** REAL 角色的密码必须进 secrets，但 Claude 在 sandbox 里读不到 secrets；SIMULATE 也用密码的话，Claude 就跑不了 `pytest -m pg`、M3 的模拟盘验收。 | 两个角色：`mooapp_sim` 只对 `simulate` schema 有权限，`mooapp_real` 只对 `real` schema 有权限。`pg_hba.conf` 对 `mooapp_sim` 从 `127.0.0.1` 用 `trust`（无密码，凭据不进 Git），`mooapp_real` 用 `scram-sha-256`，密码放 secrets 的 `[postgres] real_password`。非 REAL 角色连不上 `real` schema，这是数据库层面的第二道隔离。连接参数（host、port、库名、角色名）放 `config/db.toml`，可提交 | 给 `mooapp_sim` 设一个专用的随机密码（不在别处使用），写进一个 **gitignore 掉、但 sandbox 能读** 的本地文件，比如 `config/db.local.toml`。 这个密码算「低等级凭据」，处理方式和 Q14 里的模拟账户 acc_id 类似，但不提交到 Git。`mooapp_real` 仍然用 `scram-sha-256`，密码放在 `~/.config/mooapp/secrets.toml`. 把端口绑定写死为 `127.0.0.1:5432`，并在 M1 里写一个检查，确认端口没有暴露到外部。 ||
| Q21 | **Docker 编排文件放哪里；本机还没装 Docker。** 本机目前没有 `docker` 命令（Homebrew 里有 `postgresql@14` 客户端）。 | `deploy/postgres/compose.yaml` 和 `pg_hba.conf` 放本仓库（它们是配置，不是数据）；数据用 Docker 命名卷，不在仓库目录里；镜像固定版本 `postgres:17`；端口只绑 `127.0.0.1:5432`；`restart: unless-stopped`，满足 M9 开机恢复（还需 Docker 本身开机自启）。安装 Docker Desktop（或 OrbStack / colima）由你来做 | 同意；之后改为 Homebrew PostgreSQL，放弃 Docker（第 4 节、ADR-0024） |
| Q22 | **数据库不可用时能否撤单。** ADR-0016 说撤单不受 halt 限制，但每次撤单都要写审计；库挂了就写不了。 | 失败即关闭：库不可用时一切指令都拒绝并告警，由你在 App 里处理。另一种做法是撤单照发、审计先写本地日志文件、恢复后补录，复杂度更高 | 可否在写数据库之前也写一个Local File，作为数据库不可用时的备份。数据库执行正常后将该Local File删除或移至Archive目录                                                                                                                                                                                                            |

---

## 10. 测试策略

| 目录 | 内容 | 谁写 | 默认是否运行 |
| --- | --- | --- | --- |
| `tests/unit/` | 纯逻辑：状态机、闸门规则（每条「刚好不超限 / 超限」两例）、`round_tick`、表达式白名单、P&L 计算、人锁、防绕过与探针规则的静态扫描；需要存储的用 `MemoryStore` | Claude | 是 |
| `tests/store/` | `Store` 契约与 journal 回放（含「库不可用时撤单，恢复后补写」）：同一组用例跑 `MemoryStore`（默认）和 `PostgresStore`（`-m pg`，连本机 Homebrew PostgreSQL 的测试库 `mooapp_test`，每次建临时 schema、跑全部迁移、结束删除） | Claude | `MemoryStore` 部分是 |
| `tests/contract/` | `BrokerPort` 契约：同一组用例分别跑 `FakeBroker`（默认）和模拟盘（`-m sim`）；探针结论按 R9 落在这里 | Claude | FakeBroker 部分是 |
| `tests/acceptance/` | 第 8 节验收标准对应的测试 | **你**；Claude 不得修改（CLAUDE.md） | 是（`sim` 标记的除外） |

- `sim`、`pg` 两个 marker 在 `pyproject.toml` 的 `[tool.pytest.ini_options]` 注册，默认 `-m "not sim and not pg"`。`pytest -m pg` 走 unix socket，可以由 Claude 运行；`pytest -m sim` 要连 OpenD，sandbox 不放行，由你用 `!` 运行（ADR-0015）。
- `tests/acceptance/` 里需要数据库的验收（如 M3「杀掉程序后重启」、M7「往本地库插一条假成交」）连 `simulate` schema。
- 测试不读 `~/.config/mooapp/`；需要 secrets 的地方用 `tmp_path` 里的假文件。
- 测试里不出现 `TrdEnv.REAL` 的真实连接：凡是 REAL 分支，都注入会在被调用时让测试失败的 `BrokerPort` 工厂。
- **人锁（M1 第 5 条）**：单元测试直接调用 `require_human_for_real` 和命令行入口函数（进程内调用，不经 Bash），传入非 TTY 的 `stdin` / `stdout`，断言抛出 `HumanLockError`、退出码非 0、`getpass` 未被调用、`BrokerPort` 工厂未被调用（即没有连 OpenD）。这模拟的是「钩子失效后」的情形。另由你在自己的终端里手动跑一次 `! mooapp --env real doctor`，确认它被拒绝（`!` 那条路是否有 TTY 需实测，结果记入 learnings）。
- 完成后运行 `pytest`，原样汇报结果（CLAUDE.md）。
