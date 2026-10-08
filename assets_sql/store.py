"""Local SQLite copy of an object schema.

Every object type becomes a table plus a twin `_orig_<table>` holding the last synced state; the diff between
the two is the plan. Technical columns start with `_`; a reference column `x` gets a twin `x_key` (object keys).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor

KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*-\d+")
TECH = ("_id", "_type")


def snake(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "col"


# ───────── metadata ─────────

def load_meta(client, schema):
    types = client.object_types(schema)
    with ThreadPoolExecutor(6) as ex:
        attrs_by_type = list(ex.map(lambda t: client.attributes(t["id"]), types))
    meta, used = [], {"_meta"}
    for t, attrs in zip(types, attrs_by_type):
        name = snake(t.get("displayName") or t["name"])
        if name in used:
            name = snake(t["name"])
        if name in used:
            name = f"{name}_{t['id']}"
        used.add(name)
        cols, taken = [], set(TECH)
        for a in attrs:
            kind = {0: (a.get("defaultType") or {}).get("name", "Text"), 1: "Reference", 2: "User"}.get(a["type"], f"type{a['type']}")
            col = snake(a["name"])
            if col in taken or (kind == "Reference" and col + "_key" in taken):
                col = f"{col}_{a['id']}"
            taken.add(col)
            if kind == "Reference":
                taken.add(col + "_key")
            cols.append({"id": a["id"], "name": a["name"], "col": col, "kind": kind,
                         "ref": a.get("referenceObjectTypeId"),
                         "editable": bool(a.get("editable", False)) and not a.get("system"),
                         "options": [o.strip() for o in (a.get("options") or "").split(",") if o.strip()],
                         "multi": (a.get("maximumCardinality") or 1) != 1,
                         "required": (a.get("minimumCardinality") or 0) > 0,
                         "label": bool(a.get("label", False))})
        meta.append({"id": t["id"], "name": t["name"], "table": name, "cols": cols})
    return meta


def table_of(meta, type_id):
    return next((t for t in meta if str(t["id"]) == str(type_id)), None)


def label_col(t):
    return next((c for c in t["cols"] if c["label"]), None) or next((c for c in t["cols"] if c["col"] == "name"), None)


# ───────── API object → row ─────────

def cell_value(col, vals):
    if not vals:
        return None
    if col["kind"] == "Reference":
        items = [v.get("displayValue") for v in vals]
    elif col["kind"] == "User":
        items = [_email(v.get("displayValue", "")) for v in vals]
    elif col["kind"] == "Boolean":
        return 1 if str(vals[0].get("value")).lower() == "true" else 0
    else:
        items = [v.get("value", v.get("displayValue")) for v in vals]
    return ", ".join(str(i) for i in items) if len(items) > 1 else items[0]


def _email(display):
    m = re.search(r"\(([^()]+@[^()]+)\)\s*$", display)
    return m.group(1) if m else display


def ref_keys(vals):
    return ", ".join(v.get("searchValue", "") for v in vals) if vals else None


def row_from_object(t, o):
    by = {str(a["objectTypeAttributeId"]): a.get("objectAttributeValues") for a in o.get("attributes", [])}
    row = {"_id": str(o["id"]), "_type": str(t["id"])}
    for c in t["cols"]:
        vals = by.get(str(c["id"]))
        row[c["col"]] = cell_value(c, vals)
        if c["kind"] == "Reference":
            row[c["col"] + "_key"] = ref_keys(vals)
    return row


# ───────── sqlite ─────────

def columns(t):
    out = list(TECH)
    for c in t["cols"]:
        out.append(c["col"])
        if c["kind"] == "Reference":
            out.append(c["col"] + "_key")
    return out


def ddl(t, prefix=""):
    cols = ['"_id" TEXT', '"_type" TEXT']
    for c in t["cols"]:
        typ = "INTEGER" if c["kind"] in ("Boolean", "Integer") else ("REAL" if c["kind"] == "Double" else "TEXT COLLATE NOCASE")
        cols.append(f'"{c["col"]}" {typ}')
        if c["kind"] == "Reference":
            cols.append(f'"{c["col"]}_key" TEXT COLLATE NOCASE')
    return f'CREATE TABLE "{prefix}{t["table"]}" ({", ".join(cols)})'


def write_row(db, table, row, rowid=None):
    """Insert row (or replace the row with `rowid`) into `table`."""
    keys = list(row)
    names = ", ".join(f'"{k}"' for k in keys)
    if rowid is None:
        db.execute(f'INSERT INTO "{table}" ({names}) VALUES ({", ".join("?" * len(keys))})', list(row.values()))
    else:
        sets = ", ".join(f'"{k}" = ?' for k in keys)
        db.execute(f'UPDATE "{table}" SET {sets} WHERE rowid = ?', list(row.values()) + [rowid])


def sync(db, client, schema):
    meta = load_meta(client, schema)
    with ThreadPoolExecutor(6) as ex:
        objs = list(ex.map(lambda t: client.aql_all(f"objectTypeId = {int(t['id'])}"), meta))
    unguard(db)
    cur = db.cursor()
    for name, kind in cur.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'").fetchall():
        cur.execute(f'DROP {"VIEW" if kind == "view" else "TABLE"} IF EXISTS "{name}"')
    cur.execute('CREATE TABLE "_meta" (k TEXT PRIMARY KEY, v TEXT)')
    total = 0
    for t, objects in zip(meta, objs):
        for prefix in ("", "_orig_"):
            cur.execute(ddl(t, prefix))
        for o in objects:
            row = row_from_object(t, o)
            for prefix in ("", "_orig_"):
                write_row(cur, prefix + t["table"], row)
            total += 1
    cur.execute("INSERT INTO _meta VALUES ('meta', ?), ('schema', ?), ('synced', ?)",
                (json.dumps(meta), str(schema), datetime.datetime.now().isoformat(timespec="seconds")))
    db.commit()
    return meta, total


def open_db(cfg, client, schema, force_sync=False, log=print):
    os.makedirs(cfg.home, mode=0o700, exist_ok=True)
    path = cfg.db_path(schema)
    db = sqlite3.connect(path)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    if force_sync or not db.execute("SELECT 1 FROM sqlite_master WHERE name='_meta'").fetchone():
        log(f"Syncing schema {schema}…")
        _, n = sync(db, client, schema)
        log(f"{n} objects.")
    return db, load_local_meta(db)


def load_local_meta(db):
    return json.loads(db.execute("SELECT v FROM _meta WHERE k='meta'").fetchone()[0])


def reset(db, meta):
    for t in meta:
        db.execute(f'DELETE FROM "{t["table"]}"')
        db.execute(f'INSERT INTO "{t["table"]}" SELECT * FROM "_orig_{t["table"]}"')
    db.commit()


# ───────── guard for user SQL ─────────

def unguard(db):
    """Remove the guard. (set_authorizer(None) only works on Python 3.11+.)"""
    db.set_authorizer(lambda *a: sqlite3.SQLITE_OK)


_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_TRANSACTION,
            getattr(sqlite3, "SQLITE_RECURSIVE", 33)}
_WRITES = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}


def guard(meta, writable):
    """sqlite authorizer for statements typed by the user.

    Allows reads everywhere and INSERT/UPDATE/DELETE only on object tables of a writable schema; technical
    columns (`_id`, `_type`) and tables (`_orig_*`, `_meta`) can't be modified; DDL, ATTACH, PRAGMA are denied.
    """
    tables = {t["table"] for t in meta}

    def auth(action, arg1, arg2, dbname, source):
        if action in _ALLOWED:
            return sqlite3.SQLITE_OK
        if action in _WRITES and writable and arg1 in tables and dbname in (None, "main"):
            if action == sqlite3.SQLITE_UPDATE and arg2 in TECH:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    return auth
