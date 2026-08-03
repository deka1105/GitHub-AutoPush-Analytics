#!/usr/bin/env python3
"""
GitHub AutoPush · Analytics — live split-screen terminal dashboard.

A full-screen TUI that refreshes in place. Its layout adapts to the terminal
width:

  • Maximized (wide, cols >= --split-cols)  → analytics dashboard pinned to the
    LEFT half (totals, 14-day sparkline, status breakdown, busiest repos) with a
    live tail of the watcher's logs on the RIGHT half.
  • Half window (narrow, cols < --split-cols) → the dashboard is hidden and only
    the logs are shown, full width.

Read-only — it never touches the repos or the watcher. It reads push_log.csv
for analytics and tails watcher.log (INFO+ lines) for the log pane, so run it in
a spare terminal next to auto_git_push.py.

    python3 dashboard.py                      # ./push_log.csv + ./watcher.log
    python3 dashboard.py --split-cols 140     # require a wider window to split
    python3 dashboard.py --interval 5         # refresh every 5s (default 1.5)

Quit with q or Ctrl-C.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import re
import select
import shutil
import signal
import sys
import termios
import tty
import unicodedata
from collections import Counter, OrderedDict, deque
from datetime import datetime, timedelta

# ── palette (256-colour) ──────────────────────────────────────────────────────
# Colourblind-safe + theme-agnostic. Categorical status hues are blue / orange /
# pink-red — distinguishable under red-green colour blindness and readable on
# both light and dark terminals; success/failed/error also carry ✓ ▲ ✖ glyphs and
# text labels as non-colour cues. Legacy names are kept to limit churn:
#   GREEN → blue (success/positive)   AMBER → orange (failed)   RED → pink-red (error)
_R      = "\x1b[0m"
_BOLD   = "\x1b[1m"
_DIM    = "\x1b[2m"
TITLE   = "\x1b[38;5;33m"      # headers / INFO       — blue
GREEN   = "\x1b[38;5;33m"      # success / positive   — blue
AMBER   = "\x1b[38;5;208m"     # failed               — orange
RED     = "\x1b[38;5;197m"     # error                — pink-red
PURPLE  = "\x1b[38;5;141m"     # repo names           — lavender (non-status)
CYAN    = "\x1b[38;5;37m"      # secondary accent     — teal

# Neutrals are theme-tunable (see apply_theme) so borders and muted text keep
# contrast on any background.
BORDER  = "\x1b[38;5;244m"     # box lines
GREY    = "\x1b[38;5;246m"     # timestamps / muted text

# Blue intensity ramp (newest → oldest) for the sparkline, kept mid-range so
# both ends stay visible on light and dark backgrounds.
_RAMP = ["\x1b[38;5;45m", "\x1b[38;5;39m", "\x1b[38;5;38m", "\x1b[38;5;33m",
         "\x1b[38;5;32m", "\x1b[38;5;31m", "\x1b[38;5;66m", "\x1b[38;5;244m"]

_THEMES = {   # background-sensitive neutrals; hues above stay fixed
    "auto":  ("\x1b[38;5;244m", "\x1b[38;5;246m"),
    "dark":  ("\x1b[38;5;246m", "\x1b[38;5;250m"),   # brighter lines/text on black
    "light": ("\x1b[38;5;240m", "\x1b[38;5;238m"),   # darker  lines/text on white
}


def apply_theme(name: str):
    """Tune the background-sensitive neutrals: 'auto' (default), 'dark', 'light'."""
    global BORDER, GREY
    BORDER, GREY = _THEMES.get(name, _THEMES["auto"])


_SPARK  = "▁▂▃▄▅▆▇█"
_TS_FMT = "%Y-%m-%d %H:%M:%S"

# ── styled-line primitives ────────────────────────────────────────────────────
# A line is a list of (text, ansi) segments. Width is measured on the *text*
# only, so colour codes never corrupt padding/alignment.
Seg = tuple  # (str, str)


def seg(text: str, ansi: str = "") -> Seg:
    return (text, ansi)


def _cw(ch: str) -> int:
    """Terminal column width of one char (2 for wide/fullwidth glyphs, else 1)."""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def disp_len(text: str) -> int:
    return sum(_cw(c) for c in text)


def vis_len(line: list[Seg]) -> int:
    return sum(disp_len(t) for t, _ in line)


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
    if vis_len(line) <= width:
        return line
    out, used = [], 0
    for text, ansi in line:
        chunk = ""
        for ch in text:
            w = _cw(ch)
            if used + w > width - 1:            # leave a column for the ellipsis
                out.append((chunk, ansi))
                out.append(("…", ""))
                return out
            chunk += ch
            used += w
        out.append((chunk, ansi))
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


# ── log scrolling ──────────────────────────────────────────────────────────────
class LogView:
    """Scroll state for the live-log pane, shared between renders.

    `scroll` counts wrapped lines above the bottom (0 = following the newest).
    log_box() clamps it to the current geometry and, while scrolled, keeps the
    viewport anchored to the same content as new lines stream in.
    """
    def __init__(self):
        self.scroll     = 0
        self.max_scroll = 0
        self.prev_total = 0

    @property
    def following(self) -> bool:
        return self.scroll <= 0


def decode_keys(data: bytes) -> list[str]:
    """Translate raw terminal bytes into scroll tokens."""
    toks, i = [], 0
    while i < len(data):
        if data[i:i + 4] in (b"\x1b[5~", b"\x1b[6~"):
            toks.append("pageup" if data[i + 2:i + 3] == b"5" else "pagedown"); i += 4
        elif data[i:i + 3] == b"\x1b[A": toks.append("up");     i += 3
        elif data[i:i + 3] == b"\x1b[B": toks.append("down");   i += 3
        elif data[i:i + 3] in (b"\x1b[H", b"\x1b[1~"): toks.append("top");    i += 3
        elif data[i:i + 3] in (b"\x1b[F", b"\x1b[4~"): toks.append("bottom"); i += 3
        else:
            c = chr(data[i]) if data[i] < 128 else ""
            toks.append({"k": "up", "j": "down", "g": "top", "G": "bottom",
                         "b": "pageup", " ": "pagedown", "f": "pagedown"}.get(c, ""))
            i += 1
    return [t for t in toks if t]


def apply_scroll(view: LogView, tok: str, page: int):
    """Adjust the view for one key token (bottom clamp here, top clamp in log_box)."""
    step = {"up": 1, "down": -1, "pageup": page, "pagedown": -page,
            "top": 10 ** 9, "bottom": -(10 ** 9)}.get(tok, 0)
    view.scroll = max(0, view.scroll + step)


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
        # last 7 calendar days (mirrors the watcher's "📊 Commits pushed in past
        # 7 days" headline metric)
        self.last_7_days = list(days.items())[-7:]
        self.last7       = sum(n for _, n in self.last_7_days)

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
def summary_panel(s: Stats, width: int) -> list[str]:
    rate_col = GREEN if s.rate >= 95 else AMBER if s.rate >= 80 else RED
    cells = [
        (f"{s.total:,}",              "Total",   TITLE),
        (f"{s.rate:.0f}%",            "Success", rate_col),
        (f"{s.status.get('error',0)}", "Errors", RED),
        (f"{s.last7}",                "7 days",  CYAN),
        (f"{s.repos_active}",         "Repos",   PURPLE),
        (f"{s.today}",                "Today",   GREEN),
    ]
    inner = width - 2
    cw    = inner // len(cells)
    big, lab = [], []
    for value, label, col in cells:
        big += pad([seg(value, col + _BOLD)], cw, "center")
        lab += pad([seg(label, GREY)], cw, "center")
    return box("Overview", [big, lab], width, TITLE)


def pie_panel(s: Stats, width: int) -> list[str]:
    """A round status pie (success / failed / error) with a counts + % legend."""
    data  = [("success", s.status.get("success", 0), GREEN),
             ("failed",  s.status.get("failed", 0),  AMBER),
             ("error",   s.status.get("error", 0),   RED)]
    data  = [d for d in data if d[1] > 0]
    total = sum(c for _, c, _ in data)
    if total == 0:
        return box("Status mix", [[seg(" no pushes logged yet", GREY)]], width, RED)

    # cumulative angle boundaries, as fractions of a full turn
    bounds, acc = [], 0
    for _, cnt, col in data:
        start = acc / total; acc += cnt
        bounds.append((start, acc / total, col))

    rows_n, cols_n = 7, 15                       # cols ≈ 2·rows keeps it circular
    grid = []
    for iy in range(rows_n):
        y = (iy - (rows_n - 1) / 2) / ((rows_n - 1) / 2)
        line = []
        for ix in range(cols_n):
            x = (ix - (cols_n - 1) / 2) / ((cols_n - 1) / 2)
            if x * x + y * y <= 1.0:
                frac = (math.atan2(y, x) / (2 * math.pi)) % 1.0
                col  = next((c for a, b, c in bounds if a <= frac < b), data[-1][2])
                line.append(seg("█", col))
            else:
                line.append(seg(" "))
        grid.append(line)

    legend = []
    for label, cnt, col in data:
        legend.append([seg("● ", col), seg(f"{label:<7} ", col),
                       seg(f"{cnt:>6} ", _BOLD), seg(f"{cnt / total * 100:4.1f}%", GREY)])
    legend.append([seg("  total ", GREY), seg(f"{total:>6}", _BOLD)])

    rows = []
    for i, g in enumerate(grid):
        row = list(g) + [seg("  ")]
        if i < len(legend):
            row += legend[i]
        rows.append(row)
    return box("Status mix", rows, width, RED)


def week_panel(s: Stats, width: int) -> list[str]:
    """Per-day successful pushes over the last 7 calendar days, plus the total."""
    inner = width - 2
    days  = s.last_7_days
    peak  = max((n for _, n in days), default=0) or 1
    today = datetime.now().date()
    bar_w = max(4, inner - 15)
    rows = []
    for d, n in days:
        fil = round(n / peak * bar_w) if n else 0
        if d == today:
            col, style = GREEN, _BOLD          # today stands out
        elif n == 0:
            col, style = GREY, ""
        else:
            col, style = TITLE, ""
        rows.append([
            seg(f" {d.strftime('%a %d')} ", col + style),
            seg("▇" * fil, col),
            seg("·" * (bar_w - fil), BORDER),
            seg(f" {n:>4} ", col + _BOLD),
        ])
    return box(f"Last 7 days · {s.last7} pushes", rows, width, GREEN)


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


# ANSI-aware width helpers for the split-column joiner
def _plain_len(s: str) -> int:
    out, i = 0, 0
    while i < len(s):
        if s[i] == "\x1b":
            j = s.find("m", i)
            i = j + 1 if j != -1 else i + 1
        else:
            out += _cw(s[i])
            i += 1
    return out


def _ljust_ansi(s: str, width: int) -> str:
    return " " * max(0, width - _plain_len(s))


# ── watcher log tail (right-hand pane) ─────────────────────────────────────────
# The log pane renders fully-coloured lines (date · level · [repo] · message),
# identical to the watcher's console. The live watcher feeds pre-coloured lines
# straight from its ColourFormatter; the standalone tail rebuilds the same look
# from the plain watcher.log via _format_tail() below.
_LOG_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[(\w+)\s*\]\s?(.*)$")

_LEVELS = {  # level → (colour, glyph); DEBUG is dropped, mirroring the console
    "INFO":     (TITLE,       "▶"),
    "WARNING":  (AMBER,       "⚠"),
    "ERROR":    (RED,         "✖"),
    "CRITICAL": (RED + _BOLD, "●"),
}
_REPO_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)")
_MSG_KW = {  # leading keyword → body colour (mirrors the watcher's palette)
    "✓":    "\x1b[38;5;82m",  "Push": "\x1b[38;5;39m",  "Comm": "\x1b[38;5;75m",
    "Stag": "\x1b[38;5;244m", "Pull": "\x1b[38;5;220m", "Noth": "\x1b[38;5;240m",
    "CREA": "\x1b[38;5;82m",  "MODI": "\x1b[38;5;75m",  "DELE": "\x1b[38;5;196m",
    "MOVE": "\x1b[38;5;214m",
}


def _format_tail(ts: str, level: str, msg: str) -> str:
    """Rebuild a coloured console-style line from a plain watcher.log entry."""
    col, glyph = _LEVELS.get(level, (GREY, "●"))
    tag = f"{col}{_BOLD}{glyph} {level:<8}{_R}"
    m = _REPO_RE.match(msg)
    if m:
        repo, body = m.group(1), m.group(2)
        repo_part = f"{PURPLE}{_BOLD}[{repo}]{_R}  "
    else:
        repo_part, body = "", msg
    body_col = next((c for kw, c in _MSG_KW.items() if body.startswith(kw)), "")
    body_part = f"{body_col}{body}{_R}" if body_col else body
    return f"{GREY}{ts}{_R}  {tag}  {repo_part}{body_part}"


def tail_log(path: str, limit: int = 400) -> list[str]:
    """Most recent INFO+ entries as ready-to-print coloured lines, oldest first."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > 262_144:                     # only decode the tail of big logs
                f.seek(-262_144, os.SEEK_END)
                f.readline()                        # drop the partial first line
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    out = deque(maxlen=limit)
    for line in data.splitlines():
        m = _LOG_RE.match(line)
        if m and m.group(2) in _LEVELS:
            out.append(_format_tail(m.group(1), m.group(2), m.group(3)))
    return list(out)


def _fit_raw(s: str, width: int) -> str:
    """Truncate/pad a raw ANSI string to exactly `width` display columns."""
    out, used, i, cut = [], 0, 0, False
    while i < len(s):
        if s[i] == "\x1b":                          # copy colour codes verbatim (0 width)
            j = s.find("m", i)
            k = j + 1 if j != -1 else i + 1
            out.append(s[i:k]); i = k; continue
        w = _cw(s[i])
        if used + w > width:
            cut = True
            break
        out.append(s[i]); used += w; i += 1
    res = "".join(out)
    if cut and used < width:                        # ellipsis when there's room
        res += f"{_R}…"; used += 1
    res += _R
    if used < width:
        res += " " * (width - used)
    return res


def _wrap_raw(s: str, width: int, indent: int = 2) -> list[str]:
    """Wrap a raw ANSI string into lines of exactly `width` display columns,
    carrying the active colour across the wrap and indenting continuations."""
    if width <= 0:
        return [""]
    indent = indent if indent < width else 0
    lines, cur, used, active, i = [], [], 0, "", 0

    def flush():
        nonlocal cur, used
        lines.append("".join(cur) + _R + " " * (width - used if used < width else 0))
        cur, used = [], 0

    while i < len(s):
        if s[i] == "\x1b":                              # colour code — zero width
            j = s.find("m", i)
            k = j + 1 if j != -1 else i + 1
            code = s[i:k]
            cur.append(code)
            active = "" if code == _R else active + code
            i = k
            continue
        w = _cw(s[i])
        if used + w > width:                            # wrap: continue the colour, indented
            flush()
            if indent:
                cur.append(" " * indent); used += indent
            if active:
                cur.append(active)
        cur.append(s[i]); used += w; i += 1
    flush()
    return lines


def log_box(entries: list[str], width: int, height: int,
            note: str | None = None, view: "LogView | None" = None) -> list[str]:
    """A 'Live log' box exactly `height` lines tall; long lines wrap, newest at bottom.

    When a `view` is given the pane is scrollable: `view.scroll` lines are hidden
    below the viewport, a scrollbar is drawn on the right border, and while
    scrolled the viewport stays anchored to the same lines as new logs stream in.
    """
    inner = width - 2
    slots = max(1, height - 2)
    src = list(entries) if entries else \
        [f"{GREY}{note or ' waiting for watcher.log …'}{_R}"]
    visual: list[str] = []
    for line in src:                                    # wrap each entry, keep order
        visual.extend(_wrap_raw(line, inner))
    total = len(visual)

    scroll, max_scroll = 0, max(0, total - slots)
    if view is not None:
        # keep the viewport on the same content as new lines arrive while scrolled
        if view.scroll > 0 and total > view.prev_total:
            view.scroll += total - view.prev_total
        view.prev_total = total
        view.max_scroll = max_scroll
        view.scroll = max(0, min(view.scroll, max_scroll))
        scroll = view.scroll

    end   = total - scroll
    start = max(0, end - slots)
    shown = ([""] * slots + visual[start:end])[-slots:]  # pad top; newest sits at bottom

    title = "Live log" if scroll == 0 else f"Live log · ↑{scroll}/{max_scroll} SCROLLED"
    accent = CYAN if scroll == 0 else AMBER
    head = _truncate([seg("┌─", BORDER), seg(f" {title} ", accent + _BOLD),
                      seg("─" * max(0, inner - len(title) - 3) + "┐", BORDER)], width)
    out = [render(head)]
    # scrollbar thumb on the right border
    if total > slots:
        thumb = max(1, round(slots * slots / total))
        top   = round((start / (total - slots)) * (slots - thumb)) if total > slots else 0
        bar   = [(top <= r < top + thumb) for r in range(slots)]
    else:
        bar = [False] * slots
    for r, line in enumerate(shown):
        right = f"{CYAN}█{_R}" if bar[r] else f"{BORDER}│{_R}"
        out.append(f"{BORDER}│{_R}{_fit_raw(line, inner)}{right}")
    out.append(render([seg("└" + "─" * inner + "┘", BORDER)]))
    return out


def dashboard_column(s: Stats, width: int) -> list[str]:
    """Stacked analytics panels for the left half of a split view."""
    out: list[str] = []
    out += summary_panel(s, width);   out.append("")
    out += pie_panel(s, width);       out.append("")
    out += week_panel(s, width);      out.append("")
    out += sparkline_panel(s, width); out.append("")
    out += top_repos_panel(s, width)
    return out


def _join_split(left: list[str], right: list[str], leftw: int, height: int) -> list[str]:
    """Place `left` column (padded to leftw) beside `right`, over `height` rows."""
    out = []
    for i in range(height):
        l = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        out.append(f"{l}{_ljust_ansi(l, leftw)} {r}")
    return out


# ── screen / render loop ────────────────────────────────────────────────────────
ALT_ON, ALT_OFF   = "\x1b[?1049h", "\x1b[?1049l"
CUR_OFF, CUR_ON   = "\x1b[?25l", "\x1b[?25h"
HOME, CLEAR       = "\x1b[H", "\x1b[2J"


def frame(stats: Stats | None, logs: list[str], err: str | None,
          push_path: str, log_path: str, cols: int, rows: int,
          split_cols: int, view: "LogView | None" = None) -> str:
    """Compose one full-screen frame.

    Wide terminal ("maximized")  → dashboard on the left half, live logs on the
    right. Narrow terminal ("half window", cols < split_cols) → logs only.
    """
    height = max(3, rows)
    body_h = height - 2                      # title line + footer line
    lines: list[str] = []

    # ── title bar ─────────────────────────────────────────────────────────────
    now = datetime.now().strftime("%a %d %b · %H:%M:%S")
    title = pad([seg("  GitHub AutoPush ", TITLE + _BOLD), seg("· Analytics", GREY)],
                max(0, cols - len(now) - 2)) + [seg(now + "  ", GREY)]
    lines.append(render(_truncate(title, cols)))

    split = (stats is not None and stats.total > 0
             and cols >= split_cols and body_h >= 14)

    if err:
        lines += [render([seg("  ⚠ " + err, AMBER + _BOLD)])] + [""] * (body_h - 1)
        mode = "error"
    elif split:
        leftw   = min(56, max(44, cols // 2))
        rightw  = cols - leftw - 1
        left    = dashboard_column(stats, leftw)[:body_h]
        right   = log_box(logs, rightw, body_h, view=view)
        lines  += _join_split(left, right, leftw, body_h)
        mode = "split"
    else:
        # logs-only (half window) — full-width log pane
        lines += log_box(logs, cols, body_h, view=view,
                         note="  waiting for watcher.log … (run auto_git_push.py)")
        mode = "logs"

    # ── footer ────────────────────────────────────────────────────────────────
    scrolled = view is not None and not view.following
    if scrolled:
        hint = "↑↓/PgUp/PgDn scroll · G live · q quit"
    elif mode == "logs" and stats is not None and stats.total:
        hint = f"logs only · widen to ≥{split_cols} cols for the dashboard"
    elif mode == "split":
        hint = f"↑↓ scroll logs · {os.path.basename(log_path)}"
    else:
        hint = os.path.basename(push_path)
    live = [seg("↕ scrolled", AMBER)] if scrolled else [seg("● live", GREEN)]
    footer = [seg("  q", _BOLD), seg(" quit   ", GREY)] + live + [seg(f"  {hint}", GREY)]
    lines.append(render(pad(_truncate(footer, cols), cols)))

    lines = lines[:height]
    while len(lines) < height:
        lines.append("")
    # pad each line so stale characters from prior frames are wiped
    body = "\r\n".join(l + "\x1b[K" for l in lines)
    return HOME + body + "\x1b[J"


def run(path: str, log_path: str, interval: float, split_cols: int) -> int:
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
    logs: list[str] = []
    err: str | None = None
    view = LogView()

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

            logs = tail_log(log_path)
            cols, rows = shutil.get_terminal_size((80, 24))
            out.write(frame(stats, logs, err, path, log_path, cols, rows, split_cols, view))
            out.flush()

            # wait `interval`, but wake early if a key is pressed
            if stdin_tty:
                r, _, _ = select.select([sys.stdin], [], [], interval)
                if r:
                    data = os.read(sys.stdin.fileno(), 64)
                    if b"q" in data or b"Q" in data or b"\x03" in data:   # q / Ctrl-C
                        break
                    for tok in decode_keys(data):
                        apply_scroll(view, tok, page=max(1, rows - 4))
            else:
                import time
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Live split-screen analytics dashboard + log tail")
    ap.add_argument("--log", default="push_log.csv", help="path to push_log.csv (default: ./push_log.csv)")
    ap.add_argument("--watcher-log", default="watcher.log", help="path to the watcher's log file (default: ./watcher.log)")
    ap.add_argument("--interval", type=float, default=1.5, help="refresh seconds (default: 1.5)")
    ap.add_argument("--split-cols", type=int, default=120,
                    help="min terminal width to show the dashboard beside the logs; "
                         "narrower than this shows logs only (default: 120)")
    args = ap.parse_args()
    return run(args.log, args.watcher_log, max(0.2, args.interval), args.split_cols)


if __name__ == "__main__":
    sys.exit(main())
