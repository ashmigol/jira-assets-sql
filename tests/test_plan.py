from assets_sql.plan import Resolver, compute_plan, norm, split_values, validate
from tests.conftest import sql


def actions(plan):
    return sorted((p["action"], p["type"]["table"], p["label"]) for p in plan.items)


def test_no_changes(local):
    db, meta = local
    plan = compute_plan(db, meta)
    assert not plan.items and not plan.errors and not plan.warnings


def test_update_create_delete(local):
    db, meta = local
    sql(db, meta, "UPDATE systems SET criticality = 'Low' WHERE name = 'Pandadoc'")
    sql(db, meta, "INSERT INTO systems (name, active) VALUES ('Figma', 1)")
    sql(db, meta, "DELETE FROM roles WHERE name = 'Viewer'")
    plan = compute_plan(db, meta)
    acts = [(a, t) for a, t, _ in actions(plan)]
    assert acts == [("CREATE", "systems"), ("DELETE", "roles"), ("UPDATE", "systems")]
    upd = next(p for p in plan.items if p["action"] == "UPDATE")
    assert upd["changes"] == {"criticality": ("High", "Low")}
    new = next(p for p in plan.items if p["action"] == "CREATE")
    assert new["changes"] == {"name": (None, "Figma"), "active": (None, 1)}


def test_boolean_and_empty_normalisation_is_not_a_change(local):
    db, meta = local
    sql(db, meta, "UPDATE systems SET active = 'true' WHERE name = 'Pandadoc'")
    sql(db, meta, "UPDATE roles SET google_group = '' WHERE name = 'Viewer'")
    assert not compute_plan(db, meta).items


def test_read_only_column_edit_warns(local):
    db, meta = local
    sql(db, meta, "UPDATE systems SET created = 'x' WHERE name = 'Pandadoc'")
    plan = compute_plan(db, meta)
    assert not plan.items and "read-only" in plan.warnings[0]


def test_ref_key_edit_is_used(local, jira):
    db, meta = local
    lk = next(o["key"] for o in jira.objects.values() if jira._label(o) == "Lokalise")
    sql(db, meta, f"UPDATE roles SET system_key = '{lk}' WHERE name = 'Viewer'")
    plan = compute_plan(db, meta)
    assert plan.items[0]["changes"] == {"system": ("Pandadoc", lk)}


def test_ref_label_and_key_disagree_is_error(local):
    db, meta = local
    sql(db, meta, "UPDATE roles SET system = 'Lokalise', system_key = 'TST-1' WHERE name = 'Viewer'")
    plan = compute_plan(db, meta)
    assert plan.errors and "both system and system_key" in plan.errors[0]


def test_inserted_row_with_fake_id_is_ignored(local):
    db, meta = local
    sql(db, meta, "INSERT INTO roles (_id, name) VALUES ('999', 'x')")
    plan = compute_plan(db, meta)
    assert not plan.items and "unknown _id" in plan.warnings[0]


def test_split_and_norm():
    multi = {"kind": "Text", "multi": True}
    assert split_values(multi, "a, b,,c") == ["a", "b", "c"]
    assert split_values({"kind": "Text", "multi": False}, "a, b") == ["a, b"]
    assert norm({"kind": "User", "multi": True}, "a@x.io ,b@x.io") == "a@x.io, b@x.io"
    assert norm({"kind": "Boolean", "multi": False}, "TRUE") == 1


def _validate(db, meta, jira):
    plan = compute_plan(db, meta)
    return plan, validate(plan, Resolver(db, meta, jira))


def test_validation_multi_values_are_split(local, jira):
    db, meta = local
    sql(db, meta, "UPDATE systems SET owner = 'alice@example.com, bob@example.com', labels = 'a, b' WHERE name = 'Lokalise'")
    _, errors = _validate(db, meta, jira)
    assert errors == []


def test_validation_errors(local, jira):
    db, meta = local
    sql(db, meta, "UPDATE systems SET criticality = 'Huge' WHERE name = 'Lokalise'")
    sql(db, meta, "UPDATE roles SET system = 'Nope' WHERE name = 'Viewer'")
    sql(db, meta, "UPDATE roles SET approver = 'ghost@example.com' WHERE name = 'Viewer'")
    sql(db, meta, "INSERT INTO systems (criticality) VALUES ('Low')")
    _, errors = _validate(db, meta, jira)
    text = "\n".join(errors)
    assert "'Huge' is not one of" in text
    assert "'Nope' not found in systems" in text
    assert "ghost@example.com" in text
    assert "required column 'name'" in text


def test_validation_single_value_attribute(local, jira):
    db, meta = local
    sql(db, meta, "UPDATE roles SET approver = 'alice@example.com, bob@example.com' WHERE name = 'Viewer'")
    _, errors = _validate(db, meta, jira)
    assert "accepts one value" in errors[0]


def test_reference_to_new_object_is_pending_dependency(local, jira):
    db, meta = local
    sql(db, meta, "INSERT INTO systems (name) VALUES ('Figma')")
    sql(db, meta, "INSERT INTO roles (name, system) VALUES ('Editor', 'Figma')")
    plan, errors = _validate(db, meta, jira)
    assert errors == []
    role = next(p for p in plan.items if p["type"]["table"] == "roles")
    assert role["deps"] == {("systems", "Figma")}


def test_ambiguous_reference(local, jira):
    db, meta = local
    sql(db, meta, "INSERT INTO systems (name) VALUES ('Pandadoc')")
    sql(db, meta, "UPDATE roles SET system = 'Pandadoc' WHERE name = 'Admin' AND system = 'Lokalise'")
    _, errors = _validate(db, meta, jira)
    assert any("ambiguous" in e for e in errors)
