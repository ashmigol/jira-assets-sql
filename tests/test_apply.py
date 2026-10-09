import json
import os

from assets_sql.api import ApiError, TransportError
from assets_sql.plan import apply, compute_plan
from tests.conftest import SCHEMA, sql

YES = lambda _: True  # noqa: E731


def run(cfg, jira, local, **kw):
    db, meta = local
    kw.setdefault("confirm", YES)
    return apply(cfg, jira, db, meta, SCHEMA, **kw)


def key_of(jira, name, tid=10):
    return next(o["key"] for o in jira.of_type(tid) if jira._label(o) == name)


def test_happy_path(cfg, jira, local):
    db, meta = local
    sql(db, meta, "UPDATE systems SET criticality = 'Low', owner = 'alice@example.com, bob@example.com' WHERE name = 'Pandadoc'")
    sql(db, meta, "INSERT INTO systems (name, active) VALUES ('Figma', 'true')")
    res = run(cfg, jira, local)
    assert res == {"applied": 2, "failed": 0, "aborted": None}
    pd = key_of(jira, "Pandadoc")
    assert jira.value(pd, "Criticality") == ["Low"] and jira.value(pd, "Owner") == ["acc-alice", "acc-bob"]
    assert jira.value(key_of(jira, "Figma"), "Active") == ["true"]
    assert not compute_plan(db, meta).items, "local copy must be clean after apply"
    assert db.execute("SELECT key FROM systems WHERE name = 'Figma'").fetchone()[0] == key_of(jira, "Figma")
    log = [json.loads(line) for line in open(cfg.log_path)]
    assert [entry["status"] for entry in log] == ["ok", "ok"]


def test_create_dependency_order(cfg, jira, local):
    db, meta = local
    sql(db, meta, "INSERT INTO roles (name, system) VALUES ('Editor', 'Figma')")
    sql(db, meta, "INSERT INTO systems (name) VALUES ('Figma')")
    assert run(cfg, jira, local)["applied"] == 2
    editor = next(o for o in jira.of_type(11) if jira._label(o) == "Editor")
    assert editor["values"][114] == [key_of(jira, "Figma")]


def test_read_only_schema_refused(cfg, jira, local):
    from dataclasses import replace
    res = run(replace(cfg, writable_schemas=frozenset()), jira, local)
    assert "READ-ONLY" in res["aborted"] and not jira.writes()


def test_delete_needs_flag(cfg, jira, local):
    db, meta = local
    sql(db, meta, "DELETE FROM roles WHERE name = 'Viewer'")
    assert "refused" in run(cfg, jira, local)["aborted"]
    assert run(cfg, jira, local, allow_delete=True)["applied"] == 1
    assert len(jira.of_type(11)) == 2


def test_validation_failure_sends_nothing(cfg, jira, local):
    db, meta = local
    sql(db, meta, "UPDATE systems SET criticality = 'Huge'")
    assert "Validation failed" in run(cfg, jira, local)["aborted"] and not jira.writes()


def test_declined_confirmation(cfg, jira, local):
    db, meta = local
    sql(db, meta, "UPDATE systems SET criticality = 'Low'")
    assert run(cfg, jira, local, confirm=lambda _: False)["aborted"] == "Aborted."
    assert not jira.writes()


# ───── 3-way check against live Jira ─────

def test_remote_change_of_other_field_is_merged(cfg, jira, local):
    db, meta = local
    pd = key_of(jira, "Pandadoc")
    jira.set(pd, "Active", "false")  # someone edits in the UI
    sql(db, meta, "UPDATE systems SET criticality = 'Low' WHERE name = 'Pandadoc'")
    res = run(cfg, jira, local)
    assert res["applied"] == 1
    assert jira.value(pd, "Active") == ["false"], "remote edit must survive"
    assert jira.value(pd, "Criticality") == ["Low"]
    assert not compute_plan(db, meta).items, "no plan to revert the remote edit"
    assert db.execute("SELECT active FROM systems WHERE name = 'Pandadoc'").fetchone()[0] == 0


def test_remote_change_of_same_field_is_conflict(cfg, jira, local):
    db, meta = local
    jira.set(key_of(jira, "Pandadoc"), "Criticality", "Low")
    sql(db, meta, "UPDATE systems SET criticality = NULL WHERE name = 'Pandadoc'")
    res = run(cfg, jira, local)
    assert "Conflicts" in res["aborted"] and "criticality changed in Jira" in res["aborted"]
    assert not jira.writes()
    assert compute_plan(db, meta).items, "local edit is kept for the user to resolve"


def test_already_applied_change_is_skipped(cfg, jira, local):
    db, meta = local
    jira.set(key_of(jira, "Pandadoc"), "Criticality", "Low")
    sql(db, meta, "UPDATE systems SET criticality = 'Low' WHERE name = 'Pandadoc'")
    res = run(cfg, jira, local)
    assert res["applied"] == 0 and not res["aborted"] and not jira.writes()
    assert not compute_plan(db, meta).items


def test_update_of_object_deleted_in_jira(cfg, jira, local):
    db, meta = local
    del jira.objects[jira.by_key(key_of(jira, "Lokalise"))["key"].split("-")[1]]
    sql(db, meta, "UPDATE systems SET criticality = 'High' WHERE name = 'Lokalise'")
    assert "deleted in Jira" in run(cfg, jira, local)["aborted"]


def test_delete_of_object_edited_in_jira_is_conflict(cfg, jira, local):
    db, meta = local
    jira.set(key_of(jira, "Lokalise"), "Criticality", "High")
    sql(db, meta, "DELETE FROM systems WHERE name = 'Lokalise'")
    assert "changed in Jira" in run(cfg, jira, local, allow_delete=True)["aborted"]


def test_create_existing_name_is_conflict(cfg, jira, local):
    db, meta = local
    jira.add(10, Name='Fig"ma')  # created in Jira after our sync; quote exercises AQL escaping
    sql(db, meta, "INSERT INTO systems (name) VALUES ('Fig\"ma')")
    res = run(cfg, jira, local)
    assert "already exists in Jira" in res["aborted"] and not jira.writes()


def test_backup_written_before_changes(cfg, jira, local):
    db, meta = local
    sql(db, meta, "UPDATE systems SET criticality = 'Low' WHERE name = 'Pandadoc'")
    run(cfg, jira, local)
    files = os.listdir(cfg.backup_dir)
    assert len(files) == 1
    backup = json.load(open(os.path.join(cfg.backup_dir, files[0])))
    assert backup[0]["label"] == "Pandadoc"


# ───── failures mid-apply ─────

def test_partial_failure_keeps_failed_items_and_no_duplicates(cfg, jira, local):
    db, meta = local
    sql(db, meta, "INSERT INTO systems (name) VALUES ('Figma')")
    sql(db, meta, "UPDATE systems SET criticality = 'Low' WHERE name = 'Pandadoc'")
    lk_id = jira.by_key(key_of(jira, "Lokalise"))
    lk_id = next(i for i, o in jira.objects.items() if o is lk_id)
    sql(db, meta, "UPDATE systems SET criticality = 'High' WHERE name = 'Lokalise'")
    jira.fail[("PUT", lk_id)] = ApiError(400, "boom")
    res = run(cfg, jira, local)
    assert res["applied"] == 2 and res["failed"] == 1
    left = compute_plan(db, meta).items
    assert [(p["action"], p["label"]) for p in left] == [("UPDATE", f"Lokalise ({key_of(jira, 'Lokalise')})")]
    # second run applies only what's left: Figma is not created twice
    res = run(cfg, jira, local)
    assert res["applied"] == 1
    assert sum(1 for o in jira.of_type(10) if jira._label(o) == "Figma") == 1


def test_network_error_stops_and_next_run_does_not_duplicate(cfg, jira, local):
    db, meta = local
    sql(db, meta, "INSERT INTO systems (name) VALUES ('Alpha')")
    sql(db, meta, "INSERT INTO systems (name) VALUES ('Beta')")
    jira.fail[("POST", "Beta")] = TransportError("connection reset")
    res = run(cfg, jira, local)
    assert res["applied"] == 1 and res["failed"] == 1
    names = sorted(jira._label(o) for o in jira.of_type(10))
    assert names == ["Alpha", "Lokalise", "Pandadoc"]
    assert [p["label"] for p in compute_plan(db, meta).items] == ["Beta"]
    assert run(cfg, jira, local)["applied"] == 1
    assert sorted(jira._label(o) for o in jira.of_type(10)) == ["Alpha", "Beta", "Lokalise", "Pandadoc"]


def test_create_that_reached_jira_despite_error_is_detected(cfg, jira, local):
    """POST succeeded server-side but the response was lost → next apply must not create it again."""
    db, meta = local
    sql(db, meta, "INSERT INTO systems (name) VALUES ('Ghost')")
    orig = jira.create_object

    def lost_response(type_id, attrs):
        orig(type_id, attrs)
        raise TransportError("timeout")
    jira.create_object = lost_response
    run(cfg, jira, local)
    jira.create_object = orig
    res = run(cfg, jira, local)
    assert "already exists in Jira" in res["aborted"]
    assert sum(1 for o in jira.of_type(10) if jira._label(o) == "Ghost") == 1


def test_dependent_create_fails_when_parent_fails(cfg, jira, local):
    db, meta = local
    sql(db, meta, "INSERT INTO systems (name) VALUES ('Figma')")
    sql(db, meta, "INSERT INTO roles (name, system) VALUES ('Editor', 'Figma')")
    jira.fail[("POST", "Figma")] = ApiError(400, "nope")
    res = run(cfg, jira, local)
    assert res == {"applied": 0, "failed": 2, "aborted": None}
    assert not jira.of_type(10)[2:]


def test_remote_edit_not_reverted_after_declined_apply(cfg, jira, local):
    db, meta = local
    pd = key_of(jira, "Pandadoc")
    jira.set(pd, "Active", "false")
    sql(db, meta, "UPDATE systems SET criticality = 'Low' WHERE name = 'Pandadoc'")
    run(cfg, jira, local, confirm=lambda _: False)
    plan = compute_plan(db, meta)
    assert [p["changes"] for p in plan.items] == [{"criticality": ("High", "Low")}], "local edit kept, remote edit not reverted"


# ───── auto-sync on start ─────

def test_open_fresh_syncs_when_clean(cfg, jira, local):
    from assets_sql.plan import open_fresh
    jira.add(10, Name="Figma")  # created in Jira after the first sync
    db, _ = open_fresh(cfg, jira, SCHEMA, log=lambda *a: None)
    assert db.execute("SELECT count(*) FROM systems WHERE name = 'Figma'").fetchone()[0] == 1


def test_open_fresh_keeps_unapplied_changes(cfg, jira, local):
    from assets_sql.plan import open_fresh
    db, meta = local
    sql(db, meta, "INSERT INTO systems (name) VALUES ('Draft')")
    jira.add(10, Name="Figma")
    msgs = []
    db, meta = open_fresh(cfg, jira, SCHEMA, log=msgs.append)
    assert "Auto-sync skipped" in msgs[0]
    assert [p["label"] for p in compute_plan(db, meta).items] == ["Draft"]


def test_auto_sync_config():
    from assets_sql.config import Config
    env = {"JIRA_SITE": "s", "JIRA_EMAIL": "e", "JIRA_API_TOKEN": "t"}
    assert Config.from_env(env).auto_sync
    assert not Config.from_env({**env, "ASSETS_AUTO_SYNC": "0"}).auto_sync


def test_create_same_label_existing_other_system(cfg, jira, local):
    db, meta = local
    sql(db, meta, "INSERT INTO systems (name) VALUES ('Figma')")
    sql(db, meta, "INSERT INTO roles (name, system) VALUES ('Admin', 'Figma')")  # 'Admin' exists for 2 other systems
    res = run(cfg, jira, local)
    assert res == {"applied": 2, "failed": 0, "aborted": None}
    assert sum(1 for o in jira.of_type(11) if jira._label(o) == "Admin") == 3
