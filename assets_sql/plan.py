"""Diff local edits against the last sync (plan) and push them to Jira (apply).

apply = plan → local validation → refresh (3-way check against live Jira) → confirm → backup → execute.
Each successful change is written back to the local copy immediately, so a crash or a failed item never causes
duplicates on the next run, and failed items stay in the plan.
"""
from __future__ import annotations

import datetime
import json
import os

from . import store
from .api import ApiError, TransportError
from .output import fmt, print_table
from .store import KEY_RE, label_col, row_from_object, table_of, write_row

ORDER = {"CREATE": 0, "UPDATE": 1, "DELETE": 2}


class Plan:
    def __init__(self):
        self.items, self.warnings, self.errors = [], [], []

    def __len__(self):
        return len(self.items)


# ───────── values ─────────

def norm(col, v):
    """Canonical form for comparing cells (1 == 'true' for booleans, '' == NULL, …)."""
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    kind = col["kind"]
    if kind == "Boolean":
        s = str(v).strip().lower()
        if s in ("1", "true", "yes", "y"):
            return 1
        if s in ("0", "false", "no", "n"):
            return 0
        return v
    if kind == "Integer":
        try:
            return int(str(v).strip())
        except ValueError:
            return v
    if kind == "Double":
        try:
            return float(str(v).strip())
        except ValueError:
            return v
    if col["multi"] or kind in ("Reference", "User"):
        return ", ".join(split_values(col, v))
    return str(v) if not isinstance(v, str) else v


def split_values(col, value):
    if value is None or str(value).strip() == "":
        return []
    if col["multi"] or col["kind"] in ("Reference", "User"):
        return [x.strip() for x in str(value).split(",") if x.strip()]
    return [str(value)]


def _get(row, key):
    return row[key] if key in row.keys() else None


def _label(t, row):
    lc = label_col(t)
    lab = _get(row, lc["col"]) if lc else None
    key = _get(row, "key")
    return f"{lab} ({key})" if lab and key else str(lab or key or _get(row, "_id") or "?")


# ───────── plan ─────────

def compute_plan(db, meta):
    plan = Plan()
    for t in meta:
        table = t["table"]
        editable = [c for c in t["cols"] if c["editable"]]
        orig = {r["_id"]: r for r in db.execute(f'SELECT * FROM "_orig_{table}"').fetchall()}
        live, seen = db.execute(f'SELECT rowid AS _rowid, * FROM "{table}"').fetchall(), set()
        for r in live:
            oid = r["_id"]
            if oid is None:
                changes = {}
                for c in editable:
                    v = norm(c, r[c["col"]])
                    if v is None and c["kind"] == "Reference":
                        v = norm(c, r[c["col"] + "_key"])
                    if v is not None:
                        changes[c["col"]] = (None, v)
                lc = label_col(t)
                plan.items.append({"action": "CREATE", "type": t, "id": None, "rowid": r["_rowid"],
                                   "name": _get(r, lc["col"]) if lc else None,
                                   "label": str(_get(r, lc["col"]) if lc else "?"), "changes": changes})
                continue
            if oid not in orig:
                plan.warnings.append(f"{table}: row with unknown _id {oid} ignored (new rows must have _id NULL)")
                continue
            if oid in seen:
                plan.warnings.append(f"{table}: duplicate row for _id {oid} ignored")
                continue
            seen.add(oid)
            o, changes = orig[oid], {}
            for c in editable:
                col = c["col"]
                old, new = norm(c, o[col]), norm(c, r[col])
                if c["kind"] == "Reference":
                    key_new = norm(c, r[col + "_key"])
                    if key_new != norm(c, o[col + "_key"]):
                        if old == new or new == key_new:
                            new = key_new
                        else:
                            plan.errors.append(f"{table} {_label(t, o)}: both {col} and {col}_key changed — set only one")
                            continue
                if old != new:
                    changes[col] = (old, new)
            ro = [c["col"] for c in t["cols"] if not c["editable"] and norm(c, o[c["col"]]) != norm(c, r[c["col"]])]
            if ro:
                plan.warnings.append(f"{table} {_label(t, o)}: read-only column(s) changed locally, ignored: {', '.join(ro)}")
            if changes:
                plan.items.append({"action": "UPDATE", "type": t, "id": oid, "rowid": r["_rowid"],
                                   "label": _label(t, o), "changes": changes, "orig": o})
        for oid, o in orig.items():
            if oid not in seen:
                plan.items.append({"action": "DELETE", "type": t, "id": oid, "label": _label(t, o), "changes": {}, "orig": o})
    return plan


def show_plan(plan):
    for w in plan.warnings:
        print(f"  ! {w}")
    for e in plan.errors:
        print(f"  ✗ {e}")
    if not plan.items:
        print("No local changes.")
        return

    def show(v):
        return "∅" if v is None else str(v)

    rows = []
    for n, p in enumerate(plan.items):
        if n:
            rows.append([""] * 5)  # blank line between objects
        head = [p["action"], p["type"]["table"], p["label"]]
        if not p["changes"]:
            rows.append(head + ["", ""])
        for i, (k, (a, b)) in enumerate(p["changes"].items()):
            rows.append((head if i == 0 else ["", "", ""]) + [k, f"{show(a)} → {show(b)}" if p["action"] == "UPDATE" else show(b)])
    print_table(["action", "table", "object", "field", "value"], rows, width=80,
                footer=f"({len(plan.items)} change{'s' if len(plan.items) != 1 else ''})")


# ───────── resolving values for the API ─────────

class Resolver:
    """Local cell → API objectAttributeValues. References accept a label or an object key."""

    def __init__(self, db, meta, client):
        self.db, self.meta, self.client = db, meta, client
        self.created = {}  # (table, label) → key of objects created in this apply

    def ref(self, col, item):
        """Object key, or ('pending', table, label) for an object that is created in the same plan."""
        if KEY_RE.fullmatch(item):
            return item
        rt = table_of(self.meta, col["ref"])
        lc = label_col(rt) if rt else None
        if not lc:
            raise ValueError(f"{col['name']}: cannot resolve '{item}' (use the object key)")
        rows = self.db.execute(f'SELECT _id, "key" FROM "{rt["table"]}" WHERE "{lc["col"]}" = ?', (item,)).fetchall() \
            if "key" in [c["col"] for c in rt["cols"]] else []
        if len(rows) != 1:
            raise ValueError(f"{col['name']}: '{item}' {'not found' if not rows else 'is ambiguous'} in {rt['table']} (use the object key)")
        if rows[0]["key"]:
            return rows[0]["key"]
        return ("pending", rt["table"], item)

    def values(self, col, value, final=False):
        items = split_values(col, value)
        if len(items) > 1 and not col["multi"]:
            raise ValueError(f"{col['name']}: accepts one value, got {len(items)}")
        out = []
        for it in items:
            if col["kind"] == "Boolean":
                v = norm(col, it)
                if v not in (0, 1):
                    raise ValueError(f"{col['name']}: '{it}' is not a boolean")
                out.append({"value": "true" if v else "false"})
            elif col["kind"] == "Reference":
                key = self.ref(col, it)
                if isinstance(key, tuple) and final:
                    key = self.created.get(key[1:])
                    if not key:
                        raise ValueError(f"{col['name']}: referenced object '{it}' was not created")
                out.append({"value": key})
            elif col["kind"] == "User":
                out.append({"value": self.client.account_id(it)})
            elif col["kind"] == "Select":
                if col["options"] and it not in col["options"]:
                    raise ValueError(f"{col['name']}: '{it}' is not one of {col['options']}")
                out.append({"value": it})
            else:
                out.append({"value": it})
        return out

    def attrs(self, item, final=True):
        cols = {c["col"]: c for c in item["type"]["cols"]}
        return [{"objectTypeAttributeId": cols[k]["id"], "objectAttributeValues": self.values(cols[k], new, final)}
                for k, (_, new) in item["changes"].items()]


def validate(plan, resolver):
    errors = []
    for p in plan.items:
        p["deps"] = set()
        cols = {c["col"]: c for c in p["type"]["cols"]}
        for k, (_, new) in p["changes"].items():
            try:
                for v in resolver.values(cols[k], new):
                    if isinstance(v["value"], tuple):
                        p["deps"].add(v["value"][1:])
            except (ValueError, ApiError) as e:
                errors.append(f"{p['type']['table']} {p['label']}: {e}")
        if p["action"] == "CREATE":
            for c in p["type"]["cols"]:
                if c["required"] and c["editable"] and c["col"] not in p["changes"]:
                    errors.append(f"{p['type']['table']} '{p['label']}': required column '{c['col']}' is empty")
    return errors


# ───────── refresh: 3-way check against live Jira ─────────

def _aql_str(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def refresh(plan, client):
    """Compare base (last sync), local and live Jira for every item. Mutates the plan.

    UPDATE: a field changed in Jira since the sync is a conflict only if we change the same field to a different
    value; fields already equal to the target are dropped. DELETE: any change in Jira is a conflict.
    CREATE: an object with the same label must not exist yet. Returns (conflicts, notes).
    """
    conflicts, notes, keep = [], [], []
    plan.refreshed = []
    for p in plan.items:
        t = p["type"]
        name = f"{t['table']} {p['label']}"
        if p["action"] == "CREATE":
            lc = label_col(t)
            if lc and p.get("name"):
                hits = client.aql(f"objectTypeId = {int(t['id'])} AND {_aql_str(lc['name'])} = {_aql_str(p['name'])}", limit=5)
                found = [o["objectKey"] for o in hits.get("values") or [] if str(o.get("label", "")).lower() == str(p["name"]).lower()]
                if found:
                    conflicts.append(f"{name}: already exists in Jira ({', '.join(found)}) — .sync to pick it up")
                    continue
            keep.append(p)
            continue
        remote = client.get_object(p["id"])
        if remote is None:
            if p["action"] == "DELETE":
                notes.append(f"{name}: already deleted in Jira, skipped")
                plan.refreshed.append((p, None))
            else:
                conflicts.append(f"{name}: deleted in Jira after your last sync")
            continue
        rrow, base = row_from_object(t, remote), p["orig"]
        p["remote"] = remote
        changed_remote = [c for c in t["cols"] if c["editable"] and norm(c, rrow[c["col"]]) != norm(c, base[c["col"]])]
        if p["action"] == "DELETE":
            if changed_remote:
                conflicts.append(f"{name}: changed in Jira after your last sync ({', '.join(c['col'] for c in changed_remote)}) — .sync and retry")
            else:
                keep.append(p)
            continue
        cols = {c["col"]: c for c in t["cols"]}
        for k, (old, new) in list(p["changes"].items()):
            c = cols[k]
            rem = norm(c, rrow[k])
            rem_keys = norm(c, rrow.get(k + "_key")) if c["kind"] == "Reference" else None
            if new == rem or (rem_keys is not None and new == rem_keys):
                notes.append(f"{name}: {k} is already {fmt(new)} in Jira, skipped")
                del p["changes"][k]
            elif c in changed_remote:
                conflicts.append(f"{name}: {k} changed in Jira ({fmt(old)} → {fmt(rem)}), you set {fmt(new)}")
        others = [c["col"] for c in changed_remote if c["col"] not in p["changes"]]
        if others:
            notes.append(f"{name}: also changed in Jira since sync (kept): {', '.join(others)}")
        plan.refreshed.append((p, remote))
        if p["changes"]:
            keep.append(p)
    plan.items = keep
    return conflicts, notes


# ───────── auto-sync ─────────

def open_fresh(cfg, client, schema, log=print):
    """Open the local copy and refresh it from Jira, unless it has unapplied local changes (a sync would drop them)."""
    existed = os.path.exists(cfg.db_path(schema))
    db, meta = store.open_db(cfg, client, schema, log=log)
    if not existed:
        return db, meta
    pending = len(compute_plan(db, meta))
    if pending:
        log(f"Auto-sync skipped: {pending} unapplied local change(s) would be lost. .plan / .apply them, or .reset and .sync.")
        return db, meta
    meta, n = store.sync(db, client, schema)
    log(f"Synced from Jira: {n} objects.")
    return db, meta


# ───────── apply ─────────

def order(items):
    """CREATEs in dependency order (referenced objects first), then UPDATEs, then DELETEs."""
    creates = [p for p in items if p["action"] == "CREATE"]
    names = {(p["type"]["table"], p["name"]) for p in creates}
    done, out = set(), []
    while creates:
        ready = [p for p in creates if not ((p.get("deps") or set()) & names) - done]
        if not ready:
            raise ValueError("circular references between new objects: " + ", ".join(p["label"] for p in creates))
        for p in ready:
            out.append(p)
            done.add((p["type"]["table"], p["name"]))
            creates.remove(p)
    return out + sorted((p for p in items if p["action"] != "CREATE"), key=lambda p: ORDER[p["action"]])


def _ask(prompt):
    return input(prompt).strip().lower() == "y"


def apply(cfg, client, db, meta, schema, allow_delete=False, confirm=_ask):
    """Push local changes to Jira. Returns a summary dict."""
    res = {"applied": 0, "failed": 0, "aborted": None}

    def abort(reason):
        print(reason)
        res["aborted"] = reason
        return res

    if not cfg.is_writable(schema):
        return abort(f"Schema {schema} is READ-ONLY (allowed: {sorted(cfg.writable_schemas) or 'none'}; see ASSETS_WRITABLE_SCHEMAS).")
    plan = compute_plan(db, meta)
    show_plan(plan)
    if plan.errors:
        return abort("Fix the errors above first.")
    if not plan.items:
        return res
    deletes = [p for p in plan.items if p["action"] == "DELETE"]
    if deletes and not allow_delete:
        return abort(f"{len(deletes)} DELETE(s) in the plan — refused. Use `--allow-delete` or `.reset`.")
    resolver = Resolver(db, meta, client)
    errors = validate(plan, resolver)
    if errors:
        return abort("Validation failed:\n  " + "\n  ".join(errors))
    print("Checking current state in Jira…")
    conflicts, notes = refresh(plan, client)
    for n in notes:
        print(f"  · {n}")
    if conflicts:
        return abort("Conflicts with Jira — nothing applied:\n  " + "\n  ".join(conflicts))
    rebase(db, plan.refreshed)
    if not plan.items:
        print("Nothing left to apply — Jira already matches.")
        return res
    try:
        items = order(plan.items)
    except ValueError as e:
        return abort(str(e))
    if notes:
        show_plan(plan)
    if not confirm(f"Apply {len(items)} change(s) to schema {schema}? [y/N] "):
        return abort("Aborted.")

    os.makedirs(cfg.backup_dir, mode=0o700, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = [p["remote"] for p in items if "remote" in p]
    if backup:
        path = os.path.join(cfg.backup_dir, f"assets-{schema}-{stamp}.json")
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(backup, f, indent=1)
        print(f"Backup: {path}")

    for p in items:
        t = p["type"]
        status, detail = None, ""
        try:
            attrs = resolver.attrs(p) if p["action"] != "DELETE" else None
            if p["action"] == "CREATE":
                r = client.create_object(t["id"], attrs)
                p["id"] = str(r["id"])
                resolver.created[(t["table"], p["name"])] = r.get("objectKey")
                detail = r.get("objectKey", "")
            elif p["action"] == "UPDATE":
                client.update_object(p["id"], t["id"], attrs)
            else:
                try:
                    client.delete_object(p["id"])
                except ApiError as e:
                    if e.status != 404:
                        raise
            status = "ok"
        except ValueError as e:
            status, detail = "failed", str(e)
        except TransportError as e:
            status, detail = "unknown", str(e)
        except ApiError as e:
            status, detail = "failed", str(e)
        if status == "ok":
            _mark_applied(db, client, p)
            res["applied"] += 1
        else:
            res["failed"] += 1
        mark = {"ok": "✓", "failed": "✗", "unknown": "?"}[status]
        print(f"  {mark} {p['action']} {t['table']} {p['label']} {detail}".rstrip())
        _log(cfg, {"ts": stamp, "schema": schema, "action": p["action"], "table": t["table"], "object": p["label"],
                   "id": p.get("id"), "changes": {k: [a, b] for k, (a, b) in p["changes"].items()},
                   "status": status, "detail": detail})
        if status == "unknown":
            print("Network error — stopped. The last change may or may not have been applied: run .sync and check.")
            break
    left = len(compute_plan(db, meta))
    print(f"{res['applied']}/{len(items)} applied." + (f" {left} change(s) still pending locally (.plan)." if left else ""))
    return res


def _mark_applied(db, client, p):
    """Make the local copy reflect a change that Jira accepted."""
    t, table = p["type"], p["type"]["table"]
    if p["action"] == "DELETE":
        db.execute(f'DELETE FROM "_orig_{table}" WHERE _id = ?', (p["id"],))
        db.commit()
        return
    try:
        remote = client.get_object(p["id"])
    except ApiError:
        remote = None
    if remote is not None:
        row = row_from_object(t, remote)
    else:  # fall back to what we sent
        row = dict(db.execute(f'SELECT * FROM "{table}" WHERE rowid = ?', (p["rowid"],)).fetchone())
        row["_id"] = p["id"]
    write_row(db, table, row, rowid=p["rowid"])
    if p["action"] == "CREATE":
        write_row(db, f"_orig_{table}", row)
    else:
        db.execute(f'DELETE FROM "_orig_{table}" WHERE _id = ?', (p["id"],))
        write_row(db, f"_orig_{table}", row)
    db.commit()


def rebase(db, refreshed):
    """Move the base of refreshed UPDATEs to the live Jira state and re-apply local edits on top
    (like `git rebase`), so remote edits to other fields are not reverted by the next plan."""
    for p, remote in refreshed:
        table = p["type"]["table"]
        if remote is None:  # already deleted in Jira
            db.execute(f'DELETE FROM "_orig_{table}" WHERE _id = ?', (p["id"],))
            continue
        base = row_from_object(p["type"], remote)
        live = dict(db.execute(f'SELECT * FROM "{table}" WHERE rowid = ?', (p["rowid"],)).fetchone())
        row = dict(base)
        for k in p["changes"]:
            row[k] = live[k]
            if k + "_key" in live:
                row[k + "_key"] = live[k + "_key"]
        write_row(db, table, row, rowid=p["rowid"])
        db.execute(f'DELETE FROM "_orig_{table}" WHERE _id = ?', (p["id"],))
        write_row(db, f"_orig_{table}", base)
    db.commit()


def _log(cfg, entry):
    os.makedirs(os.path.dirname(cfg.log_path), mode=0o700, exist_ok=True)
    fd = os.open(cfg.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with open(fd, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
