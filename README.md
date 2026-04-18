# 🚀 Auto Git Pusher

Automatically watches local directories and pushes any new or modified files to their linked GitHub repositories — no manual `git add`, `git commit`, or `git push` required.

A companion HTML dashboard renders push history with a GitHub-style activity calendar, multiple charts, live search, and filters.

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
5. Open `push_analytics.html` via a local server to explore the dashboard.

---

## Files

| File | Purpose |
|---|---|
| `auto_git_push.py` | Main watcher script |
| `repos_config.csv` | Config: maps local directories to GitHub repos |
| `push_log.csv` | Auto-generated push event log |
| `push_analytics.html` | Analytics dashboard |
| `requirements.txt` | Python dependencies (`watchdog`) |

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
# Custom log path:
python auto_git_push.py --csv repos_config.csv --log push_log.csv
```

### 4. Open the dashboard
```bash
cd /path/to/project
python3 -m http.server 8080
# Then open: http://localhost:8080/push_analytics.html
```

> **Note:** The dashboard must be served via `http://` (not `file://`). Opening the HTML directly will show a helpful error with the exact fix command.

> **Prerequisites:** Git must be installed and credentials configured so `git push` works without a password prompt.

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
| `timestamp` | Date and time of the push event (`YYYY-MM-DD HH:MM:SS`) |
| `repo_name` | Repo display name |
| `repo_url` | GitHub remote URL |
| `file_changed` | Relative path of the file that triggered the push |
| `event_type` | `created` / `modified` / `moved` / `deleted` |
| `status` | `success` / `failed` / `error` |
| `message` | Git output or error message |

### Dashboard data source (`push_analytics.html`)

Two constants at the top of the `<script>` block:

```js
const LOCAL_CSV  = "push_log.csv";   // relative path to this HTML file
const GITHUB_CSV = "";               // GitHub blob or raw URL, or ""
```

**Load priority:**
1. `LOCAL_CSV` is fetched first.
2. Falls back to `GITHUB_CSV` (blob URLs auto-converted to raw).
3. If both fail, a styled error screen shows the exact fix steps.

---

## Dashboard Features

### Stat cards
- Total pushes, active days
- Successful pushes + success rate
- Failed / error count
- Repos tracked, unique files changed
- Average pushes per active day

### Visualizations
- **GitHub-style contribution calendar** — 52-week heatmap showing push frequency per day with colour intensity levels (like GitHub's profile page)
- **Status donut chart** — SVG donut showing Success / Failed / Error proportions with counts and percentages
- **Event type breakdown** — horizontal bar chart of created / modified / moved / deleted events
- **Top files changed** — most frequently pushed files with a mini bar chart
- **Pushes by repo** — horizontal bar chart ranked by activity
- **Cumulative pushes over time** — SVG area/line chart showing total push growth
- **Daily activity** — bar chart of pushes per day
- **Hourly activity heatmap** — 24-cell heat strip showing which hours of the day are busiest

### Table & filtering
- Full-text search across repo, file, status, event, message
- Filters: repo, status, event type, date range (from / to)
- Sortable columns (click any header)
- Pagination (15 rows per page)
- Refresh button re-fetches CSV without page reload

---

## Development History

### v1 — Initial release

- Python watcher using `watchdog` to monitor directories defined in a CSV file.
- On any file change: `git add -A` → `git commit` → `git push`.
- CSV columns: `local_path`, `repo_url`, `repo_name`.
- 5-second cooldown to prevent commit spam on rapid saves.
- Ignores `.git/`, `__pycache__`, `.DS_Store`, `.tmp`, `.swp` files.
- Auto-sets up git repo: clones if empty, or runs `git init` + `remote add` if files already exist.

---

### v1.1 — Bug fix: NoneType CSV parsing

**Problem:** Empty CSV cells returned `None` from `csv.DictReader`, causing:
```
AttributeError: 'NoneType' object has no attribute 'strip'
```
**Fix:** Added a `None` guard in the row-parsing dict comprehension:
```python
row = {k.strip(): (v.strip() if v is not None else "") for k, v in row.items()}
```

---

### v1.2 — Bug fix: non-fast-forward push rejection

**Problem:** If the remote had commits the local branch didn't (e.g. a README created on GitHub), git refused to push:
```
! [rejected] HEAD -> main (non-fast-forward)
```
**Fix:** Added `git pull --rebase origin HEAD` before every push. Uses rebase (not merge) to keep history linear. If the rebase gets stuck on a conflict, it is automatically aborted with `git rebase --abort`.

---

### v2 — CSV hot-reload, push logging, analytics dashboard

#### Python (`auto_git_push.py`)

- **CSV hot-reload:** A `ConfigCSVHandler` watches the config CSV with `watchdog`. Save a new row and the watcher immediately starts monitoring that directory — no restart needed.
- **Push log:** Every push outcome appended to `push_log.csv` (thread-safe with `threading.Lock`). Columns: `timestamp`, `repo_name`, `repo_url`, `file_changed`, `event_type`, `status`, `message`.
- **`AutoGitPusher` class:** Orchestration refactored into a class with a `_watched` dict and a `reload_config()` method.
- **`--log` CLI flag** to set push log output path (default: `push_log.csv`).

#### Dashboard (`push_analytics.html`) — first version

- Standalone single-file HTML, no build step or dependencies.
- Stat cards, CSS horizontal bar charts.
- Searchable table with pagination and sortable columns.
- Filters: repo, status, event type, date range.
- GitHub URL loader (auto-converts blob URL to raw URL) and local file picker button.

---

### v3 — Auto-loading dashboard, removed manual controls

#### Dashboard (`push_analytics.html`)

- **Removed all manual controls:** no "Load local CSV" button, no GitHub URL input field.
- **Auto-load with priority fallback:** `LOCAL_CSV` → `GITHUB_CSV` → 404 error screen.
- **GitHub URL auto-conversion:** `/blob/` URLs silently converted to `raw.githubusercontent.com`.
- **Loading spinner** shown while fetching.
- **Refresh button** in the table header re-runs the full auto-load without a page reload.
- Configuration is two constants at the top of the HTML — clearly documented.

---

### v3.1 — Bug fix: file:// protocol detection

**Problem:** Opening the HTML directly in a browser (`file:///...`) caused `fetch()` to silently fail due to browser same-origin security policy, showing a confusing 404 screen with no clear reason.

**Fix:** Added a `window.location.protocol === "file:"` check on boot. If detected, instead of the generic 404, the page shows a dedicated error screen with the exact terminal commands to fix it:
```bash
cd /path/to/project
python3 -m http.server 8080
# Then open: http://localhost:8080/push_analytics.html
```

---

### v4 — Full visualization suite + GitHub activity calendar

#### Dashboard (`push_analytics.html`) — major expansion

**New visualizations added:**

- **GitHub-style contribution calendar** — 52-week heatmap grid (Sun–Sat rows, week columns), colour-coded by push count per day using 5 intensity levels matching GitHub's palette. Hovering a cell shows the date and push count via tooltip.

- **Status donut chart** — Pure SVG donut chart showing Success / Failed / Error split with a legend displaying counts and percentages.

- **Hourly activity heatmap** — A 24-cell strip showing push counts by hour of day, colour-coded with the same 5-level palette. Helps identify peak working hours.

- **Cumulative pushes over time** — SVG line/area chart with a gradient fill, showing the running total of pushes across all days. Includes Y-axis gridlines and X-axis date labels.

- **Top files changed** — Ranked list of the most frequently pushed file paths with inline mini bar charts.

- **Event type breakdown** — Horizontal bar chart of `created` / `modified` / `moved` / `deleted` events.

**Stat cards expanded:**
- Added "Avg / Day" card (average pushes per active day).
- All cards now include a subtitle line with contextual detail.
- Cards have a colour-coded bottom accent stripe.

**Layout:**
- Organised into labelled sections with visual dividers: Stat Cards → Calendar → Breakdowns (3-col) → Trends (2-col) → Daily + Hourly (2-col) → Push Log table.
- Fully responsive grid that collapses to 1 column on mobile.

---

## Running as a Background Service

```bash
# Background process (macOS / Linux)
nohup python auto_git_push.py --csv repos_config.csv --log push_log.csv &

# Stop it
kill $(pgrep -f auto_git_push.py)
```

For auto-start on boot, use `systemd` (Linux) or `launchd` (macOS).

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `non-fast-forward` | Remote has commits local doesn't | Handled automatically by `git pull --rebase` |
| `NoneType has no attribute strip` | Empty CSV cell | Fixed in v1.1; avoid trailing commas in CSV |
| Dashboard shows 404 | `LOCAL_CSV` / `GITHUB_CSV` not set or unreachable | Set the constants in the HTML |
| Dashboard blank when opened via `file://` | Browser blocks `fetch()` on `file://` | Run `python3 -m http.server 8080` and use `http://localhost` |
| GitHub fetch fails | CORS or bad URL | Blob URLs are auto-converted; ensure the file is public |
| Watcher doesn't pick up new CSV row | File saved atomically by editor (replace, not write) | Save the file; `on_created` and `on_modified` both trigger reload |
