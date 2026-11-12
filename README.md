# mooapp

用 Claude Code 开发的 Moomoo OpenAPI 交易 Agent 学习项目。本版不追求盈利，目标是学懂订单生命周期、风控和 Agent 接入，同时具备安全的实盘能力。

完整流程见 [docs/manual.md](docs/manual.md)；当前进度见 [PROGRESS.md](PROGRESS.md)；Claude Code 必须遵守的规则见 [CLAUDE.md](CLAUDE.md)。

## 架构一句话

三个入口（命令行、规则引擎、Agent）共用一条写路径：**风控闸门 → OMS → BrokerPort → OpenD**。测试时把 MoomooBroker 换成 FakeBroker，其余代码不变。

## 安全设计

- 密钥只在 `~/.config/mooapp/secrets.toml`（权限 600），仓库里只有占位模板 `config/secrets.example.toml`。
- 实盘三把锁：配置锁（默认 SIMULATE，须显式传 `--env real`）、人锁（交互终端 + 交易密码 + 确认码）、限额锁（`[risk.real]`）。
- Claude Code 护栏：`.claude/settings.json` 的 deny 规则、PreToolUse 钩子 `.claude/hooks/block-env-real.sh`、不可绕过的 sandbox；gitleaks 作为 pre-commit 钩子拦截密钥提交。

## 阶段 0：准备环境

在**你自己的终端**里运行（不要交给 Claude Code：脚本要交互输入密码，密钥也不应进入 AI 上下文）：

```bash
bash ~/GitRepos/mooapp/scripts/stage0_bootstrap.sh          # 全部步骤，可反复运行，已完成的自动跳过
bash ~/GitRepos/mooapp/scripts/stage0_bootstrap.sh smoke    # 只跑冒烟测试（需 OpenD 已登录）
bash ~/GitRepos/mooapp/scripts/stage0_bootstrap.sh token    # 重新设置 OpenD 连接 token
```

重跑到结尾显示「无。阶段 0 完成」，冒烟测试 4 项（OpenD 监听、账户列表、模拟账户资金、SMTP 发信）全部 PASS，并生成 `config/broker.toml`。

需要你亲手做的事（脚本会以 `[TODO]` 列出）：生成邮箱应用专用密码、`gh auth login`、安装并登录 OpenD、在脚本提示时输入 SMTP 信息。

## 启动 Claude Code

```bash
cd ~/GitRepos/mooapp && claude
```

- 首次进入后运行 `/permissions` 确认 deny / ask 规则，运行 `/sandbox` 确认 sandbox 已开启。
- 每个里程碑开一个新会话，先用 Shift+Tab 进入 plan mode，再粘贴 `docs/manual.md` 第 5 节对应的提示词。
- `git push`、`gh` 等需要联网或改 `.git/config` 的命令在 sandbox 里会被拦，用 `! <命令>` 自己运行。

## 目录

```text
config/          settings.toml（白名单、限额、收件人）、secrets.example.toml（占位）
docs/adr/        编码前定死的决策，一条一个文件
docs/vendor/     官方 API 文档
docs/            order-transitions.md、learnings.md、drills.md 等
scripts/         阶段 0 脚本与探针脚本
src/mooapp/      config broker oms risk strategy notify eod agent ops
tests/           unit contract acceptance
```
