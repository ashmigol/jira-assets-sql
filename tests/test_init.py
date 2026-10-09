import os
import stat

import pytest

from assets_sql import init, tokens
from assets_sql.api import ApiError
from assets_sql.config import Config, read_file

SCHEMAS = [{"id": "9", "name": "Engineering", "objectSchemaKey": "ENG"}, {"id": "42", "name": "Systems", "objectSchemaKey": "SYS"}]


class FakeClient:
    tokens_ok = {"good-token"}

    def __init__(self, cfg):
        self.cfg = cfg

    def myself(self):
        if self.cfg.token not in self.tokens_ok:
            raise ApiError(401, "unauthorized")
        return {"displayName": "Test User"}

    @property
    def workspace(self):
        return "ws-1"

    def schemas(self):
        return SCHEMAS


def run_init(tmp_path, answers, secrets, env=None, **kw):
    answers, secrets, prompts, out = list(answers), list(secrets), [], []

    def ask(p):
        prompts.append(p)
        return answers.pop(0)

    def ask_secret(p):
        prompts.append(p)
        return secrets.pop(0)

    env = {"ASSETS_CONFIG": str(tmp_path / "cfg" / "config"), **(env or {})}
    values = init.run(env=env, ask=ask, ask_secret=ask_secret, client_factory=FakeClient, keychain=False, out=out.append, **kw)
    assert not answers and not secrets, "all scripted answers used"
    return values, prompts, "\n".join(out), env


def test_first_run_prompts_and_saves(tmp_path):
    values, prompts, out, env = run_init(tmp_path, ["hogwarts.atlassian.net", "harry@example.com", "42", "42"], ["good-token"])
    assert prompts == [
        "Jira site (example https://hogwarts.atlassian.net): ",
        "Email: ",
        "API token (create at https://id.atlassian.com/manage-profile/security/api-tokens): ",
        "Default schema ID (https://hogwarts.atlassian.net/jira/assets/object-schema/<THIS_NUMBER>): ",
        "Writable schema ID (https://hogwarts.atlassian.net/jira/assets/object-schema/<THIS_NUMBER>) [empty = read-only]: ",
    ]
    assert "logged in as Test User" in out
    path = env["ASSETS_CONFIG"]
    assert read_file(path) == {"JIRA_SITE": "https://hogwarts.atlassian.net", "JIRA_EMAIL": "harry@example.com",
                               "ASSETS_SCHEMA": "42", "ASSETS_WRITABLE_SCHEMAS": "42"}
    assert "good-token" not in open(path).read(), "token is not stored in the config file"
    token_path = tokens.token_file(path)
    assert stat.S_IMODE(os.stat(token_path).st_mode) == 0o600 and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    cfg = Config.load(env)  # what `assets` will use
    assert (cfg.site, cfg.email, cfg.token, cfg.schema, cfg.writable_schemas) == \
        ("https://hogwarts.atlassian.net", "harry@example.com", "good-token", "42", frozenset({"42"}))


def test_read_only_by_default_and_schema_key_accepted(tmp_path):
    values, *_ = run_init(tmp_path, ["hogwarts.atlassian.net", "h@example.com", "sys", ""], ["good-token"])
    assert values["ASSETS_SCHEMA"] == "42" and values["ASSETS_WRITABLE_SCHEMAS"] == ""


def test_wrong_token_asks_again(tmp_path):
    _, prompts, out, _ = run_init(tmp_path, ["hogwarts.atlassian.net", "h@example.com", "hogwarts.atlassian.net", "h@example.com", "", ""],
                                  ["bad", "good-token"])
    assert "wrong email or API token" in out
    assert sum(p.startswith("API token") for p in prompts) == 2


def test_three_wrong_tokens_abort_and_save_nothing(tmp_path):
    with pytest.raises(init.Aborted):
        run_init(tmp_path, ["s.atlassian.net", "h@example.com"] * 3, ["bad"] * 3)
    assert not os.path.exists(tmp_path / "cfg" / "config")


def test_unknown_schema_shows_list_and_asks_again(tmp_path):
    values, _, out, _ = run_init(tmp_path, ["s.atlassian.net", "h@example.com", "176", "9", "9, 777", "9,42"], ["good-token"])
    assert "no schema '176'" in out and "Systems" in out and "no schema 777" in out
    assert values["ASSETS_SCHEMA"] == "9" and values["ASSETS_WRITABLE_SCHEMAS"] == "9,42"


def test_rerun_keeps_values_with_enter_and_dash_clears(tmp_path):
    run_init(tmp_path, ["s.atlassian.net", "h@example.com", "42", "42"], ["good-token"])
    values, prompts, _, env = run_init(tmp_path, ["", "", "", "-"], [""])
    assert prompts[0].endswith("[https://s.atlassian.net]: ") and "[Enter = keep current]" in prompts[2]
    assert "[42, '-' = none (read-only)]" in prompts[4]
    assert values["ASSETS_SCHEMA"] == "42" and values["ASSETS_WRITABLE_SCHEMAS"] == ""
    assert Config.load(env).token == "good-token"


def test_env_overrides_config_file(tmp_path):
    _, _, _, env = run_init(tmp_path, ["s.atlassian.net", "h@example.com", "42", "42"], ["good-token"])
    cfg = Config.load({**env, "ASSETS_WRITABLE_SCHEMAS": "9", "JIRA_API_TOKEN": "from-env"})
    assert cfg.writable_schemas == frozenset({"9"}) and cfg.token == "from-env"


def test_no_config_tells_to_run_init(tmp_path):
    from assets_sql.config import ConfigError
    with pytest.raises(ConfigError, match="assets init"):
        Config.load({"ASSETS_CONFIG": str(tmp_path / "none")})


def test_keychain_token_goes_via_stdin(monkeypatch):
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(tokens.subprocess, "run", lambda args, **kw: calls.append((args, kw)) or R())
    tokens.keychain_set("https://s.atlassian.net", "h@example.com", "s3cret")
    args, kw = calls[0]
    assert "s3cret" not in " ".join(args) and kw["input"] == "s3cret\ns3cret\n"
