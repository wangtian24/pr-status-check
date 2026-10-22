# pr-status-check

A local PR inbox: one page that shows which of your GitHub PRs need you, across every repo you work in. Zero dependencies beyond Python 3 and the GitHub CLI.

## What it shows

Each PR is sorted into one bucket, derived from its review, CI and merge state:

| Bucket | Meaning | Example ask |
|--------|---------|-------------|
| **Needs you** | Your PR is blocked on you: conflicts, failing CI, changes requested, or unresolved review comments | `Fix CI: lint`, `Reply to 2 unresolved comments from @alice` |
| **Ready to merge** | Approved, CI green, nothing unresolved | `Merge it` |
| **Needs your review** | Open PRs by others from the past 2 days, not yet approved | `Review @bob's PR` |
| **CI running** | Checks still running, nothing else blocking | |
| **Waiting on review** | Your PR is fine, waiting on reviewers | |
| **Draft** | Your drafts from the past 30 days | |
| **Merged** | Your last 5 merged PRs per repo | |

The pill strip at the top shows the count per bucket; click a pill to filter. By default the board shows the first four buckets, with the rest folded under "other PRs".

Every PR card carries the harness that built it (Claude Code, Couch, Devin, ...) with a link to its session, the approval / CI / branch state, and when it last changed.

## Views

- **Blocks** — cards grouped by repo, status or harness.
- **List** — one dense line per PR, closest to a table.
- **Catch up** — one PR at a time: its ask, unresolved comment threads, failing checks, reviews and description. `←` `→` browse, `O` open on GitHub, `↓` done, `S` snooze, `X` star.

## Triage

- **✓ Done** hides a PR until it changes (new commit, comment, review or check result).
- **Snooze** hides it for 4 hours, or until it changes.
- **Star** keeps it in view; the Starred toggle shows only starred PRs.
- **NEW** marks PRs that changed since your last visit; "Mark all seen" clears them.

Triage state lives in the browser (localStorage), so it is private to you and survives restarts of the server.

## Scopes

The scope picker (top right) selects which PRs are fetched: **My PRs** (auto-refreshed by the server), **Review queue** and **Recently merged** (fetched live when you refresh). Default: My PRs + Review queue.

## Prerequisites

- **Python 3** (no pip packages needed)
- **[gh](https://cli.github.com)** (GitHub CLI), authenticated via `gh auth login`

## Quick start

```bash
git clone <this-repo>
cd pr-status-check

# 1. Create your config
cp config.yml.example config.yml
# Edit config.yml with your repos

# 2. Run
python3 server.py
```

Then open http://localhost:9600.

## Configuration

Copy the example and edit:

```bash
cp config.yml.example config.yml
```

```yaml
# config.yml
repos:
  - https://github.com/org/repo1
  - https://github.com/org/repo2
  - org/repo3          # shorthand works too

# Optional:
# port: 9600
# interval: 300
```

`config.yml` is gitignored — your settings stay local.

You can also pass repos as CLI arguments (overrides config.yml):

```bash
python3 server.py https://github.com/org/repo1 org/repo2
```

### CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 9600 | Port to serve on |
| `--interval` | 300 | Data refresh interval in seconds |

## Install as daemon

```bash
bash setup.sh
pr-status start
```

`setup.sh` installs a single `pr-status` command into `~/.local/bin` (and migrates
off the older `pr-status-start`/`-stop`/`-restart` aliases if you had them).

| Command | Description |
|---------|-------------|
| `pr-status start` | Start the dashboard in the background |
| `pr-status stop` | Stop the daemon |
| `pr-status restart` | Restart the daemon |
| `pr-status status` | Show whether it's running (pid + URL) |
| `pr-status logs` | Tail the daemon log |
| `pr-status run` | Run in the foreground for debugging (extra args pass through to `server.py`) |

## When the dashboard says "Can't reach GitHub"

The daemon runs `gh` to fetch PRs. A long-running background process can lose access
to the macOS login keychain where `gh` stores its token; after that, every `gh` call
fails. The dashboard detects this and shows a red banner instead of a misleading empty
list — just run `pr-status restart` to fix it.

## How it works

- Uses one GitHub GraphQL `search` query per tab (not one call per repo), so a large workspace stays well inside the 5,000-points-per-hour GraphQL budget
- Auto-detects your GitHub username from `gh auth`
- Classifies PRs on the server (`classify_mine` in `server.py`) and serves them as JSON from `/api/prs?scope=mine,review,merged`; `index.html` renders the inbox
- Caches your own PRs and refreshes them in a background thread (default: every 5 min); the review queue and merged list are fetched on demand
- Checks GitHub CLI auth on each refresh and surfaces failures in the UI
- `python3 test_server.py` runs the self-check for the classification rules
