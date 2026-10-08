"""Interactive SQL shell."""
from __future__ import annotations

import re
import sqlite3

from . import output, store
from .api import ApiError
from .output import print_table
from .plan import apply, compute_plan, show_plan

try:
    import readline
except ImportError:  # Windows
    readline = None

HELP = """
SQL (end with ;)          SELECT / INSERT / UPDATE / DELETE on the local copy
.tables                   tables with row counts
.schema [table]           columns: name, kind, editable, options
.sync                     reload from Jira (drops unapplied local changes)
.plan                     show what .apply would send to Jira
.apply [--allow-delete]   check against live Jira, then push local changes (writable schemas only)
.reset                    discard local changes (restore last synced copy)
.aql <query>              run live AQL against Jira, e.g. .aql objectType = "Roles" AND Name LIKE "adm"
.mode table|csv|vertical  output format (or end a query with \\G instead of ; for vertical)
.pager on|off             wide/long tables open in `less -S` (←/→ scroll, q quit); default on
show tables;  describe <table>;  show columns from <table>;   MySQL-style shortcuts
.quit                     exit

Columns are snake_case. A reference column holds the label of the referenced object, <column>_key holds its
key; set either one (a key also works in the label column). Users = email. Booleans = 1/0 (true/false work too).
New objects: INSERT a row with _id NULL. Columns starting with _ are technical and read-only.
"""


def hidden(col):
    return col.startswith("_") or col.endswith("_key") or col in ("created", "updated")


def tables_summary(db, meta):
    print_table(["table", "type", "rows"], [[t["table"], t["name"], db.execute(f'SELECT count(*) FROM "{t["table"]}"').fetchone()[0]] for t in meta])


def describe(meta, name=None):
    found = False
    for t in meta:
        if name and t["table"] != name:
            continue
        found = True
        print(f"{t['table']}  (\"{t['name']}\", type {t['id']})")
        print_table(["column", "attribute", "kind", "editable", "options/ref"],
                    [[c["col"], c["name"], c["kind"] + (" [multi]" if c["multi"] else "") + (" *" if c["required"] else ""),
                      "yes" if c["editable"] else "",
                      ", ".join(c["options"]) or ((store.table_of(meta, c["ref"]) or {}).get("table", "") if c["ref"] else "")]
                     for c in t["cols"]])
    if name and not found:
        print(f"No table {name!r}.")


def run_sql(db, meta, sql, mode, writable):
    """Run one user statement under the authorizer guard."""
    db.set_authorizer(store.guard(meta, writable))
    try:
        cur = db.execute(sql)
        if cur.description:
            headers = [d[0] for d in cur.description]
            star = re.match(r"\s*select\s+\*", sql, re.I) is not None
            keep = [i for i, h in enumerate(headers) if not (star and hidden(h))]
            rows = cur.fetchall()
            print_table([headers[i] for i in keep], [[r[i] for i in keep] for r in rows], mode)
        else:
            db.commit()
            n = cur.rowcount
            print(f"OK ({n} row{'s' if n != 1 else ''} changed locally — .plan / .apply to push)")
    except sqlite3.DatabaseError as e:
        db.rollback()
        msg = str(e)
        if "not authorized" in msg:
            msg += " (read-only schema, technical table/column, or DDL/ATTACH/PRAGMA)" if writable else " (schema is read-only)"
        m = re.match(r"no such column: (.+)", msg)
        if m:
            msg += f"   (text values need single quotes: '{m.group(1)}')"
        print(f"SQL error: {msg}")
    finally:
        store.unguard(db)


def live_aql(client, meta, q, mode):
    try:
        d = client.aql(q, limit=200)
    except ApiError as e:
        print(e)
        return
    rows, headers, extra = [], ["key", "type", "name"], []
    for o in d.get("values", []):
        t = store.table_of(meta, o["objectType"]["id"])
        attrs = {str(a["objectTypeAttributeId"]): a["objectAttributeValues"] for a in o.get("attributes", [])}
        rec = {"key": o["objectKey"], "type": o["objectType"]["name"], "name": o["label"]}
        for c in (t["cols"] if t else []):
            if c["col"] in ("key", "name", "created", "updated"):
                continue
            rec[c["col"]] = store.cell_value(c, attrs.get(str(c["id"])))
            if c["col"] not in extra:
                extra.append(c["col"])
        rows.append(rec)
    headers += extra
    print_table(headers, [[r.get(h) for h in headers] for r in rows], mode)
    if not d.get("isLast", True):
        print(f"(showing first {len(rows)} of {d.get('total')})")


SHORT_DESCRIBE = re.compile(r"(?:describe|desc|show columns from|show columns in) (\w+)")


def handle_sql(db, meta, sql, mode, writable):
    short = re.sub(r"\s+", " ", sql.rstrip(";").strip()).lower()
    m = SHORT_DESCRIBE.fullmatch(short)
    if short in ("show tables", "show table"):
        tables_summary(db, meta)
    elif m:
        describe(meta, m.group(1))
    else:
        run_sql(db, meta, sql, mode, writable)


def shell(cfg, client, schema):
    db, meta = store.open_db(cfg, client, schema)
    writable = cfg.is_writable(schema)
    synced = db.execute("SELECT v FROM _meta WHERE k='synced'").fetchone()[0]
    print(f"Assets schema {schema}{'' if writable else ' — READ-ONLY'}. Local copy synced {synced}. "
          f"Tables: {', '.join(t['table'] for t in meta)}")
    pending = len(compute_plan(db, meta))
    if pending:
        print(f"{pending} unapplied local change(s) — .plan to review")
    print("Type .help for commands, SQL ends with ';'")
    if readline:
        try:
            readline.read_history_file(cfg.history_path)
        except OSError:
            pass
        readline.parse_and_bind("tab: complete")
        words = [t["table"] for t in meta] + [c["col"] for t in meta for c in t["cols"]] + \
            "SELECT FROM WHERE JOIN ON AND OR ORDER BY GROUP INSERT INTO VALUES UPDATE SET DELETE LIKE IS NULL NOT COUNT".split()
        readline.set_completer(lambda text, i: ([w for w in words if w.lower().startswith(text.lower())] + [None])[i])
    mode, buf = "table", ""
    while True:
        try:
            line = input(f"assets[{schema}]> " if not buf else "        ...> ")
        except (EOFError, KeyboardInterrupt):
            print()
            if buf:
                buf = ""
                continue
            break
        s = line.strip()
        if not buf and not s:
            continue
        if not buf and s.startswith("."):
            cmd, _, arg = s.partition(" ")
            arg = arg.strip()
            try:
                if cmd in (".quit", ".exit", ".q"):
                    break
                elif cmd == ".help":
                    print(HELP)
                elif cmd == ".tables":
                    tables_summary(db, meta)
                elif cmd == ".schema":
                    describe(meta, arg or None)
                elif cmd == ".sync":
                    meta, n = store.sync(db, client, schema)
                    print(f"Synced: {n} objects.")
                elif cmd == ".plan":
                    show_plan(compute_plan(db, meta))
                elif cmd == ".apply":
                    apply(cfg, client, db, meta, schema, allow_delete="--allow-delete" in arg)
                elif cmd == ".reset":
                    store.reset(db, meta)
                    print("Local changes discarded.")
                elif cmd == ".aql":
                    live_aql(client, meta, arg, mode)
                elif cmd == ".mode":
                    mode = arg if arg in ("table", "csv", "vertical") else mode
                    print(f"mode = {mode}")
                elif cmd == ".pager":
                    output.settings["pager"] = arg != "off"
                    print(f"pager = {'on' if output.settings['pager'] else 'off'}")
                else:
                    print("Unknown command. .help")
            except (ApiError, ValueError, sqlite3.Error, OSError) as e:
                print(f"Error: {e}")
            continue
        buf += line + "\n"
        vertical = buf.rstrip().endswith("\\G")
        if vertical:
            buf = buf.rstrip()[:-2] + ";"
        if sqlite3.complete_statement(buf):
            sql, buf = buf.strip(), ""
            handle_sql(db, meta, sql, "vertical" if vertical else mode, writable)
    if readline:
        try:
            readline.write_history_file(cfg.history_path)
        except OSError:
            pass
