"""Shell loop and input helpers, driven by a scripted reader (no terminal needed)."""
import json

import pytest

from assets_sql import complete, reader, shell
from assets_sql.complete import Context
from tests.conftest import SCHEMA


class Scripted:
    def __init__(self, lines):
        self.todo, self.done = list(lines), []

    def read(self, prompt, statement=""):
        if not self.todo:
            raise EOFError
        line = self.todo.pop(0)
        self.done.append(line)
        return line

    def save(self):
        pass

    def lines(self):
        return list(self.done)


def run_shell(cfg, jira, lines, monkeypatch, capsys):
    script = Scripted(lines)
    monkeypatch.setattr(shell, "make_reader", lambda *a, **k: script)
    shell.shell(cfg, jira, SCHEMA, auto_sync=False)
    return capsys.readouterr().out


def test_shell_loop_end_to_end(cfg, jira, local, monkeypatch, capsys):
    with open(cfg.log_path, "w") as f:
        f.write(json.dumps({"ts": "t1", "schema": "5", "action": "CREATE", "table": "roles", "object": "Admin",
                            "status": "ok", "detail": "TST-9"}) + "\n")
    out = run_shell(cfg, jira, [
        "SELECT count(*) AS n",
        "FROM systems;",                                   # multi-line statement
        "insert into roles (name) values ('X');",
        ".plan",
        "sync;",                                           # hint, not a syntax error
        ".history insert",
        ".log",
        "DROP TABLE roles;",
        ".quit",
    ], monkeypatch, capsys)
    assert "│ 2 │" in out
    assert "1 row changed locally" in out and "CREATE" in out
    assert "use .sync" in out
    assert "insert into roles" in out and "TST-9" in out
    assert "not authorized" in out


def test_shell_survives_ctrl_c_and_eof(cfg, jira, local, monkeypatch, capsys):
    class Interrupting(Scripted):
        def read(self, prompt, statement=""):
            if self.todo and self.todo[0] == "^C":
                self.todo.pop(0)
                raise KeyboardInterrupt
            return super().read(prompt, statement)
    script = Interrupting(["^C", "SELECT 1", "^C", "SELECT 2 AS two;"])  # ^C on an empty line must not exit
    monkeypatch.setattr(shell, "make_reader", lambda *a, **k: script)
    shell.shell(cfg, jira, SCHEMA, auto_sync=False)
    out = capsys.readouterr().out
    assert "two" in out and "│ 1 " not in out  # the half-typed statement was dropped by Ctrl+C


# ───── completion ─────

@pytest.fixture
def ctx(local):
    db, meta = local
    return Context(meta, db)


def texts(ctx, text):
    return [s.text for s in complete.suggest(ctx, text)]


def test_insert_ghost_lists_editable_columns(ctx):
    g = complete.ghost(ctx, "insert into roles ")
    assert g == "(name, system, approver, google_group) VALUES ("
    assert complete.ghost(ctx, "INSERT INTO roles (") == "name, system, approver, google_group) VALUES ("


def test_insert_column_completion_skips_used(ctx):
    assert texts(ctx, "insert into roles (name, s") == ["system"]
    assert "name" not in texts(ctx, "insert into roles (name, ")


def test_table_completion(ctx):
    assert texts(ctx, "select * from ro") == ["roles"]
    assert texts(ctx, "update sy") == ["systems"]


def test_value_completion_select_reference_user(ctx):
    assert texts(ctx, "update systems set criticality = '") == ["High'", "Low'"]
    assert texts(ctx, "update roles set system = 'pan") == ["Pandadoc'"]
    assert texts(ctx, "select * from roles where approver = 'ali") == ["alice@example.com'"]


def test_column_completion_uses_statement_table(ctx):
    got = texts(ctx, "select * from roles where goo")
    assert got == ["google_group"]
    assert "criticality" not in texts(ctx, "select * from roles where c")


def test_dot_commands(ctx):
    assert texts(ctx, ".hi") == [".history"]
    assert texts(ctx, ".schema ro") == ["roles"]


def test_keywords_keep_case(ctx):
    assert "WHERE" in texts(ctx, "select * from roles WHE")
    assert "where" in texts(ctx, "select * from roles whe")


def test_columns_hint(ctx):
    hint = complete.columns_hint(ctx, "insert into roles ")
    assert hint.startswith("roles: ") and "name*" in hint and "system→systems" in hint and "approver:user" in hint
    assert complete.columns_hint(ctx, "select 1").startswith("Tables: ")


def test_readline_history_import(tmp_path):
    old, new = tmp_path / "history", tmp_path / "history.ptk"
    old.write_text("_HiStOrY_V2_\nselect\\040*\\040from\\040roles;\n.plan\n")
    reader._import_readline_history(str(old), str(new))
    if reader.PromptSession is None:
        pytest.skip("prompt_toolkit not installed")
    from prompt_toolkit.history import FileHistory
    assert list(FileHistory(str(new)).load_history_strings()) == [".plan", "select * from roles;"]
