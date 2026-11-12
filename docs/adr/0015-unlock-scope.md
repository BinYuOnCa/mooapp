# ADR-0015：REAL 解锁的作用范围

- **状态**：已定
- **日期**：2026-11-11
- **来源**：SPEC.md 第 9 节 Q1（2026-11-11 答复「同意」）

## 决策

`unlock_trade` 对整个 OpenD 生效：任一连接解锁后，本机所有连到 OpenD 的进程都能下实盘单（vendor § Unlock Trade）。人锁只守住启动这一步，需要补充措施

## 取值

三条都做：① REAL 运行期间不开 Claude Code；② 程序正常退出、halt、未处理异常时调用 `unlock_trade(is_unlock=False)` 重新上锁，上锁失败要告警；③ 用探针 P6 确认 Claude Code 的 sandbox 能否拦截到 `127.0.0.1:11111` 的连接，能拦就在 sandbox 配置里禁止

## 理由

OpenD 没有按连接隔离的解锁，只能缩短解锁时间窗，并让 Claude Code 在这个时间窗里不在场

## 注记（2026-11-11）

第 3 条已落实为：`.claude/settings.json` 的 sandbox 只放行 PostgreSQL 的 unix socket，不放行本机 TCP（不开 `allowLocalBinding`）。Claude Code 因此连不上 OpenD 的 11111 端口，连 OpenD 的命令都由你在自己的终端运行。是否真的拦住，由探针 P6 确认。
