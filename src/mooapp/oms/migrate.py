"""数据库初始化执行器（SPEC 第 4 节「初始化代码」；自写，不引入 Alembic）。

在指定 schema 里、以当前连接的角色执行：
1. db/migrations/NNNN_<名>.sql：按编号只执行一次，记在该 schema 的 schema_migrations 表
2. db/views/、db/routines/：每次都执行（文件里用 CREATE OR REPLACE）
3. db/grants/：最后执行
每个文件在自己的事务里执行；失败时异常原样抛出，已执行的文件保持已提交。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from psycopg import sql

_MIGRATION = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def _set_schema(conn: Any, schema: str) -> None:
    conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
    current = conn.execute("SELECT current_schema()").fetchone()[0]
    if current != schema:
        raise RuntimeError(f"schema {schema!r} 不存在或无权使用（current_schema() = {current!r}）；schema 由你手动创建（SPEC 第 4 节）")


def migrate(conn: Any, schema: str, db_dir: Path) -> list[str]:
    """返回本次新执行的迁移编号。conn 需为 autocommit 连接。"""
    _set_schema(conn, schema)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version text PRIMARY KEY, name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    applied: list[str] = []
    for f in sorted((db_dir / "migrations").glob("*.sql")):
        m = _MIGRATION.match(f.name)
        if not m:
            raise RuntimeError(f"迁移文件名不合规：{f.name}（应为 NNNN_<名>.sql）")
        version = m.group(1)
        if version in done:
            continue
        with conn.transaction():
            conn.execute(f.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version, name) VALUES (%s, %s)", (version, f.name))
        applied.append(version)
    for sub in ("views", "routines", "grants"):
        for f in sorted((db_dir / sub).glob("*.sql")) if (db_dir / sub).is_dir() else []:
            with conn.transaction():
                conn.execute(f.read_text(encoding="utf-8"))
    return applied
