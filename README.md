# pr-status-check

A local dashboard that shows your GitHub PR status across multiple repos. Zero dependencies beyond Python 3 and the GitHub CLI.

## What it shows

For each repo, grouped by section:

| Column | Description |
|--------|-------------|
| **PR** | PR number (links to GitHub) |
| **Title** | PR title |
| **Created** | How long ago the PR was created |
| **Last Comment** | Time since the last comment/review |
| **Unresolved Comments** | Count + who left them (human authors highlighted in red) |
| **Approval** | Review approval state |
| **CI** | Passing / failed check names / running |
| **Branch** | Up to date / behind main / conflicts |

PRs are categorized as:
- **Open** — all non-draft open PRs
- **Drafts** — draft PRs created in the last 30 days
- **Recently Closed** — PRs closed in the last 7 days

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
source ~/.bashrc  # or ~/.zshrc
pr-status-start
```

| Command | Description |
|---------|-------------|
| `pr-status-start` | Start the dashboard in the background |
| `pr-status-stop` | Stop the daemon |
| `pr-status-restart` | Restart the daemon |

## How it works

- Uses `gh pr list` and the GitHub GraphQL API to fetch PR data
- Auto-detects your GitHub username from `gh auth`
- Caches results and refreshes in a background thread (default: every 5 min)
- Serves a static HTML page with a live "X min ago" timer
