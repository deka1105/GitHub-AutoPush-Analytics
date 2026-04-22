# 🚀 Auto Git Pusher

Automatically watches local directories and pushes any new or modified files to their linked GitHub repositories — no manual `git add`, `git commit`, or `git push` required.

A companion HTML dashboard (`push_analytics.html`) displays push history with a GitHub-style activity calendar, interactive repo filter, multiple charts, search, and filters.

---

## How It Works

```
repos_config.csv  ──►  auto_git_push.py  ──►  GitHub Repo
                              │
                              └──►  push_log.csv  ──►  push_analytics.html
```

1. Define watched directories and linked GitHub repos in `repos_config.csv`.
2. The Python watcher monitors those directories for any file change.
3. On any change: `git add` → `git commit` → `git pull --rebase` → `git push`.
4. Every push outcome is appended to `push_log.csv`.
5. Open `push_analytics.html` via a local server to explore the dashboard.

---

## Files

| File | Purpose |
|---|---|
| `auto_git_push.py` | Main watcher script |
| `repos_config.csv` | Config: maps local directories to GitHub repos |
| `push_log.csv` | Auto-generated push event log |
| `push_analytics.html` | Analytics dashboard (open via local server) |
| `requirements.txt` | Python dependency: `watchdog` |

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
# Custom push log path:
python auto_git_push.py --csv repos_config.csv --log push_log.csv
```

### 4. Open the dashboard
```bash
cd /path/to/project
python3 -m http.server 8080
# Then open: http://localhost:8080/push_analytics.html
```

> **Note:** The dashboard must be served via `http://` — not `file://`. Opening the HTML directly shows a clear error with the exact fix command.

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
| `timestamp` | Date and time of event (`YYYY-MM-DD HH:MM:SS`) |
| `repo_name` | Repo display name |
| `repo_url` | GitHub remote URL |
| `file_changed` | Relative path of the file that triggered the push |
| `event_type` | `created` / `modified` / `moved` / `deleted` |
| `status` | `success` / `failed` / `error` |
| `message` | Git output or error message |

### Dashboard data source

Two constants at the top of the `<script>` block in `push_analytics.html`:

```js
const LOCAL_CSV  = "push_log.csv";   // path relative to this HTML file
const GITHUB_CSV = "https://raw.githubusercontent.com/deka1105/GitHub-AutoPush-Analytics/refs/heads/main/push_log.csv";
```

**Load priority:** `LOCAL_CSV` → `GITHUB_CSV` → 404 error screen.  
GitHub blob URLs (`/blob/`) are auto-converted to raw URLs.

---

## Dashboard Features

### Stat cards
Total pushes · Successful · Failed/Error · Repos tracked · Unique files · Avg pushes per day

### Calendar + Repo filter panel (side by side)
- **GitHub-style activity calendar** — Jan 1 to Dec 31 of the current year, 5-level green intensity
- **Filter by Repo panel** — top 4 repos by push count, plus "All repos"
  - Clicking a button filters **every panel on the page** simultaneously
  - Shows per-repo stats: total pushes, success rate bar, unique files, last push date
  - Panel and calendar share the same height

### Charts
Status donut · Event type breakdown · Top files changed · Pushes by repo · Cumulative timeline · Daily activity bars · Hourly heatmap

### Push log table
Full-text search · Filters (repo, status, event, date range) · Sortable columns · Pagination

---

## Development History

### v1 — Initial release

- Python watcher using `watchdog` monitoring directories from `repos_config.csv`.
- On any file change: `git add -A` → `git commit` → `git push`.
- 5-second cooldown to prevent commit spam on rapid saves.
- Ignores `.git/`, `__pycache__`, `.DS_Store`, `.tmp`, `.swp`.
- Auto-setups repo: clones if empty, or runs `git init` + remote add if files exist.

---

### v1.1 — Bug fix: NoneType CSV parsing

**Problem:** Empty CSV cells returned `None` from `csv.DictReader`:
```
AttributeError: 'NoneType' object has no attribute 'strip'
```
**Fix:** `v.strip() if v is not None else ""`

---

### v1.2 — Bug fix: non-fast-forward push rejection

**Problem:** Remote had commits the local branch didn't (e.g. README created on GitHub):
```
! [rejected] HEAD -> main (non-fast-forward)
```
**Fix:** Added `git pull --rebase origin HEAD` before every push. If rebase conflicts, it auto-aborts.

---

### v2 — CSV hot-reload + push logging + first analytics dashboard

**Python:**
- `ConfigCSVHandler` watches the config CSV with watchdog. Adding a new row immediately starts watching that directory — no restart needed.
- Every push outcome appended to `push_log.csv` (thread-safe with `threading.Lock`).
- `AutoGitPusher` class with `_watched` dict and `reload_config()` method.
- New `--log` CLI flag.

**Dashboard (first version):**
- Standalone single-file HTML, no build step.
- Stat cards, CSS bar charts, searchable table, sortable columns.
- GitHub URL loader + local file picker.

---

### v3 — Auto-loading dashboard, removed manual controls

- **Removed all manual controls** (no load button, no URL input).
- **Auto-load priority:** `LOCAL_CSV` → `GITHUB_CSV` → 404 error screen.
- GitHub blob URLs silently converted to raw URLs.
- Loading spinner while fetching.
- Refresh button re-runs auto-load without page reload.

---

### v3.1 — Bug fix: file:// protocol detection

**Problem:** Opening HTML via `file:///` caused `fetch()` to fail silently — showed confusing 404.

**Fix:** `window.location.protocol === "file:"` check on boot. Shows a dedicated error screen with exact commands:
```bash
python3 -m http.server 8080
# http://localhost:8080/push_analytics.html
```

---

### v4 — Full visualization suite + GitHub activity calendar

New charts added:
- **GitHub-style contribution calendar** — 52-week heatmap, 5 intensity levels, hover tooltips
- **Status donut chart** — SVG donut with success/failed/error proportions
- **Hourly activity heatmap** — 24-cell strip showing peak push hours
- **Cumulative pushes timeline** — SVG line+area chart with gradient fill
- **Top files changed** — ranked file paths with mini bars
- **Event type breakdown** — created/modified/moved/deleted bar chart

Stat cards expanded with subtitle lines and colour-coded bottom accents.

---

### v5 — Mobile responsive + resizable panels + grey calendar cells

- **Mobile layout (≤ 600px):** reduced padding, 2-column stat cards, full-width filters, table horizontal scroll, donut legend stacks below.
- **Tablet (≤ 768px):** 2-column chart rows collapse to 1.
- **Resizable panels:** native CSS `resize:both` added to `.chart-box` (disabled on mobile).
- **Calendar empty cells:** `--cal0` changed from `#161b22` (invisible) to `#2d333b` (visible grey). Future cells also use grey instead of transparent.

---

### v6 — GitHub URL hardcoded + improved drag resize

- **`GITHUB_CSV`** set to `https://raw.githubusercontent.com/.../push_log.csv`.
- **Removed** native CSS `resize:both` (browser grab triangle, no affordance, unreliable).
- **New custom drag handle:** each `.chart-box` gets a bottom bar with 3 pill dots. Drag it to resize height. Turns accent purple on hover/drag. Uses `mousedown` + `mousemove` tracking with `min-height:160px` floor. Touch support for tablets. Fully removed on mobile.

---

### v7 — Fluid panels + Jan–Dec calendar

**Fluid animations:**
- `fadeSlideUp` entrance animation on all cards and chart boxes (staggered delays).
- Cards lift `translateY(-3px)` on hover with shadow and accent border glow.
- Chart boxes glow on hover.
- Bar fills animate from `width:0` using `barGrow` keyframes with Material easing.
- Filter focus ring (`box-shadow: 0 0 0 3px`).
- Calendar cells scale to `1.45×` with `brightness(1.3)` on hover.
- Dashboard fades in on data load.

**Calendar redesign:**
- Changed from "52 weeks back from today" to **Jan 1 → Dec 31 of the current year**.
- Days before Jan 1 (in the first partial week) are transparent spacers.
- Future days within the year show as grey placeholders.
- Day labels show Sun/Tue/Thu/Sat.
- Year label above the grid.

---

### v8 — Remove resize + calendar repo filter pills

- **Removed** the custom drag resize feature entirely (CSS + JS IIFE).
- **Added** repo filter pills inside the calendar card header — "All repos" + top 4 by push count, with colour dots. Clicking a pill re-rendered only the calendar.

---

### v9 — Calendar + repo panel side-by-side, global filter

**Layout change:**
- Calendar section switched from full-width to a **CSS grid** (`1fr 230px`).
- Calendar occupies the left column (narrower).
- New **Repo Panel** occupies the right column as a separate `chart-box`.
- Both columns collapse to single-column on screens ≤ 960px.

**Repo panel design:**
- "All repos" button + top 4 repos ranked by push count.
- Each button: colour dot, repo name, push count badge.
- Active button: left accent bar + purple tint + name turns accent colour.
- Stats block below buttons: total pushes, success rate + progress bar, unique files, last push date. Updates when switching repos.

**Global filter (`activeRepo`):**
- Removed `calendarRepo` (calendar-only filter).
- Introduced `activeRepo` state variable and `getRows()` helper.
- `refreshAll()` re-renders every panel when `activeRepo` changes.
- Panels now filtered: stat cards, calendar, donut, event chart, top files, repo bars, timeline, hourly heatmap, push log table.

---

### v10/v11 — Calendar height matches repo panel

**Problem:** Calendar card was shorter than the repo panel — mismatched heights looked unpolished.

**Fix:**
- `.cal-section` changed from `align-items:start` to `align-items:stretch` — both grid children stretch to the tallest column.
- `.calendar-wrap` set to `display:flex; flex-direction:column; height:100%` — fills the stretched height.
- `.calendar-scroll` set to `flex:1; min-height:0` — scroll area fills remaining space inside the flex column.
- `.repo-panel` set to `height:100%` — explicitly fills the grid cell.
- Calendar `chart-box` gets `min-height:0` override so the grid controls the height, not the default `160px` floor.

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `non-fast-forward` | Remote has commits local doesn't | Handled by `git pull --rebase` automatically |
| `NoneType has no attribute strip` | Empty CSV cell | Fixed in v1.1; avoid trailing commas |
| Dashboard shows 404 | `LOCAL_CSV` / `GITHUB_CSV` not set or unreachable | Set the constants in the HTML |
| Blank dashboard via `file://` | Browser blocks `fetch()` on `file://` | Run `python3 -m http.server 8080` |
| GitHub fetch fails | CORS or bad URL | Blob URLs are auto-converted; ensure file is public |
| New CSV row not picked up | File replaced atomically by editor | Both `on_created` and `on_modified` trigger reload |

---

## Running in the Background

```bash
# Start in background
nohup python auto_git_push.py --csv repos_config.csv --log push_log.csv &

# Stop
kill $(pgrep -f auto_git_push.py)
```

For auto-start on boot use `systemd` (Linux) or `launchd` (macOS).
