"""Context-aware SQL suggestions for the shell (pure functions, no UI).

`suggest(ctx, text)` → list of Suggestion for the text before the cursor (the whole statement so far);
`ghost(ctx, text)` → inline grey text to accept with → (e.g. the column list after `INSERT INTO roles `);
`columns_hint(ctx, text)` → one-line summary of the columns of the table the statement is about.
"""
from __future__ import annotations

import re
from typing import NamedTuple

KEYWORDS = ("SELECT FROM WHERE JOIN LEFT ON AND OR NOT NULL IS IN LIKE ORDER BY GROUP HAVING LIMIT AS DISTINCT COUNT "
            "INSERT INTO VALUES UPDATE SET DELETE").split()
DOT_COMMANDS = (".help .tables .schema .sync .plan .apply .reset .aql .mode .pager .history .log .quit").split()
TECH_HIDDEN = ("_id", "_type")

TABLE_CTX = re.compile(r"\b(?:from|join|into|update|describe|desc|table)\s+(\w*)$", re.I)
TABLE_REF = re.compile(r"\b(?:from|join|into|update|describe|desc)\s+\"?(\w+)", re.I)
INSERT_HEAD = re.compile(r"^\s*insert\s+into\s+\"?(\w+)\"?\s+$", re.I)
INSERT_COLS = re.compile(r"^\s*insert\s+into\s+\"?(\w+)\"?\s*\(([^)]*)$", re.I)
VALUE_CTX = re.compile(r"\"?(\w+)\"?\s*(?:=|!=|<>|like|in\s*\(\s*(?:'[^']*'\s*,\s*)*)\s*'([^']*)$", re.I)
WORD = re.compile(r"[\w.]*$")


class Suggestion(NamedTuple):
    text: str      # what gets inserted
    replace: int   # how many chars before the cursor it replaces
    meta: str = ""  # shown next to it in the menu


class Context:
    """What the completer knows: tables/columns from meta, plus value lists from the local copy."""

    def __init__(self, meta, db=None):
        self.meta = meta
        self.tables = {t["table"]: t for t in meta}
        self.db = db
        self._values = {}

    def reset(self, meta):
        """After .sync / .apply: new metadata, forget cached values."""
        self.__init__(meta, self.db)

    def cols(self, table):
        t = self.tables.get(table)
        if not t:
            return []
        out = []
        for c in t["cols"]:
            out.append(c)
            if c["kind"] == "Reference":
                out.append({**c, "col": c["col"] + "_key", "kind": "Key", "editable": c["editable"], "required": False, "options": []})
        return out

    def editable_cols(self, table):
        return [c for c in self.cols(table) if c["editable"] and c["kind"] != "Key"]

    def values(self, table, col):
        """Known values for a column: select options, labels of referenced objects, emails seen in user columns."""
        key = (table, col)
        if key in self._values:
            return self._values[key]
        c = next((x for x in self.cols(table) if x["col"] == col), None) if table else None
        if c is None:  # table unknown: look the column up anywhere
            c = next((x for t in self.tables for x in self.cols(t) if x["col"] == col), None)
        vals = []
        if c:
            if c["options"]:
                vals = list(c["options"])
            elif c["kind"] == "Reference" and self.db is not None:
                rt = next((t for t in self.meta if str(t["id"]) == str(c["ref"])), None)
                lc = next((x["col"] for x in rt["cols"] if x.get("label")), "name") if rt else None
                if rt and lc:
                    vals = [r[0] for r in self.db.execute(f'SELECT DISTINCT "{lc}" FROM "{rt["table"]}" WHERE "{lc}" IS NOT NULL ORDER BY 1')]
            elif c["kind"] == "User" and self.db is not None:
                emails = set()
                for t in self.meta:
                    for u in (x for x in t["cols"] if x["kind"] == "User"):
                        for (v,) in self.db.execute(f'SELECT DISTINCT "{u["col"]}" FROM "{t["table"]}" WHERE "{u["col"]}" IS NOT NULL'):
                            emails.update(e.strip() for e in str(v).split(","))
                vals = sorted(emails)
            elif c["kind"] == "Boolean":
                vals = ["1", "0"]
            elif self.db is not None and table:
                vals = [str(r[0]) for r in self.db.execute(
                    f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL ORDER BY 1 LIMIT 50')]
        self._values[key] = vals
        return vals


def kind_label(c):
    return {"Reference": "ref", "Boolean": "bool", "User": "user", "Select": "select", "Key": "key"}.get(c["kind"], c["kind"].lower())


def current_table(ctx, text):
    """The last table referenced in the statement (FROM/JOIN/INTO/UPDATE …)."""
    names = [m.group(1) for m in TABLE_REF.finditer(text) if m.group(1) in ctx.tables]
    return names[-1] if names else None


def _starts(word, candidates, meta=""):
    w = word.lower()
    return [Suggestion(c, len(word), meta) for c in candidates if c.lower().startswith(w) and c.lower() != w]


def suggest(ctx, text):
    stripped = text.lstrip()
    if stripped.startswith(".") and "\n" not in stripped and " " not in stripped:
        return _starts(stripped, DOT_COMMANDS)
    if re.match(r"^\.(schema|describe)\s+\w*$", stripped):
        word = WORD.search(text).group()
        return _starts(word, list(ctx.tables), "table")

    m = VALUE_CTX.search(text)
    if m:
        col, typed = m.group(1), m.group(2)
        table = current_table(ctx, text)
        vals = ctx.values(table, col)
        t = typed.lower()
        hits = [v for v in vals if t in str(v).lower() and str(v) != typed]
        hits.sort(key=lambda v: (not str(v).lower().startswith(t), str(v).lower()))
        return [Suggestion(f"{v}'", len(typed), col) for v in hits[:30]]

    m = INSERT_HEAD.match(text)
    if m and m.group(1) in ctx.tables:
        cols = ", ".join(c["col"] for c in ctx.editable_cols(m.group(1)))
        return [Suggestion(f"({cols}) VALUES (", 0, "all editable columns")]

    m = INSERT_COLS.match(text)
    if m and m.group(1) in ctx.tables:
        word = WORD.search(text).group()
        used = {w.strip().strip('"') for w in m.group(2).split(",")[:-1]}
        return [Suggestion(c["col"], len(word), kind_label(c)) for c in ctx.editable_cols(m.group(1))
                if c["col"] not in used and c["col"].lower().startswith(word.lower())]

    m = TABLE_CTX.search(text)
    if m:
        return _starts(m.group(1), list(ctx.tables), "table")

    word = WORD.search(text).group()
    if not word:
        return []
    if "." in word:  # table.column
        tname, _, part = word.partition(".")
        return [Suggestion(c["col"], len(part), kind_label(c)) for c in ctx.cols(tname)
                if c["col"].lower().startswith(part.lower()) and c["col"].lower() != part.lower()]
    table = current_table(ctx, text)
    cols = [c for c in (ctx.cols(table) if table else [c for t in ctx.tables for c in ctx.cols(t)]) if c["col"] not in TECH_HIDDEN]
    seen, out = set(), []
    for c in cols:
        if c["col"] not in seen and c["col"].lower().startswith(word.lower()) and c["col"].lower() != word.lower():
            seen.add(c["col"])
            out.append(Suggestion(c["col"], len(word), kind_label(c)))
    upper = word.isupper()
    out += [Suggestion(k if upper else k.lower(), len(word), "keyword") for k in KEYWORDS
            if k.lower().startswith(word.lower()) and k.lower() != word.lower()]
    out += _starts(word, list(ctx.tables), "table")
    return out


def ghost(ctx, text):
    """Inline suggestion (grey text after the cursor), or None."""
    m = INSERT_HEAD.match(text)
    if m and m.group(1) in ctx.tables:
        return "(" + ", ".join(c["col"] for c in ctx.editable_cols(m.group(1))) + ") VALUES ("
    m = INSERT_COLS.match(text)
    if m and m.group(1) in ctx.tables and text.rstrip().endswith("("):
        return ", ".join(c["col"] for c in ctx.editable_cols(m.group(1))) + ") VALUES ("
    hits = suggest(ctx, text)
    if len(hits) == 1 and hits[0].replace > 0:  # unambiguous prefix: show the rest
        s = hits[0]
        typed = text[len(text) - s.replace:]
        if s.text.lower().startswith(typed.lower()):
            return s.text[len(typed):]
    return None


def columns_hint(ctx, text):
    table = current_table(ctx, text)
    if not table:
        return "Tables: " + ", ".join(ctx.tables) if ctx.tables else ""
    parts = []
    for c in ctx.tables[table]["cols"]:
        if not c["editable"]:
            continue
        mark = "*" if c["required"] else ""
        kind = {"Reference": "→", "Boolean": ":bool", "User": ":user", "Select": ":select"}.get(c["kind"], "")
        if c["kind"] == "Reference":
            rt = next((t["table"] for t in ctx.meta if str(t["id"]) == str(c["ref"])), "?")
            kind = f"→{rt}"
        parts.append(f"{c['col']}{mark}{kind}")
    return f"{table}: " + "  ".join(parts)
