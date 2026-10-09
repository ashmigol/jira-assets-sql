"""assets — SQL shell over Jira Assets with Terraform-style plan / apply.

  assets init [--advanced]    set up: Jira site, email, API token, schemas (saved to ~/.config/assets)
  assets                      interactive shell (default schema)
  assets --schema 42          another schema (read-only unless listed in ASSETS_WRITABLE_SCHEMAS)
  assets -c "SELECT ..."      run one statement against the local copy and exit
  --no-sync                   don't refresh the local copy on start (shell / -c); also ASSETS_AUTO_SYNC=0
  assets sync                 refresh the local copy from Jira
  assets plan                 show unapplied local changes
  assets apply [--allow-delete] [--yes]
                              check against live Jira and push local changes

Configuration: `assets init`, or environment variables with the same names (they take precedence), see README.
"""
from __future__ import annotations

import argparse
import os
import sys

from . import store
from .api import ApiError, Client
from .config import Config, ConfigError, check_schema
from .plan import _ask, apply, compute_plan, open_fresh, show_plan
from .shell import handle_sql, shell


def parse(argv):
    p = argparse.ArgumentParser(prog="assets", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--schema", help="object schema id (default: ASSETS_SCHEMA)")
    p.add_argument("-c", dest="sql", metavar="SQL", help="run one SQL statement and exit")
    p.add_argument("command", nargs="?", choices=["shell", "init", "sync", "plan", "apply"], default="shell")
    p.add_argument("--allow-delete", action="store_true", help="apply: allow DELETEs")
    p.add_argument("--advanced", action="store_true", help="init: also ask for workspace id, data folder, auto-sync")
    p.add_argument("--no-sync", action="store_true", help="don't sync from Jira on start")
    p.add_argument("--yes", "-y", action="store_true", help="apply: don't ask for confirmation")
    return p.parse_args(argv)


def main(argv=None, env=None):
    args = parse(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    if args.command == "init":
        from . import init
        try:
            init.run(env=env, advanced=args.advanced)
        except (init.Aborted, ConfigError, OSError) as e:
            sys.exit(f"assets init: {e}")
        except (KeyboardInterrupt, EOFError):
            sys.exit("\nassets init: cancelled, nothing saved")
        return
    try:
        cfg = Config.load(env)
        schema = check_schema(args.schema) if args.schema else cfg.schema
        if not schema:
            raise ConfigError("no schema: pass --schema <id> or set ASSETS_SCHEMA")
    except ConfigError as e:
        sys.exit(f"assets: {e}")
    client = Client(cfg)
    auto_sync = cfg.auto_sync and not args.no_sync
    try:
        if args.sql:
            db, meta = open_fresh(cfg, client, schema) if auto_sync else store.open_db(cfg, client, schema)
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
            shell(cfg, client, schema, auto_sync=auto_sync)
    except ApiError as e:
        sys.exit(f"assets: {e}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
