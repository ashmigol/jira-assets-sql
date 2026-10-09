"""Line input for the shell: prompt_toolkit (completion menu, inline suggestions, column toolbar) with a
plain readline fallback."""
from __future__ import annotations

import os
import re

from . import complete

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggest, AutoSuggestFromHistory, Suggestion
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style
except ImportError:  # optional dependency
    PromptSession = None

try:
    import readline
except ImportError:  # Windows
    readline = None


def make_reader(cfg, ctx, fancy=True):
    if fancy and PromptSession is not None:
        return PtkReader(cfg, ctx)
    return ReadlineReader(cfg, ctx)


class ReadlineReader:
    def __init__(self, cfg, ctx):
        self.path = cfg.history_path
        self.ctx = ctx
        if readline:
            try:
                readline.read_history_file(self.path)
            except OSError:
                pass
            readline.parse_and_bind("tab: complete")
            readline.set_completer_delims(" \t\n,()=")
            readline.set_completer(self._complete)

    def _complete(self, text, i):
        words = [t for t in self.ctx.tables] + [c["col"] for t in self.ctx.tables for c in self.ctx.cols(t)] + complete.KEYWORDS
        return ([w for w in words if w.lower().startswith(text.lower())] + [None])[i]

    def read(self, prompt, statement=""):
        line = input(prompt)
        self.save()
        return line

    def save(self):
        if readline:
            try:
                readline.write_history_file(self.path)
            except OSError:
                pass

    def lines(self):
        if not readline:
            return []
        items = (readline.get_history_item(i) for i in range(1, readline.get_current_history_length() + 1))
        return [h for h in items if h]


if PromptSession is not None:
    class _Completer(Completer):
        def __init__(self, ctx, reader):
            self.ctx, self.reader = ctx, reader

        def get_completions(self, document, complete_event):
            text = self.reader.statement + document.text_before_cursor
            for s in complete.suggest(self.ctx, text):
                yield Completion(s.text, start_position=-s.replace, display_meta=s.meta)

    class _Suggest(AutoSuggest):
        def __init__(self, ctx, reader):
            self.ctx, self.reader = ctx, reader
            self.history = AutoSuggestFromHistory()

        def get_suggestion(self, buffer, document):
            if document.text_after_cursor.strip():
                return None
            g = complete.ghost(self.ctx, self.reader.statement + document.text_before_cursor)
            if g:
                return Suggestion(g)
            return self.history.get_suggestion(buffer, document)


class PtkReader:
    """prompt_toolkit input: menu while typing (Tab / ↓ to pick), grey inline suggestion (→ to accept),
    bottom toolbar with the columns of the table in the current statement."""

    def __init__(self, cfg, ctx):
        self.ctx, self.statement = ctx, ""
        path = cfg.history_path + ".ptk"
        _import_readline_history(cfg.history_path, path)
        self.history = FileHistory(path)
        kb = KeyBindings()

        @kb.add("tab")
        def _(event):
            """Tab: accept the inline suggestion if there is one, otherwise open/cycle the menu."""
            b = event.current_buffer
            if b.suggestion and b.document.is_cursor_at_the_end:
                b.insert_text(b.suggestion.text)
            elif b.complete_state:
                b.complete_next()
            else:
                b.start_completion(select_first=False)

        self.session = PromptSession(
            history=self.history, completer=_Completer(ctx, self), auto_suggest=_Suggest(ctx, self),
            complete_while_typing=True, key_bindings=kb, enable_history_search=False,
            bottom_toolbar=lambda: complete.columns_hint(ctx, self.statement + self.session.default_buffer.text),
            style=Style.from_dict({"bottom-toolbar": "noreverse bg:default fg:ansibrightblack",
                                   "auto-suggestion": "fg:ansibrightblack"}),
        )

    def read(self, prompt, statement=""):
        self.statement = statement
        return self.session.prompt(prompt)

    def save(self):
        pass  # FileHistory appends every entry immediately

    def lines(self):
        return list(reversed(list(self.history.load_history_strings())))


def _import_readline_history(old, new):
    """One-time copy of the readline/libedit history file into prompt_toolkit's format."""
    if os.path.exists(new) or not os.path.exists(old):
        return
    try:
        with open(old, encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip() and not ln.startswith("_HiStOrY_V2_")]
        with open(os.open(new, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            for ln in lines:
                ln = re.sub(r"\\0(\d\d)", lambda m: chr(int(m.group(1), 8)), ln)  # libedit escapes: \040 = space
                f.write("\n# imported\n+" + ln + "\n")
    except OSError:
        pass
