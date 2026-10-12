import pytest

from assets_sql import fields, store
from assets_sql.api import ApiError
from assets_sql.plan import apply, compute_plan, open_fresh, show_plan
from assets_sql.shell import handle_sql
from tests.conftest import SCHEMA, sql

YES = lambda _: True  # noqa: E731


def alter(local, statement, writable=True):
    db, meta = local
    handle_sql(db, meta, statement, "table", writable)


def cols(db, table):
    return [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]


@pytest.mark.parametrize("statement, expected", [
    ('ALTER TABLE systems ADD COLUMN "Public comment" TEXT;', ("systems", "Public comment", "Text", None)),
    ("alter table systems add public_comment textarea", ("systems", "Public comment", "Textarea", None)),
    ("ALTER TABLE roles ADD COLUMN backup REFERENCES systems;", ("roles", "Backup", "Reference", "systems")),
    ('ALTER TABLE systems ADD COLUMN "Go-live" DATE', ("systems", "Go-live", "Date", None)),
    ("ALTER TABLE systems ADD COLUMN deputy USER", ("systems", "Deputy", "User", None)),
])
def test_parse_alter(statement, expected):
    assert fields.parse_alter(statement) == expected


@pytest.mark.parametrize("statement, error", [
    ("ALTER TABLE systems DROP COLUMN name", "only `ALTER TABLE"),
    ("ALTER TABLE systems RENAME TO apps", "only `ALTER TABLE"),
    ("ALTER TABLE systems ADD COLUMN x", "type missing"),
    ("ALTER TABLE systems ADD COLUMN x BLOB", "unsupported type"),
])
def test_parse_alter_rejects(statement, error):
    with pytest.raises(ValueError, match=error):
        fields.parse_alter(statement)


def test_add_field_with_values_in_one_apply(cfg, jira, local, capsys):
    db, meta = local
    alter(local, 'ALTER TABLE systems ADD COLUMN "Public comment" TEXT;')
    assert "public_comment" in cols(db, "systems") and "public_comment" in cols(db, "_orig_systems")
    sql(db, meta, "UPDATE systems SET public_comment = 'Used by finance' WHERE name = 'Pandadoc'")
    plan = compute_plan(db, meta)
    assert [p["action"] for p in plan.items] == ["ADD FIELD", "UPDATE"]
    show_plan(plan)
    assert '"Public comment" Text' in capsys.readouterr().out
    assert not jira.writes(), "nothing goes to Jira before apply"

    res = apply(cfg, jira, db, meta, SCHEMA, confirm=YES)
    assert res == {"applied": 2, "failed": 0, "aborted": None}
    kinds = [w[1] for w in jira.writes()]
    assert kinds[0] == "attribute of 10", "the field is created before objects use it"
    pd = next(o["key"] for o in jira.of_type(10) if jira._label(o) == "Pandadoc")
    assert jira.value(pd, "Public comment") == ["Used by finance"]
    assert not compute_plan(db, meta).items
    saved = store.load_local_meta(db)
    col = next(c for t in saved for c in t["cols"] if c["col"] == "public_comment")
    assert col["id"] is not None and "pending" not in col

    store.sync(db, jira, SCHEMA)  # the field is now a normal one
    assert db.execute("SELECT public_comment FROM systems WHERE name = 'Pandadoc'").fetchone()[0] == "Used by finance"


def test_add_reference_field(cfg, jira, local):
    db, meta = local
    alter(local, "ALTER TABLE roles ADD COLUMN backup_system REFERENCES systems;")
    assert {"backup_system", "backup_system_key"} <= set(cols(db, "roles"))
    sql(db, meta, "UPDATE roles SET backup_system = 'Lokalise' WHERE name = 'Viewer'")
    assert apply(cfg, jira, db, meta, SCHEMA, confirm=YES)["applied"] == 2
    lk = next(o["key"] for o in jira.of_type(10) if jira._label(o) == "Lokalise")
    viewer = next(o["key"] for o in jira.of_type(11) if jira._label(o) == "Viewer")
    assert jira.value(viewer, "Backup system") == [lk]


def test_new_field_used_by_new_object(cfg, jira, local):
    db, meta = local
    alter(local, "ALTER TABLE systems ADD COLUMN public_comment TEXT")
    sql(db, meta, "INSERT INTO systems (name, public_comment) VALUES ('Figma', 'design')")
    assert apply(cfg, jira, db, meta, SCHEMA, confirm=YES)["applied"] == 2
    figma = next(o["key"] for o in jira.of_type(10) if jira._label(o) == "Figma")
    assert jira.value(figma, "Public comment") == ["design"]


def test_reset_drops_new_field(cfg, jira, local):
    db, meta = local
    sql(db, meta, "UPDATE systems SET criticality = 'Low' WHERE name = 'Pandadoc'")
    alter(local, "ALTER TABLE systems ADD COLUMN public_comment TEXT")
    sql(db, meta, "UPDATE systems SET public_comment = 'x'")
    store.reset(db, meta)
    assert "public_comment" not in cols(db, "systems") and "public_comment" not in cols(db, "_orig_systems")
    assert not compute_plan(db, meta).items
    assert all(not c.get("pending") for t in store.load_local_meta(db) for c in t["cols"])
    assert db.execute("SELECT criticality FROM systems WHERE name = 'Pandadoc'").fetchone()[0] == "High"


def test_pending_field_blocks_auto_sync(cfg, jira, local):
    alter(local, "ALTER TABLE systems ADD COLUMN public_comment TEXT")
    db, meta = open_fresh(cfg, jira, SCHEMA, log=lambda *a: None)
    assert "public_comment" in cols(db, "systems")


def test_field_created_in_jira_meanwhile_is_a_conflict(cfg, jira, local):
    db, meta = local
    alter(local, 'ALTER TABLE systems ADD COLUMN "Public comment" TEXT')
    jira.create_attribute(10, {"name": "public comment", "type": 0, "defaultTypeId": 0})
    jira.calls.clear()
    res = apply(cfg, jira, db, meta, SCHEMA, confirm=YES)
    assert "already exists in Jira" in res["aborted"] and not jira.writes()


def test_failed_field_creation_skips_objects_using_it(cfg, jira, local):
    db, meta = local
    alter(local, "ALTER TABLE systems ADD COLUMN public_comment TEXT")
    sql(db, meta, "UPDATE systems SET public_comment = 'x' WHERE name = 'Pandadoc'")
    sql(db, meta, "UPDATE systems SET criticality = 'High' WHERE name = 'Lokalise'")
    jira.fail[("ATTR", "Public comment")] = ApiError(403, "no permission")
    res = apply(cfg, jira, db, meta, SCHEMA, confirm=YES)
    assert res["applied"] == 1 and res["failed"] == 2
    assert [p["action"] for p in compute_plan(db, meta).items] == ["ADD FIELD", "UPDATE"], "failed items stay in the plan"


@pytest.mark.parametrize("statement, msg", [
    ("ALTER TABLE systems ADD COLUMN name TEXT", "already has a column name"),
    ('ALTER TABLE systems ADD COLUMN "ACTIVE" BOOLEAN', "already has a column active"),
    ("ALTER TABLE nope ADD COLUMN x TEXT", "no table 'nope'"),
    ("ALTER TABLE roles ADD COLUMN x REFERENCES nope", "no table 'nope' to reference"),
    ("ALTER TABLE systems DROP COLUMN name", "rename and delete fields in Jira"),
])
def test_alter_errors_change_nothing(local, capsys, statement, msg):
    db, meta = local
    before = cols(db, "systems")
    alter(local, statement)
    assert msg in capsys.readouterr().out
    assert cols(db, "systems") == before and not compute_plan(db, meta).items


def test_read_only_schema_refuses_alter(local, capsys):
    db, meta = local
    alter(local, "ALTER TABLE systems ADD COLUMN public_comment TEXT", writable=False)
    assert "read-only" in capsys.readouterr().out and "public_comment" not in cols(db, "systems")


def test_other_ddl_still_denied(local, capsys):
    db, meta = local
    handle_sql(db, meta, "CREATE TABLE x (a)", "table", True)
    handle_sql(db, meta, "DROP TABLE systems", "table", True)
    assert capsys.readouterr().out.count("not authorized") == 2
