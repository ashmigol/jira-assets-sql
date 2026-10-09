"""Table rendering (box / csv / vertical) with an optional `less -S` pager."""
from __future__ import annotations

import csv
import shutil
import subprocess
import sys

settings = {"pager": True, "max_cell": 60}


def fmt(v, width=None):
    width = width or settings["max_cell"]
    if v is None:
        return ""
    s = str(v).replace("\n", " ")
    return s if len(s) <= width else s[:width - 1] + "…"


def emit(lines):
    """Print lines; if wider/taller than the terminal, open `less -S` (←/→ to scroll, q to quit)."""
    size = shutil.get_terminal_size((200, 50))
    too_big = any(len(line) > size.columns for line in lines) or len(lines) > size.lines - 2
    if settings["pager"] and sys.stdout.isatty() and too_big:
        try:
            subprocess.run(["less", "-S", "-F", "-X", "-R", "-K"], input="\n".join(lines) + "\n", text=True)
            return
        except (OSError, KeyboardInterrupt):
            pass
    print("\n".join(lines))


def print_table(headers, rows, mode="table", width=None, footer=None):
    if mode == "csv":
        w = csv.writer(sys.stdout)
        w.writerow(headers)
        w.writerows(rows)
        return
    if not headers:
        return
    count = footer or f"({len(rows)} row{'s' if len(rows) != 1 else ''})"
    if mode == "vertical":
        pad = max(len(h) for h in headers)
        out = []
        for n, r in enumerate(rows, 1):
            out.append(f"*************************** {n}. row ***************************")
            out += [f"{h:>{pad}}: {'' if v is None else v}" for h, v in zip(headers, r)]
        emit(out + [count])
        return
    cells = [[fmt(v, width) for v in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]

    def line(left, mid, right):
        return left + mid.join("─" * (w + 2) for w in widths) + right

    out = [line("┌", "┬", "┐"), "│" + "│".join(f" {h:<{widths[i]}} " for i, h in enumerate(headers)) + "│", line("├", "┼", "┤")]
    out += ["│" + "│".join(f" {c:<{widths[i]}} " for i, c in enumerate(r)) + "│" for r in cells]
    out += [line("└", "┴", "┘"), count]
    emit(out)
