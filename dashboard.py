#!/usr/bin/env python3
"""
GitHub AutoPush · Analytics — live terminal dashboard.

A full-screen TUI that reads push_log.csv and renders push analytics that
refresh in place: totals, a 14-day activity sparkline, per-status breakdown,
the busiest repos and the most recent pushes. Read-only — it never touches
the repos or the watcher; run it in a spare terminal alongside auto_git_push.py.

    python3 dashboard.py                      # watches ./push_log.csv
    python3 dashboard.py --log push_log.csv   # explicit path
    python3 dashboard.py --interval 5         # refresh every 5s (default 2)

Quit with q or Ctrl-C.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import select
import shutil
import signal
import sys
import termios
import tty
from collections import Counter, OrderedDict, deque
from datetime import datetime, timedelta

# ── palette (256-colour, matched to auto_git_push.py) ─────────────────────────
_R      = "\x1b[0m"
_BOLD   = "\x1b[1m"
_DIM    = "\x1b[2m"
BORDER  = "\x1b[38;5;240m"     # dark grey box lines
TITLE   = "\x1b[38;5;39m"      # sky blue
GREEN   = "\x1b[38;5;82m"      # success
AMBER   = "\x1b[38;5;214m"     # failed
RED     = "\x1b[38;5;196m"     # error
PURPLE  = "\x1b[38;5;141m"     # repo names
GREY    = "\x1b[38;5;244m"     # timestamps / muted
CYAN    = "\x1b[38;5;80m"

# green ramp, freshest → palest, for the 14-day sparkline
_RAMP = ["\x1b[38;5;22m", "\x1b[38;5;28m", "\x1b[38;5;34m", "\x1b[38;5;40m",
         "\x1b[38;5;46m", "\x1b[38;5;83m", "\x1b[38;5;120m", "\x1b[38;5;157m"]

_SPARK  = "▁▂▃▄▅▆▇█"
_TS_FMT = "%Y-%m-%d %H:%M:%S"

# ── styled-line primitives ────────────────────────────────────────────────────
# A line is a list of (text, ansi) segments. Width is measured on the *text*
# only, so colour codes never corrupt padding/alignment.
Seg = tuple  # (str, str)


def seg(text: str, ansi: str = "") -> Seg:
    return (text, ansi)


def vis_len(line: list[Seg]) -> int:
    return sum(len(t) for t, _ in line)


def pad(line: list[Seg], width: int, align: str = "left") -> list[Seg]:
    gap = width - vis_len(line)
    if gap <= 0:
        return _truncate(line, width)
    if align == "right":
        return [seg(" " * gap)] + line
    if align == "center":
        l = gap // 2
        return [seg(" " * l)] + line + [seg(" " * (gap - l))]
    return line + [seg(" " * gap)]


def _truncate(line: list[Seg], width: int) -> list[Seg]:
    out, used = [], 0
    for text, ansi in line:
        if used >= width:
            break
        room = width - used
        if len(text) > room:
            text = text[: max(0, room - 1)] + "…"
        out.append((text, ansi))
        used += len(text)
    return out


def render(line: list[Seg]) -> str:
    return "".join(f"{ansi}{text}{_R}" if ansi else text for text, ansi in line)


def box(title: str, rows: list[list[Seg]], width: int, accent: str = TITLE) -> list[str]:
    """Wrap styled rows in a titled border of the given total width."""
    inner = width - 2
    head = [seg("┌─", BORDER), seg(f" {title} ", accent + _BOLD),
            seg("─" * max(0, inner - len(title) - 3) + "┐", BORDER)]
    out = [render(_truncate(head, width))]
    for r in rows:
        body = pad(r, inner)
        out.append(render([seg("│", BORDER)] + body + [seg("│", BORDER)]))
    out.append(render([seg("└" + "─" * inner + "┘", BORDER)]))
    return out


# ── data ──────────────────────────────────────────────────────────────────────
class Stats:
    """One parsed snapshot of push_log.csv."""

    def __init__(self, rows: list[dict]):
        self.total   = len(rows)
        self.status  = Counter((r.get("status") or "").strip() for r in rows)
        self.success = self.status.get("success", 0)
        self.rate    = (self.success / self.total * 100) if self.total else 0.0

        # success pushes per repo → busiest repos
        repo = Counter()
        for r in rows:
            if (r.get("status") or "").strip() == "success":
                repo[(r.get("repo_name") or "?").strip()] += 1
        self.repos_active = len(repo)
        self.top_repos    = repo.most_common(6)

        # success pushes per day for the last 14 days
        today = datetime.now().date()
        days  = OrderedDict(
            ((today - timedelta(days=i)), 0) for i in range(13, -1, -1)
        )
        for r in rows:
            if (r.get("status") or "").strip() != "success":
                continue
            d = _parse_day(r.get("timestamp"))
            if d in days:
                days[d] += 1
        self.per_day = days
        self.busiest = max(days.items(), key=lambda kv: kv[1]) if days else (today, 0)
        self.today   = days.get(today, 0)

        # most recent rows (any status), newest first
        self.recent = list(reversed(rows[-12:]))


def _parse_day(ts: str | None):
    try:
        return datetime.strptime((ts or "").strip(), _TS_FMT).date()
    except (ValueError, AttributeError):
        return None


def load(path: str) -> list[dict]:
    with open(path, newline="") as f:
        return [
            {k.strip(): (v.strip() if v else "") for k, v in row.items()}
            for row in csv.DictReader(f)
        ]


# ── panel builders ─────────────────────────────────────────────────────────────
def _clean(msg: str) -> str:
    return (msg or "").rstrip().removesuffix("--END--").rstrip()


def summary_panel(s: Stats, width: int) -> list[str]:
    rate_col = GREEN if s.rate >= 95 else AMBER if s.rate >= 80 else RED
    cells = [
        (f"{s.total:,}",         "Total pushes", TITLE),
        (f"{s.rate:.1f}%",       "Success rate", rate_col),
        (f"{s.repos_active}",    "Active repos", PURPLE),
        (f"{s.today}",           "Today",        GREEN),
    ]
    inner = width - 2
    cw    = inner // len(cells)
    big, lab = [], []
    for value, label, col in cells:
        big += pad([seg(value, col + _BOLD)], cw, "center")
        lab += pad([seg(label, GREY)], cw, "center")
    return box("Overview", [big, lab], width, TITLE)


def sparkline_panel(s: Stats, width: int) -> list[str]:
    peak = max(s.per_day.values()) or 1
    spark, labels = [], []
    for i, (d, n) in enumerate(s.per_day.items()):
        col = _RAMP[min(7, (len(s.per_day) - 1 - i))]
        ch  = _SPARK[min(7, round(n / peak * 7))] if n else "·"
        spark.append(seg(ch, col + _BOLD))
        spark.append(seg(" "))
    spark.append(seg(f"  peak {peak}", GREY))
    labels = [seg(f"{next(iter(s.per_day)).strftime('%b %d')}"
                  f" → {list(s.per_day)[-1].strftime('%b %d')}"
                  f"   busiest {s.busiest[0].strftime('%b %d')} ({s.busiest[1]})", GREY)]
    return box("Pushes · last 14 days", [spark, labels], width, GREEN)


def status_panel(s: Stats, width: int) -> list[str]:
    order = [("success", GREEN, "✓"), ("failed", AMBER, "▲"), ("error", RED, "✖")]
    total = s.total or 1
    inner = width - 2
    bar_w = max(6, inner - 22)
    rows = []
    for name, col, mark in order:
        n   = s.status.get(name, 0)
        fil = round(n / total * bar_w)
        row = [
            seg(f" {mark} {name:<8}", col),
            seg("▇" * fil, col),
            seg("·" * (bar_w - fil), BORDER),
            seg(f" {n:>5} ", col + _BOLD),
        ]
        rows.append(row)
    return box("Status breakdown", rows, width, AMBER)


def top_repos_panel(s: Stats, width: int) -> list[str]:
    inner = width - 2
    peak  = s.top_repos[0][1] if s.top_repos else 1
    name_w = min(18, max((len(n) for n, _ in s.top_repos), default=6))
    bar_w  = max(4, inner - name_w - 9)
    rows = []
    for name, n in s.top_repos:
        fil = max(1, round(n / peak * bar_w))
        rows.append([
            seg(f" {name[:name_w]:<{name_w}} ", PURPLE),
            seg("▇" * fil, GREEN),
            seg(f" {n:>4}", GREEN + _BOLD),
        ])
    if not rows:
        rows = [[seg(" no successful pushes yet", GREY)]]
    return box("Busiest repos", rows, width, PURPLE)


def recent_panel(s: Stats, width: int) -> list[str]:
    mark = {"success": (GREEN, "✓"), "failed": (AMBER, "▲"), "error": (RED, "✖")}
    rows = []
    for r in s.recent:
        col, m = mark.get((r.get("status") or "").strip(), (GREY, "·"))
        ts = (r.get("timestamp") or "")[5:16]        # MM-DD HH:MM
        rows.append([
            seg(f" {ts} ", GREY),
            seg(f"{m} ", col),
            seg(f"{(r.get('repo_name') or '?')[:16]:<16} ", PURPLE),
            seg(_clean(r.get("message"))[:40], GREY),
        ])
    if not rows:
        rows = [[seg(" no pushes logged yet", GREY)]]
    return box("Recent activity", rows, width, CYAN)


def two_col(left: list[str], right: list[str], gap: int = 1) -> list[str]:
    lw = max((_plain_len(x) for x in left), default=0)
    out, pad_s = [], " " * gap
    for i in range(max(len(left), len(right))):
        l = left[i] if i < len(left) else " " * lw
        r = right[i] if i < len(right) else ""
        out.append(f"{l}{_ljust_ansi(l, lw)}{pad_s}{r}")
    return out


# ANSI-aware width helpers for the two-column joiner
def _plain_len(s: str) -> int:
    out, i = 0, 0
    while i < len(s):
        if s[i] == "\x1b":
            j = s.find("m", i)
            i = j + 1 if j != -1 else i + 1
        else:
            out += 1
            i += 1
    return out


def _ljust_ansi(s: str, width: int) -> str:
    return " " * max(0, width - _plain_len(s))


# ── screen / render loop ────────────────────────────────────────────────────────
ALT_ON, ALT_OFF   = "\x1b[?1049h", "\x1b[?1049l"
CUR_OFF, CUR_ON   = "\x1b[?25l", "\x1b[?25h"
HOME, CLEAR       = "\x1b[H", "\x1b[2J"


def frame(stats: Stats | None, err: str | None, path: str,
          cols: int, rows_avail: int) -> str:
    width = min(96, max(48, cols - 2))
    lines: list[str] = []

    now = datetime.now().strftime("%a %d %b · %H:%M:%S")
    title = [seg("  GitHub AutoPush ", TITLE + _BOLD), seg("· Analytics", GREY)]
    title = pad(title, width - 12) + [seg(now, GREY)]
    lines.append(render(title))
    lines.append("")

    if err:
        lines.append(render([seg("  ⚠ " + err, AMBER + _BOLD)]))
    elif stats is None or stats.total == 0:
        lines.append(render([seg("  Waiting for push data…", GREY)]))
    else:
        lines += summary_panel(stats, width)
        lines.append("")
        lines += sparkline_panel(stats, width)
        lines.append("")
        lines += status_panel(stats, width)
        lines.append("")
        lines += two_col(top_repos_panel(stats, width // 2 - 1),
                         recent_panel(stats, width - width // 2))

    lines.append("")
    lines.append(render([seg("  q", _BOLD), seg(" quit   ", GREY),
                         seg("live", GREEN), seg(f" · {os.path.basename(path)}", GREY)]))

    lines = lines[:rows_avail]
    # pad each line to full columns so stale characters from prior frames are wiped
    body = "\r\n".join(l + "\x1b[K" for l in lines)
    return HOME + body + "\x1b[J"


def run(path: str, interval: float) -> int:
    if not os.path.exists(path):
        print(f"push log not found: {path}", file=sys.stderr)
        return 1

    stdin_tty = sys.stdin.isatty()
    old_term = None
    if stdin_tty:
        try:
            old_term = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        except (termios.error, ValueError):
            old_term = None

    out = sys.stdout
    out.write(ALT_ON + CUR_OFF + CLEAR)
    out.flush()

    cache_mtime = -1.0
    stats: Stats | None = None
    err: str | None = None

    def cleanup(*_):
        out.write(CUR_ON + ALT_OFF)
        out.flush()
        if old_term is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_term)

    signal.signal(signal.SIGTERM, lambda *_: (cleanup(), sys.exit(0)))

    try:
        while True:
            try:
                mtime = os.path.getmtime(path)
                if mtime != cache_mtime:
                    stats, err = Stats(load(path)), None
                    cache_mtime = mtime
            except (OSError, csv.Error) as e:
                err = f"cannot read {path}: {e}"

            cols, rows = shutil.get_terminal_size((80, 24))
            out.write(frame(stats, err, path, cols, rows - 1))
            out.flush()

            # wait `interval`, but wake early if a key is pressed
            if stdin_tty:
                r, _, _ = select.select([sys.stdin], [], [], interval)
                if r:
                    ch = sys.stdin.read(1)
                    if ch in ("q", "Q", "\x03"):   # q or Ctrl-C
                        break
            else:
                import time
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Live terminal analytics dashboard for push_log.csv")
    ap.add_argument("--log", default="push_log.csv", help="path to push_log.csv (default: ./push_log.csv)")
    ap.add_argument("--interval", type=float, default=2.0, help="refresh seconds (default: 2)")
    args = ap.parse_args()
    return run(args.log, max(0.2, args.interval))


if __name__ == "__main__":
    sys.exit(main())
