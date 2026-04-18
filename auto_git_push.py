#!/usr/bin/env python3
"""
Auto Git Pusher
---------------
Watches local directories defined in a CSV file and automatically
commits + pushes any new or modified files to the linked GitHub repo.

CSV Format:
    local_path, repo_url, repo_name

Usage:
    python auto_git_push.py --csv repos_config.csv
    python auto_git_push.py --csv repos_config.csv --interval 10
"""

import os
import csv
import time
import argparse
import logging
import subprocess
from pathlib import Path
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Git helpers ───────────────────────────────────────────────────────────────

def run(cmd: list[str], cwd: str) -> tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def ensure_repo(local_path: str, repo_url: str, repo_name: str) -> bool:
    """
    Make sure the local directory is a git repo linked to repo_url.
    Clones if the directory is empty / doesn't exist.
    Initialises + adds remote if directory exists but isn't a repo.
    """
    path = Path(local_path)
    path.mkdir(parents=True, exist_ok=True)

    git_dir = path / ".git"

    if not git_dir.exists():
        # Check if the directory is empty → clone into it
        contents = list(path.iterdir())
        if not contents:
            log.info(f"[{repo_name}] Cloning {repo_url} → {local_path}")
            code, _, err = run(["git", "clone", repo_url, "."], cwd=local_path)
            if code != 0:
                log.error(f"[{repo_name}] Clone failed: {err}")
                return False
        else:
            # Existing files → init + add remote
            log.info(f"[{repo_name}] Initialising existing directory as git repo")
            run(["git", "init"], cwd=local_path)
            run(["git", "remote", "add", "origin", repo_url], cwd=local_path)
            run(["git", "checkout", "-b", "main"], cwd=local_path)
    else:
        # Already a repo – make sure the remote is correct
        code, remote_url, _ = run(
            ["git", "remote", "get-url", "origin"], cwd=local_path
        )
        if code != 0:
            run(["git", "remote", "add", "origin", repo_url], cwd=local_path)
        elif remote_url != repo_url:
            log.warning(
                f"[{repo_name}] Remote mismatch – expected {repo_url}, got {remote_url}"
            )

    log.info(f"[{repo_name}] Repo ready at {local_path}")
    return True


def git_add_commit_push(local_path: str, repo_name: str, changed_file: str = None):
    """Stage all changes, commit, and push to origin."""
    # Stage
    code, _, err = run(["git", "add", "-A"], cwd=local_path)
    if code != 0:
        log.error(f"[{repo_name}] git add failed: {err}")
        return

    # Check if there is anything to commit
    code, status, _ = run(["git", "status", "--porcelain"], cwd=local_path)
    if not status:
        log.info(f"[{repo_name}] Nothing to commit.")
        return

    # Commit
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = (
        f"auto-push: {changed_file} [{ts}]"
        if changed_file
        else f"auto-push: bulk update [{ts}]"
    )
    code, _, err = run(["git", "commit", "-m", msg], cwd=local_path)
    if code != 0:
        log.error(f"[{repo_name}] git commit failed: {err}")
        return

    # Pull latest remote changes first to avoid non-fast-forward rejection
    log.info(f"[{repo_name}] Pulling remote changes before push…")
    pull_code, _, pull_err = run(
        ["git", "pull", "--rebase", "origin", "HEAD"], cwd=local_path
    )
    if pull_code != 0:
        log.warning(f"[{repo_name}] git pull --rebase had issues: {pull_err}")
        run(["git", "rebase", "--abort"], cwd=local_path)

    # Push
    log.info(f"[{repo_name}] Pushing → {repo_name}…")
    code, out, err = run(
        ["git", "push", "--set-upstream", "origin", "HEAD"], cwd=local_path
    )
    if code != 0:
        log.error(f"[{repo_name}] git push failed: {err}")
    else:
        log.info(f"[{repo_name}] ✅ Push successful. {out}")


# ── File-system watcher ───────────────────────────────────────────────────────

class RepoEventHandler(FileSystemEventHandler):
    """Handles FS events for a single watched directory."""

    # Cooldown in seconds – avoids spamming commits for rapid saves
    COOLDOWN = 5

    def __init__(self, local_path: str, repo_name: str):
        super().__init__()
        self.local_path = local_path
        self.repo_name = repo_name
        self._last_push: float = 0.0

    def _should_ignore(self, path: str) -> bool:
        """Skip git internals and common temp files."""
        ignore = {".git", "__pycache__", ".DS_Store", "Thumbs.db"}
        parts = Path(path).parts
        return any(p in ignore for p in parts) or path.endswith((".tmp", ".swp", "~"))

    def _handle(self, event, label: str):
        if event.is_directory or self._should_ignore(event.src_path):
            return

        now = time.time()
        if now - self._last_push < self.COOLDOWN:
            return  # still in cooldown

        rel = os.path.relpath(event.src_path, self.local_path)
        log.info(f"[{self.repo_name}] {label}: {rel}")
        self._last_push = now
        git_add_commit_push(self.local_path, self.repo_name, changed_file=rel)

    def on_created(self, event):
        self._handle(event, "FILE ADDED")

    def on_modified(self, event):
        self._handle(event, "FILE MODIFIED")

    def on_moved(self, event):
        self._handle(event, "FILE MOVED")

    def on_deleted(self, event):
        self._handle(event, "FILE DELETED")


# ── CSV loader ────────────────────────────────────────────────────────────────

def load_csv(csv_path: str) -> list[dict]:
    """Read the CSV and return a list of repo config dicts."""
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Auto Git Pusher")
    parser.add_argument(
        "--csv", required=True, help="Path to CSV config file"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Observer poll interval in seconds (default: 5)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.csv):
        log.error(f"CSV file not found: {args.csv}")
        return

    repos = load_csv(args.csv)
    if not repos:
        log.error("No valid repo entries found in CSV.")
        return

    log.info(f"Loaded {len(repos)} repo(s) from {args.csv}")

    observer = Observer()

    for repo in repos:
        local_path = repo["local_path"]
        repo_url   = repo["repo_url"]
        repo_name  = repo["repo_name"]

        ok = ensure_repo(local_path, repo_url, repo_name)
        if not ok:
            log.warning(f"Skipping {repo_name} – setup failed.")
            continue

        handler = RepoEventHandler(local_path, repo_name)
        observer.schedule(handler, path=local_path, recursive=True)
        log.info(f"👀 Watching: {local_path}  →  {repo_url}")

    if not observer._handlers:
        log.error("No directories to watch. Exiting.")
        return

    observer.start()
    log.info("Watcher running. Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("Stopping watcher…")
        observer.stop()

    observer.join()
    log.info("Done.")


if __name__ == "__main__":
    main()
