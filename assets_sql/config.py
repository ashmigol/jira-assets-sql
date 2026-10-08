"""Configuration from environment variables. Nothing tenant-specific is hardcoded."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional


class ConfigError(Exception):
    pass


SCHEMA_RE = re.compile(r"\d+")


def check_schema(schema: str) -> str:
    if not SCHEMA_RE.fullmatch(str(schema)):
        raise ConfigError(f"invalid schema id {schema!r}: must be a number")
    return str(schema)


@dataclass(frozen=True)
class Config:
    site: str                            # https://<your-site>.atlassian.net
    email: str                           # Atlassian account email (for basic auth)
    token: str = field(repr=False)       # Atlassian API token
    workspace_id: Optional[str] = None   # discovered from the site when not set
    schema: Optional[str] = None         # default object schema id
    writable_schemas: frozenset = frozenset()  # empty = everything is read-only
    home: str = os.path.expanduser("~/.local/share/assets")

    @property
    def log_path(self) -> str:
        return os.path.join(self.home, "changes.log")

    @property
    def backup_dir(self) -> str:
        return os.path.join(self.home, "backups")

    @property
    def history_path(self) -> str:
        return os.path.join(self.home, "history")

    def db_path(self, schema: str) -> str:
        return os.path.join(self.home, f"schema_{check_schema(schema)}.db")

    def is_writable(self, schema: str) -> bool:
        return str(schema) in self.writable_schemas

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Config":
        missing = [k for k in ("JIRA_SITE", "JIRA_EMAIL", "JIRA_API_TOKEN") if not env.get(k)]
        if missing:
            raise ConfigError(f"missing environment variable(s): {', '.join(missing)} (see .env.example)")
        site = env["JIRA_SITE"].strip().rstrip("/")
        if not site.startswith("https://"):
            site = "https://" + site
        writable = frozenset(check_schema(s.strip()) for s in env.get("ASSETS_WRITABLE_SCHEMAS", "").split(",") if s.strip())
        schema = env.get("ASSETS_SCHEMA", "").strip() or None
        return cls(
            site=site,
            email=env["JIRA_EMAIL"].strip(),
            token=env["JIRA_API_TOKEN"].strip(),
            workspace_id=env.get("ASSETS_WORKSPACE_ID", "").strip() or None,
            schema=check_schema(schema) if schema else None,
            writable_schemas=writable,
            home=os.path.expanduser(env.get("ASSETS_HOME", "").strip() or "~/.local/share/assets"),
        )
