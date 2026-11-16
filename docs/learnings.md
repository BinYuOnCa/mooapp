# 学习记录

每个里程碑结束后追加：实测确认的 Moomoo API 行为（注明文档章节或探针脚本）和踩过的坑。

## 阶段 1：由 Claude Code 搭骨架与护栏

### 安装 Skills 的风险评估
交易类 skill 全程不用交易密码，登录一次后只凭 OAuth token 就能实盘下单，会绕过人锁。下一步建议在模拟盘写探针，实测 token 能否限定为只读或只用于模拟盘

对 Agent 入口：不装 skill 更安全

如果装了 trading、crypto、sim-trading 或 grid 这几个 skill，Claude 就会学到第二条下单路：直接用 Bash 执行 moomoo order place。这条路：

- 绕过 MCP 的提议和审批队列，也绕过 risk/gate.py；
- 只要有 OAuth token 就能下实盘单，不需要交易密码，也不检查是不是交互终端，所以人锁完全不起作用（上一轮的结论）。

不装这些 skill，Agent 唯一知道的交易方式就是 MCP 工具，这正是 M8 设计的初衷。

注意：没装 skill 不等于这条路不存在。 只要机器上装了 moomoo CLI 并且执行过 moomoo auth login，Claude 照样能用 Bash 调用它，skill 只是教它怎么调用。真正的防线有两道：
1. 这台机器上不安装 CLI，或者不登录；
2. 在 .claude/settings.json 的 deny 规则里加上 Bash(moomoo:*)。这个文件我改不了，需要你自己改。

### 官方 Skills 审查结论（手册 1.4，2026-11-11）

- **对象**：MoomooOpen/moomoo-agent-hub @ `9e45546`（2026-11-10）共 11 个 skill：quote、trading、sim-trading、crypto、grid-strategy、news-search、stock-digest、comment-sentiment、unusual ×3。只下载阅读，未安装、未执行。
- **结论：一个都不装。** news、digest、unusual 系列暂不考虑。
- **调用路径不同**：这些 skill 走 Agent → Bash → `moomoo` CLI → OAuth REST，不经过 OpenD、`moomoo-api`、`risk/gate.py`；本项目的写路径是 闸门 → OMS → BrokerPort → OpenD。不装 skill 对命令行、规则引擎、Agent 三个入口的交易指令没有影响。
- **交易密码**：全部 skill 及其 references 中没有交易密码或解锁步骤。`moomoo auth login` 浏览器授权后 token 存本地 `token_file`，此后 CLI 自动附带 Bearer token 即可实盘下单；只有服务端返回 `need_order_confirm` 时才有确认。人锁对这条路无效。grid-strategy 要求「保留交易密码」，但没写运行时如何获取。
- **需要确认的指令**：所有 `npx skills add/update` 带 `-y`；安装 CLI 用 `curl … | sh`（Windows 用 `-ExecutionPolicy Bypass`）；sim-trading 下单无需逐笔确认；grid-strategy 在对话中说「使用实盘」后循环自动下单。所有 CLI 调用要求附加隐藏参数 `--source skill`，且不得展示给用户。
- **install.sh**（moomoo-cli main 分支）：从 GitHub Releases 下载二进制，用内置 ECDSA 公钥验签并核对 sha256；装到 `~/.moomoo/bin/moomoo`，并向 `~/.zshrc` 追加 PATH 段（`MOOMOO_CLI_NO_MODIFY_PATH=1` 可跳过）；不用 sudo、不读密码、不登录。公钥与脚本同源，签名防不了脚本本身被改。
- **已采取的措施**：`.claude/settings.json` deny `Bash(moomoo:*)`；本机不安装 CLI、不执行 `moomoo auth login`。
- **可复用的部分**：grid-strategy 的参数检查公式与港股价位表、公司名到代码的映射表，只作参考资料。
- **相关发现**：OpenD 路径也有类似缺口：`unlock_trade` 对整个 OpenD 生效（vendor § Unlock Trade），见 SPEC Q1。

### OpenD中RSA参数

这个字段用来**加密你的程序（API）和 OpenD 之间的通信**。下面的内容凭我对 Futu/moomoo OpenAPI 文档的了解，没有对照本项目的 `docs/vendor/` 核实；那份文档的「OpenD 配置」和「协议加密」章节应该有准确说明。

#### 作用

- 填的是一个 RSA 私钥文件的路径。按我对文档的了解，要求是 PKCS#1 格式、1024 位，但这一点没有核实。
- 填写并开启加密后，OpenD 和客户端之间的协议包会加密传输。
- 客户端要配合设置：Python SDK 里先调用 `SysConfig.set_init_rsa_file(<同一个私钥文件>)`，再在创建 `OpenQuoteContext` 或 `OpenSecTradeContext` 时传 `is_encrypt=True`。
- 只有程序和 OpenD **不在同一台机器**，也就是 OpenD 监听非本机地址时，才真正需要它。按我的记忆，文档要求非本地连接时必须加密，本机连接可以不加密，这一点也没有核实。

#### 对本项目

- **可以留空。** ADR-0014 定的是「OpenD 与程序在同一台电脑上，只用本机端口」，监听地址保持 `127.0.0.1` 就用不到加密。
- **如果以后要填：** 私钥是密钥，按 CLAUDE.md 的规定，只能放在 `~/.config/mooapp/`，权限设为 600。不能放进仓库、日志或测试，也不要让 Claude 读取。
- **别和 OpenD 连接 token 混淆。** `secrets.example.toml` 里的 `[opend] token` 是另一回事：用 moomoo 桌面客户端连接时才需要，和 RSA 加密无关。

需要确认的话，可以在主对话里让 Claude 查 `docs/vendor/` 里的 OpenD 配置和协议加密章节，并注明出处。

## From Claude App

在 docs/learnings.md 追加「阶段 0 实测」一节，内容如下，逐条整理成简洁条目，不改任何代码：
1. moomoo 桌面客户端的「应用连接」只实现了部分 OpenD 协议：不支持 GetGlobalState（1002）；
   账户列表只有实盘、没有模拟账户；security_firm 返回 FUTUMY（错误）。独立版 OpenD 返回 FUTUCA，
   且列出 US SIMULATE 账户。结论：本项目只用独立版 OpenD。
2. 桌面版应用连接需要连接 token（SysConfig.set_token），否则握手报 Unauthorized。
3. SDK 同步建连在鉴权失败时每 6 秒重试、永不返回；需要限时的地方用 is_async_connect=True 加超时。
4. SDK 10.4 起 security_firm 默认自动识别券商，不用手填。
5. 模拟账户：总资产 1,000,000，现金 1,000,000，购买力 2,000,000，说明是保证金账户；
   ADR 0002 定的是现金账户，M4 风控要按现金限额，不能按购买力。资金查询返回的 currency 为 N/A。
6. SMTP：smtplib 的 login() 在第一种认证方式被拒后会换方式重试，服务器断开后真实原因被
   「Connection unexpectedly closed」盖住；smtplib 默认不校验服务器证书，必须传
   ssl.create_default_context()；Gmail 对本机约 10 秒才发问候；实测可用的是 465（SSL）加应用专用密码。
7. macOS 自带 bash 3.2：变量后紧跟全角字符会被当成变量名的一部分，变量一律写成 ${var}。

## 关于探针

### 英文名

**probe script**，也就是本项目里用的叫法。意思相近的说法还有：

- **spike**：极限编程（XP）里的叫法，指为了回答一个技术问题而写的一次性试验代码。
- **learning test**：专门用来验证第三方 API 实际行为的测试。
- **exploratory test**：泛指探索性测试。

### 作用

**用最小的代码，在真实环境里实测一个文档没说清楚的问题，拿到事实以后再写正式代码。**

CLAUDE.md 的规定是：「文档不明确时写探针脚本在模拟盘实测，不要猜。」它要解决的问题是，AI 和人都容易凭印象脑补 API 行为，比如我前面讲 RSA 参数时就有几处只能注明「没有核实」。探针就是把这种猜测换成实测结果。

本项目已经排好的探针：
- **M1 `scripts/probe_m1.py`**：在模拟盘挂一张远离市价的限价单再撤掉，确认模拟盘需不需要 `unlock_trade`。
- **SPEC 第 7 节 P1–P6**：订单时间字段的时区、模拟盘部分成交的表现、`deal_id` 能不能查到、模拟账户类型、Claude Code 的 sandbox 能不能拦住对本机 OpenD 端口的连接等。

### 常见做法

1. **一个探针只回答一个问题。** 在文件开头写清楚问题和预期，比如「问题：模拟盘下单是否需要 unlock？预期：不需要（vendor § Unlock Trade）」。
2. **代码越短越好，直接调用 SDK。** 不依赖项目里的模块，避免把正式代码的 bug 混进实验结果。
3. **把原始输出打印出来，不要只打印结论。** 比如把返回的 DataFrame、错误信息、时间戳原样输出，方便事后复查。
4. **把结论写进 `docs/learnings.md`**，注明脚本名、日期、SDK 和 OpenD 版本。这是手册要求的学习留痕。
5. **用完保留在 `scripts/` 里。** SDK 升级后可以重跑一遍，确认行为有没有变。
6. **在合适的时候固化成测试。** 实测确认过的行为可以变成 `pytest -m sim` 契约测试的一部分，或者写进 FakeBroker 的场景。

### 注意事项（结合本项目）

- **只在 SIMULATE 下跑。** 探针里**显式传 `trd_env=TrdEnv.SIMULATE`**，因为 `place_order` 和 `modify_order` 的默认值是 `REAL`。禁止带 `--env real`。
- **让下单在设计上就不会成交。** 限价远离市价，数量 1 股，跑完立即撤单，并确认撤单成功。脚本中途出错时也要撤单，可以用 `try/finally`，但不能用 `except Exception: pass` 这种静默吞错。
- **探针本身也要经过审批。** 它会真的发单，只是不走风控闸门。CLAUDE.md 禁止新增绕过闸门的路径，所以探针只能当一次性实验工具，不能被正式代码 import，也不能变成常驻程序的一部分。
- **不碰密钥。** 不读取或打印 `~/.config/mooapp/` 下的文件，输出里的账号只显示后 4 位。
- **注意限频。** 下单每 30 秒最多 15 次，撤单每 30 秒最多 20 次，循环测试时要加间隔。
- **结论要标注适用范围。** 模拟盘的结果不一定适用于实盘，比如模拟盘没有成交推送、不支持 STOP 单。learnings 里要写明「模拟盘实测」，实盘阶段再补一条对照记录。
- **需要先给计划。** 按工作方式，写探针之前先说明要测什么、会发哪些单，你确认后再写再跑。

## 阶段 1 会话 4：SPEC 审阅中发现的问题（2026-11-11）

### 架构

- **代码里只有一条写路径，运行时不一定只有一个闸门。** `mooapp run`、`mooapp order`、`mooapp approve`、`mooapp agent serve` 是本项目的独立进程，不是 SDK 的要求。如果每个进程各自构造 `RiskGate` + `OMS`，它们的「当日次数」「halt」互不知情，还会各连一次 OpenD、各自写库。结论：只有常驻进程持有闸门和券商连接，其他进程经 `commands` 表提交（SPEC 3.4、Q18）。
- **SDK 推送回调在 SDK 自己的线程里执行。** 所有事件进一个队列，由单个消费线程调用 `OMS.handle`（SPEC 3.4）。
- **「保护性」标记本身就是绕过口。** 保护性卖单不受当日次数和单笔金额限制（ADR-0021），所以闸门只承认来源为规则引擎的卖单带这个标记；否则 Agent 或命令行可以借它绕过限额。
- **「每个标的一条活动规则」会反转依赖方向。** 闸门要知道有没有活动规则，就变成 risk 依赖 strategy。改为规则状态存进 `Store`，risk 只做只读查询（SPEC 3.3）。
- **存储过程也可能成为第二条写路径。** 约定存储过程不得写 OMS 拥有的表；分析类函数只读（SPEC 第 4 节）。
- **数据库不可用时，撤单与审计冲突。** 撤单不受 halt 限制（ADR-0016），但审计写不进库。解决：写库前先写本地 journal；库不可用时撤单、halt 照常，新单拒绝（ADR-0025）。

### 安全与 sandbox

- **`unlock_trade` 对整个 OpenD 生效**（vendor § Unlock Trade：「只要一个连接解锁，其他连接都能调用交易接口」）。人锁只守住启动这一步，对策见 ADR-0015。
- **sandbox 实测**（用 shell 命令，不是探针脚本）：
  - 默认拦截本机 TCP：`127.0.0.1:5432` 无响应，`127.0.0.1:11111` 报 `PermissionError: Operation not permitted`；unix socket 也拦。
  - `sandbox.network.allowUnixSockets` 可以按路径放行单个 socket。放行本机 TCP 只有 `allowLocalBinding` 一个开关，没找到按端口放行的选项。
  - 单条命令的 `allowed_domains: ["127.0.0.1:5432"]` 对本机 TCP 无效。
  - 最终只放行 `/tmp/.s.PGSQL.5432`，Claude 连不上 OpenD（ADR-0015 注记）。
- **密钥与 sandbox 的冲突。** sandbox 禁止读 `~/.config/mooapp/`，放在那里的值（包括模拟账户 `acc_id`、数据库密码）Claude 都用不了。因此凭据分了级：
  - 模拟账户 `acc_id` 写进 `config/broker.toml`，进 Git；
  - `mooapp_sim` 的密码写进 `config/db.local.toml`，不进 Git，sandbox 可读；
  - 实盘账号与 `mooapp_real` 的密码留在 secrets。
- **模拟账户与实盘账户的关系**（vendor § Get the List of Trading Accounts）：同一个登录下由 `get_acc_list` 一起列出。文档示例中实盘 `acc_id` 18 位、模拟 7 位，模拟账户的 `uni_card_num`、`card_num` 为 `N/A`。两者能否互相推算，文档未写明。
- **PreToolUse 钩子按命令文本匹配。** 用 Bash heredoc 写文档时，内容里只要出现那串 REAL 启动参数就会被拦，即使并没有执行它。本条就是因此改用 Edit 工具写的。
- **`.claude/settings.json` 在 sandbox 的禁写清单里。** Claude 改不了 sandbox 设置，必须由你修改。
- **M1「在 Claude Code 里运行 REAL 启动命令，必须被人锁拒绝」无法直接观察：** 命令会先被钩子拦下。按纵深防御理解，用进程内单元测试假定钩子失效，直接验证人锁（SPEC 第 10 节）。

### PostgreSQL（Homebrew）

- `initdb` 默认生成 `trust` 规则，本机连接不验证密码。必须把 `local`、`host` 行都改成 `scram-sha-256`，再用「不带密码的连接应被拒」来验证。
- `listen_addresses = 'localhost'` 会同时监听 `127.0.0.1` 和 `::1`，两个都是回环地址，不算暴露。
- PATH 上的 `psql`、`pg_isready` 可能来自旧的 `postgresql@14`（14.12），而服务端是 17；工具要用 `/opt/homebrew/opt/postgresql@17/bin/` 下的版本。
- `pg_isready` 报 `no response` 不一定是服务没启动，在 sandbox 里也可能是连接被拦。先用 `lsof -nP -iTCP:5432 -sTCP:LISTEN` 看监听情况。

### vendor 要点（写 SPEC 时核对）

- 美股最小价格变动单位：< $1 为 $0.0001，≥ $1 为 $0.01（vendor FAQ Q14；§ Place Orders › price）。本项目自己取整，不用 `adjust_limit`。
- `order_fee_query` 用来查费用，要求 OpenD ≥ 8.2.4218（vendor § Get Order Fee）。模拟盘是否返回费用，待探针 P7。
- 交易日历的 `trade_date_type` 有 `WHOLE`、`MORNING`、`AFTERNOON` 三种（vendor § TradeDateType）。美股半日市怎么标记，文档未写明，待探针 P8。
- ~~一台电脑可以同时运行两个命令行版 OpenD，各用不同的 `api_port`（vendor FAQ Q17）；同一账户最多 10 个 OpenD 同时登录（FAQ Q8）。~~ **更正**：上面这句把两条 FAQ 拼在一起，推出「同一账户可在同一台电脑上开两个 OpenD」，原文不支持，实验也否定了：
  - vendor FAQ Q8（第 27795–27797 行）：一个账户可以在**多台机器**上登录 OpenD 或其他客户端，最多 10 个 OpenD 同时在线；高级行情权限只有一个终端能拿到。
  - vendor FAQ Q17（第 27861–27896 行）：一台电脑同时跑两个 OpenD 只有命令行版支持，可视化版不支持；示例的两份 `OpenD.xml` 用的是不同的 `login_account`（Account 1、Account 2），不同的 `api_port`（11111、11112）。原文没有直说「必须是不同账户」，但给出的做法只有不同账户。
  - **手动实验（你做的，不是探针）**：同一台 Mac 上，第一个 OpenD 已登录时，用**同一个 moomoo 账户**启动第二个 OpenD，登录失败。第一次实验的时间未记录；复测在 2026-11-12 约 21:00 EST（复测后 21:07 EST 写入），记录如下。第二个 OpenD（命令行版）的输出原文：
    ```
    Login Method: Log in with Account and Password
    Logging in
    Login failed,User is already logged in on this device
    Please enter account
    ```
    组合结果：
        
    | 第一个 OpenD | 第二个 OpenD | 结果 |
    |-----------| ----- | --- |
    | 命令行       | 命令行 | 失败，`User is already logged in on this device` |
    | 可视化（UI）   | 命令行 | 失败，同上 |
    | 命令行       | 可视化（UI） | 失败，提示同样是 `User is already logged in on this device` |
  
        限制在「同一设备」上（on this device），和 Q8 说的「多台机器」对得上；跟第一个 OpenD 是哪种版本无关。
  
  - 结论：同一台电脑上的两个 OpenD 必须登录不同的 moomoo 账户。本项目的 SIMULATE 与 REAL 账户在同一个登录账户下（`get_acc_list` 一次返回 REAL ****9247 和 SIMULATE ****4165），所以「同机双 OpenD 分别服务 SIMULATE 和 REAL」在现有账户下做不到。
  - 「一个 OpenD 解锁后另一个是否仍锁定」仍未验证，而且只在下面的附加条件下才有意义。

### 工作方式

- 问题清单的答复要写回正文。如果只填在表格里，正文仍写「见 Qn」，后续实现会照旧方案做。
- 答复之间会互相牵动：Q14（账号放 secrets）与 sandbox 冲突；Q15（改用 PostgreSQL）连带出凭据（Q20）和故障处理（Q22）。改一条答复后，要回头检查受影响的条目。

# M1 计划执行第 1–2 步

## After ! uv run mooapp doctor

几个值得记进 learnings 的点：

- OpenD 版本显示为 1012：server_ver 返回的是这个数字，不是 9.x.xxxx 那种格式（vendor 示例里是 '504'）。
- 账户与资金：实盘账户有 US 和 CA 两个市场的权限。模拟账户资金 currency=N/A，购买力是现金的 2 倍，和阶段 0 的结论一致。
- 市场状态 OVERNIGHT：现在是美东 23:48，处于夜盘时段。快照更新时间停在收盘的 16:00:00，所以现在判断不了 P2（行情是实时还是延时）。
- SDK 日志打到控制台：开头那 4 行是 SDK 自己的日志，有点吵。要不要关、怎么关，我先查 vendor 文档有没有设置日志级别的接口，有出处再改；这不影响验收。

关于探针 scripts/probe_m1.py，建议常规时段再跑（美东 9:30–16:00）：

- 现在是夜盘，按 vendor § Place Orders，模拟盘美股不支持夜盘。下单很可能被拒，这样就分不清 P1 的失败是「需要解锁」还是「时段不对」。
- P2 也要在开盘时比较 update_time 和本机时间才有意义。

现在跑也行，结果会如实记录，开盘后再跑一次补上。



# M1 连通与配置（2026-11-12）

### 验收结果

| 验收 | 结果 | 依据 |
| --- | --- | --- |
| 1 `mooapp doctor` 输出五项，账号已脱敏 | 通过 | 你用 `!` 运行，11 项全部 PASS；`pytest -m sim` 7 passed |
| 2 移走 secrets 后给出清晰报错 | 通过 | 只有 secrets 一项 FAIL：「配置文件不存在：…」，其余照常，无堆栈 |
| 3 关掉 OpenD 后几秒内提示无法连接 | 通过 | 5 秒后一行提示，1–5 项跳过，6–8 项照常 |
| 4 learnings 里有 unlock 的实测结论 | 通过 | 见下方 P1 |
| 5 在 Claude Code 里运行 REAL 启动命令被人锁拒绝 | 通过 | 第一道：PreToolUse 钩子拦下了含 REAL 环境参数的 Bash 命令（实际发生过一次）；第二道：`tests/unit/test_cli_real.py` 进程内非 TTY 调用被 `HumanLockError` 拒绝；你在自己的交互终端运行得到「人锁：REAL 在 M4 实现，M1 不支持 REAL，已退出」 |

### 探针结论

#### 模拟盘下单不需要解锁交易（P1）

- **结论**：不解锁直接 `place_order(trd_env=SIMULATE, acc_id=…)`，两次都 `ret=0` 并返回 order_id（常规时段一次、夜盘一次）
- **依据**：两者（文档说模拟盘不需要，实测一致）
- **出处**：vendor § Unlock Trade、§ Place Orders；`scripts/probe_m1.py` @ 09f90ac + 未提交改动（随 M1 提交）
- **记录**：`recordings/probes/probe_m1_20261112T153002Z.jsonl`，运行时间 2026-11-12T15:30:02Z；另 `probe_m1_20261112T052002Z.jsonl`（夜盘）；SDK 10.12.7208，OpenD 1012
- **影响**：M2 `MoomooBroker`：SIMULATE 不调用解锁；`unlock()` 只用于 REAL（SPEC 5.3）

#### 模拟盘美股快照是实时行情（P2）

- **结论**：常规时段快照 `update_time`（美东时间，原文 `2026-11-12 10:30:02`）比本机 UTC 晚约 2.2 秒，为实时行情。夜盘时 `last_price` 和 `update_time` 停在常规时段收盘（16:00:00），夜盘价格在 `overnight_price` 等字段
- **依据**：实测
- **出处**：vendor § Get Market Snapshot（「美股为美东时间」）；`scripts/probe_m1.py` @ 同上
- **记录**：`recordings/probes/probe_m1_20261112T153002Z.jsonl`（`p2.compare`，`lag_seconds = 2.218`）；对照 `…T052002Z.jsonl`（`lag_seconds = 30003`，夜盘）
- **影响**：M6 规则引擎与 M4「限价偏离」用 `last_price` 时，只在常规时段有意义（与 ADR-0003 一致）；`[quote] stale_after_seconds` 的健康检查只应在常规时段告警

#### sandbox 拦住了 OpenD 端口（P6）

- **结论**：Claude Code 的 sandbox 内，标准库连 `127.0.0.1:11111` 报 `PermissionError errno 1`；你的终端里同一调用连上。sandbox 内 `import moomoo` 也会失败：SDK 一导入就要写 `~/.com.moomoo.OpenD/Log/py_*.log`
- **依据**：实测
- **出处**：ADR-0015 第 3 条；`scripts/probe_m1.py` @ 同上
- **记录**：`recordings/probes/probe_m1_20261112T044304Z.jsonl`（sandbox 内）对比 `…T153002Z.jsonl`（你的终端）
- **影响**：ADR-0015 第 3 条确认落实。`broker/opend.py` 只在 `connect()` 里 import SDK，单元测试才能在 sandbox 里使用 broker 的类型

### 其他实测发现

- **撤单返回成功 ≠ 已撤销**：`modify_order(CANCEL)` 返回 `ret=0` 后立即 `order_list_query`，状态仍是 `SUBMITTED`；你在 App 里确认两张单（838902、839557）最终都是已撤单。撤单是异步的。M2/M3：OMS 收到撤单受理后进入待撤销状态，等推送或轮询看到终态才算撤销（对应 `order-transitions.md`）；M2 的探针应轮询到终态再结束（vendor § Modify or Cancel Orders 未写明）
- **夜盘时模拟盘照样接受常规时段的 DAY 限价单**：美东 00:20（`market_us` 为 `AFTER_HOURS_END`，`get_market_state` 为 `OVERNIGHT`）下单返回 `SUBMITTING`、`session=RTH`，订单排队等开盘。上面 2026-11-11 那节「下单很可能被拒」的推测与实测不符。影响：ADR-0003「只做常规时段」不能依赖券商拒单，M4 闸门的 `regular_hours_only` 必须自己判断
- **同一时刻两个接口的时段字段不同**：`get_global_state` 的 `market_us` 为 `AFTER_HOURS_END`，`get_market_state(['US.SPY'])` 为 `OVERNIGHT`。时段判断以 `get_market_state` 为准（vendor § Get global market status 本身也建议按品种查）
- **`server_ver` 是 `1012`**，不是 `9.x.xxxx` 格式（vendor 示例为 `'504'`）
- **模拟账户类型**：`acc_type=MARGIN`、`sim_acc_type=STOCK_AND_OPTION`（vendor § Get the List of Trading Accounts：美股模拟账户就是保证金账户）。P5 的「现金还是融资」由此得到部分回答；`can_sell_qty` 部分仍待 M2
- **OpenD 过夜保持登录**：从 00:20 到 10:30，OpenD 没有手动操作，cron 运行时 `trd_logined`、`qot_logined` 都是 true（期间 Mac 是否睡眠未确认）。vendor FAQ Q12：「记住密码」登录可能在断线后无法自动重连，手动输入密码登录则能自动处理
- **SDK 交易连接没有异步选项**：`OpenSecTradeContext` 的构造函数没有 `is_async_connect`，同步构造失败时每 6 秒重试、永不返回（SDK 源码 `common/open_context_base.py`；vendor 未写明）。`connect()` 的做法：先等异步行情连接 READY（证明 OpenD 在线、握手成功），再在守护线程里限时构造交易连接。OpenD 关闭时行情连接停在 `WAIT_RECONNECT`，所以拒绝连接也要等满 5 秒才报错
- **`set_all_thread_daemon(True)`** 触发 SDK 内部 `setDaemon()` 的 DeprecationWarning（Python 3.15），不影响功能
- **SDK 日志打印到控制台**，每次连接、断开各一行；是否关闭待查 vendor 有无设置日志级别的接口
- **cron 运行**：cron 的 PATH 不含 `/opt/homebrew/bin`，要写 uv 的绝对路径；`var/logs/` 要先手动建
- **`Path.exists()` 会把 PermissionError 当成「不存在」**：在 sandbox 里，`~/.config/mooapp/secrets.toml` 被报告为「不存在」而不是「无权读取」。已改为直接读文件、按异常类型区分（`config/loader.py`）；修复后的 sandbox 表现未验证（Claude 不得访问该目录）
- **sandbox 内运行 uv**：要设 `UV_CACHE_DIR=$TMPDIR/…`；安装依赖要按命令放行 `pypi.org`、`files.pythonhosted.org`

## M2 券商适配层：探针结论（2026-11-13）

两次运行均在常规时段（`get_market_state` = `AFTERNOON`），你用 `!` 运行，OpenD 为可视化版、未解锁。SDK 10.12.7208，OpenD `1012`，探针提交 `29aa4e5`（无未提交改动）。

- `recordings/probes/probe_m2_20261113T143130Z.jsonl`（14:31:30Z 起）
- `recordings/probes/probe_m2_partial_fill_20261113T143243Z.jsonl`（14:32:43Z 起）

App 里当天模拟账户的 5 张订单与录制一致（你 2026-11-13 核对）：撤单 1 张（683.61），成交 4 张（买 1 @759.67、卖 1 @759.79、买 100 @759.07、卖 100 @759.18）。

#### 订单时间字段是美东时间（P3）

- **结论**：`create_time`、`updated_time` 是 `America/New_York` 本地时间，格式 `yyyy-MM-dd HH:mm:ss`，精确到秒。按美东解释后与下单前的本机 UTC 相差 −0.70、−0.38、−0.18、+0.08、−0.45 秒（秒级截断）；按 UTC 解释则差 −18000 秒。订单推送和查询结果的格式相同
- **依据**：实测
- **出处**：vendor § Place Orders 返回字段表（只说期货参见 OpenD 配置，美股文档未写明）；`scripts/probe_m2.py`、`scripts/probe_m2_partial_fill.py` @ 29aa4e5
- **记录**：上面两个文件的 `p3.compare.*` 行；运行时间 2026-11-13T14:31:31Z–14:33:18Z；SDK 10.12.7208，OpenD 1012
- **影响**：`broker/convert.py` 按美东时间解析，再转成 UTC（ADR-0011）。冬令时期间测的；夏令时由 `zoneinfo` 处理，未实测

#### 模拟盘查不到成交记录和费用（P4 部分、P7）

- **结论**：`deal_list_query` 返回 `ret=-1`，原文 `Paper trading does not support deal data.`；`order_fee_query` 返回 `ret=-1`，原文 `Paper trading is not supported.`。与文档一致
- **依据**：两者
- **出处**：vendor § Get Today's Deals（只支持实盘）、§ Get Order Fee › Interface Limitations（不支持模拟账户）；`scripts/probe_m2.py` @ 29aa4e5
- **记录**：`probe_m2_20261113T143130Z.jsonl` 的 `deal_list_query`、`order_fee_query` 行
- **影响**：SPEC Q2「模拟盘定时轮询 deal_list_query」不可行。模拟盘成交只能从订单推送 / 查询的 `dealt_qty` 增量得到，id 用 `order_id:累计成交量` 合成（M2 计划第 5 点）。模拟盘的费用只能记 0 或标为缺失（M7 决定）

#### 模拟盘不会部分成交，整单一次成交（P4）

- **结论**：限价买 100 股 @ 卖一价 759.58（当时卖一量只有 40 股），28.4 秒后一条推送直接从 `SUBMITTED` 变成 `FILLED_ALL`，`dealt_qty` 由 0 变为 100，均价 759.07（低于限价）。中间没有出现 `FILLED_PART`。另外 3 张成交单也都是一次全部成交。模拟盘撮合看来不按盘口数量拆单
- **第二次（按你 2026-11-13 的决定）**：换白名单里成交量最低的 US.TD（当日成交量 66448 股），限价买 1000 股 @ 卖一价 124.02（卖一量 104 股），8.8 秒后同样一条推送从 `SUBMITTED` 直接变成 `FILLED_ALL`，均价 123.965；随后市价卖 1000 股 9.3 秒后整单成交，均价也是 123.965
- **依据**：实测，共 6 张成交单（1、1、100、100、1000、1000 股），全部一次成交
- **出处**：vendor § Trading Definitions › Order Status 列出 `FILLED_PART`，模拟盘撮合规则文档未写明；`scripts/probe_m2_partial_fill.py` @ 29aa4e5
- **记录**：`probe_m2_partial_fill_20261113T143243Z.jsonl` 的 `push.order`、`p4.summary` 行
- **记录**（第二次）：`probe_m2_partial_fill_20261113T145538Z.jsonl`；运行时间 2026-11-13T14:55:38Z；SDK 10.12.7208，OpenD 1012
- **影响**：M2 验收第 3 条（录到部分成交）在模拟盘上做不到。你 2026-11-13 决定**改验收口径**：模拟盘只录「整单成交 + 撤单」（已有录制），部分成交由 FakeBroker 注入的推送覆盖；SPEC 8 节的验收原文由你手工修改。M3 的部分成交场景只能由 FakeBroker 构造

#### 模拟盘当日买入可立即卖出（P5）

- **结论**：买 1 股成交后立即查持仓，`qty=1`、`can_sell_qty=1`；随后市价卖出成功。模拟账户 `acc_type=MARGIN`、`sim_acc_type=STOCK_AND_OPTION`，资金 100 万、购买力 200 万。实盘账户 `acc_type=TFSA`
- **依据**：实测
- **出处**：vendor § Get Positions（`can_sell_qty` = 持仓 − 冻结）；`scripts/probe_m2.py` @ 29aa4e5
- **记录**：`probe_m2_20261113T143130Z.jsonl` 的 `position_list_query.*`、`p5.can_sell`、`accinfo_query` 行
- **影响**：闸门的「可卖数量」用 `can_sell_qty`（ADR-0002）。实盘是 TFSA，`can_sell_qty` 的表现可能和模拟盘的保证金账户不同，REAL 阶段再核对

#### 撤单到终态约 0.05 秒，但撤单刚返回时的查询仍是旧状态

- **结论**：`modify_order(CANCEL)` 返回 `ret=0` 后 5 毫秒查询仍是 `SUBMITTED`（与 M1 一致）；`CANCELLED_ALL` 推送在返回后 46 毫秒到达，0.53 秒时查询也已是终态
- **依据**：实测
- **出处**：vendor § Modify or Cancel Orders 未写明；`scripts/probe_m2.py` @ 29aa4e5
- **记录**：`probe_m2_20261113T143130Z.jsonl` 订单 840514
- **影响**：OMS 的待撤销状态以推送或查询看到终态为准（M3）；契约测试轮询到终态

### 其他实测发现

- **模拟盘可成交单也要等几秒到几十秒才成交**：限价买 1 股（限价高于卖一价）6.7 秒，市价卖 1 股 15.0 秒，限价买 100 股 28.4 秒，市价卖 100 股 5.7 秒。成交推送都在整秒后约 0.1 秒到达。契约测试等待成交的超时至少 60 秒
- **成交价可能优于限价**：限价 761.07 成交 759.67，限价 759.58 成交 759.07
- **市价单回报里 `price` 为 0.0**；`dealt_avg_price` 可有 3 位小数（759.179）
- **模拟账户的 `average_cost` 有值**（759.67），与 vendor § Get Positions「对证券模拟账户无效」不符。`Position.avg_cost` 仍定为 `Decimal | None`，有值就解析
- **持仓清零后行仍在**：卖出后 `position_list_query` 仍返回 SPY 一行，`qty=0`。转换时要过滤 `qty == 0`
- **推送顺序**：每张单依次推送 `SUBMITTING` → `SUBMITTED` → 终态。`SUBMITTING` 推送比 `place_order` 返回早约 3 毫秒，印证 vendor § Place Orders 的提示（推送可能先于返回）
- **快照有 `ask_vol`、`bid_vol`**：快照的 `update_time` 也是美东时间
- **同一笔买卖成交价相同**：US.TD 买 1000 股（限价 124.02）与随后市价卖 1000 股都成交在 123.965，介于下单时的买一价 123.93 和卖一价 124.02 之间。模拟盘的成交价看来取撮合时的某个参考价，不是盘口价
- **`SysConfig.enable_console_log(False)` 有效**：第二次部分成交探针的终端输出里只有探针自己打印的 JSON 行，没有 SDK 的连接/断开日志（你 2026-11-13 贴的终端输出）。`broker/opend.py` 按计划加上这一行

## M2 券商适配层：实现与模拟盘契约测试（2026-11-13）

#### 推送回调里抛出的异常会被 SDK 接住，只写进 SDK 自己的日志

- **结论**：SDK 在 `packet_callback` 里用 `try … except Exception` 包住 handler 的 `on_recv_rsp`，异常只以 `logger.warning(..., exc_info=True)` 写进 SDK 日志（`~/.com.moomoo.OpenD/Log/`），不会传到我们的代码，关掉控制台日志后终端上也看不到
- **依据**：读 SDK 源码（未实测）
- **出处**：SDK 10.12.7208 `moomoo/common/open_context_base.py` `packet_callback`；vendor § Orders Push Callback 未写明
- **影响**：`MoomooBroker` 的推送处理先用 Python `logging` 记 ERROR（默认打到 stderr）再抛出，不让错误只留在 SDK 日志里。M3 的 OMS 回调出错时同样会走这条路径；常驻进程（M9）要配置 logging 落盘

#### 模拟盘契约测试：限流、撤单、整单成交、合成成交全部按预期

- **结论**：`tests/contract/test_broker_contract.py`（sim）7 项通过，用时 50.7 秒；验收测试 `tests/acceptance/test_m2_rate_limit.py` 通过，用时 34.1 秒，20 笔不出现限频错误。录到 23 张单共 69 条推送，每张单都是 `SUBMITTING` → `SUBMITTED` → 终态（21 张 `CANCELLED_ALL`，2 张 `FILLED_ALL`）；23 张单的 remark 都是 `moo:` 格式并在推送里原样带回
- **成交**：限价买 1 股 @ 754.22，5.4 秒后整单成交，均价 752.70；随后市价卖 1 股，8.6 秒后成交，均价 752.68。每张单只生成一条合成成交（`fill_id = order_id:1`）
- **推送行字段**：比 vendor § Orders Push Callback 的列表多 `jp_acc_type`；没有 `acc_id` 字段
- **依据**：实测（你 2026-11-13 在常规时段用 `uv run pytest -m sim` 运行）
- **出处**：`src/mooapp/broker/`（M2 实现，未提交）
- **记录**：`recordings/pushes/SIMULATE_20261113T184058Z.jsonl`（契约测试；验收测试未开录制）；SDK 10.12.7208，OpenD 1012
- **影响**：M2 验收 1、2 在模拟盘通过；验收 3 按改后的口径（整单成交 + 撤单）已有录制，部分成交由 FakeBroker 覆盖。这份录制是 M3 回放的输入

## M3 OMS 核心：设计与实现中的发现（2026-11-13）

探针 P9（DAY 单收盘过期、DELETED）：过期结论见本节末尾「P9 第二次 `--check`」（2026-11-16）：DAY 单过期显示为 CANCELLED_ALL。DISABLED 仍无样本，DELETED 未测，二者继续按「映射不了」报警（docs/status-mapping.md）。

#### Moomoo 订单状态共 11 个，不是 12 个

- **结论**：vendor § Trading Definitions › Order Status 列出 `NONE` 加 10 个（WAITING_SUBMIT … DELETED），共 11 个。SPEC 6 节「11 个（WAITING_SUBMIT … DELETED）」的写法容易数错，M3 计划里我也写成了 12 个，已在 status-mapping.md 和测试里改正
- **依据**：文档（`tests/unit/test_status_mapping.py` 直接从 vendor 文档解析，防止再数错）
- **出处**：vendor § Order Status（第 26023–26069 行）
- **影响**：`oms/mapping.py` 的 `VENDOR_STATUSES`

#### 快照是状态，不是事件：映射要看本地状态

- **结论**：Moomoo 只给订单快照（状态 + 累计成交量），状态机要事件。同一个快照，本地状态不同，产生的事件也不同：
  - 本地 PENDING_NEW 或 UNKNOWN 时直接看到 `CANCELLED_*`（推送丢了、轮询看到终态）：矩阵里没有 PENDING_NEW 加撤销的转换，要先补一个 ACK
  - 本地已 CANCELED 再收到撤销快照：重复快照，不能再发事件，否则被判为非法回报、误报警
  - 内容没变的快照（每 5 秒一次的轮询）直接跳过，不写事件，否则 order_events 会被轮询刷满
- **依据**：设计推演，并由 `tests/unit/test_replay.py` 验证（M2 录制的 69 条推送随机丢 40% 再轮询）
- **影响**：`oms/mapping.py`、`oms/oms.py`

#### broker 层的 FillSynthesizer 重启后会造成超额成交

- **结论**：`FillSynthesizer` 把已见的累计量记在内存里。重启后从 0 算起：重启前已成交 40 股，重启后收到累计 100 股的推送，会合成一条 100 股的成交。OMS 改为按本地 `cum_qty` 自己算增量，broker 层的合成成交不进 OMS（模拟盘）。成交 id `order_id:累计量` 是确定的，跨重启也能按 fill_id 去重
- **依据**：读代码 + `test_oms_ingest.py::test_restart_after_partial_does_not_overfill`
- **影响**：`oms/mapping.py synthesized_fill`、`EventPump.attach`

#### 超时重发与「绝不重发」的冲突（T55）

- **结论**：mini-OMS 的 UNKNOWN 态「只能等回报或对账，绝不重发」，而 M3 要求「确认不存在才重发」。你决定加 T55：用 `refresh_cache=True` 查询，等宽限期后连续 2 次都查不到这个 remark 才用同一 client_order_id 重发，最多发 3 次；用完次数判为 REJECTED 并报警
- **依据**：你 2026-11-13 的决定；`tests/unit/test_executor_idempotency.py`
- **出处**：vendor § Get open Orders（refresh_cache；限频 30 秒 10 次）；docs/order-transitions.md T55
- **影响**：`oms/state.py`、`oms/executor.py`；查不到是否等于不存在仍依赖宽限期（默认 3 秒 × 2），模拟盘验收 4 会实测

#### 管理员策略会盖住项目里的 sandbox socket 放行

- **结论**：项目 `.claude/settings.json` 放行了 `/tmp/.s.PGSQL.5432`，但 sandbox 内连接仍报 `Operation not permitted`（`/tmp` 和 `/private/tmp` 两种写法都不行）。当时管理员策略 `managed-settings.json` 的 `sandbox.network` 只有 `allowedDomains`，没有 `allowUnixSockets`。你在管理员策略里加上 socket 放行后立即生效，`pytest -m pg` 25 passed
- **依据**：实测（2026-11-13）
- **影响**：以后改 sandbox 网络设置，要看管理员策略，不能只看项目配置

#### 同步测试模式下「本地动作排在已到推送之后」

- **结论**：EventPump 的 `call()` 先处理已入队的推送，再执行本地动作，和线程模式下的先进先出一致。因此要测「券商拒绝撤单」，必须让推送真的丢失（attach=False），否则成交推送会先到，撤单前订单已是 FILLED。这符合真实情况：OMS 能看到的，就是已到达的回报
- **影响**：`tests/unit/test_executor_idempotency.py::test_cancel_rejected_by_broker_goes_back_and_raises`（第 3 步修改）

#### vendor 限频按账户计算，多个 broker 实例共用一份额度（第一次 `pytest -m sim`）

- **结论**：限频是整个账户共用的，不管有几个连接、几个进程。我们的 `ThrottledBroker` 每个实例单独计数：每个测试、每个子进程都会新建 broker，计数从 0 开始。同一个 30 秒窗口里先后建几个 broker，合计调用量就会超过账户额度，被券商拒绝
- **实测**：你在美东约 16:00:45 运行 `uv run pytest -m sim`，用时 38.48 秒，结果 5 failed、12 passed、1 skipped。最先运行的 M2 限流验收（连下 20 笔、再全部撤销）通过。之后的 5 个失败全是券商的限频报错：
  - 撤单 4 个：`Modify or Cancel Order request failed due to high frequency. Maximum 20 times per 30 seconds.`（两份 M3 验收的 `finally`、契约测试 `test_place_query_cancel_push`、`test_twenty_rapid_orders_no_rate_limit`）
  - 下单 1 个：`Place Order request failed due to high frequency. Maximum 15 times per 30 seconds.`（`test_refresh_query_finds_order_by_remark`）
- **依据**：实测（你贴的运行输出），加 OMS 数据库里的事件记录（撤单都是收到 SUBMITTED、本地已是 NEW 之后才被拒）
- **出处**：vendor § Place Orders、§ Modify or Cancel Orders 的 Interface Limitations（限额按 acc_id 计）；文档没有写明多个连接是否共用额度，本次实测表明是共用的
- **影响**：
  - 生产环境：SPEC 3.4 规定只有常驻进程连券商、其他命令经 `commands` 表提交，这个设计正好避开了额度被分摊的问题。M4 必须坚持：命令行不得自己连 OpenD 下单或撤单
  - 测试：sim 测试接连新建 broker 时会互相挤占额度。办法有两种（待你决定）：同一进程里按 acc_id 共用一个限流器（进程级注册表）；跨进程的（M3 重启验收的子进程）只能等满一个窗口再开始
  - 两份 M3 验收：异常发生在 `finally` 撤单，会盖掉 `try` 里断言的结果，这次不能算通过。券商数据和验收条件一致（841874 那个 cid 在券商端只有 1 张单；重启后本地 NEW 对应券商 SUBMITTED），但要周一常规时段单独重跑。建议你在 `finally` 里也处理撤单失败（例如记录后再抛），并在两份 M3 验收之前等满 31 秒，或单独运行
- **更正**：之前我推测撤单失败是因为订单还在 PENDING_NEW、被闸门拒绝，这个推测是错的。事件记录显示闸门放行了撤单，是券商因额度用完而拒绝。「PENDING_NEW 时的撤单等确认后自动发」仍有意义（halt 时可能有单还没确认），但不是这次失败的原因

#### P9 第一次 `--check`：收盘后下的 DAY 单 2 小时后仍是 SUBMITTED（P9 部分；「排队到下一交易日」的推测已被第二次查询推翻）

- **结论**：841874、841875 下于美东 16:00:46、16:00:48，比收盘晚几十秒。美东 18:04 查询，两张单都是 `SUBMITTED`，`time_in_force=DAY`、`session=RTH`，最后更新时间仍是下单时刻。缓存查询（refresh_cache=False）和绕过缓存的查询结果相同。`history_order_list_query` 也能查到，状态同样是 `SUBMITTED`。这与 M1 的「夜盘时模拟盘照样接受常规时段的 DAY 单并排队」一致
- **另外发现**：账户上一共挂着 10 张 `SUBMITTED` 单（841874–841883，16:00:46–16:00:50，US.SPY 限价买 1 股 @ 678.78，全带 `moo:` remark），都是这次 `pytest -m sim` 撤单失败留下的。只有 841874、841875 在 OMS 数据库里，另外 8 张来自直接调用 BrokerPort 的契约测试。你决定不撤：它们会排队到周一开盘、挂一整天、收盘时过期，周一收盘后再查，正好当 10 个 DAY 过期样本。价格比市价低 10%，正常不会成交。这 8 张本地没有记录的单，OMS 每次启动恢复都会报 `unknown_client_order_id`，这是预期行为
- **依据**：实测
- **出处**：vendor § Get open Orders、§ Get Historical Orders；`scripts/probe_m3_expiry.py` @ f6104e9（`--check` 模式）
- **记录**：`recordings/probes/probe_m3_expiry_20261113T230431Z.jsonl`；运行时间 2026-11-13T23:04:31Z（美东 18:04）；SDK 10.12.7208，OpenD 1012
- **已补**（见下一条）。原待补：周一（2026-11-16）美东 16:00 之后对 841874–841883 再跑一次 `--check`，看 DAY 单过期后显示什么状态（DISABLED？CANCELLED_ALL？），有没有推送；周二再查一次，看 24 小时后还能不能查到。之后再定 `mapping.py` 里 DISABLED 的映射。DELETED 需要你在 App 里删一张单再查

## M6a 规则引擎纯逻辑（2026-11-14）

#### 表达式白名单：常量按源码原文转 Decimal

- **结论**：`ast` 解析后，`0.95` 这类小数在语法树里已经是二进制浮点数。直接 `Decimal(0.95)` 会得到 0.9499999999999999555910790149937…。编译器改用 `ast.get_source_segment` 取源码原文再转 `Decimal`，`0.95` 就精确等于 `Decimal("0.95")`
- **依据**：实现时发现，`test_expr.py::test_float_literals_are_exact_decimals` 固化
- **影响**：`strategy/expr.py`。规则文件里的限价同理，要求写成字符串（Schema 规定）

#### 「同一时段两边都碰到」只在 K 线回放里出现

- **结论**：逐笔行情一次只有一个价格，止损价又一定低于止盈价，所以不可能同时越过两边。只有用 K 线（时段内的最低价和最高价）回放时才会两边都碰到，而且先后顺序不明。你决定按止盈算（ADR-0027 第 2 条），dry-run 输出里注明了这一点。实时监控（M6b）用逐笔行情，不受影响
- **影响**：`strategy/machine.py on_bar`；dry-run 的价格文件可以混用 tick 和 bar

#### 首次成交时就冻结价格，avg_cost 等于首次成交价

- **结论**：ADR-0005 规定「首次成交后算一次」，所以计算用的 `avg_cost` 就是首次成交价，之后分批成交使均价变化也不重算（路径 5a：均价变成 99.964，止损价和止盈价仍是 95.00 和 105.22）。只有卖出数量随成交增加（ADR-0008）
- **影响**：M6b 接 OMS 时，冻结用的是第一笔成交的价格，不是订单的最终均价

#### 终端里的中文对齐要按显示宽度

- **结论**：Python 的格式化补空格是按字符数算的，中文字符在终端里占 2 列，所以列对不齐。dry-run 输出改用 `unicodedata.east_asian_width` 计算显示宽度
- **影响**：`strategy/dryrun.py format_events`。以后命令行里的中文表格（例如 `order list`）也应这样处理

## M6b 规则引擎接入实时运行（2026-11-14）

P11（报价推送）待开盘实测；在那之前，取开盘价、判断触发都按 vendor § Real-time Quote Callback 的字段说明实现，相关代码集中在 `broker/convert.py quote_from_push_row` 和 `strategy/engine.py _on_quote`，便于按 P11 修改。

#### 规则引擎的状态只放在事件线程上

- **结论**：状态机不加锁，所有修改都在 EventPump 的事件线程上进行。行情推送（SDK 线程）、OrderChanged、HaltChanged 只排队；`add`、`cancel`、`recover` 经 `pump.call` 在事件线程上执行。OMS 发布事件时持有自己的锁，所以订阅方只能排队、不能在回调里阻塞，否则会和事件线程互相等待（M3 恢复时遇到过同类问题）
- **影响**：`strategy/engine.py`；新的事件订阅方都要遵守「只排队」

#### 成交按订单累计量算增量，并记录已计入的数量和金额

- **结论**：同一笔成交可能经推送、轮询、重启后的恢复多次到达。引擎为每张单记下已计入的数量和金额（存进 rules.state.orders），每次用「累计量 − 已计入量」算增量，成交价用「累计金额 − 已计入金额」反推。这样重复到达不会重复计，也不依赖 fill_id 的格式
- **影响**：模拟盘（合成成交 id）和实盘（deal_id）走同一套逻辑

#### 状态快照只在实质变化时写库

- **结论**：每笔行情都会更新「最近看到的价格」。如果每次都写 rules 表，写库太频繁。持久化时忽略这个字段的变化，其余变化（价格冻结、数量、状态、失败次数、开盘价）都写。开盘价必须写：重启后如果入场单才成交，冻结价格要用它
- **影响**：`RuleEngine._persist`

#### 先恢复规则，再做 OMS 启动恢复

- **结论**：OMS 启动恢复会把停机期间的成交以 OrderChanged 发出。规则必须先从数据库加载好，这些事件才有地方可去，否则会被当成「不属于规则的订单」而丢掉。Runtime.start 的顺序因此是：事件线程 → 恢复规则 → OMS 启动恢复
- **影响**：`ops/run.py Runtime.start`；以后新增依赖 OrderChanged 的模块，也要在 OMS 启动恢复之前就绪

## M6b 代码审查与修复（2026-11-14）

`/code-review high` 有 10 条发现，加上附带的 2 条小问题，逐条核实都成立，全部修复（PROGRESS「M6b 审查修复」）。每条先写复现测试（`tests/unit/test_rule_engine_review.py`）再修。

#### 闸门锁与事件线程互相等待：死锁

- **结论**：闸门在自己的锁里调用 Executor，Executor 要等事件线程处理 OMS 操作（SPEC 3.4：检查与发单在同一把锁内）。如果规则引擎在事件线程上调用闸门，就会反过来等这把锁。命令线程正在下单、同时规则触发时，两边互相等，`mooapp run` 卡死，halt 也执行不完
- **依据**：实测。在 scratchpad 里用修复前的引擎、真正的多线程跑同一场景，「5 秒内完成？False」；修复后连跑 5 次都通过
- **修复**：事件线程只负责决定做什么，不调用闸门，也不做任何网络调用。下单、撤单、时段查询、快照、退订都交给专门的动作线程（`mooapp-rule-actions`），结果再排回事件线程。`rule add` 和 `rule cancel` 在命令线程上调用闸门
- **规则**：凡是持锁等事件线程的代码（闸门、Executor），都不能在事件线程上调用；新模块订阅事件时，回调只能排队

#### 「同步测试全部通过」掩盖了线程问题

- **结论**：单元测试里事件通道是同步模式，所有代码在同一个线程上运行，死锁根本不会出现。M6b 的 713 个测试全部通过，但常驻进程里一下单就可能卡死。涉及多线程的地方，至少要有一个用真正线程、带超时的测试（`test_no_deadlock_between_gate_lock_and_event_thread`）
- **影响**：以后改动 EventPump、闸门、Executor、规则引擎之间的调用关系时，要先跑这条测试

#### 卖单「结束」不只有被拒一种

- **结论**：保护性卖单可能被券商拒绝，也可能被撤销（例如 halt 撤掉全部挂单）或过期。原来只处理了被拒，被撤后规则一直停在 TRIGGERED，resume 后持仓没有任何保护。现在三种情况都按卖单失败处理，剩余数量在下一笔行情重试
- **影响**：halt 期间规则暂停；resume 后的下一笔行情，如果仍在触发价外面，就会重发卖单

#### 启动恢复的顺序还要考虑「OMS 就绪」

- **结论**：规则要在 OMS 启动恢复之前加载（M6b learnings）。但这时闸门还没就绪，会以 `not_ready` 拒绝所有新单；规则如果在这时发卖单，会被计入失败次数，多重启几次就进入 ERROR。现在 OMS 恢复完成后发 `OmsReady` 事件，在那之前规则的动作先排队
- **影响**：`oms/oms.py` 的 recover 结尾发 OmsReady；`strategy/engine.py` 收到后执行排队的动作

## M4 审查修复：发现（2026-11-14）

#### 「先查再改」的撤单判断要在 OMS 锁里一步完成

- **结论**：闸门先查订单「还没确认」，再让 Executor 挂待撤标记；两步之间确认推送到达，订单变成 NEW，待撤标记就再也不会被处理（确认已经过去了）。改为 `OMS.prepare_cancel` 持锁一次完成判断和打标记，返回「deferred / send / inactive / missing」
- **依据**：`tests/unit/test_m4_review.py::test_cancel_not_broken_by_ack_between_check_and_mark`（修复前失败）
- **影响**：`oms/oms.py`、`oms/executor.py`

#### 库不可用时 halt 不能依赖本地订单表

- **结论**：ADR-0025 说库不可用时撤单照常，但 halt 先 `open_orders()` 读库，库一挂就抛错，一张单都撤不了。改为读不到时从券商 `query_orders(open_only=True)` 取 remark 带 client_order_id 的本程序挂单，带 broker_order_id 直接撤，并报 `halt_degraded`。单张撤单失败（状态已变、券商拒绝）只记入 `failed`，不中断
- **依据**：`test_halt_cancels_at_broker_when_db_down`、`test_halt_continues_past_order_filled_meanwhile`
- **影响**：`risk/gate.py`

#### 只写进 journal 的 halt，重启后要在回放之后再读一次

- **结论**：闸门在构造时读 halt，而 journal 是在 OMS 启动恢复时回放的，所以闸门读的时候还没回放。现在 OMS 恢复完成后发 `OmsReady`，闸门收到后再读一次（只并入，不覆盖内存里已有的 halt）
- **依据**：`test_halt_written_only_to_journal_survives_restart`
- **影响**：`oms/events.py`、`oms/oms.py`、`risk/gate.py`；M6 分支已有同名事件，合并时以一份为准

#### 心跳和命令处理不能在同一个线程

- **结论**：心跳在命令线程里写，一条慢命令（例如 halt 撤 3 张单、每张都受限频）超过 10 秒，命令行就判定常驻进程不在、拒绝下一条命令。心跳改为独立线程，每秒写一次，写失败最多 60 秒报警一次；心跳直接写数据库、不经 journal（库挂着时写 journal 只会每秒堆一条）
- **依据**：`test_heartbeat_fresh_while_command_is_slow`（慢命令 4 秒，第 3 秒检查）
- **影响**：`ops/run.py`

#### 命令行超时之后，命令仍会被执行

- **结论**：命令行等 30 秒没结果就报超时退出，但命令还在表里，常驻进程恢复后照样下单。用户以为没下，可能再下一次。现在 place / cancel 带 `expires_at`（入队时间 + 等待时间），过期才取出的命令结果为 `expired`、不交给闸门；halt 不过期。常驻进程启动时把上一个进程遗留的 running 命令标为 error（结果未知，需人工查询）
- **依据**：`test_place_command_expires_when_cli_gave_up`、`test_orphaned_running_commands_marked_error_on_start`、`tests/store/test_store_contract.py::test_fail_running_commands_only_touches_running`（内存版和 PostgreSQL 版都过）
- **影响**：`ops/commands.py`、`oms/store.py`、`oms/pg_store.py`

#### resume 的交互确认只在命令行进程里，命令表本身不设防

- **结论**：能连数据库的任何进程都能往 commands 表插一行 resume，绕过交互终端确认。按 ADR-0029 加 HMAC-SHA256 签名，密钥在 secrets 的 `[resume] key`
- **影响**：`ops/resume_auth.py`、`ops/commands.py`、`ops/cli.py`、`config/models.py`

#### 规则命令也要过期：「迟到的撤销」不一定减少风险（ADR-0030）

- **结论**：halt 迟到也该执行，因为它只减少风险。`rule_cancel` 不同：入场单已成交时，取消规则会拿掉止损保护，迟到执行会在你不知情时让持仓失去保护。所以 `rule_add`、`rule_cancel` 和 `place`、`cancel` 一样，命令行超时即过期。规则 id 不能复用、同一标的只能有一条活动规则（ADR-0020），本来就防住了「重复添加」，过期解决的是「迟到」
- **依据**：你 2026-11-14 的决定；`tests/unit/test_commands.py::test_rule_add_expires_when_cli_gave_up`、`test_rule_cancel_expires_when_cli_gave_up`（修复前失败）
- **影响**：`ops/commands.py`

#### P9 第二次 `--check`：收盘后下的 DAY 单在当天盘后时段结束时过期，状态 CANCELLED_ALL（更正第一次的推测）

- **结论**：
  - 841874–841883（周五美东 16:00:46–16:00:50 下单，DAY、session=RTH）全部是 `CANCELLED_ALL`，`updated_time` 都是 2026-11-13 20:01:00（美东），即周五盘后时段 20:00 结束后一分钟。第一次查询（18:04）看到 SUBMITTED，是因为还在盘后时段内；「排队到下一交易日」的推测是错的
  - DAY 单过期和主动撤单显示同一个状态 `CANCELLED_ALL`，`dealt_qty=0`，没有单独的过期状态；DISABLED 在这次没有出现，仍无样本
  - 前一天的订单，`order_list_query` 不论 `refresh_cache` 真假都返回空列表；只有 `history_order_list_query` 能查到。结束近 3 天（约 67 小时）后仍可在历史订单中查到
  - 当天新下、已撤销的单（842592、842718）`history_order_list_query` 也能查到
- **依据**：实测（2026-11-16 美东 10:29）；App 里模拟账户 ****4165 订单列表显示全部「已撤销」，与查询一致
- **出处**：vendor § Get open Orders、§ Get Historical Orders；`scripts/probe_m3_expiry.py` @ a54bbc7（`--check` 模式）
- **记录**：`recordings/probes/probe_m3_expiry_20261116T152905Z.jsonl`；运行时间 2026-11-16T15:29:05Z；SDK 10.12.7208，OpenD 1012
- **影响**：
  - `oms/mapping.py`：`CANCELLED_ALL` 映射为 CANCELED 已覆盖 DAY 过期，不需要新增状态；DISABLED、DELETED 仍按「映射不了」处理
  - **OMS 启动恢复的缺口**：恢复只查 `order_list_query`，本地仍为 NEW 的前一天订单（841874、841875）查不到，只报 `missing_at_broker`，本地状态一直停在 NEW。后果：每次启动都报警；halt 会去撤它们并被券商拒绝；持仓上限把它们的剩余量算作挂单。待修：缺失订单再查 `history_order_list_query`，按实际状态结束（需要先出计划）
  - 周一收盘后、周二的复查不再需要


## M4 探针 P10：模拟盘当日盈亏与可卖数量（2026-11-16）

#### 模拟盘 `today_pl_val` 有效，qty=0 的行仍带当日盈亏（P10a、P10b）

- **结论**：
  - 模拟账户 `position_list_query` 的 `today_pl_val` 是数值（不是 N/A），可以作为当日亏损规则的数据源（M4 确认点 2）。`today_trd_val`、`today_buy_qty`、`today_sell_qty`、`unrealized_pl`、`realized_pl` 在模拟盘都是 N/A，与 vendor 文档一致
  - 卖光后 SPY 那一行仍在（qty=0、can_sell_qty=0），`today_pl_val` 仍有值，所以求和必须包括 qty=0 的行（`today_pnl_from_rows` 已经这样做）
  - 本次一买一卖：买入 745.755、卖出 745.75，`pl_val` 卖出后为 −0.005（只反映这一轮）；`today_pl_val` 从买前的 −0.055 变为卖后的 −0.06（−0.055 + −0.005），是当日累计。买前的 −0.055 来自当天更早的成交：0.6 契约测试 10:42:43 买入约 744.71、10:43:15 市价卖出约 744.65（App 订单列表核对），价差约 −0.06
  - 买单和市价卖单都在约 13 秒后成交；推送顺序 SUBMITTING → SUBMITTED → FILLED_ALL
- **依据**：实测（2026-11-16 美东 10:48–10:49）
- **出处**：vendor § Get Positions；`scripts/probe_m4_pnl.py` @ 3e52fe7
- **记录**：`recordings/probes/probe_m4_pnl_20261116T154846Z.jsonl`；运行时间 2026-11-16T15:48:46Z；SDK 10.12.7208，OpenD 1012
- **影响**：闸门的当日亏损规则在模拟盘可用，不会因为字段无效而拒绝所有买单。`today_pl_val` 是当日累计（含当天更早的成交）；何时清零（美东 0 点？开盘？）本次没有观察到

#### 券商的 `can_sell_qty` 已扣掉挂着的卖单，闸门的可卖数量规则会重复扣（P10d）

- **结论**：持仓 1 股、挂一张 1 股的限价卖单（820.41，远离市价）后，`qty=1`、`can_sell_qty=0`。券商的可卖数量已经扣除了未成交卖单。闸门目前按「`can_sell_qty` − 本程序未完成卖单剩余量 ≥ 本单」检查（M4 确认点 3），等于扣了两次：有 N 股卖单挂着时，再卖会被少算 N 股，可能被错误拒绝
- **依据**：实测（同上，`p10.with_open_sell`）
- **出处**：vendor § Get Positions（`can_sell_qty` 的含义文档未说明是否扣挂单）；`scripts/probe_m4_pnl.py` @ 3e52fe7
- **记录**：同上
- **影响**：`risk/gate.py` 的 `_collect` / `risk/rules.py` 的 can_sell 规则需要改为只用券商的 `can_sell_qty`（或用 qty − 本程序未完成卖单）。这改变了你确认过的 M4 确认点 3，要先出计划、经你确认（可能需要 ADR）。今天的 M4 验收只下买单、M6 验收触发时只有一张卖单，都不受影响

## M6 探针 P11：报价推送（2026-11-16，常规时段）

#### QUOTE 推送约每秒 6 条，不只在成交时推；时间是美东时间（P11a、P11b）

- **结论**：
  - 订阅 US.SPY 的 QUOTE 后，约 61 秒收到 367 条推送：本机接收间隔中位数 0.151 秒，最大 0.401 秒
  - 推送不等于成交：相邻两条中 163 次 `last_price` 不变、190 次 `volume` 不变、160 次 `data_date + data_time` 与上一条相同。规则引擎必须容忍重复和价格不变的推送（按价格比较触发，本身是幂等的）
  - `data_date`、`data_time` 是美东时间（11 月为 UTC−5）：按 UTC−5 换算后，本机接收比 `data_time` 晚 0.037–1.658 秒，中位数 0.084 秒。换算要用 America/New_York 时区（夏令时 UTC−4），不能写死 −5
- **依据**：实测（2026-11-16 美东 11:01:13–11:02:13，市场状态 AFTERNOON）
- **出处**：vendor § Subscribe and Unsubscribe、§ Real-time Quote Callback；`scripts/probe_m6_quote.py` @ 13f1486
- **记录**：`recordings/probes/probe_m6_quote_20261116T160110Z.jsonl`；运行时间 2026-11-16T16:01:10Z；SDK 10.12.7208，OpenD 1012
- **影响**：`strategy/engine.py` 每条推送只排进事件队列，每秒 6 条不构成压力；`poll_fallback_seconds` 的「推送中断」判断在常规时段几乎不会触发

#### 常规时段的 `open_price` 是当天开盘价；盘前、盘后、夜盘价格是单独的字段（P11c、P11d 部分）

- **结论**：
  - 常规时段内，推送和快照的 `open_price` 都是 749.72，不同于 `prev_close_price` 754.37，是当天的开盘价；整段推送里 `open_price` 不变
  - 推送和快照都带 `pre_price`（749.68）、`after_price`（749.6）、`overnight_price`（752.28）以及 `pre_*`、`after_*` 系列字段，延长时段的价格不混在 `last_price` 里。常规时段内 `after_price` 是上一交易日盘后的价格
  - 开盘前 `open_price` 的取值、盘前盘后 `last_price` 是否变化，这次没有测（只在常规时段跑）
- **依据**：实测（同上）
- **出处**：同上
- **记录**：同上
- **影响**：规则引擎表达式里的 `open` 在常规时段可直接用 `open_price`；规则只在常规时段触发（M6b 确认点 1），所以盘前的取值不影响触发，但影响盘前 `rule add` 时的校验。待补：盘后（今天 16:00 后）或明天盘前再跑一次对照

## M4 验收 3：第一次运行被闸门的限价偏离规则拒绝（2026-11-16）

- **结论**：验收草稿沿用 M3 验收的「最新价 × 0.9」挂远离市价的单，偏离 10%，超过 `[risk.simulate]` 的 `max_limit_deviation_pct = 5.0`，第一张单就被闸门拒绝（未下单、未 halt）。M3 的闸门骨架没有这条规则，所以 M3 验收没暴露。你把系数改为 0.96（偏离约 4%）后，第二次运行 `1 passed`
- **依据**：实测（2026-11-16 两次运行）
- **影响**：
  - 以后写验收或探针的「不会成交」价格，要同时满足当前环境的限价偏离上限（模拟盘 5%）；在 4%–5% 之间取值，并在注释里写明依据
  - 闸门拒绝本身说明这条规则在模拟盘按预期生效
  - 日志里 P10、契约测试的订单被报为 `external_order`（不在本地库），是预期行为


## 修复计划 2：启动恢复改查历史订单（2026-11-16）

- **结论**：启动恢复对「本地未完成、当日列表查不到」的订单（CREATED 除外）查一次 `history_order_list_query`，命中的按快照处理：DAY 过期的 CANCELLED_ALL 变成 CANCELED，已成交的照常合成成交。窗口取最早那张单的下单时间往前 1 天到现在往后 1 天，再按 client_order_id / broker_order_id 精确匹配，所以不依赖 vendor 没写清的时区。其余历史订单不处理，不报 unknown / external。历史查询出错报警 `history_query_failed`，恢复照常完成
- **依据**：`tests/unit/test_recovery_history.py`（实现前 14 条失败，现在全部通过）；P9 第二次 `--check` 的实测
- **出处**：vendor § Get Historical Orders（参数 start / end、Interface Limitations 30 秒 10 次）
- **影响**：`BrokerPort.query_order_history`（SPEC 已加）、`broker/moomoo.py`、`broker/throttle.py`（`HISTORY_ORDERS_RULE`，与 `order_list_query` 分开计）、`broker/fake.py`（`roll_day()`）、`oms/oms.py`；本地仍为 NEW 的 841874、841875 下次启动时会被结束

## 修复计划 1：可卖数量不再重复扣已确认的卖单（ADR-0031，2026-11-16）

- **结论**：卖出时可卖 = min(`can_sell_qty` − 本程序未确认卖单剩余量, 持仓 `qty` − 本程序全部未完成卖单剩余量)，未确认 = CREATED、PENDING_NEW、UNKNOWN。第一项用券商已扣好的数字，第二项在缓存还没刷新时保底，两项取小，五种情形都不会多算
- **依据**：`tests/unit/test_risk_rules.py`（ADR-0031 一节，五种情形各「刚好够卖 / 多一股」）、`tests/unit/test_can_sell_adr31.py`；实现前 8 条失败
- **教训**：FakeBroker 原来的 `can_sell_qty` 恒等于 `qty`，和真实券商不一样，正好掩盖了重复扣减。闸门那条回归测试（`test_gate_allows_selling_rest_after_acked_sell`）在旧 FakeBroker 上能通过；把 FakeBroker 按 P10d 改成扣已确认卖单之后，它才真正测到了 bug。**测试替身的行为要按实测校准**，否则测试通过并不说明代码正确
- **影响**：`risk/rules.py`（`OrderContext.open_sell_unacked`）、`oms/oms.py`（`open_leaves_summary` 取代 `open_leaves_both`，仍只扫一次）、`risk/gate.py`、`broker/fake.py`
