# 🚀 Auto Git Pusher

Automatically watches local directories and pushes any new or modified files to their linked GitHub repositories — no manual `git add`, `git commit`, or `git push` required.

A companion HTML dashboard renders push history with live search, filters, and charts.

---

## How It Works

```
repos_config.csv  ──►  auto_git_push.py  ──►  GitHub Repo
                              │
                              └──►  push_log.csv  ──►  push_analytics.html
```

1. You define watched directories and their GitHub repos in `repos_config.csv`.
2. The Python watcher monitors those directories for any file changes.
3. On any change it runs `git add → commit → pull --rebase → push`.
4. Every push outcome (success or failure) is appended to `push_log.csv`.
5. Open `push_analytics.html` in any browser to explore the push history.

---

## Files

| File | Purpose |
|---|---|
| `auto_git_push.py` | Main watcher script |
| `repos_config.csv` | Config: maps local directories to GitHub repos |
| `push_log.csv` | Auto-generated push event log |
| `push_analytics.html` | Analytics dashboard (open in browser) |
| `requirements.txt` | Python dependencies |

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Edit `repos_config.csv`

```csv
local_path,repo_url,repo_name
/home/you/my-project,https://github.com/you/my-project.git,my-project
/home/you/notes,https://github.com/you/notes.git,notes
```

### 3. Run the watcher

```bash
python auto_git_push.py --csv repos_config.csv
```

Custom push log path:
```bash
python auto_git_push.py --csv repos_config.csv --log push_log.csv
```

### 4. Open the dashboard

Open `push_analytics.html` in your browser. It auto-loads `push_log.csv` from the same folder.

> **Prerequisites:** Git must be installed and your machine must have SSH keys or a credential helper configured so `git push` works without a password prompt.

---

## Configuration

### `repos_config.csv` columns

| Column | Description |
|---|---|
| `local_path` | Absolute path to the local directory to watch |
| `repo_url` | GitHub remote URL (HTTPS or SSH) |
| `repo_name` | Short display name used in logs and the dashboard |

### `push_log.csv` columns (auto-generated)

| Column | Description |
|---|---|
| `timestamp` | Date and time of the push event |
| `repo_name` | Repo display name |
| `repo_url` | GitHub remote URL |
| `file_changed` | Relative path of the file that triggered the push |
| `event_type` | `created` / `modified` / `moved` / `deleted` |
| `status` | `success` / `failed` / `error` |
| `message` | Git output or error message |

### Dashboard data source (`push_analytics.html`)

At the top of the `<script>` block in the HTML:

```js
const LOCAL_CSV  = "push_log.csv";   // path relative to this HTML file
const GITHUB_CSV = "";               // GitHub blob or raw URL, or ""
```

**Load priority:**
1. `LOCAL_CSV` is fetched first (works when the HTML is served locally).
2. If that fails or is empty, `GITHUB_CSV` is tried (auto-converted to a raw URL).
3. If both fail, a **404 screen** is shown with setup instructions.

**GitHub URL formats — both work:**
```
https://github.com/user/repo/blob/main/push_log.csv   ← blob URL
https://raw.githubusercontent.com/user/repo/main/push_log.csv  ← raw URL
```

---

## Dashboard Features

- **Stat cards** — Total pushes, Successful, Failed, Repos tracked, Files changed, Success rate
- **Bar charts** — Pushes by repo and daily activity
- **Search** — Full-text search across repo, file, status, event, and message
- **Filters** — Filter by repo, status, event type, and date range
- **Sortable columns** — Click any column header to sort ascending or descending
- **Pagination** — 15 rows per page
- **Refresh button** — Re-fetches the CSV from the configured source without reloading the page

---

## Development History

### v1 — Initial release

- Python watcher using `watchdog` to monitor directories defined in a CSV file.
- On any file change: `git add -A` → `git commit` → `git push`.
- CSV columns: `local_path`, `repo_url`, `repo_name`.
- Cooldown (5 seconds) to prevent commit spam on rapid saves.
- Ignores `.git/`, `__pycache__`, `.DS_Store`, `.tmp`, `.swp` files.
- Auto-sets up the git repo if the directory is empty (clones) or already has files (`git init` + remote add).

### v1.1 — Bug fix: NoneType CSV parsing

- **Problem:** Empty CSV cells returned `None` from `csv.DictReader`, causing `AttributeError: 'NoneType' object has no attribute 'strip'`.
- **Fix:** Added a `None` guard in the row-parsing dict comprehension:
  ```python
  row = {k.strip(): (v.strip() if v is not None else "") for k, v in row.items()}
  ```

### v1.2 — Bug fix: non-fast-forward push rejection

- **Problem:** If the remote had commits the local branch did not have (e.g. a README created on GitHub), git refused to push with a `non-fast-forward` error.
- **Fix:** Added `git pull --rebase origin HEAD` before every push. Uses rebase (not merge) to keep history linear. If the rebase gets stuck (conflict), it is automatically aborted.

### v2 — CSV hot-reload + push logging + analytics dashboard

#### Python (`auto_git_push.py`)

- **CSV hot-reload:** A `ConfigCSVHandler` watches the config CSV itself with `watchdog`. When you save a new row, the watcher immediately starts monitoring the new directory — no restart needed.
- **Push log:** Every push event is appended to `push_log.csv` (thread-safe with a `threading.Lock`). Columns: `timestamp`, `repo_name`, `repo_url`, `file_changed`, `event_type`, `status`, `message`.
- **`AutoGitPusher` class:** Refactored orchestration into a class with a `_watched` dict to track active watches and a `reload_config()` method called on CSV change.
- **New `--log` CLI flag** to specify push log output path (default: `push_log.csv`).

#### Dashboard (`push_analytics.html`)

- Standalone single-file HTML dashboard (no build step, no dependencies).
- Stat cards, CSS bar charts, searchable table with pagination.
- Filters: repo, status, event type, date range.
- Sortable columns.
- GitHub URL loader (auto-converts blob URL to raw) and local file picker.

### v3 — Auto-loading dashboard + clean UX

#### Dashboard (`push_analytics.html`)

- **Removed manual controls:** No "Load local CSV" button, no GitHub URL input field — the dashboard is fully automatic.
- **Auto-load with priority fallback:**
  1. Fetches `LOCAL_CSV` path configured at the top of the script.
  2. Falls back to `GITHUB_CSV` URL if local fetch fails or is not set.
  3. Shows a styled **404 screen** with fix instructions if both fail.
- **GitHub URL auto-conversion:** Blob URLs (`/blob/`) are silently converted to raw URLs (`raw.githubusercontent.com`) — no manual URL editing needed.
- **Refresh button** in the table header re-runs the full auto-load without a page reload.
- **Loading screen** with spinner shown while fetching.
- Configuration is two constants at the top of the HTML — easy to edit, clearly documented.

---

## Running as a Background Service (macOS / Linux)

```bash
# Background process
nohup python auto_git_push.py --csv repos_config.csv --log push_log.csv &

# Stop it
kill $(pgrep -f auto_git_push.py)
```

For auto-start on boot, create a `systemd` service (Linux) or a `launchd` plist (macOS).

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `non-fast-forward` push | Remote has commits local doesn't | Already handled by `git pull --rebase`; check for merge conflicts |
| `NoneType has no attribute strip` | Empty CSV cell | Fixed in v1.1; ensure no trailing commas in CSV |
| Push log not created | Wrong `--log` path or permission issue | Check directory write permissions |
| Dashboard shows 404 | `LOCAL_CSV` / `GITHUB_CSV` not set or unreachable | Set the constants in the HTML and ensure the file is accessible |
| GitHub fetch blocked (CORS) | Browser blocks cross-origin fetch | Use `raw.githubusercontent.com` URL; blob URLs are auto-converted |
