# ADR-0029：resume 命令必须带签名

- **状态**：已定
- **日期**：2026-11-14
- **来源**：M4 代码审查第 10 条（2026-11-14 你的决定）；补充 ADR-0022

## 决策

怎样保证「只有 `mooapp resume`（交互确认）能解除 halt」

## 取值

- `~/.config/mooapp/secrets.toml` 增加 `[resume] key`：一个只用于 resume 的随机密钥（至少 32 字节，十六进制），由你生成并写入，权限 600。Claude Code 的 sandbox 读不到这个目录
- `mooapp resume` 在你的终端里：先过 TTY 和确认码（Q10），再读密钥，用 HMAC-SHA256 对 `resume|<UTC 时间>|<随机数>` 签名，随命令写入命令表
- 常驻进程验签：签名正确、时间在 60 秒以内、随机数没用过（用过的记在 kv），才解除 halt；否则命令以 error 结束并报警
- 常驻进程读不到密钥时，resume 一律拒绝（halt 只能等配好密钥后解除）
- **例外**：为此 SIMULATE 下的常驻进程和 `mooapp resume` 也要读 secrets 文件（原规则是 SIMULATE 不读 secrets，SPEC Q14、Q20）。只读 `[resume] key` 这一项

## 理由

TTY 和确认码只在命令行进程里检查；常驻进程只认命令表。任何能写数据库的程序（包括能连 simulate 库的 Claude Code）都能写一条 resume 进去，绕过人工确认。签名把「人工确认」和一个数据库里拿不到的凭据绑在一起。
