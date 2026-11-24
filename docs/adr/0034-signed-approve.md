# ADR-0034：批准 Agent 提议的命令必须签名

- **状态**：已定
- **日期**：2026-11-17
- **来源**：M8 计划问题 2（2026-11-17 你选 A）；做法同 ADR-0029

## 决策

`mooapp approve <id>` 写进命令表的 approve 命令要不要签名

## 取值

- approve 命令带 HMAC-SHA256 签名，消息为 `approve|<approval_id>|<ts>|<nonce>`；60 秒内有效，nonce 只能用一次（记在 kv）
- 密钥与 resume 共用 secrets 的 `[resume] key`（ADR-0029）；消息前缀 `approve|` 与 `resume|` 不同，两种签名不能互换
- 常驻进程验签失败：报警 `approve_rejected`，命令以 error 结束，提议保持待批准
- MCP server（`mooapp agent serve`）**不读 secrets**，所以生成不了有效签名
- reject 不需要签名（拒绝只会让提议失效，不会下单）
- REAL 下另需交互终端与确认码（SPEC 5.9、Q10）；SIMULATE 下 `auto_approve_simulate = true` 时由常驻进程在进程内自动批准，不经命令表

## 理由

MCP server 能写数据库（提议要写 approvals 表）。如果 Agent 被提示注入、或进程被滥用，它可以直接往命令表插一条 approve 命令，绕过人的批准。签名让「只有能读密钥、在你终端里运行的 `mooapp approve`」才能批准。
