# mooapp：Moomoo API 与交易 Agent 学习项目

## 目的
学习 Moomoo OpenAPI 和交易 Agent。本版不追求盈利，但必须具备实盘能力和安全管理。

## 硬性规则（不得违反）
- 不得读取、打印、复制 ~/.config/mooapp/ 下的任何文件；代码、日志、测试中不得出现任何密钥。
- 不得运行带 --env real 的命令。默认环境永远是 SIMULATE。
- 所有下单、改单、撤单必须经过 risk/gate.py，不得新增绕过路径。
- 不得修改风控限额，也不得修改 tests/acceptance/ 下的测试；需要改时停下来问我。
- 禁止 except Exception: pass 等任何静默吞错。
- API 用法以 docs/vendor/ 为准，并在注释里注明章节；文档不明确时写探针脚本在模拟盘实测，不要猜。
- 探针遵守 SPEC.md 7.1 节 R1–R9：只在 SIMULATE 跑；放在 scripts/probe_*.py，src/ 与 tests/ 不得 import；不读密钥；原样打印并记录输入输出到 recordings/probes/（只脱敏账号）；结论按 7.2 格式写进 docs/learnings.md 并注明出处。
- 遇到 docs/adr/ 没覆盖的业务决策，停下来问我。
- 只修改计划中列出的文件；新增依赖需要我批准。

## 实盘三把锁（手册 2.1 节；代码在 M1、M4 实现）
- 配置锁：默认 SIMULATE，必须显式传 `--env real` 才进入 REAL。
- 人锁：REAL 启动时检查交互终端，再用 getpass 输入交易密码和随机确认码；交易密码不落盘。Claude Code 的非交互 Bash 必须过不了。
- 限额锁：REAL 使用 config/settings.toml 中更严的 `[risk.real]`。

## 工作方式
- 先给计划，我确认后再写代码。
- 先写失败的测试，再写实现。
- 完成后运行 pytest，原样汇报结果。

## 常用命令
- pytest：单元测试和 FakeBroker 测试
- pytest -m sim：连模拟盘（需要 OpenD 在线）
- mooapp doctor：连通检查
