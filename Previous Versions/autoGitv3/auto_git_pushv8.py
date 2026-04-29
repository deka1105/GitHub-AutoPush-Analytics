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

CSV config format  (repos_config.csv):
    local_path, repo_url, repo_name

Push log format  (push_log.csv)  — written automatically:
    timestamp, repo_name, repo_url, file_changed, event_type, status, message

Usage:
    python auto_git_push.py --csv repos_config.csv
    python auto_git_push.py --csv repos_config.csv --log push_log.csv --logfile watcher.log
"""

import os
import csv
import time
import argparse
import logging
import logging.handlers
import threading
import subprocess
from pathlib import Path
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


# ══════════════════════════════════════════════════════════════════════════════
# Logging setup — console + rotating file
# ══════════════════════════════════════════════════════════════════════════════

LOG_FORMAT      = "%(asctime)s [%(levelname)-8s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

def setup_logging(logfile: str = "watcher.log") -> logging.Logger:
    """
    Configure root logger with:
      - Console handler  (INFO+, coloured level tags)
      - Rotating file handler (DEBUG+, max 5 MB × 3 backups)
    Returns the named logger for this module.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    # Console — INFO and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Rotating file — DEBUG and above (captures everything)
    fh = logging.handlers.RotatingFileHandler(
        logfile, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
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
    log.debug(f"Push log written: status={row['status']} file={row['file_changed']}")


# ══════════════════════════════════════════════════════════════════════════════
# Git helpers
# ══════════════════════════════════════════════════════════════════════════════

def run(cmd: list, cwd: str) -> tuple[int, str, str]:
    """Run a git command, log it at DEBUG level, return (code, stdout, stderr)."""
    log.debug(f"  $ {' '.join(cmd)}  [cwd={cwd}]")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
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
            log.warning(f"[{repo_name}] Remote URL mismatch (expected {repo_url})")

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

    pull_code, _, pull_err = run(
        ["git", "pull", "--rebase", "origin", "HEAD"], cwd=local_path
    )
    if pull_code != 0:
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
        line = line.strip()
        if line:
            parts = line[3:].split(" -> ")
            changed_files.append(parts[-1].strip())

    log.info(f"[{repo_name}] Startup sync: {len(changed_files)} offline change(s) → {changed_files[:5]}")

    code, _, err = run(["git", "add", "-A"], cwd=local_path)
    if code != 0:
        log.error(f"[{repo_name}] Startup sync: git add failed: {err}")
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

    # Build commit message
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = build_commit_message(local_path, ts)
    subject_line = msg.splitlines()[0]
    log.info(f"[{repo_name}] Committing: {subject_line}")

    code, _, err = run(["git", "commit", "-m", msg], cwd=local_path)
    if code != 0:
        log.error(f"[{repo_name}] git commit failed: {err}")
        _log("error", f"git commit failed: {err}")
        return

    # Stash any unstaged changes so pull --rebase has a clean working tree
    log.info(f"[{repo_name}] Pull --rebase before push…")
    _, stash_out, _ = run(["git", "stash", "--include-untracked"], cwd=local_path)
    stashed = "No local changes" not in stash_out
    if stashed:
        log.debug(f"[{repo_name}] Stashed working tree changes: {stash_out}")

    pull_code, _, pull_err = run(
        ["git", "pull", "--rebase", "origin", "HEAD"], cwd=local_path
    )
    if pull_code != 0:
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

    def _trigger(self, event):
        if os.path.abspath(event.src_path) != self._csv_path:
            return
        now = time.time()
        if now - self._last_reload < 2:
            return
        self._last_reload = now
        log.info("Config CSV changed — reloading…")
        self._reload_callback()

    def on_modified(self, event): self._trigger(event)
    def on_created(self, event):  self._trigger(event)


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
    def __init__(self, csv_path: str, push_log_path: str):
        self.csv_path      = csv_path
        self.push_log_path = push_log_path
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

        try:
            while True:
                time.sleep(1)
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
    args = parser.parse_args()

    global log
    log = setup_logging(args.logfile)

    if not os.path.exists(args.csv):
        log.error(f"CSV not found: {args.csv}")
        return

    log.info("=" * 60)
    log.info("Auto Git Pusher v4 starting")
    log.info(f"  Config CSV : {args.csv}")
    log.info(f"  Push log   : {args.log}")
    log.info(f"  Watcher log: {args.logfile}")
    log.info("=" * 60)

    AutoGitPusher(csv_path=args.csv, push_log_path=args.log).start()


if __name__ == "__main__":
    main()
