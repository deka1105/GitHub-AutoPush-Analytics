#!/usr/bin/env python3
"""
Auto Git Pusher  v4
-------------------
• Watches every local directory listed in a CSV config file
• Auto-detects CSV changes and starts monitoring new directories on-the-fly
• Commits + pushes changed files to the linked GitHub repo
• Logs every push event to push_log.csv for analytics
• Detailed rotating file logging to watcher.log
• Auto-resolves rebase conflicts on append-only files (push_log.csv etc.)
• Live panel pinned to the terminal's top-right showing pushes per day (7 days)

CSV config format  (repos_config.csv):
    local_path, repo_url, repo_name

Push log format  (push_log.csv)  — written automatically:
    timestamp, repo_name, repo_url, file_changed, event_type, status, message

Usage:
    python auto_git_push.py --csv repos_config.csv
    python auto_git_push.py --csv repos_config.csv --log push_log.csv --logfile watcher.log
"""

import os
import sys
import csv
import time
import argparse
import logging
import logging.handlers
import threading
import subprocess
import shutil
from collections import deque
from pathlib import Path
from datetime import datetime, timedelta
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Optional: the split-screen renderer (dashboard.py, same directory). When
# present and the console is a TTY, the watcher paints a live analytics
# dashboard beside the logs. If it can't be imported we silently fall back to
# the classic scrolling logs + small top-right stats panel.
try:
    import dashboard as _dash
except Exception:       # pragma: no cover — any import failure ⇒ classic UI
    _dash = None


# ══════════════════════════════════════════════════════════════════════════════
# Logging setup — console + rotating file
# ══════════════════════════════════════════════════════════════════════════════

LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"   # console and file both show full date+time
LOG_FILE_FORMAT = "%(asctime)s [%(levelname)-8s] %(message)s"
LOG_FILE_DATE   = "%Y-%m-%d %H:%M:%S"

# ── ANSI colour palette ───────────────────────────────────────────────────────
_R = "[0m"          # reset
_BOLD = "[1m"
_DIM  = "[2m"

_LEVEL_COLOURS = {
    "DEBUG"   : "[38;5;240m",   # dark grey
    "INFO"    : "[38;5;39m",    # sky blue
    "WARNING" : "[38;5;214m",   # amber
    "ERROR"   : "[38;5;196m",   # red
    "CRITICAL": "[48;5;196m[97m",  # red bg, white text
}

_REPO_COLOUR = "[38;5;141m"   # soft purple for [RepoName]
_TIME_COLOUR = "[38;5;244m"   # mid grey for timestamp
_MSG_COLOURS = {
    "✓"   : "[38;5;82m",    # bright green — success
    "Push": "[38;5;39m",    # blue
    "Comm": "[38;5;75m",    # lighter blue — committing
    "Stag": "[38;5;244m",   # grey — staging
    "Pull": "[38;5;220m",   # yellow
    "Noth": "[38;5;240m",   # dark grey — nothing to commit
    "CREA": "[38;5;82m",    # green — file created
    "MODI": "[38;5;75m",    # blue — file modified
    "DELE": "[38;5;196m",   # red — file deleted
    "MOVE": "[38;5;214m",   # amber — file moved
}

import re as _re
_REPO_PAT = _re.compile(r"^\[([^\]]+)\]\s*(.*)")


class ColourFormatter(logging.Formatter):
    """
    Console formatter:
        YYYY-MM-DD HH:MM:SS  LEVEL  [RepoName]  message text
    Each part has its own colour. Message text is coloured by keyword.
    File/plain formatter stays plain (no ANSI codes).
    """
    def format(self, record: logging.LogRecord) -> str:
        ts    = self.formatTime(record, LOG_DATE_FORMAT)
        level = record.levelname
        lc    = _LEVEL_COLOURS.get(level, "")
        level_tag = f"{lc}{_BOLD}{'▶' if level == 'INFO' else '⚠' if level == 'WARNING' else '✖' if level == 'ERROR' else '●'} {level:<8}{_R}"

        msg   = record.getMessage()

        # Separate [RepoName] from the rest
        m = _REPO_PAT.match(msg)
        if m:
            repo, body = m.group(1), m.group(2)
            repo_part  = f"{_REPO_COLOUR}{_BOLD}[{repo}]{_R}"
        else:
            repo_part  = ""
            body       = msg

        # Colour the message body by leading keyword
        body_colour = _R
        for kw, colour in _MSG_COLOURS.items():
            if body.startswith(kw):
                body_colour = colour
                break

        # Shade the timestamp by the record's age: today darkest → older lighter
        age = (datetime.now().date() - datetime.fromtimestamp(record.created).date()).days
        ts_colour = _DAY_SHADES[min(max(age, 0), len(_DAY_SHADES) - 1)]
        time_part = f"{ts_colour}{ts}{_R}"
        body_part = f"{body_colour}{body}{_R}"

        if repo_part:
            return f"{time_part}  {level_tag}  {repo_part}  {body_part}"
        return f"{time_part}  {level_tag}  {body_part}"


def setup_logging(logfile: str = "watcher.log", console=None) -> logging.Logger:
    """
    Configure root logger:
      Console  → INFO+,  coloured, time-only timestamp
      Log file → DEBUG+, plain,    full date+time timestamp, rotating 5 MB × 3

    `console`, when given, replaces the default scrolling console handler — the
    LiveUI passes its buffering handler here so log lines feed the split-screen
    renderer instead of being printed directly.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console — coloured, with live stats panel repaint (or the LiveUI buffer)
    ch = console if console is not None else PanelStreamHandler()
    ch.setLevel(logging.INFO)
    if console is None:
        ch.setFormatter(ColourFormatter(datefmt=LOG_DATE_FORMAT))
    root.addHandler(ch)

    # Rotating file — plain text, full timestamps
    fh = logging.handlers.RotatingFileHandler(
        logfile, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(LOG_FILE_FORMAT, datefmt=LOG_FILE_DATE))
    root.addHandler(fh)

    return logging.getLogger(__name__)


log: logging.Logger = logging.getLogger(__name__)  # replaced in main()

PUSH_LOG_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# Push CSV log helpers
# ══════════════════════════════════════════════════════════════════════════════

PUSH_LOG_HEADERS = [
    "timestamp", "repo_name", "repo_url",
    "file_changed", "event_type", "status", "message"
]


def flatten_msg(text: str) -> str:
    """Collapse real newlines into literal \\n so the CSV stays single-line."""
    if not text:
        return ""
    return "\\n".join(line.rstrip() for line in text.splitlines())


def init_push_log(log_path: str):
    """Create push_log.csv with headers if it does not exist."""
    if not os.path.exists(log_path):
        with open(log_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=PUSH_LOG_HEADERS)
            writer.writeheader()
        log.info(f"Push log created: {log_path}")


def write_push_log(log_path: str, **fields):
    """
    Append one row to push_log.csv (thread-safe).
    The message field always ends with --END-- as a sentinel so the HTML
    dashboard knows where the message text finishes.
    """
    row = {h: fields.get(h, "") for h in PUSH_LOG_HEADERS}
    row["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = row.get("message", "") or ""
    msg = flatten_msg(msg)
    msg = msg.rstrip().removesuffix("--END--").rstrip()
    row["message"] = f"{msg} --END--" if msg else "--END--"
    with PUSH_LOG_LOCK:
        with open(log_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=PUSH_LOG_HEADERS)
            writer.writerow(row)
    if STATS_PANEL is not None:
        STATS_PANEL.invalidate()
    log.debug(f"Push log written: status={row['status']} file={row['file_changed']}")


def commits_last_7_days(log_path: str) -> int:
    """Count successful pushes recorded in push_log.csv over the past 7 days."""
    cutoff = datetime.now() - timedelta(days=7)
    count = 0
    try:
        with PUSH_LOG_LOCK:
            with open(log_path, newline="") as f:
                for row in csv.DictReader(f):
                    if (row.get("status") or "").strip() != "success":
                        continue
                    try:
                        ts = datetime.strptime(
                            (row.get("timestamp") or "").strip(), "%Y-%m-%d %H:%M:%S"
                        )
                    except ValueError:
                        continue
                    if ts >= cutoff:
                        count += 1
    except OSError as exc:
        log.debug(f"Could not read push log for 7-day stat: {exc}")
    return count


def log_weekly_commits(log_path: str):
    log.info(f"📊 Commits pushed in past 7 days: {commits_last_7_days(log_path)}")


def commits_per_day_last_7(log_path: str) -> dict:
    """Successful pushes per calendar day for the past 7 days (oldest first)."""
    today = datetime.now().date()
    counts = {today - timedelta(days=i): 0 for i in range(6, -1, -1)}
    try:
        with PUSH_LOG_LOCK:
            with open(log_path, newline="") as f:
                for row in csv.DictReader(f):
                    if (row.get("status") or "").strip() != "success":
                        continue
                    try:
                        ts = datetime.strptime(
                            (row.get("timestamp") or "").strip(), "%Y-%m-%d %H:%M:%S"
                        )
                    except ValueError:
                        continue
                    d = ts.date()
                    if d in counts:
                        counts[d] += 1
    except OSError:
        pass  # keep zeros; may run inside a log-handler emit, so don't log here
    return counts


# ══════════════════════════════════════════════════════════════════════════════
# Live stats panel — per-day push counts pinned to the terminal's top-right
# ══════════════════════════════════════════════════════════════════════════════

# Shades for log-line timestamps: darkest green for lines logged today,
# progressively lighter the older the line's date is. Index = days ago.
_DAY_SHADES = [
    "\x1b[38;5;34m",    # today      — deep green
    "\x1b[38;5;40m",
    "\x1b[38;5;46m",
    "\x1b[38;5;83m",
    "\x1b[38;5;120m",
    "\x1b[38;5;157m",
    "\x1b[38;5;194m",   # 6 days ago — palest green
]


class StatsPanel:
    """
    Repaints a small box in the top-right corner of the terminal after every
    console log line (and periodically from the main loop), showing successful
    pushes per day over the past 7 days. No-op when the stream isn't a TTY.
    """
    WIDTH     = 30            # total box width incl. borders
    BAR_WIDTH = 10
    CACHE_TTL = 60            # seconds between push_log.csv re-reads
    MIN_COLS  = WIDTH + 50    # skip drawing on terminals too narrow to share

    def __init__(self, push_log_path: str):
        self.push_log_path = push_log_path
        self._cache        = None
        self._cache_time   = 0.0
        self._draw_lock    = threading.Lock()

    def invalidate(self):
        """Force a CSV re-read on the next draw (called after each push)."""
        self._cache_time = 0.0

    def _counts(self) -> dict:
        now = time.time()
        if self._cache is None or now - self._cache_time > self.CACHE_TTL:
            self._cache      = commits_per_day_last_7(self.push_log_path)
            self._cache_time = now
        return self._cache

    def _render(self) -> list:
        counts = self._counts()
        inner  = self.WIDTH - 2
        total  = sum(counts.values())
        peak   = max(counts.values()) or 1
        today  = datetime.now().date()

        border = _LEVEL_COLOURS["DEBUG"]
        lines  = [f"{border}┌{' Pushes · last 7 days '.center(inner, '─')}┐{_R}"]
        for d, n in counts.items():
            bar  = "▇" * (max(1, round(n / peak * self.BAR_WIDTH)) if n else 0)
            text = f" {d.strftime('%a %d')} {bar:<{self.BAR_WIDTH}} {n:>4} ".ljust(inner)[:inner]
            if d == today:
                body = f"{_MSG_COLOURS['✓']}{_BOLD}{text}{_R}"
            elif n == 0:
                body = f"{border}{text}{_R}"
            else:
                body = f"{_TIME_COLOUR}{text}{_R}"
            lines.append(f"{border}│{_R}{body}{border}│{_R}")
        lines.append(f"{border}├{'─' * inner}┤{_R}")
        total_text = f" Total {total:>{inner - 8}} "
        lines.append(f"{border}│{_R}{_BOLD}{total_text}{_R}{border}│{_R}")
        lines.append(f"{border}└{'─' * inner}┘{_R}")
        return lines

    def draw(self, stream=None):
        stream = stream or sys.stderr
        try:
            if not stream.isatty():
                return
            cols = shutil.get_terminal_size().columns
        except (OSError, ValueError, AttributeError):
            return
        if cols < self.MIN_COLS:
            return
        left = cols - self.WIDTH + 1
        with self._draw_lock:
            out = ["\x1b7"]                               # save cursor
            for i, line in enumerate(self._render(), start=1):
                out.append(f"\x1b[{i};{left}H{line}")     # paint row i at right edge
            out.append("\x1b8")                           # restore cursor
            try:
                stream.write("".join(out))
                stream.flush()
            except (OSError, ValueError):
                pass


STATS_PANEL = None   # set in AutoGitPusher.start(); None until then


class PanelStreamHandler(logging.StreamHandler):
    """Console handler that repaints the stats panel after each log line."""
    def emit(self, record):
        super().emit(record)
        if STATS_PANEL is not None:
            STATS_PANEL.draw(self.stream)


# ══════════════════════════════════════════════════════════════════════════════
# Live split-screen UI — dashboard (left) + logs (right), driven by dashboard.py
# ══════════════════════════════════════════════════════════════════════════════

class _BufferHandler(logging.Handler):
    """Feeds INFO+ log records into the LiveUI's log buffer (never prints).

    Records are rendered with the same ColourFormatter the console uses, so the
    live log pane looks identical to the classic scrolling output — full
    timestamp, coloured level tag, purple [repo], keyword-coloured message.
    """
    def __init__(self, ui: "LiveUI"):
        super().__init__()
        self.ui = ui
        self.setFormatter(ColourFormatter(datefmt=LOG_DATE_FORMAT))

    def emit(self, record):
        try:
            self.ui.push_line(self.format(record))
        except Exception:       # logging must never raise
            pass


class LiveUI:
    """
    Full-screen renderer that repaints on its own thread:

      • terminal wide  (cols >= split_cols) → analytics dashboard on the LEFT
        half + live log tail on the RIGHT half
      • terminal narrow (cols <  split_cols) → logs only, full width

    All rendering is delegated to dashboard.py so the two stay in lockstep.
    Active only on a TTY with dashboard.py importable; otherwise inert and the
    watcher keeps its classic scrolling output.
    """
    def __init__(self, push_log_path: str, logfile: str, split_cols: int = 120,
                 interval: float = 0.5):
        self.push_log_path = push_log_path
        self.logfile       = logfile
        self.split_cols    = split_cols
        self.interval      = interval
        self._buf          = deque(maxlen=500)
        self._stop         = threading.Event()
        self._thread       = None
        self._out          = sys.stdout
        self._stats        = None
        self._stats_mtime  = -1.0

    # -- logging bridge --------------------------------------------------------
    def handler(self) -> logging.Handler:
        h = _BufferHandler(self)
        h.setLevel(logging.INFO)
        return h

    def push_log_line(self, ts: str, level: str, msg: str):
        self._buf.append((ts, level, msg))

    # -- lifecycle -------------------------------------------------------------
    def active(self) -> bool:
        return _dash is not None and self._out.isatty()

    def start(self) -> bool:
        if not self.active():
            return False
        self._out.write("\x1b[?1049h\x1b[?25l\x1b[2J")   # alt screen, hide cursor, clear
        self._out.flush()
        self._thread = threading.Thread(target=self._loop, name="LiveUI", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        try:
            self._out.write("\x1b[?25h\x1b[?1049l")       # show cursor, leave alt screen
            self._out.flush()
        except (OSError, ValueError):
            pass

    # -- render loop -----------------------------------------------------------
    def _current_stats(self):
        try:
            mtime = os.path.getmtime(self.push_log_path)
            if mtime != self._stats_mtime:
                self._stats = _dash.Stats(_dash.load(self.push_log_path))
                self._stats_mtime = mtime
        except (OSError, csv.Error):
            pass
        return self._stats

    def _loop(self):
        while not self._stop.is_set():
            try:
                stats = self._current_stats()
                logs  = list(self._buf)
                cols, rows = shutil.get_terminal_size((80, 24))
                self._out.write(_dash.frame(
                    stats, logs, None, self.push_log_path, self.logfile,
                    cols, rows, self.split_cols))
                self._out.flush()
            except (OSError, ValueError):
                pass
            except Exception:
                pass          # a render glitch must never kill the watcher
            self._stop.wait(self.interval)


# ══════════════════════════════════════════════════════════════════════════════
# Git helpers
# ══════════════════════════════════════════════════════════════════════════════

# Absolute path to git: subprocess only takes the posix_spawn() fast path when
# the executable has a directory component (no PATH search in posix_spawn).
GIT_BIN = shutil.which("git") or "git"

def run(cmd: list, cwd: str) -> tuple[int, str, str]:
    """Run a git command, log it at DEBUG level, return (code, stdout, stderr)."""
    log.debug(f"  $ {' '.join(cmd)}  [cwd={cwd}]")
    # Never block on an interactive credential prompt. If git can't find a
    # credential helper / stored token it fails fast with a clear error that
    # we log, instead of hanging on "Username for 'https://github.com':".
    env = {k: v for k, v in os.environ.items() if not k.startswith("Malloc")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", "")
    env.setdefault("SSH_ASKPASS", "")
    # Use `git -C <dir>` instead of cwd=, an absolute git path, and
    # close_fds=False so CPython spawns via posix_spawn() rather than fork().
    # fork() trips libmalloc's at-fork handler on recent macOS, which spams
    # "MallocStackLogging: can't turn off malloc stack logging" to the terminal.
    if cmd and cmd[0] == "git":
        cmd = [GIT_BIN, "-C", cwd] + cmd[1:]
        result = subprocess.run(cmd, capture_output=True, text=True, env=env,
                                close_fds=False)
    else:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                env=env)
    if result.stdout.strip():
        log.debug(f"  stdout: {result.stdout.strip()[:400]}")
    if result.stderr.strip():
        log.debug(f"  stderr: {result.stderr.strip()[:400]}")
    return result.returncode, result.stdout.strip(), result.stderr.strip()


# Files that are always append-only — rebase conflicts are resolved by keeping
# the local (ours) version rather than failing.
APPEND_ONLY_PATTERNS = {"push_log.csv", "push_log.csv.bak"}

def is_append_only(filename: str) -> bool:
    return Path(filename).name in APPEND_ONLY_PATTERNS

# GitHub hard limit
GITHUB_MAX_BYTES = 100 * 1024 * 1024   # 100 MB

# Dirs never to commit (supplements .gitignore)
DEFAULT_IGNORE_DIRS = {
    "venv", ".venv", "env", ".env",
    "node_modules", "__pycache__",
    ".tox", "dist", "build",
    ".mypy_cache", ".pytest_cache",
}


def check_large_files(local_path: str, repo_name: str) -> list:
    """Return staged files exceeding GITHUB_MAX_BYTES."""
    _, files_out, _ = run(["git", "diff", "--cached", "--name-only"], cwd=local_path)
    oversized = []
    for rel in files_out.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        abs_path = os.path.join(local_path, rel)
        if os.path.isfile(abs_path):
            size = os.path.getsize(abs_path)
            if size >= GITHUB_MAX_BYTES:
                mb = size / (1024 * 1024)
                log.warning(f"[{repo_name}] Oversized: {rel} ({mb:.1f} MB) — GitHub limit is 100 MB")
                oversized.append(rel)
    return oversized


def unstage_and_ignore(local_path: str, repo_name: str, files: list):
    """Unstage oversized files and add them to .gitignore."""
    gitignore_path = os.path.join(local_path, ".gitignore")
    existing = set()
    if os.path.exists(gitignore_path):
        with open(gitignore_path) as f:
            existing = {ln.strip() for ln in f}
    new_entries = []
    for f in files:
        run(["git", "rm", "--cached", "--force", f], cwd=local_path)
        log.info(f"[{repo_name}] Unstaged {f} from index")
        if f not in existing:
            new_entries.append(f)
    if new_entries:
        with open(gitignore_path, "a") as gi:
            gi.write("\n# Auto-added by watcher (too large for GitHub)\n")
            for entry in new_entries:
                gi.write(entry + "\n")
        run(["git", "add", ".gitignore"], cwd=local_path)
        log.info(f"[{repo_name}] Added to .gitignore: {new_entries}")


def ensure_gitignore(local_path: str, repo_name: str):
    """Ensure common junk dirs are in .gitignore."""
    gitignore_path = os.path.join(local_path, ".gitignore")
    existing_text  = ""
    if os.path.exists(gitignore_path):
        with open(gitignore_path) as f:
            existing_text = f.read()
    missing = [d for d in DEFAULT_IGNORE_DIRS if (d + "/") not in existing_text and d not in existing_text]
    if not missing:
        return
    with open(gitignore_path, "a") as gi:
        gi.write("\n# Auto-added by watcher\n")
        for d in missing:
            gi.write(f"{d}/\n")
    log.info(f"[{repo_name}] Updated .gitignore with: {missing}")
    run(["git", "add", ".gitignore"], cwd=local_path)



def is_auth_or_network_error(err: str) -> bool:
    """
    True if a git pull/push failure is due to authentication or connectivity,
    NOT a merge conflict. These need a stored token / network, not conflict
    resolution, so they should be reported honestly instead of as "conflicts".
    """
    e = err.lower()
    signals = (
        "could not read username",
        "terminal prompts disabled",
        "authentication failed",
        "invalid username or token",
        "password authentication is not supported",
        "permission denied",
        "could not resolve host",
        "could not resolve proxy",
        "connection timed out",
        "connection refused",
        "failed to connect",
        "does not appear to be a git repository",
        "repository not found",
    )
    return any(s in e for s in signals)


def resolve_rebase_conflict(local_path: str, repo_name: str) -> bool:
    """
    After a failed `git pull --rebase`, attempt to auto-resolve conflicts:
      - Append-only files (push_log.csv) → keep ours (local version)
      - All other conflicted files        → abort and report

    Returns True if conflict was fully resolved and rebase can continue.
    Returns False if conflict could not be auto-resolved (rebase is aborted).
    """
    _, status_out, _ = run(["git", "status", "--porcelain"], cwd=local_path)
    conflicted = [
        line[3:].strip()
        for line in status_out.splitlines()
        if line.startswith("UU") or line.startswith("AA") or line.startswith("DD")
        or line[:2] in ("DU", "UD", "AU", "UA")
    ]

    if not conflicted:
        log.warning(f"[{repo_name}] Rebase issue but no conflicted files found.")
        run(["git", "rebase", "--abort"], cwd=local_path)
        return False

    log.info(f"[{repo_name}] Conflicted files: {conflicted}")

    unresolvable = [f for f in conflicted if not is_append_only(f)]
    if unresolvable:
        log.error(
            f"[{repo_name}] Cannot auto-resolve conflicts in: {unresolvable}. "
            "Aborting rebase — resolve manually."
        )
        run(["git", "rebase", "--abort"], cwd=local_path)
        return False

    # All conflicts are in append-only files — keep ours
    for f in conflicted:
        log.info(f"[{repo_name}] Auto-resolving conflict in {f} → keeping local version")
        run(["git", "checkout", "--ours", f], cwd=local_path)
        run(["git", "add", f], cwd=local_path)

    code, _, err = run(
        ["git", "-c", "core.editor=true", "rebase", "--continue"],
        cwd=local_path
    )
    if code != 0:
        log.error(f"[{repo_name}] Rebase --continue failed: {err}")
        run(["git", "rebase", "--abort"], cwd=local_path)
        return False

    log.info(f"[{repo_name}] Rebase conflict auto-resolved successfully.")
    return True


def ensure_repo(local_path: str, repo_url: str, repo_name: str) -> bool:
    path = Path(local_path)
    path.mkdir(parents=True, exist_ok=True)

    if not (path / ".git").exists():
        contents = [p for p in path.iterdir() if p.name != ".git"]
        if not contents:
            log.info(f"[{repo_name}] Cloning {repo_url} → {local_path}")
            code, _, err = run(["git", "clone", repo_url, "."], cwd=local_path)
            if code != 0:
                log.error(f"[{repo_name}] Clone failed: {err}")
                return False
        else:
            log.info(f"[{repo_name}] Initialising existing directory as git repo")
            run(["git", "init"], cwd=local_path)
            run(["git", "remote", "add", "origin", repo_url], cwd=local_path)
            run(["git", "checkout", "-b", "main"], cwd=local_path)
    else:
        code, remote_url, _ = run(["git", "remote", "get-url", "origin"], cwd=local_path)
        if code != 0:
            run(["git", "remote", "add", "origin", repo_url], cwd=local_path)
        elif remote_url != repo_url:
            log.warning(
                f"[{repo_name}] Remote URL mismatch "
                f"(found {remote_url!r}, expected {repo_url!r}) — fixing"
            )
            run(["git", "remote", "set-url", "origin", repo_url], cwd=local_path)

    ensure_gitignore(local_path, repo_name)
    log.info(f"[{repo_name}] Repo ready at {local_path}")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Startup sync — push offline changes on watcher start
# ══════════════════════════════════════════════════════════════════════════════

def startup_sync(local_path: str, repo_name: str, repo_url: str, push_log_path: str):
    """
    Called once per repo when the watcher starts.
    Detects any files changed while the watcher was offline and pushes them.
    """
    log.info(f"[{repo_name}] Startup sync — checking for offline changes…")

    _, ss_out, _ = run(["git", "stash", "--include-untracked"], cwd=local_path)
    ss_stashed = "No local changes" not in ss_out
    if ss_stashed:
        log.debug(f"[{repo_name}] Startup: stashed working tree before pull")

    # -c diff.ignoreSubmodules=all → dirty nested repos (gitlinks) can't be
    # stashed and would otherwise fail rebase's clean-tree check forever.
    # --autostash → files written between our stash and the rebase (e.g. an
    # app appending to a log) get stashed by the rebase itself, closing the race.
    pull_code, _, pull_err = run(
        ["git", "-c", "diff.ignoreSubmodules=all", "pull", "--rebase",
         "--autostash", "origin", "HEAD"], cwd=local_path
    )
    if pull_code != 0:
        if is_auth_or_network_error(pull_err):
            if ss_stashed:
                run(["git", "stash", "pop"], cwd=local_path)
            log.error(
                f"[{repo_name}] Startup sync skipped — cannot reach remote "
                f"(auth/network): {pull_err}"
            )
            write_push_log(push_log_path, repo_name=repo_name, repo_url=repo_url,
                           file_changed="", event_type="startup-sync",
                           status="failed", message=f"auth/network error: {pull_err}")
            return
        log.warning(f"[{repo_name}] Startup pull --rebase issue: {pull_err}")
        if not resolve_rebase_conflict(local_path, repo_name):
            if ss_stashed:
                run(["git", "stash", "pop"], cwd=local_path)
            log.error(f"[{repo_name}] Startup sync aborted due to unresolvable conflict.")
            return

    if ss_stashed:
        pop_code, _, pop_err = run(["git", "stash", "pop"], cwd=local_path)
        if pop_code != 0:
            log.warning(f"[{repo_name}] Startup stash pop issue (non-fatal): {pop_err}")

    _, status_out, _ = run(["git", "status", "--porcelain"], cwd=local_path)
    if not status_out:
        log.info(f"[{repo_name}] Startup sync: nothing to push — already up to date.")
        return

    changed_files = []
    for line in status_out.splitlines():
        if not line.strip():
            continue
        # Porcelain format is "XY PATH" (2 status chars + space); slice the raw
        # line so paths keep their first character (don't strip() beforehand).
        parts = line[3:].split(" -> ")
        changed_files.append(parts[-1].strip())

    log.info(f"[{repo_name}] Startup sync: {len(changed_files)} offline change(s) → {changed_files[:5]}")

    code, _, err = run(["git", "add", "-A"], cwd=local_path)
    if code != 0:
        log.error(f"[{repo_name}] Startup sync: git add failed: {err}")
        return

    # Nothing actually staged? This happens when the only changes live inside
    # nested/submodule repos (dirty content the parent can't stage) or when
    # everything is gitignored. Skip gracefully instead of failing the commit.
    _, cached_out, _ = run(["git", "diff", "--cached", "--name-only"], cwd=local_path)
    if not cached_out.strip():
        log.info(
            f"[{repo_name}] Startup sync: nothing staged after git add "
            "(changes may be inside submodules or gitignored) — skipping."
        )
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if len(changed_files) <= 5:
        names   = ", ".join(Path(f).name for f in changed_files)
        subject = f"startup-sync: {names} [{ts}]"
        commit_msg = subject
    else:
        subject    = f"startup-sync: {len(changed_files)} offline changes [{ts}]"
        body       = "Files changed while watcher was offline:\n" + "\n".join(f"- {f}" for f in changed_files)
        commit_msg = f"{subject}\n\n{body}"

    code, _, err = run(["git", "commit", "-m", commit_msg], cwd=local_path)
    if code != 0:
        log.error(f"[{repo_name}] Startup sync commit failed: {err}")
        write_push_log(push_log_path, repo_name=repo_name, repo_url=repo_url,
                       file_changed=", ".join(changed_files[:5]),
                       event_type="startup-sync", status="error",
                       message=f"commit failed: {err}")
        return

    code, out, err = run(
        ["git", "push", "--set-upstream", "origin", "HEAD"], cwd=local_path
    )
    if code != 0:
        log.error(f"[{repo_name}] Startup sync push failed: {err}")
        write_push_log(push_log_path, repo_name=repo_name, repo_url=repo_url,
                       file_changed=", ".join(changed_files[:5]),
                       event_type="startup-sync", status="failed", message=err)
    else:
        log.info(f"[{repo_name}] Startup sync: pushed {len(changed_files)} change(s) ✓")
        write_push_log(push_log_path, repo_name=repo_name, repo_url=repo_url,
                       file_changed=", ".join(changed_files[:5]),
                       event_type="startup-sync", status="success", message=subject)


# ══════════════════════════════════════════════════════════════════════════════
# Smart commit message builder
# ══════════════════════════════════════════════════════════════════════════════

def build_commit_message(local_path: str, ts: str) -> str:
    """
    ≤ 5 staged files → list filenames in subject.
    > 5 staged files → count in subject, full list in body.
    """
    _, files_out, _ = run(
        ["git", "diff", "--cached", "--name-only"], cwd=local_path
    )
    staged = [f.strip() for f in files_out.splitlines() if f.strip()]

    if not staged:
        return f"auto-push: bulk update [{ts}]"
    if len(staged) <= 5:
        names = ", ".join(Path(f).name for f in staged)
        return f"auto-push: {names} [{ts}]"

    subject = f"auto-push: {len(staged)} files changed [{ts}]"
    body    = "Modified files:\n" + "\n".join(f"- {f}" for f in staged)
    return f"{subject}\n\n{body}"


# ══════════════════════════════════════════════════════════════════════════════
# Main push function
# ══════════════════════════════════════════════════════════════════════════════

def git_add_commit_push(
    local_path: str,
    repo_name: str,
    repo_url: str,
    push_log_path: str,
    changed_file: str = "",
    event_type: str = "modified",
):
    t0 = time.time()

    def _log(status: str, msg: str):
        elapsed = f"{time.time()-t0:.2f}s"
        log.info(f"[{repo_name}] Push result: {status} ({elapsed}) — {msg[:120]}")
        write_push_log(
            push_log_path, repo_name=repo_name, repo_url=repo_url,
            file_changed=changed_file, event_type=event_type,
            status=status, message=msg,
        )
        if status == "success":
            log_weekly_commits(push_log_path)

    # Stage
    log.debug(f"[{repo_name}] Staging all changes…")
    code, _, err = run(["git", "add", "-A"], cwd=local_path)
    if code != 0:
        log.error(f"[{repo_name}] git add failed: {err}")
        _log("error", f"git add failed: {err}")
        return

    # Nothing to commit?
    code, status_out, _ = run(["git", "status", "--porcelain"], cwd=local_path)
    if not status_out:
        log.info(f"[{repo_name}] Nothing to commit.")
        return

    # Reject oversized files before committing
    oversized = check_large_files(local_path, repo_name)
    if oversized:
        log.warning(f"[{repo_name}] Removing {len(oversized)} oversized file(s) from commit")
        unstage_and_ignore(local_path, repo_name, oversized)
        _, status_out2, _ = run(["git", "status", "--porcelain"], cwd=local_path)
        if not status_out2:
            log.info(f"[{repo_name}] Nothing left to commit after removing oversized files.")
            _log("skipped", f"oversized files removed: {', '.join(oversized)}")
            return

    # Verify something is actually staged before committing
    _, cached_out, _ = run(["git", "diff", "--cached", "--name-only"], cwd=local_path)
    if not cached_out.strip():
        log.info(f"[{repo_name}] Nothing staged after git add (files may be gitignored).")
        return

    # Build commit message
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = build_commit_message(local_path, ts)
    subject_line = msg.splitlines()[0]
    log.info(f"[{repo_name}] Committing: {subject_line}")

    code, out, err = run(["git", "commit", "-m", msg], cwd=local_path)
    if code != 0:
        reason = err.strip() or out.strip() or "unknown reason"
        log.error(f"[{repo_name}] git commit failed: {reason}")
        _log("error", f"git commit failed: {reason}")
        return

    # Stash any unstaged changes so pull --rebase has a clean working tree
    log.info(f"[{repo_name}] Pull --rebase before push…")
    _, stash_out, _ = run(["git", "stash", "--include-untracked"], cwd=local_path)
    stashed = "No local changes" not in stash_out
    if stashed:
        log.debug(f"[{repo_name}] Stashed working tree changes: {stash_out}")

    # -c diff.ignoreSubmodules=all → dirty nested repos (gitlinks) can't be
    # stashed and would otherwise fail rebase's clean-tree check forever.
    # --autostash → files written between our stash and the rebase (e.g. an
    # app appending to a log) get stashed by the rebase itself, closing the race.
    pull_code, _, pull_err = run(
        ["git", "-c", "diff.ignoreSubmodules=all", "pull", "--rebase",
         "--autostash", "origin", "HEAD"], cwd=local_path
    )
    if pull_code != 0:
        if is_auth_or_network_error(pull_err):
            if stashed:
                run(["git", "stash", "pop"], cwd=local_path)
            log.error(f"[{repo_name}] Cannot reach remote (auth/network): {pull_err}")
            _log("failed", f"auth/network error: {pull_err}")
            return
        log.warning(f"[{repo_name}] pull --rebase issue: {pull_err}")
        resolved = resolve_rebase_conflict(local_path, repo_name)
        if not resolved:
            # Restore stash before giving up
            if stashed:
                run(["git", "stash", "pop"], cwd=local_path)
            log.error(f"[{repo_name}] Rebase not resolved — skipping push.")
            _log("failed", f"rebase conflict unresolved: {pull_err}")
            return

    # Restore stashed changes after successful rebase
    if stashed:
        pop_code, _, pop_err = run(["git", "stash", "pop"], cwd=local_path)
        if pop_code != 0:
            log.warning(f"[{repo_name}] Stash pop issue (non-fatal): {pop_err}")

    # Push
    log.info(f"[{repo_name}] Pushing to origin…")
    code, out, err = run(
        ["git", "push", "--set-upstream", "origin", "HEAD"], cwd=local_path
    )
    if code != 0:
        log.error(f"[{repo_name}] Push failed: {err}")
        _log("failed", err)
    else:
        elapsed = f"{time.time()-t0:.2f}s"
        log.info(f"[{repo_name}] ✓ Push successful in {elapsed}")
        _log("success", subject_line)


# ══════════════════════════════════════════════════════════════════════════════
# File-system watcher per repo
# ══════════════════════════════════════════════════════════════════════════════

class RepoEventHandler(FileSystemEventHandler):
    COOLDOWN = 5  # seconds

    def __init__(self, local_path, repo_name, repo_url, push_log_path):
        super().__init__()
        self.local_path = local_path
        self.repo_name  = repo_name
        self.repo_url   = repo_url
        self.push_log   = push_log_path
        self._last_push = 0.0

    def _should_ignore(self, path: str) -> bool:
        ignore = {".git", "__pycache__", ".DS_Store", "Thumbs.db"}
        parts  = Path(path).parts
        return any(p in ignore for p in parts) or path.endswith((".tmp", ".swp", "~"))

    def _handle(self, event, event_type: str):
        if event.is_directory or self._should_ignore(event.src_path):
            return
        now = time.time()
        if now - self._last_push < self.COOLDOWN:
            log.debug(f"[{self.repo_name}] Cooldown active, skipping {event.src_path}")
            return
        self._last_push = now
        rel = os.path.relpath(event.src_path, self.local_path)
        log.info(f"[{self.repo_name}] {event_type.upper()}: {rel}")
        git_add_commit_push(
            self.local_path, self.repo_name, self.repo_url,
            self.push_log, changed_file=rel, event_type=event_type,
        )

    def on_created(self, e):  self._handle(e, "created")
    def on_modified(self, e): self._handle(e, "modified")
    def on_moved(self, e):    self._handle(e, "moved")
    def on_deleted(self, e):  self._handle(e, "deleted")


# ══════════════════════════════════════════════════════════════════════════════
# CSV config watcher — hot-reload when config file changes
# ══════════════════════════════════════════════════════════════════════════════

class ConfigCSVHandler(FileSystemEventHandler):
    def __init__(self, csv_path, reload_callback):
        super().__init__()
        self._csv_path        = os.path.abspath(csv_path)
        self._reload_callback = reload_callback
        self._last_reload     = 0.0

    def _trigger(self, path: str):
        if os.path.abspath(path) != self._csv_path:
            return
        now = time.time()
        if now - self._last_reload < 2:
            return
        self._last_reload = now
        log.info("Config CSV changed — reloading…")
        self._reload_callback()

    def on_modified(self, event): self._trigger(event.src_path)
    def on_created(self, event):  self._trigger(event.src_path)
    # Atomic-write editors (incl. this repo's own Edit tooling) save by writing
    # a temp file then renaming it over the target — watchdog reports that as
    # a move, not a modify/create, so it must be handled here too or the
    # reload never fires and the CSV edit is silently ignored.
    def on_moved(self, event):    self._trigger(event.dest_path)


# ══════════════════════════════════════════════════════════════════════════════
# CSV helpers
# ══════════════════════════════════════════════════════════════════════════════

def load_csv(csv_path: str) -> list[dict]:
    repos = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = {k.strip(): (v.strip() if v is not None else "") for k, v in row.items()}
            if row.get("local_path") and row.get("repo_url") and row.get("repo_name"):
                repos.append(row)
            else:
                log.warning(f"Skipping incomplete CSV row: {row}")
    return repos


# ══════════════════════════════════════════════════════════════════════════════
# Main orchestrator
# ══════════════════════════════════════════════════════════════════════════════

class AutoGitPusher:
    def __init__(self, csv_path: str, push_log_path: str, live_ui: "LiveUI" = None):
        self.csv_path      = csv_path
        self.push_log_path = push_log_path
        self.live_ui       = live_ui
        self.observer      = Observer()
        self._watched: dict = {}

    def _add_repo(self, repo: dict):
        local_path = repo["local_path"]
        repo_url   = repo["repo_url"]
        repo_name  = repo["repo_name"]

        if local_path in self._watched:
            return

        log.info(f"[{repo_name}] Setting up repo…")
        ok = ensure_repo(local_path, repo_url, repo_name)
        if not ok:
            log.warning(f"[{repo_name}] Skipping — setup failed.")
            return

        startup_sync(local_path, repo_name, repo_url, self.push_log_path)

        handler = RepoEventHandler(local_path, repo_name, repo_url, self.push_log_path)
        watch   = self.observer.schedule(handler, path=local_path, recursive=True)
        self._watched[local_path] = watch
        log.info(f"[{repo_name}] Watching: {local_path}")

    def reload_config(self):
        try:
            repos = load_csv(self.csv_path)
        except Exception as exc:
            log.error(f"Failed to reload CSV: {exc}")
            return

        current_paths = set(self._watched.keys())
        new_paths     = {r["local_path"] for r in repos}

        for repo in repos:
            if repo["local_path"] not in current_paths:
                log.info(f"New repo detected: {repo['repo_name']}")
                self._add_repo(repo)

        for removed in current_paths - new_paths:
            log.warning(f"{removed} removed from CSV (still watching until restart)")

    def start(self):
        global STATS_PANEL
        # The LiveUI renders its own full-screen dashboard, so the small
        # top-right stats panel is only used in the classic (non-UI) path.
        STATS_PANEL = None if self.live_ui else StatsPanel(self.push_log_path)

        init_push_log(self.push_log_path)

        repos = load_csv(self.csv_path)
        if not repos:
            log.error("No valid entries in CSV. Exiting.")
            return

        log.info(f"Loaded {len(repos)} repo(s) from {self.csv_path}")
        for repo in repos:
            self._add_repo(repo)

        csv_handler = ConfigCSVHandler(self.csv_path, self.reload_config)
        self.observer.schedule(
            csv_handler,
            path=str(Path(self.csv_path).parent),
            recursive=False,
        )

        self.observer.start()
        log.info("Auto Git Pusher v4 running. Press Ctrl+C to stop.\n")
        log.info(f"Detailed logs → {logging.getLogger().handlers[1].baseFilename}")
        log_weekly_commits(self.push_log_path)

        STAT_INTERVAL  = 3600  # repeat the 7-day commit stat every hour
        PANEL_INTERVAL = 5     # keep the stats panel painted even when idle
        next_stat  = time.time() + STAT_INTERVAL
        next_panel = time.time() + PANEL_INTERVAL
        try:
            while True:
                time.sleep(1)
                if STATS_PANEL is not None and time.time() >= next_panel:
                    next_panel = time.time() + PANEL_INTERVAL
                    STATS_PANEL.draw()
                if time.time() >= next_stat:
                    next_stat = time.time() + STAT_INTERVAL
                    log_weekly_commits(self.push_log_path)
        except KeyboardInterrupt:
            log.info("Stopping…")
            self.observer.stop()

        self.observer.join()
        log.info("Done.")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Auto Git Pusher v4")
    parser.add_argument("--csv",     required=True,          help="Path to repos config CSV")
    parser.add_argument("--log",     default="push_log.csv", help="Push events CSV output path")
    parser.add_argument("--logfile", default="watcher.log",  help="Detailed log file path (default: watcher.log)")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Disable the live split-screen dashboard; use classic scrolling logs")
    parser.add_argument("--split-cols", type=int, default=120,
                        help="Min terminal width to show the dashboard beside the logs; "
                             "narrower shows logs only (default: 120)")
    args = parser.parse_args()

    # Build the live UI first (if wanted + possible) so its buffering handler
    # captures the startup banner too. Inert on a non-TTY or if dashboard.py
    # is unavailable, in which case the classic scrolling console is used.
    live_ui = None
    if not args.no_dashboard:
        candidate = LiveUI(args.log, args.logfile, split_cols=args.split_cols)
        if candidate.active():
            live_ui = candidate

    global log
    log = setup_logging(args.logfile, console=live_ui.handler() if live_ui else None)

    if not os.path.exists(args.csv):
        log.error(f"CSV not found: {args.csv}")
        return

    # Running under sudo uses root's HOME/keychain, so the user's stored
    # GitHub token is invisible and git falls back to interactive prompts.
    if os.geteuid() == 0 or os.environ.get("SUDO_USER"):
        log.warning(
            "Running as root (sudo) — git can't see your keychain credentials "
            "and will fail auth. Run WITHOUT sudo: python auto_git_push.py ..."
        )

    if live_ui:
        live_ui.start()      # enter alt-screen + start the render thread

    log.info("=" * 60)
    log.info("Auto Git Pusher v4 starting")
    log.info(f"  Config CSV : {args.csv}")
    log.info(f"  Push log   : {args.log}")
    log.info(f"  Watcher log: {args.logfile}")
    log.info("=" * 60)

    try:
        AutoGitPusher(csv_path=args.csv, push_log_path=args.log,
                      live_ui=live_ui).start()
    finally:
        if live_ui:
            live_ui.stop()   # always restore the terminal, even on crash


if __name__ == "__main__":
    main()
