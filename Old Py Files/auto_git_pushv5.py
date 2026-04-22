#!/usr/bin/env python3
"""
Auto Git Pusher  v3
-------------------
• Watches every local directory listed in a CSV config file
• Auto-detects CSV changes and starts monitoring new directories on-the-fly
• Commits + pushes changed files to the linked GitHub repo
• Logs every push event to push_log.csv for analytics

CSV config format  (repos_config.csv):
    local_path, repo_url, repo_name

Push log format  (push_log.csv)  – written automatically:
    timestamp, repo_name, repo_url, file_changed, event_type, status, message

Usage:
    python auto_git_push.py --csv repos_config.csv
    python auto_git_push.py --csv repos_config.csv --log push_log.csv
"""

import os
import csv
import time
import argparse
import logging
import threading
import subprocess
from pathlib import Path
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

PUSH_LOG_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# Push log helpers
# ══════════════════════════════════════════════════════════════════════════════

PUSH_LOG_HEADERS = [
    "timestamp", "repo_name", "repo_url",
    "file_changed", "event_type", "status", "message"
]


def init_push_log(log_path: str):
    """Create push_log.csv with headers if it does not exist."""
    if not os.path.exists(log_path):
        with open(log_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=PUSH_LOG_HEADERS)
            writer.writeheader()
        log.info(f"Push log created: {log_path}")


def write_push_log(log_path: str, **fields):
    """Append one row to push_log.csv (thread-safe).
    The message field always ends with --END-- as a sentinel so the HTML
    dashboard knows where the message text finishes.
    """
    row = {h: fields.get(h, "") for h in PUSH_LOG_HEADERS}
    row["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Append sentinel — strip any existing one first to avoid doubling
    msg = row.get("message", "") or ""
    msg = msg.rstrip().removesuffix("--END--").rstrip()
    row["message"] = f"{msg} --END--" if msg else "--END--"
    with PUSH_LOG_LOCK:
        with open(log_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=PUSH_LOG_HEADERS)
            writer.writerow(row)


# ══════════════════════════════════════════════════════════════════════════════
# Git helpers
# ══════════════════════════════════════════════════════════════════════════════

def run(cmd, cwd):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def ensure_repo(local_path, repo_url, repo_name):
    path = Path(local_path)
    path.mkdir(parents=True, exist_ok=True)

    if not (path / ".git").exists():
        contents = [p for p in path.iterdir() if p.name != ".git"]
        if not contents:
            log.info(f"[{repo_name}] Cloning {repo_url} -> {local_path}")
            code, _, err = run(["git", "clone", repo_url, "."], cwd=local_path)
            if code != 0:
                log.error(f"[{repo_name}] Clone failed: {err}")
                return False
        else:
            log.info(f"[{repo_name}] Init existing directory as git repo")
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




def startup_sync(local_path: str, repo_name: str, repo_url: str, push_log_path: str):
    """
    Called once per repo when the watcher starts.

    Checks whether any files were added or modified while the watcher was
    offline and pushes them immediately before live monitoring begins.

    Strategy
    --------
    1. Pull latest remote state (--rebase) so we have an up-to-date baseline.
    2. Run `git status --porcelain` to detect:
         - Untracked files   (new files added while offline)
         - Modified files    (edits made while offline)
         - Deleted files     (removals while offline)
    3. If anything is found, stage → commit → push with a clear startup message.
    4. If there is nothing to push, log "already up to date" and move on.
    """
    log.info(f"[{repo_name}] Startup sync — checking for offline changes...")

    # Pull remote changes first so our local history is current
    pull_code, _, pull_err = run(
        ["git", "pull", "--rebase", "origin", "HEAD"], cwd=local_path
    )
    if pull_code != 0:
        log.warning(f"[{repo_name}] Startup pull issue: {pull_err}")
        run(["git", "rebase", "--abort"], cwd=local_path)

    # Check for any local changes not yet committed
    _, status_out, _ = run(["git", "status", "--porcelain"], cwd=local_path)
    if not status_out:
        log.info(f"[{repo_name}] Startup sync: nothing to push, already up to date.")
        return

    # Collect what changed
    changed_files = []
    for line in status_out.splitlines():
        line = line.strip()
        if line:
            # git status --porcelain: "XY filename" or "XY old -> new"
            parts = line[3:].split(" -> ")
            changed_files.append(parts[-1].strip())  # use the new name for renames

    log.info(f"[{repo_name}] Startup sync: {len(changed_files)} offline change(s) detected.")

    # Stage everything
    code, _, err = run(["git", "add", "-A"], cwd=local_path)
    if code != 0:
        log.error(f"[{repo_name}] Startup sync: git add failed: {err}")
        return

    # Build commit message
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if len(changed_files) <= 5:
        names = ", ".join(Path(f).name for f in changed_files)
        subject = f"startup-sync: {names} [{ts}]"
        commit_msg = subject
    else:
        subject = f"startup-sync: {len(changed_files)} offline changes [{ts}]"
        body = "Files changed while watcher was offline:\n" + "\n".join(f"- {f}" for f in changed_files)
        commit_msg = f"{subject}\n\n{body}"

    log.info(f"[{repo_name}] Committing: {subject}")
    code, _, err = run(["git", "commit", "-m", commit_msg], cwd=local_path)
    if code != 0:
        log.error(f"[{repo_name}] Startup sync: commit failed: {err}")
        write_push_log(push_log_path, repo_name=repo_name, repo_url=repo_url,
                       file_changed=", ".join(changed_files[:5]),
                       event_type="startup-sync", status="error",
                       message=f"commit failed: {err}")
        return

    # Push
    code, out, err = run(
        ["git", "push", "--set-upstream", "origin", "HEAD"], cwd=local_path
    )
    if code != 0:
        log.error(f"[{repo_name}] Startup sync: push failed: {err}")
        write_push_log(push_log_path, repo_name=repo_name, repo_url=repo_url,
                       file_changed=", ".join(changed_files[:5]),
                       event_type="startup-sync", status="failed",
                       message=err)
    else:
        log.info(f"[{repo_name}] Startup sync: pushed {len(changed_files)} change(s) successfully.")
        write_push_log(push_log_path, repo_name=repo_name, repo_url=repo_url,
                       file_changed=", ".join(changed_files[:5]),
                       event_type="startup-sync", status="success",
                       message=subject)

def build_commit_message(local_path: str, ts: str) -> str:
    """
    Build a commit message subject + optional body.

    ≤ 5 staged files → subject lists filenames:
        auto-push: app.js, utils.py, README.md [2026-04-19 10:30:00]

    > 5 staged files → subject gives count; body lists every file:
        auto-push: 8 files changed [2026-04-19 10:30:00]

        Modified files:
        - src/app.js
        - src/utils.py
        ...
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
    else:
        subject = f"auto-push: {len(staged)} files changed [{ts}]"
        body    = "Modified files:
" + "
".join(f"- {f}" for f in staged)
        return f"{subject}

{body}"


def git_add_commit_push(local_path, repo_name, repo_url, push_log_path,
                        changed_file="", event_type="modified"):

    def _log(status, msg):
        write_push_log(push_log_path, repo_name=repo_name, repo_url=repo_url,
                       file_changed=changed_file, event_type=event_type,
                       status=status, message=msg)

    # Stage all changes
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

    # Build smart commit message
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = build_commit_message(local_path, ts)

    # Log first line for readability
    subject_line = msg.splitlines()[0]
    log.info(f"[{repo_name}] Committing: {subject_line}")

    code, _, err = run(["git", "commit", "-m", msg], cwd=local_path)
    if code != 0:
        log.error(f"[{repo_name}] git commit failed: {err}")
        _log("error", f"git commit failed: {err}")
        return

    # Pull --rebase before push to avoid non-fast-forward rejection
    log.info(f"[{repo_name}] Pulling remote changes...")
    pull_code, _, pull_err = run(
        ["git", "pull", "--rebase", "origin", "HEAD"], cwd=local_path)
    if pull_code != 0:
        log.warning(f"[{repo_name}] git pull --rebase issue: {pull_err}")
        run(["git", "rebase", "--abort"], cwd=local_path)

    # Push
    log.info(f"[{repo_name}] Pushing...")
    code, out, err = run(
        ["git", "push", "--set-upstream", "origin", "HEAD"], cwd=local_path)
    if code != 0:
        log.error(f"[{repo_name}] Push failed: {err}")
        _log("failed", err)
    else:
        log.info(f"[{repo_name}] Push successful.")
        _log("success", subject_line)


# ══════════════════════════════════════════════════════════════════════════════
# File-system watcher per repo
# ══════════════════════════════════════════════════════════════════════════════

class RepoEventHandler(FileSystemEventHandler):
    COOLDOWN = 5

    def __init__(self, local_path, repo_name, repo_url, push_log_path):
        super().__init__()
        self.local_path    = local_path
        self.repo_name     = repo_name
        self.repo_url      = repo_url
        self.push_log      = push_log_path
        self._last_push    = 0.0

    def _should_ignore(self, path):
        ignore = {".git", "__pycache__", ".DS_Store", "Thumbs.db"}
        parts  = Path(path).parts
        return any(p in ignore for p in parts) or path.endswith((".tmp", ".swp", "~"))

    def _handle(self, event, event_type):
        if event.is_directory or self._should_ignore(event.src_path):
            return
        now = time.time()
        if now - self._last_push < self.COOLDOWN:
            return
        self._last_push = now
        rel = os.path.relpath(event.src_path, self.local_path)
        log.info(f"[{self.repo_name}] {event_type.upper()}: {rel}")
        git_add_commit_push(self.local_path, self.repo_name, self.repo_url,
                            self.push_log, changed_file=rel, event_type=event_type)

    def on_created(self, e):  self._handle(e, "created")
    def on_modified(self, e): self._handle(e, "modified")
    def on_moved(self, e):    self._handle(e, "moved")
    def on_deleted(self, e):  self._handle(e, "deleted")


# ══════════════════════════════════════════════════════════════════════════════
# CSV config watcher  – hot-reload when config changes
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
        log.info("Config CSV changed -- reloading...")
        self._reload_callback()

    def on_modified(self, event): self._trigger(event)
    def on_created(self, event):  self._trigger(event)


# ══════════════════════════════════════════════════════════════════════════════
# CSV helpers
# ══════════════════════════════════════════════════════════════════════════════

def load_csv(csv_path):
    repos = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = {k.strip(): (v.strip() if v is not None else "") for k, v in row.items()}
            if row.get("local_path") and row.get("repo_url") and row.get("repo_name"):
                repos.append(row)
            else:
                log.warning(f"Skipping incomplete row: {row}")
    return repos


# ══════════════════════════════════════════════════════════════════════════════
# Main orchestrator
# ══════════════════════════════════════════════════════════════════════════════

class AutoGitPusher:
    def __init__(self, csv_path, push_log_path):
        self.csv_path      = csv_path
        self.push_log_path = push_log_path
        self.observer      = Observer()
        self._watched      = {}   # local_path -> watch handle

    def _add_repo(self, repo):
        local_path = repo["local_path"]
        repo_url   = repo["repo_url"]
        repo_name  = repo["repo_name"]

        if local_path in self._watched:
            return   # already watching

        ok = ensure_repo(local_path, repo_url, repo_name)
        if not ok:
            log.warning(f"[{repo_name}] Skipping -- setup failed.")
            return

        # Push any changes that accumulated while the watcher was offline
        startup_sync(local_path, repo_name, repo_url, self.push_log_path)

        handler = RepoEventHandler(local_path, repo_name, repo_url, self.push_log_path)
        watch   = self.observer.schedule(handler, path=local_path, recursive=True)
        self._watched[local_path] = watch
        log.info(f"Watching: {local_path}  ->  {repo_url}")

    def reload_config(self):
        try:
            repos = load_csv(self.csv_path)
        except Exception as exc:
            log.error(f"Failed to reload CSV: {exc}")
            return

        current_paths = set(self._watched.keys())
        for repo in repos:
            if repo["local_path"] not in current_paths:
                log.info(f"New repo detected: {repo['repo_name']}")
                self._add_repo(repo)

        new_paths = {r["local_path"] for r in repos}
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

        # Watch the CSV file itself
        csv_handler = ConfigCSVHandler(self.csv_path, self.reload_config)
        self.observer.schedule(
            csv_handler,
            path=str(Path(self.csv_path).parent),
            recursive=False,
        )

        self.observer.start()
        log.info("Auto Git Pusher running. Press Ctrl+C to stop.\n")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Stopping...")
            self.observer.stop()

        self.observer.join()
        log.info("Done.")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Auto Git Pusher v2")
    parser.add_argument("--csv", required=True, help="Path to repos config CSV")
    parser.add_argument("--log", default="push_log.csv", help="Push log CSV output path")
    args = parser.parse_args()

    if not os.path.exists(args.csv):
        log.error(f"CSV not found: {args.csv}")
        return

    AutoGitPusher(csv_path=args.csv, push_log_path=args.log).start()


if __name__ == "__main__":
    main()
