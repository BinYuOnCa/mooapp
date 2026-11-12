# PROGRESS

**当前**：阶段 1 完成：SPEC 已定，ADR 0001–0025 已定（0012 被 0024 取代），已提交
**下一步**：你用 `!` 执行 git push；然后进入 M1 连通与配置（先给计划）

## 已完成

- 阶段 0：环境、仓库、官方文档 `docs/vendor/`、Python 环境与冒烟脚本
- 阶段 1 会话 1：安全护栏（deny / ask 规则、.gitignore、gitleaks pre-commit），提交 4d64df8
- 阶段 1 会话 2：护栏验证，金丝雀 4 项全部被拒；加固 sandbox（不可绕过）与 `--env real` 钩子，提交 e44d248
- 阶段 1 会话 3：14 条 ADR、CLAUDE.md、目录骨架、order-transitions.md 与 docs/reference/order_state.py（来自 ~/Downloads）、config/settings.toml、config/secrets.example.toml、README.md
- 官方 Skills 审查（手册 1.4，2026-11-11）：MoomooOpen/moomoo-agent-hub @ 9e45546 共 11 个 skill，不安装。原因：moomoo CLI 只凭 OAuth token 即可下实盘单，绕过 risk/gate.py 与人锁。已在 `.claude/settings.json` deny `Bash(moomoo:*)`
- ADR 0001–0014 全部已定；手册第 3 节状态列已同步为「已定」，第 12 条改为 PostgreSQL
- 阶段 1 会话 4：
  - SPEC.md：目标与非目标、架构、进程与线程模型、接口草案、vendor 要点、探针规则 R1–R9 与探针 P1–P8、M1–M9 验收标准、问题清单 Q1–Q22、测试策略
  - 新建 ADR 0015–0025（来自 Q1、Q5–Q8、Q11、Q12、Q15–Q17、Q22）；ADR-0006、0012 加注记，0012 被 0024 取代
  - 主要决定：
    - 进程模型：只有常驻进程 `mooapp run` 持有闸门、OMS 和券商连接，其他命令经 `commands` 表提交（Q18）
    - 存储：PostgreSQL 17（Homebrew，只监听回环地址 5432 端口）；`simulate` 和 `real` 两个 schema；库、角色、schema 由你手动创建（已完成）；初始化代码放 `db/` 进 Git；存储过程不得写 OMS 拥有的表（已定）
    - journal：写库前先写本地 journal，不加密，与日志同等对待（ADR-0025）
    - 凭据：模拟账户 `simulate_acc_id` 写在 `config/broker.toml`（进 Git）；`mooapp_sim` 的数据库密码写在 `config/db.local.toml`（不进 Git，sandbox 可读）；`mooapp_real` 的密码和实盘 `acc_id` 放 secrets
  - 配置模板：`config/db.toml`、`config/db.local.example.toml`、`secrets.example.toml` 增加 `[accounts]` 和 `[postgres]`、`settings.toml` 增加 `[agent]`；`.gitignore` 增加 `var/` 和 `config/*.local.toml`
  - CLAUDE.md 增加探针规则（引用 SPEC 7.1）
  - learnings.md 增加「阶段 1 会话 4：SPEC 审阅中发现的问题」

- sandbox 与数据库实测（2026-11-11）：只放行 PostgreSQL socket 后，sandbox 内经 socket 连接成功，TCP 5432 和 11111 都被拦；pg_hba 已改为 scram，无密码连接被拒；两个角色互相访问不了对方的 schema。`pytest -m pg` 可由 Claude 运行，连 OpenD 的命令由你用 `!` 运行

## 待你处理

- [ ] 核对 `config/settings.toml`：`[risk.real]` 白名单 ETF 与现价、`max_limit_deviation_pct`、`[risk.simulate]` 数值、邮件收件人
- [ ] `config/db.local.toml` 权限目前是 644，建议 `chmod 600`
- [ ] 在 secrets 里填 `[accounts] real_acc_id`（REAL 阶段之前；`[postgres] real_password` 已填）

- [ ] 待议（REAL 阶段前）：是否用两个命令行 OpenD 分别服务 SIMULATE 和 REAL（vendor FAQ Q17、Q8 支持）。需要先由你手动验证「一个 OpenD 解锁后，另一个 OpenD 是否仍是锁定状态」

## 已知问题

- `unlock_trade` 作用于整个 OpenD（vendor § Unlock Trade），对策见 ADR-0015；第 3 条对策要等探针 P6 的结果
- 模拟盘没有成交推送，也不支持 STOP 单：成交来源按 SPEC Q2；STOP 对照实验本版不做（ADR-0006 注记）
- PATH 上的 `psql` / `pg_isready` 是 14.12 客户端（来自 `postgresql@14`），服务端是 17；连接不受影响，但 `pg_dump` 等工具需要用 17 版本
- `config/broker.toml` 已生成，但还没有 `simulate_acc_id`；M1 由生成脚本补上

## M1 前置条件

- SPEC.md 已定并提交（已满足）
- `docs/adr/` 全部已定（已满足）
- OpenD 在线，冒烟测试 4 项 PASS，存在 `config/broker.toml`（已满足，缺 `simulate_acc_id`）
- PostgreSQL 17 已启动，库、角色、schema、密码已就绪（已满足）
