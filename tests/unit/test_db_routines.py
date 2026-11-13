"""SPEC 第 4 节「存储过程的边界」：db/routines/ 里的函数与存储过程不得写 OMS 拥有的表（M3 加这条静态检查）。

只做文本扫描（去掉注释后找 INSERT INTO / UPDATE / DELETE FROM / TRUNCATE / MERGE INTO / COPY ... FROM 指向 OMS 表）。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROUTINES = REPO / "db" / "routines"
MIGRATIONS = REPO / "db" / "migrations"
OMS_TABLES = {
    "orders", "order_events", "fills", "recon_events", "journal_applied",
    "rules", "approvals", "commands", "audit", "kv",
}
_WRITE = re.compile(
    r"\b(?:insert\s+into|update|delete\s+from|truncate(?:\s+table)?|merge\s+into|copy)\s+(?:only\s+)?(?:[a-z_]+\.)?\"?([a-z_]+)\"?",
    re.I,
)


def strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


def writes_to_oms_tables(sql: str) -> set[str]:
    return {m.group(1).lower() for m in _WRITE.finditer(strip_comments(sql))} & OMS_TABLES


def test_scanner_catches_violations():
    bad = """
    CREATE OR REPLACE FUNCTION analytics_x() RETURNS void LANGUAGE sql AS $$
      -- update orders set x = 1  (注释不算)
      UPDATE simulate.orders SET status = 'FILLED';
      INSERT INTO "fills" VALUES (1);
      DELETE FROM order_events;
      TRUNCATE kv;
    $$;
    """
    assert writes_to_oms_tables(bad) == {"orders", "fills", "order_events", "kv"}
    assert writes_to_oms_tables("SELECT * FROM orders; INSERT INTO analytics_daily SELECT 1;") == set()


def test_routines_dir_exists():
    assert ROUTINES.is_dir()


def test_routines_do_not_write_oms_tables():
    bad = {}
    for p in sorted(ROUTINES.rglob("*.sql")):
        hits = writes_to_oms_tables(p.read_text(encoding="utf-8"))
        if hits:
            bad[str(p.relative_to(REPO))] = sorted(hits)
    assert not bad, f"存储过程不得写 OMS 拥有的表（SPEC 第 4 节）：{bad}"


def test_migrations_exist_and_numbered():
    files = sorted(MIGRATIONS.glob("*.sql"))
    assert files and files[0].name.startswith("0001_")
    assert all(re.match(r"^\d{4}_[a-z0-9_]+\.sql$", f.name) for f in files)
