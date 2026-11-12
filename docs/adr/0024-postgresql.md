# ADR-0024：存储改用 PostgreSQL

- **状态**：已定
- **日期**：2026-11-11
- **来源**：SPEC.md 第 9 节 Q15（2026-11-11 答复）；取代 ADR-0012

## 决策

存储

## 取值

- 不实现 SQLite，直接用 PostgreSQL。
- SIMULATE 与 REAL 在同一个库的两个 schema：`simulate`、`real`。一个 `Store` 实例只连一个 schema，不做跨环境查询。
- 使用本机 Homebrew 安装的 PostgreSQL，只监听 `127.0.0.1:5432`；它是本项目之外的基础设施，数据文件不进 Git。不用 Docker。
- 库、角色、schema 由你手动创建；建表、索引、视图、函数与存储过程、授权等初始化代码放仓库 `db/`，进 Git。
- 以后可以用存储过程承担部分业务逻辑（尤其是 ad hoc 分析），但不得写 OMS 拥有的表；需要改动交易状态时另建 ADR。
- 代码只依赖 `Store` 协议；单元测试用内存实现，`PostgresStore` 用 `pg` 标记的契约测试验证。

## 理由

你的决定：以后要用 PostgreSQL，就不先做一遍 SQLite；本机已有 Homebrew PostgreSQL。两个 schema 可以按数据库角色分别授权，比两个 SQLite 文件隔离得更彻底。
