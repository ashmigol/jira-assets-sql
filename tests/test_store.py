import sqlite3

import pytest

from assets_sql import store
from tests.conftest import SCHEMA, sql


def test_snake():
    assert store.snake("Owner (Business)") == "owner_business"
    assert store.snake("  Google group ") == "google_group"
    assert store.snake("()") == "col"


def test_sync_builds_tables(local):
    db, meta = local
    assert [t["table"] for t in meta] == ["systems", "roles"]
    rows = db.execute("SELECT name, criticality, owner, active, labels FROM systems ORDER BY name").fetchall()
    assert [tuple(r) for r in rows] == [("Lokalise", "Low", None, 0, None), ("Pandadoc", "High", "alice@example.com", 1, None)]
    role = db.execute("SELECT system, system_key, approver FROM roles WHERE google_group IS NOT NULL").fetchone()
    assert role["system"] == "Pandadoc" and role["system_key"].startswith("TST-") and role["approver"] == "alice@example.com"
    # base copy is identical
    assert db.execute("SELECT count(*) FROM (SELECT * FROM roles EXCEPT SELECT * FROM _orig_roles)").fetchone()[0] == 0


def test_pagination_reads_all(local, jira):
    db, _ = local
    assert db.execute("SELECT count(*) FROM roles").fetchone()[0] == len(jira.of_type(11)) == 3


def test_duplicate_column_names_get_unique(jira):
    from tests import conftest
    extra = conftest.attr(199, "Owner-", "Text")  # snakes to "owner", same as "Owner"
    conftest.TYPES[10][1].append(extra)
    try:
        meta = store.load_meta(jira, SCHEMA)
        cols = [c["col"] for c in meta[0]["cols"]]
        assert len(cols) == len(set(cols)) and "owner_199" in cols
    finally:
        conftest.TYPES[10][1].remove(extra)


def test_ref_key_collision_renamed(jira):
    from tests import conftest
    extra = conftest.attr(198, "System key")  # would collide with system_key of the reference
    conftest.TYPES[11][1].append(extra)
    try:
        cols = [c["col"] for c in store.load_meta(jira, SCHEMA)[1]["cols"]]
        assert "system_key_198" in cols
    finally:
        conftest.TYPES[11][1].remove(extra)


def test_cell_value_kinds():
    user = {"kind": "User"}
    assert store.cell_value(user, [{"displayValue": "A B (a@x.io)"}, {"displayValue": "C (c@x.io)"}]) == "a@x.io, c@x.io"
    assert store.cell_value({"kind": "Boolean"}, [{"value": "TRUE"}]) == 1
    assert store.cell_value({"kind": "Text"}, []) is None


@pytest.mark.parametrize("stmt", [
    "DROP TABLE systems",
    "CREATE TABLE x (a)",
    "ALTER TABLE systems ADD COLUMN z",
    "ATTACH DATABASE ':memory:' AS other",
    "PRAGMA writable_schema = 1",
    "UPDATE _orig_systems SET name = 'x'",
    "DELETE FROM _meta",
    "UPDATE systems SET _id = '1'",
    "UPDATE systems SET _type = '1'",
])
def test_guard_blocks_dangerous_sql(local, stmt):
    db, meta = local
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        sql(db, meta, stmt)


@pytest.mark.parametrize("stmt", [
    "UPDATE systems SET name = 'x'",
    "INSERT INTO roles (name) VALUES ('x')",
    "WITH s AS (SELECT 1) DELETE FROM roles",
    "/* sneaky */ DELETE FROM roles",
    "REPLACE INTO roles (name) VALUES ('x')",
])
def test_guard_read_only_schema_blocks_all_writes(local, stmt):
    db, meta = local
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        sql(db, meta, stmt, writable=False)


def test_guard_allows_reads_and_edits(local):
    db, meta = local
    assert sql(db, meta, "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<3) SELECT count(*) FROM n").fetchone()[0] == 3
    assert sql(db, meta, "SELECT count(*) FROM _orig_roles r JOIN systems s ON s.name = r.system").fetchone()[0] == 3
    assert sql(db, meta, "UPDATE roles SET google_group = 'g@example.com' WHERE name = 'Viewer'").rowcount == 1
    assert sql(db, meta, "SELECT count(*) FROM systems", writable=False).fetchone()[0] == 2


def test_guard_is_removed_after_statement(local):
    from assets_sql.shell import run_sql
    db, meta = local
    run_sql(db, meta, "DROP TABLE systems", "table", True)
    db.execute("CREATE TABLE internal_ok (a)")  # internal code is not restricted


def test_reset_restores_base(local):
    db, meta = local
    sql(db, meta, "DELETE FROM roles")
    store.reset(db, meta)
    assert db.execute("SELECT count(*) FROM roles").fetchone()[0] == 3


def test_db_file_is_private(cfg, local):
    import os
    import stat
    assert stat.S_IMODE(os.stat(cfg.db_path(SCHEMA)).st_mode) == 0o600
