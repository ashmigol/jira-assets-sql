import io
import json
import urllib.error

import pytest

from assets_sql import cli
from assets_sql.api import Client, TransportError
from assets_sql.config import Config, ConfigError

ENV = {"JIRA_SITE": "example.atlassian.net/", "JIRA_EMAIL": "me@example.com", "JIRA_API_TOKEN": "secret"}


# ───── config ─────

def test_config_from_env_defaults_read_only():
    cfg = Config.from_env(ENV)
    assert cfg.site == "https://example.atlassian.net"
    assert cfg.writable_schemas == frozenset() and not cfg.is_writable("176")
    assert cfg.schema is None and cfg.workspace_id is None
    assert "secret" not in repr(cfg)


def test_config_writable_list():
    cfg = Config.from_env({**ENV, "ASSETS_WRITABLE_SCHEMAS": " 3, 7 ", "ASSETS_SCHEMA": "3", "ASSETS_HOME": "/tmp/x"})
    assert cfg.is_writable("3") and cfg.is_writable("7") and not cfg.is_writable("9")
    assert cfg.db_path("3") == "/tmp/x/schema_3.db"


@pytest.mark.parametrize("env", [{}, {**ENV, "JIRA_API_TOKEN": ""}])
def test_config_missing(env):
    with pytest.raises(ConfigError, match="missing"):
        Config.from_env(env)


@pytest.mark.parametrize("bad", ["../../etc", "1;2", "abc", ""])
def test_schema_must_be_numeric(bad):
    with pytest.raises(ConfigError):
        Config.from_env({**ENV, "ASSETS_WRITABLE_SCHEMAS": bad or "x"})


def test_cli_rejects_bad_schema(capsys):
    with pytest.raises(SystemExit, match="invalid schema"):
        cli.main(["--schema", "../x", "plan"], env=ENV)


def test_cli_requires_schema():
    with pytest.raises(SystemExit, match="no schema"):
        cli.main(["plan"], env=ENV)


def test_cli_missing_config():
    with pytest.raises(SystemExit, match="JIRA_SITE"):
        cli.main(["plan"], env={})


# ───── HTTP client ─────

class Resp:
    def __init__(self, status, body):
        self.status, self._body = status, json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, headers=None):
    return urllib.error.HTTPError("u", code, "err", headers or {}, io.BytesIO(b'{"e":1}'))


def make_client(responses):
    calls, sleeps = [], []

    def opener(req, timeout):
        calls.append((req.get_method(), req.full_url, dict(req.header_items())))
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    c = Client(Config(site="https://s", email="me@example.com", token="t", workspace_id="ws"), opener=opener, sleep=sleeps.append)
    return c, calls, sleeps


def test_client_retries_429_with_retry_after():
    c, calls, sleeps = make_client([http_error(429, {"Retry-After": "3"}), Resp(200, {"ok": 1})])
    assert c.request("POST", "https://x", {}) == (200, {"ok": 1})
    assert sleeps == [3.0] and len(calls) == 2


def test_client_returns_error_status():
    c, _, _ = make_client([http_error(400)])
    assert c.request("GET", "https://x") == (400, {"e": 1})


def test_client_retries_network_error_for_get_only():
    c, calls, _ = make_client([urllib.error.URLError("reset"), Resp(200, [])])
    assert c.request("GET", "https://x") == (200, [])
    c, calls, _ = make_client([urllib.error.URLError("reset"), Resp(200, [])])
    with pytest.raises(TransportError):
        c.request("POST", "https://x", {})
    assert len(calls) == 1, "POST must not be retried: it may have created an object"


def test_client_auth_header_and_url():
    c, calls, _ = make_client([Resp(200, {"values": [], "isLast": True})])
    c.aql_all("objectTypeId = 1")
    method, url, headers = calls[0]
    assert url.startswith("https://api.atlassian.com/jsm/assets/workspace/ws/v1/object/aql")
    assert headers["Authorization"].startswith("Basic ")


def test_workspace_discovery():
    c, calls, _ = make_client([Resp(200, {"values": [{"workspaceId": "abc"}]})])
    c._workspace = None
    assert c.workspace == "abc" and calls[0][1] == "https://s/rest/servicedeskapi/assets/workspace"


def test_get_object_404_is_none():
    c, _, _ = make_client([http_error(404)])
    assert c.get_object("1") is None


def test_account_id_exact_email_match():
    c, _, _ = make_client([Resp(200, [{"emailAddress": "me@example.com.evil", "accountId": "x"},
                                      {"emailAddress": "Me@Example.com", "accountId": "y"}])])
    assert c.account_id("me@example.com") == "y"
