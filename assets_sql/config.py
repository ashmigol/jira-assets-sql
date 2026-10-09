"""Configuration from environment variables. Nothing tenant-specific is hardcoded."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional


class ConfigError(Exception):
    pass


KEYS = ("JIRA_SITE", "JIRA_EMAIL", "JIRA_API_TOKEN", "ASSETS_WORKSPACE_ID", "ASSETS_SCHEMA", "ASSETS_WRITABLE_SCHEMAS",
        "ASSETS_HOME", "ASSETS_AUTO_SYNC")


def normalize_site(site: str) -> str:
    site = re.sub(r"^https?://", "", site.strip()).split("/")[0]
    return f"https://{site}" if site else ""


def config_path(env: Mapping[str, str] = os.environ) -> Optional[str]:
    """$ASSETS_CONFIG, else $XDG_CONFIG_HOME/assets/config, else $HOME/.config/assets/config."""
    if env.get("ASSETS_CONFIG"):
        return os.path.expanduser(env["ASSETS_CONFIG"])
    base = env.get("XDG_CONFIG_HOME") or (os.path.join(env["HOME"], ".config") if env.get("HOME") else None)
    return os.path.join(base, "assets", "config") if base else None


def read_file(path: Optional[str]) -> dict:
    """KEY=VALUE lines (same names as the environment variables); # comments allowed."""
    out = {}
    if not path or not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() in KEYS:
                    out[k.strip()] = v.strip()
    return out


def write_file(path: str, values: dict) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write("# jira-assets-sql — written by `assets init`; environment variables with the same names take precedence\n")
        for k in KEYS:
            if values.get(k) not in (None, ""):
                f.write(f"{k}={values[k]}\n")


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
    auto_sync: bool = True               # sync on start unless there are unapplied local changes

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
            raise ConfigError(f"not configured: run `assets init` (missing {', '.join(missing)})")
        site = normalize_site(env["JIRA_SITE"])
        writable = frozenset(check_schema(s.strip()) for s in env.get("ASSETS_WRITABLE_SCHEMAS", "").split(",") if s.strip())
        schema = env.get("ASSETS_SCHEMA", "").strip() or None
        return cls(
            site=site,
            email=env["JIRA_EMAIL"].strip(),
            token=env["JIRA_API_TOKEN"].strip(),
            workspace_id=env.get("ASSETS_WORKSPACE_ID", "").strip() or None,
            schema=check_schema(schema) if schema else None,
            writable_schemas=writable,
            auto_sync=env.get("ASSETS_AUTO_SYNC", "1").strip().lower() not in ("0", "false", "no", "off"),
            home=os.path.expanduser(env.get("ASSETS_HOME", "").strip() or "~/.local/share/assets"),
        )

    @classmethod
    def load(cls, env: Mapping[str, str] = os.environ) -> "Config":
        """Config file (from `assets init`) overlaid by non-empty environment variables; token from env, the file
        store or the macOS Keychain."""
        from . import tokens
        path = config_path(env)
        values = read_file(path)
        values.update({k: v for k, v in env.items() if k in KEYS and v.strip()})
        if not values.get("JIRA_API_TOKEN"):
            site, email = normalize_site(values.get("JIRA_SITE", "")), values.get("JIRA_EMAIL", "").strip()
            token = (tokens.file_get(path) if path else None) or tokens.keychain_get(site, email)
            if token:
                values["JIRA_API_TOKEN"] = token
        return cls.from_env(values)
