"""API token storage: macOS Keychain when available, otherwise a 0600 file next to the config."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

SERVICE = "jira-assets-sql"


def _service(site):
    return f"{SERVICE} {site}"


def keychain_available():
    return sys.platform == "darwin" and shutil.which("security") is not None


def keychain_get(site, email):
    if not keychain_available() or not site or not email:
        return None
    r = subprocess.run(["security", "find-generic-password", "-s", _service(site), "-a", email, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None if r.returncode == 0 else None


def keychain_set(site, email, token):
    """Store via stdin (the token never appears in the process list)."""
    r = subprocess.run(["security", "add-generic-password", "-s", _service(site), "-a", email, "-U", "-w"],
                       input=f"{token}\n{token}\n", capture_output=True, text=True)
    if r.returncode != 0:
        raise OSError(f"Keychain: {r.stderr.strip() or r.returncode}")


def token_file(config_path):
    return os.path.join(os.path.dirname(config_path), "token")


def file_get(config_path):
    try:
        with open(token_file(config_path)) as f:
            return f.read().strip() or None
    except OSError:
        return None


def file_set(config_path, token):
    path = token_file(config_path)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(token + "\n")
    return path
