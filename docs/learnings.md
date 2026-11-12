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
- 一台电脑可以同时运行两个命令行版 OpenD，各用不同的 `api_port`（vendor FAQ Q17）；同一账户最多 10 个 OpenD 同时登录（FAQ Q8）。「一个 OpenD 解锁后另一个是否仍锁定」文档未写明，REAL 阶段前由你手动验证。

### 工作方式

- 问题清单的答复要写回正文。如果只填在表格里，正文仍写「见 Qn」，后续实现会照旧方案做。
- 答复之间会互相牵动：Q14（账号放 secrets）与 sandbox 冲突；Q15（改用 PostgreSQL）连带出凭据（Q20）和故障处理（Q22）。改一条答复后，要回头检查受影响的条目。