# GitHub AutoPush Analytics

**A watcher that commits and pushes your work while you're still doing it, and keeps a record of every push so you can see what you actually built.**

Point it at a CSV of local directories and their GitHub remotes. It watches them
all, commits each change as it lands, pushes it, and appends a row to a log.
Then it draws that log back to you as a dashboard.

---

## Why

Work that isn't committed isn't anywhere. The usual answer is discipline — commit
often, write good messages — which works right up until you're deep in something
and the last thing you want is to stop and think about git.

This removes the decision. Every save becomes a commit with a timestamped message
and a push. You lose curated history; you gain never losing work, and a complete
record of when every file in every project changed.

The log turns out to be the interesting part. A year of `push_log.csv` is an
honest account of what you worked on and when — not what you remember working on.

## What it does

- **Watches many repositories at once**, each with its own remote, from one CSV
- **Picks up config changes live** — add a row and it starts watching that directory without a restart
- **Commits and pushes per change**, with the path and timestamp in the message
- **Logs every event** — timestamp, repo, file, event type, status, message
- **Resolves rebase conflicts on append-only files** automatically, so two machines writing the same log don't deadlock
- **Rotating logs** per host (`watcher_mac.log`, `watcher_linux.log`) for diagnosis
- **An analytics dashboard** over the log: what changed, where, how often

## Running it

```bash
pip install -r requirements.txt
python auto_git_pushv9.py --csv repos_config.csv
```

Defaults can be overridden:

```bash
python auto_git_pushv9.py \
  --csv repos_config.csv \
  --log push_log.csv \
  --logfile watcher.log
```

The dashboard is a single file — open `push_analyticsv20_3.html` in a browser, or
run `dashboard.py` to serve it.

### The config

`repos_config.csv`, three columns, no surprises:

```csv
local_path,repo_url,repo_name
/Users/you/Projects/Thing,https://github.com/you/Thing.git,Thing
```

The file is re-read while running. Adding a row is enough to start watching a new
project; you don't restart the watcher.

### The log

`push_log.csv`, appended to and never rewritten:

```csv
timestamp,repo_name,repo_url,file_changed,event_type,status,message
```

Per-host logs (`push_log_mac.csv`, `push_log_linux.csv`) keep machines from
fighting over the same file. `status` records failures too — a push that didn't
land is more interesting than one that did.

## Versions

`auto_git_push.py`, `auto_git_pushv8.py`, `auto_git_pushv9.py` are deliberate
history, not clutter. **v9 is the live one.** Earlier versions stay runnable
because this tool guards real work, and being able to drop back a version matters
more than a tidy directory.

Same for the `repos_config.csv.bak*` files — a watcher config that silently loses
a row stops protecting that project, and you won't notice until you need the
commits.

## Things worth knowing

**It commits everything that changes.** That is the point, and it means your
`.gitignore` is the only thing standing between a secret and a public repository.
Check it before adding a directory here, not after.

**History is granular, not curated.** Hundreds of small commits with generated
messages. If you need a clean story for a reviewer, branch and squash — don't
turn this off and hope to remember.

**It cannot push what the remote rejects.** Protected branches, missing
credentials, and diverged histories all surface in the log's `status` column
rather than as exceptions. Read the log occasionally.

## Licence

See [LICENSE](LICENSE).
