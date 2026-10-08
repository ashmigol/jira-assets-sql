# jira-assets-sql

A SQL shell over **Jira Service Management Assets** (cloud) with Terraform-style `plan` / `apply`.

It pulls an object schema into a local SQLite file (one table per object type), lets you query and edit it
with plain SQL, and pushes your edits back to Jira only after an explicit `plan` → `apply`. Before applying,
it checks every change against the live state in Jira, so it won't overwrite someone else's edit.

```
$ assets
Assets schema 42. Local copy synced 2026-10-08T12:00:00. Tables: systems, roles
assets[42]> SELECT name, system, google_group FROM roles WHERE google_group IS NULL;
assets[42]> UPDATE roles SET google_group = 'crm-admins@example.com' WHERE name = 'Admin' AND system = 'CRM';
OK (1 row changed locally — .plan / .apply to push)
assets[42]> .plan
┌────────┬───────┬────────────────────┬──────────────────────────────────────┐
│ action │ table │ object             │ changes                              │
├────────┼───────┼────────────────────┼──────────────────────────────────────┤
│ UPDATE │ roles │ Admin (ITSM-1234)  │ google_group: ∅ → crm-admins@exam…   │
└────────┴───────┴────────────────────┴──────────────────────────────────────┘
assets[42]> .apply
Checking current state in Jira…
Apply 1 change(s) to schema 42? [y/N] y
Backup: ~/.local/share/assets/backups/assets-42-20261008-120102.json
  ✓ UPDATE roles Admin (ITSM-1234)
1/1 applied.
```

Python ≥ 3.9, no dependencies.

## Install

```sh
pipx install git+https://github.com/<owner>/jira-assets-sql   # or: pip install -e .
cp .env.example .env   # fill in, then export the variables in your shell profile
```

## Configuration

All configuration comes from environment variables.

| Variable | Required | Meaning |
|---|---|---|
| `JIRA_SITE` | yes | `https://your-site.atlassian.net` |
| `JIRA_EMAIL` | yes | your Atlassian account email |
| `JIRA_API_TOKEN` | yes | [API token](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `ASSETS_SCHEMA` | no | default object schema id (the number in `…/object-schema/<id>` URLs) |
| `ASSETS_WRITABLE_SCHEMAS` | no | comma-separated schema ids that `apply` may change. **Empty = everything is read-only.** |
| `ASSETS_WORKSPACE_ID` | no | auto-discovered from the site |
| `ASSETS_HOME` | no | local copies, change log, backups, shell history (default `~/.local/share/assets`) |

The write allowlist is the main safety switch: a schema that isn't in `ASSETS_WRITABLE_SCHEMAS` can be queried
but never modified, neither locally nor in Jira. Your Jira permissions still apply on top of it.

## Usage

```
assets                         interactive shell
assets --schema 42             another schema
assets -c "SELECT ..."         one statement against the local copy
assets sync                    refresh the local copy from Jira
assets plan                    show unapplied local changes
assets apply [--allow-delete] [--yes]
```

Shell commands: `.tables`, `.schema [table]`, `.sync`, `.plan`, `.apply [--allow-delete]`, `.reset`,
`.aql <query>` (live AQL), `.mode table|csv|vertical`, `.pager on|off`, `.help`. End a query with `\G` instead
of `;` for vertical output. MySQL-style `show tables;` and `describe <table>;` work too.

### Table model

* Object type → table, attribute → column, both in `snake_case`.
* Reference attribute `system` → column `system` (label of the referenced object) plus `system_key` (its key).
  To change a reference, set either one. A key in the label column (`system = 'ITSM-17'`) also works, which helps
  when labels are ambiguous.
* User attributes are emails, booleans are `1`/`0` (`'true'`/`'false'` are accepted), multi-value attributes are
  comma-separated.
* New object: `INSERT` a row and leave `_id` NULL. New objects can reference each other by label; they are
  created in dependency order.
* Columns starting with `_` and the shadow tables `_orig_*`/`_meta` are technical and read-only. DDL, `ATTACH`
  and `PRAGMA` are blocked.

### What `apply` does

1. **Plan:** diff between your local copy and the last sync.
2. **Validate** locally: select options, user emails, references (exactly one match), required fields, cardinality.
3. **Refresh:** a 3-way check of each change. *base* is the value at the last sync, *local* is your edit,
   *remote* is the value in Jira now:
   * a field changed in Jira that you don't touch → fine, your change is applied on top;
   * the same field changed in Jira to a different value → **conflict**, nothing is applied;
   * Jira already has your value → the change is dropped;
   * delete of an object edited in Jira → conflict; `CREATE` of a label that already exists → conflict.
4. **Confirm** (`--yes` skips the prompt), then **back up** the current Jira state of every touched object
   to `backups/`.
5. **Execute.** Each successful change is written back to the local copy immediately. Failed changes stay in the
   plan. After a network error it stops, and the next run detects an object that was created anyway.
   Every change is appended to `changes.log`.

DELETEs are refused unless `--allow-delete` is passed.

## Local data

`ASSETS_HOME` contains object data (including user emails), backups and logs. Files are created with `0600`
permissions and are git-ignored; don't commit them.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q && .venv/bin/ruff check .
```

The tests run against an in-memory fake of the Assets API (`tests/conftest.py`) and make no network calls.

## Limitations / roadmap

* Objects only: object types and attributes (schema structure) are read, not changed.
* Planned: saved plan files (`plan -out` / `apply <file>`), declarative mode (export to YAML, review in a PR,
  `plan`/`apply` from files), and scheduled drift detection in CI.
