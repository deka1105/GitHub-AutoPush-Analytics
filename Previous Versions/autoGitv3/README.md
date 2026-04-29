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

---

### v12 — Fluid calendar grid (ResizeObserver-driven)

**Problem:** Calendar cells were fixed at `12×12px` with a `3px` gap hardcoded in CSS. When the panel was taller or wider (due to the repo panel height or browser window size), the calendar stayed the same size and left blank space instead of filling the available area.

**Architecture change — two-function split:**

The old single `renderCalendar()` function is replaced by two:

| Function | Responsibility |
|---|---|
| `renderCalendar()` | Computes push-count data, builds week/day objects, attaches `ResizeObserver` |
| `drawCalendar(scrollEl)` | Measures available space, computes cell/gap/radius sizes, builds and injects HTML |

`drawCalendar()` is called by the `ResizeObserver` on every dimension change (debounced via `requestAnimationFrame`), so the grid redraws instantly whenever the panel resizes or the browser window changes.

**Cell size calculation:**

```
availW = scrollEl.clientWidth  - dayLabelWidth - padding
availH = scrollEl.clientHeight - monthRowH - yearRowH - legendH - margins

rawCellW = availW / numWeeks   (~53 columns)
rawCellH = availH / 7          (7 rows)
cell     = clamp(min(rawCellW, rawCellH), 4px, 18px)
gap      = max(2, round(cell × 0.22))     ← scales with cell
radius   = max(1, round(cell × 0.18))     ← scales with cell
fontSize = clamp(cell − 1, 8px, 11px)    ← scales with cell
```

Everything — cell size, gap, border-radius, font sizes for day/month labels, legend cell size — is derived from the single computed `cell` value, so the entire calendar scales as one proportional unit.

**CSS changes:**
- Removed all fixed pixel values from `.cal-cell`, `.cal-days`, `.cal-weeks`, `.cal-week`.
- `.calendar-scroll` changed to `overflow:hidden` (no scrollbar needed — cells shrink to fit).
- `.cal-body` uses `flex:1; min-height:0` to fill the remaining flex space.
- Static legend HTML removed from the HTML template — now built dynamically inside `drawCalendar()`.
- `id="calendar-scroll"` added to the scroll container so the `ResizeObserver` can target it precisely.

---

### v18 — Smart commit messages (Python)

`build_commit_message()` now reads `git diff --cached --name-only` after staging to build a context-aware subject line:

- **≤ 5 files** → filenames listed in the subject: `auto-push: app.js, utils.py [2026-04-20 10:30:00]`
- **> 5 files** → count in subject, full file list in the body:
  ```
  auto-push: 8 files changed [2026-04-20 10:30:00]

  Modified files:
  - src/components/App.jsx
  - src/utils/api.js
  ...
  ```

---

### v18.1 — Startup sync (Python)

`startup_sync()` is called once per repo when the watcher starts, before the live `watchdog` observer begins. It:
1. Runs `git pull --rebase origin HEAD` to bring local history up to date
2. Checks `git status --porcelain` for any uncommitted changes (files added, modified, or deleted while the watcher was offline)
3. If changes exist: stages → commits with a `startup-sync:` prefix → pushes
4. Logs the outcome to `push_log.csv` with `event_type="startup-sync"`

This ensures no offline changes are ever silently skipped.

---

### v18.5 — Multiline message handling

**Python (`flatten_msg`):** All multiline messages (e.g. multi-file commit bodies, git error output) are collapsed to a single CSV line with literal `\n` separators before writing. The `--END--` sentinel is always appended last.

**HTML (`cleanMsg` / `cleanMsgFlat`):**
- `cleanMsg(msg)` strips `--END--`, then converts all line-break representations (`/n`, `\n`, literal `\\n`) back to real newlines for rich display.
- `cleanMsgFlat(msg)` converts newlines to ` · ` for single-line contexts (table cells, tooltips).
- Modal, drawer, and failures panels use `white-space:pre-wrap` so multiline messages render as proper line breaks.

---

### v19 — Pulsing dots on all plots + 3D donut

**Pulsing dots:**
- `addPulseDot(svgEl, x, y, color)` — appends a two-circle pulse group (expanding ring + steady dot) directly into SVG charts
- `addPulseDotToEl(el, color)` — injects an overlay `<div>` with a mini SVG for HTML-container charts
- All plots covered: calendar, donut, timeline, hourly heatmap, month heatmap, all bar charts
- Colours match chart accent: green for data, purple for donut, teal for file/event/month charts

**3D donut watch-dial (enhanced further):**
- Donut slices: extrude shadow layer (3px drop), radial gradient bevel, `feDropShadow` filter, glass highlight overlay, inner hole gloss
- Bezel: 3-stop linear gradient (steel blue → near-black), inner/outer rim highlight lines, drop shadow filter
- Arc: triple-layer glow (wide blurred halo + solid fill + bright core line)
- Needle: slim line with glow layer, tip highlight, 3-layer jewel pivot (disc + fill + specular dot)
- Drag knob: halo ring + solid orb + specular dot
- Reset button added below the label

---

### v19.1 — Message display fixes

Fixed double-escaped regex patterns in `cleanMsg` that prevented `--END--` from being stripped. Existing CSV data uses `/n` (forward-slash-n) from git error output serialisation — `cleanMsg` now handles all three formats: ` /n ` (spaced), `/n` (unspaced), and `\\n` (Python flatten_msg output). The `gi` flag ensures case-insensitive sentinel removal.

---

### v20 — 3D graphs throughout + minimisable toast

**3D visual treatment applied to every chart panel:**

| Element | 3D technique |
|---|---|
| Chart boxes | Multi-stop gradient background (dark blue-grey), bottom extrude shadow, inner top rim highlight |
| Stat cards | Diagonal gradient, bottom extrude `box-shadow`, hover lift |
| Bar fills | Gradient highlight overlay (white→transparent), bottom extrude `::after` pseudo-element, inset shadow on track |
| Hourly heatmap cells | `feDropShadow`-style `box-shadow`, inner top rim |
| Timeline | Deeper gradient area fill (3-stop), `feGaussianBlur` glow filter on the polyline, thicker stroke (2.5px) |
| Calendar cells | `feDropShadow` SVG filter applied to active (non-zero) cells only |
| Bar chart rebuild | Rewritten as layered `<div>` stack: track → extrude shadow → 3D bar with staggered `animation-delay` per row |

**Minimisable "Help the builder" toast:**
- **`–` button** added to top-right of the expanded toast — collapses it to a small floating bubble
- **Mini bubble**: `44×44px` gradient circle (accent→teal), spring-in animation, springs back to full toast on click
- `minimiseToast()` → hides toast, shows mini after 300ms
- `restoreToast()` → hides mini, shows toast after 200ms
- `dismissToast()` → hides both, sets `sessionStorage` flag so neither reappears this session

---

### v21 — Pulsing data points on all plots

Added two CSS keyframe animations and two JS helper functions (`addPulseDot`, `addPulseDotToEl`) to place a pulsing indicator on the most significant data point in every chart:

| Chart | Pulse location |
|---|---|
| Calendar | Level-4 (darkest green) cells — expanding SVG ring |
| Donut | Top-right corner of the SVG |
| Timeline | Last (most recent) data point — expanding ring + inner dot |
| Hourly heatmap | Peak hour cell — CSS `scale` + glow |
| Month heatmap | Busiest day cell — SVG ripple circle |
| All bar charts | Tip dot on the tallest bar |
| All chart boxes | Top-right overlay dot via absolute-positioned SVG |

---

### v21.1 — Targeted pulses, 7-day timeline, today-only calendar/month pulse

**Removed** bar chart tip pulse and hourly heatmap peak-hour pulse (too noisy).

**Timeline** — now shows only the **last 7 days** instead of all-time cumulative data. Every day in the 7-day window gets its own pulsing SVG ring + dot (staggered by 0.3s per point). Days with no pushes show a dim grey dot so the full week is always visible.

**Calendar** — pulse moved from level-4 cells to **today's cell only**. Today gets an accent-coloured border (`stroke="var(--accent)"`) + opacity pulse + expanding ripple ring.

**Month heatmap** — pulse moved from the busiest day to **today's cell only** — whether it has pushes or not. Empty today cell shows accent border + purple ripple ring.

---

### v20.3 — Month heatmap future tiles

Future days in the current month were rendered as `fill="transparent"` — invisible — making the grid appear to stop at today. Fixed: all days of the month now render. Past/today days with no pushes use full-opacity `--cal0` grey. Future days use `opacity: 0.4` with a dimmed day-number label so they're visually distinct but present.

---

### v4 (Python) — Rebase conflict auto-resolution + detailed file logging

**Rebase conflict on `push_log.csv`**

When `push_log.csv` is appended to both locally and remotely between pushes, `git pull --rebase` halts with a conflict. The new `resolve_rebase_conflict()` function handles this automatically:

```
git pull --rebase fails
  → inspect conflicted files
  → if ALL conflicts are in append-only files (push_log.csv)
      → git checkout --ours <file>   ← keep local version
      → git add <file>
      → git rebase --continue
  → if ANY conflict is in a real code file
      → git rebase --abort
      → log error — requires manual resolution
```

`APPEND_ONLY_PATTERNS` is a set at the top of the file — add more filenames as needed. This runs in both the live push path and the startup sync path.

**Detailed rotating file logging**

New `--logfile` CLI argument (default: `watcher.log`):

```bash
python auto_git_push.py --csv repos_config.csv --log push_log.csv --logfile watcher.log
```

| Handler | Level | Content |
|---|---|---|
| Console | `INFO` | Clean status messages (same as before) |
| `watcher.log` | `DEBUG` | Every git command + its stdout/stderr, push timing (`elapsed: 1.23s`), cooldown skips, CSV write confirmations |

Log rotates at **5 MB**, keeps **3 backups** (`watcher.log.1`, `.2`, `.3`). The path to the log file is printed to console on startup.
