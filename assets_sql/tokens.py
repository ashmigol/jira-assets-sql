"""API token storage: the OS keychain via `keyring` (macOS Keychain, Secret Service, Windows Credential Locker)
when available, otherwise a 0600 file next to the config."""
from __future__ import annotations

import os

try:
    import keyring
    from keyring.errors import KeyringError
except ImportError:  # optional
    keyring = None
    KeyringError = Exception

SERVICE = "jira-assets-sql"


def _service(site):
    return f"{SERVICE} {site}"


def keychain_available():
    if keyring is None:
        return False
    try:
        kr = keyring.get_keyring()
    except Exception:
        return False
    return getattr(kr, "priority", 0) > 0 and type(kr).__module__ not in ("keyring.backends.fail", "keyring.backends.null")


def keychain_get(site, email):
    if not site or not email or not keychain_available():
        return None
    try:
        return keyring.get_password(_service(site), email) or None
    except KeyringError:
        return None


def keychain_set(site, email, token):
    """Store and read back: a store that silently changes the token (e.g. truncation) is an error."""
    try:
        keyring.set_password(_service(site), email, token)
        back = keyring.get_password(_service(site), email)
    except KeyringError as e:
        raise OSError(f"keychain: {e}") from e
    if back != token:
        raise OSError("keychain returned a different token than was stored")


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
