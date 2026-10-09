"""`assets init` — interactive setup: asks for the connection and schemas, checks them, saves the config."""
from __future__ import annotations

import getpass
import os

from . import tokens
from .api import ApiError, Client, TransportError
from .config import KEYS, Config, ConfigError, config_path, normalize_site, read_file, write_file

TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"


class Aborted(Exception):
    pass


def _schema_hint(site):
    return f"{site or 'https://hogwarts.atlassian.net'}/jira/assets/object-schema/<THIS_NUMBER>"


def run(env=os.environ, ask=input, ask_secret=getpass.getpass, client_factory=Client, advanced=False,
        keychain=None, out=print):
    path = config_path(env)
    if not path:
        raise ConfigError("cannot find a config location: set HOME or ASSETS_CONFIG")
    cur = read_file(path)
    keychain = tokens.keychain_available() if keychain is None else keychain

    def prompt(label, default="", hint=""):
        suffix = f" [{hint or default}]" if (hint or default) else ""
        v = ask(f"{label}{suffix}: ").strip()
        return v or default

    # ── connection: site, email, token — checked together ──
    site = normalize_site(cur.get("JIRA_SITE", ""))
    email = cur.get("JIRA_EMAIL", "")
    old_token = (tokens.file_get(path) or (tokens.keychain_get(site, email) if keychain else None)) if site and email else None
    for attempt in range(3):
        site = ""
        while not site:
            site = normalize_site(prompt("Jira site (example https://hogwarts.atlassian.net)", normalize_site(cur.get("JIRA_SITE", ""))))
        email = ""
        while "@" not in email:
            email = prompt("Email", cur.get("JIRA_EMAIL", ""))
        token = ask_secret(f"API token (create at {TOKEN_URL})" + (" [Enter = keep current]" if old_token else "") + ": ").strip()
        token = token or old_token or ""
        if not token:
            out("  ✗ the token is required")
            continue
        cfg = Config(site=site, email=email, token=token, workspace_id=cur.get("ASSETS_WORKSPACE_ID") or None)
        client = client_factory(cfg)
        out("  Checking…")
        try:
            me = client.myself()
            workspace = client.workspace
            schemas = client.schemas()
            break
        except TransportError as e:
            out(f"  ✗ cannot reach {site}: {e}")
        except ApiError as e:
            if e.status in (401, 403) and attempt < 2:
                out("  ✗ wrong email or API token" if e.status == 401 else f"  ✗ no access: {e}")
            else:
                out(f"  ✗ {e}")
    else:
        raise Aborted("could not connect; nothing saved")
    out(f"  ✓ logged in as {me.get('displayName', email)}; Assets workspace {workspace}; {len(schemas)} schema(s) visible")

    by_id = {str(s["id"]): s for s in schemas}
    by_key = {str(s.get("objectSchemaKey", "")).upper(): s for s in schemas if s.get("objectSchemaKey")}

    def resolve(v):
        s = by_id.get(v) or by_key.get(v.upper())
        return str(s["id"]) if s else None

    def list_schemas():
        out("  Schemas you can see:")
        for s in schemas:
            out(f"    {str(s['id']):>6}  {s.get('objectSchemaKey', ''):<10} {s.get('name', '')}")

    # ── default schema ──
    while True:
        cur_schema = cur.get("ASSETS_SCHEMA", "")
        v = prompt(f"Default schema ID ({_schema_hint(site)})", cur_schema, hint=f"{cur_schema}, '-' = none" if cur_schema else "")
        if not v or v == "-":
            schema = ""
            break
        schema = resolve(v)
        if schema:
            break
        out(f"  ✗ no schema {v!r} (or no access)")
        list_schemas()

    # ── writable schemas ──
    while True:
        cur_w = cur.get("ASSETS_WRITABLE_SCHEMAS", "")
        v = prompt(f"Writable schema ID ({_schema_hint(site)})", cur_w,
                   hint=f"{cur_w}, '-' = none (read-only)" if cur_w else "empty = read-only")
        if v == "-":
            v = ""
        parts = [p.strip() for p in v.split(",") if p.strip()]
        ids = [resolve(p) for p in parts]
        bad = [p for p, i in zip(parts, ids) if not i]
        if not bad:
            writable = ",".join(ids)
            break
        out(f"  ✗ no schema {', '.join(bad)} (or no access)")
        list_schemas()

    values = {k: cur.get(k, "") for k in KEYS if k != "JIRA_API_TOKEN"}
    values.update(JIRA_SITE=site, JIRA_EMAIL=email, ASSETS_SCHEMA=schema, ASSETS_WRITABLE_SCHEMAS=writable,
                  ASSETS_WORKSPACE_ID=cur.get("ASSETS_WORKSPACE_ID", ""))
    if advanced:
        values["ASSETS_WORKSPACE_ID"] = prompt("Assets workspace ID", workspace)
        values["ASSETS_HOME"] = prompt("Data folder (local copies, log, backups)", cur.get("ASSETS_HOME", "") or "~/.local/share/assets")
        sync = prompt("Sync from Jira on start (y/n)", "n" if cur.get("ASSETS_AUTO_SYNC", "1") in ("0", "false", "no", "off") else "y")
        values["ASSETS_AUTO_SYNC"] = "0" if sync.lower().startswith("n") else ""

    # ── save ──
    write_file(path, values)
    if keychain:
        tokens.keychain_set(site, email, token)
        where = "token in the system keychain"
    else:
        where = f"token in {tokens.file_set(path, token)} (mode 600)"
    out(f"  ✓ Saved: {path}, {where}")
    out(f"  Default schema: {schema or '—'}; writable: {writable or 'none (read-only)'}")
    shadow = [k for k in KEYS if env.get(k, "").strip()]
    if shadow:
        out(f"  ! These environment variables are set and override the config: {', '.join(shadow)}")
    out("  Run: assets")
    return values
