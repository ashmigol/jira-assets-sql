"""New fields (attributes) via `ALTER TABLE <table> ADD COLUMN …`.

The column is added to the local copy at once (so values can be set in the same plan) and marked `pending`
in the metadata; `.apply` creates the attribute in Jira first, then pushes the object changes.
"""
from __future__ import annotations

import re

from .store import snake

# SQL type word → (kind as Jira names it, Jira defaultTypeId)
TYPES = {
    "TEXT": ("Text", 0), "VARCHAR": ("Text", 0), "STRING": ("Text", 0),
    "TEXTAREA": ("Textarea", 9),
    "INTEGER": ("Integer", 1), "INT": ("Integer", 1),
    "REAL": ("Double", 3), "DOUBLE": ("Double", 3), "FLOAT": ("Double", 3),
    "BOOLEAN": ("Boolean", 2), "BOOL": ("Boolean", 2),
    "DATE": ("Date", 4),
    "DATETIME": ("DateTime", 6), "TIMESTAMP": ("DateTime", 6),
    "URL": ("URL", 7),
    "EMAIL": ("Email", 8),
    "USER": ("User", None),
}
TYPE_HELP = "TEXT, TEXTAREA, INTEGER, REAL, BOOLEAN, DATE, DATETIME, URL, EMAIL, USER, or REFERENCES <table>"

ALTER = re.compile(r"\s*alter\s+table\b", re.I)
ADD_COLUMN = re.compile(r"""\s*alter\s+table\s+"?(\w+)"?\s+add\s+(?:column\s+)?(?:"([^"]+)"|(\w+))
                            (?:\s+(\w+))?(?:\s+references\s+"?(\w+)"?)?\s*;?\s*""", re.I | re.X)


def parse_alter(sql):
    """`ALTER TABLE t ADD [COLUMN] name TYPE | REFERENCES t2` → (table, Jira name, kind, ref table or None)."""
    m = ADD_COLUMN.fullmatch(sql)
    if not m:
        raise ValueError("only `ALTER TABLE <table> ADD COLUMN <name> <type>` is supported "
                         "(rename and delete fields in Jira)")
    table, quoted, bare, typ, ref = m.groups()
    name = quoted.strip() if quoted else bare.replace("_", " ").strip().capitalize()
    if ref:
        if typ and typ.upper() not in ("REFERENCE", "TEXT"):
            raise ValueError(f"a reference column has no type of its own: use `{bare or quoted} REFERENCES {ref}`")
        return table, name, "Reference", ref
    if not typ:
        raise ValueError(f"column type missing: {TYPE_HELP}")
    if typ.upper() not in TYPES:
        raise ValueError(f"unsupported type {typ}: {TYPE_HELP}")
    return table, name, TYPES[typ.upper()][0], None


def new_column(t, name, kind, ref_type=None):
    """Column metadata for a field that does not exist in Jira yet."""
    if not name:
        raise ValueError("empty field name")
    col = snake(name)
    taken = {"_id", "_type"} | {c["col"] for c in t["cols"]} | {c["col"] + "_key" for c in t["cols"] if c["kind"] == "Reference"}
    if col in taken or (kind == "Reference" and col + "_key" in taken):
        raise ValueError(f"{t['table']} already has a column {col}")
    if any(c["name"].lower() == name.lower() for c in t["cols"]):
        raise ValueError(f"{t['table']} already has a field \"{name}\"")
    return {"id": None, "name": name, "col": col, "kind": kind, "ref": ref_type["id"] if ref_type else None,
            "editable": True, "options": [], "multi": False, "required": False, "label": False, "pending": True}


def describe(col, meta):
    if col["kind"] == "Reference":
        rt = next((t["table"] for t in meta if str(t["id"]) == str(col["ref"])), "?")
        return f'"{col["name"]}" → {rt}'
    return f'"{col["name"]}" {col["kind"]}'


def body(col, reference_types):
    """Request body for POST /objecttypeattribute/{objectTypeId}."""
    if col["kind"] == "Reference":
        if not reference_types:
            raise ValueError("no reference types in this schema")
        rt = next((r for r in reference_types if r.get("name") == "Reference"), reference_types[0])
        return {"name": col["name"], "type": 1, "typeValue": str(col["ref"]), "additionalValue": str(rt["id"])}
    if col["kind"] == "User":
        return {"name": col["name"], "type": 2}
    default_id = next(i for k, i in TYPES.values() if k == col["kind"])
    return {"name": col["name"], "type": 0, "defaultTypeId": default_id}
