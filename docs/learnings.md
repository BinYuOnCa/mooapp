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
