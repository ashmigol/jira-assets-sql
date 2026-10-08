"""assets — SQL shell over Jira Assets with Terraform-style plan / apply.

  assets                      interactive shell (schema from ASSETS_SCHEMA)
  assets --schema 42          another schema (read-only unless listed in ASSETS_WRITABLE_SCHEMAS)
  assets -c "SELECT ..."      run one statement against the local copy and exit
  assets sync                 refresh the local copy from Jira
  assets plan                 show unapplied local changes
  assets apply [--allow-delete] [--yes]
                              check against live Jira and push local changes

Configuration: environment variables, see README / .env.example.
"""
from __future__ import annotations

import argparse
import sys

from . import store
from .api import ApiError, Client
from .config import Config, ConfigError, check_schema
from .plan import _ask, apply, compute_plan, show_plan
from .shell import handle_sql, shell


def parse(argv):
    p = argparse.ArgumentParser(prog="assets", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--schema", help="object schema id (default: ASSETS_SCHEMA)")
    p.add_argument("-c", dest="sql", metavar="SQL", help="run one SQL statement and exit")
    p.add_argument("command", nargs="?", choices=["shell", "sync", "plan", "apply"], default="shell")
    p.add_argument("--allow-delete", action="store_true", help="apply: allow DELETEs")
    p.add_argument("--yes", "-y", action="store_true", help="apply: don't ask for confirmation")
    return p.parse_args(argv)


def main(argv=None, env=None):
    args = parse(sys.argv[1:] if argv is None else argv)
    try:
        cfg = Config.from_env() if env is None else Config.from_env(env)
        schema = check_schema(args.schema) if args.schema else cfg.schema
        if not schema:
            raise ConfigError("no schema: pass --schema <id> or set ASSETS_SCHEMA")
    except ConfigError as e:
        sys.exit(f"assets: {e}")
    client = Client(cfg)
    try:
        if args.sql:
            db, meta = store.open_db(cfg, client, schema)
            handle_sql(db, meta, args.sql, "table", cfg.is_writable(schema))
        elif args.command == "sync":
            store.open_db(cfg, client, schema, force_sync=True)
        elif args.command == "plan":
            db, meta = store.open_db(cfg, client, schema)
            show_plan(compute_plan(db, meta))
        elif args.command == "apply":
            db, meta = store.open_db(cfg, client, schema)
            res = apply(cfg, client, db, meta, schema, allow_delete=args.allow_delete,
                        confirm=(lambda _: True) if args.yes else _ask)
            if res["aborted"] or res["failed"]:
                sys.exit(1)
        else:
            shell(cfg, client, schema)
    except ApiError as e:
        sys.exit(f"assets: {e}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
