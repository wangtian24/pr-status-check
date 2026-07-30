#!/usr/bin/env python3
"""PR Status Dashboard - a local web dashboard showing your GitHub PR status.

Usage:
    python3 server.py https://github.com/org/repo1 https://github.com/org/repo2 ...

Options:
    --port PORT     Port to serve on (default: 9600)
    --interval SEC  Refresh interval in seconds (default: 300)

Prerequisites:
    - python3 (no pip dependencies)
    - gh (GitHub CLI, authenticated via `gh auth login`)
"""

import argparse
import html as html_mod
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

# Known bot accounts - comments from anyone else are "human"
BOT_AUTHORS = {
    "yupp-reviews", "gemini-code-assist", "copilot", "github-actions",
    "dependabot", "renovate", "codecov", "sonarcloud", "vercel",
}

# Populated at startup
CONFIG = {
    "repos": [],       # list of (owner, name)
    "workspace": None, # workspace dir to re-scan each refresh (None = fixed repo list)
    "gh_user": "",
    "port": 9600,
    "interval": 300,
}

# Global cache
_cache_lock = threading.Lock()
_cache = {"my_prs_html": "", "fetched_epoch": 0, "gh_error": None}

# Track last page visit so background refresh pauses when page is closed
_last_visit_lock = threading.Lock()
_last_visit = {"t": 0.0}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_repo_url(url):
    """Parse a GitHub repo URL or shorthand into (owner, name)."""
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    # SSH: git@github.com:owner/name
    m = re.match(r"git@github\.com:([^/]+)/(.+)$", url)
    if m:
        return m.group(1), m.group(2)
    # HTTPS or owner/name shorthand
    m = re.match(r"(?:https?://github\.com/)?([^/]+)/([^/]+)$", url)
    if not m:
        return None
    return m.group(1), m.group(2)


def load_config_file():
    """Load config.yml from the same directory as this script. Returns dict or None."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "config.yml")

    if not os.path.exists(config_path):
        return None

    # Minimal YAML parser for our simple config (no dependency needed)
    config = {"repos": [], "workspace": None}
    in_repos = False
    with open(config_path) as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped == "repos:":
                in_repos = True
                continue
            if in_repos and stripped.startswith("- "):
                val = stripped[2:].strip().strip("'\"")
                if val and not val.startswith("#"):
                    config["repos"].append(val)
                continue
            # Non-list key: stop reading repos
            in_repos = False
            if ":" in stripped:
                key, val = stripped.split(":", 1)
                key = key.strip()
                val = val.strip().strip("'\"")
                if not val or val.startswith("#"):
                    continue
                if key == "port":
                    config["port"] = int(val)
                elif key == "interval":
                    config["interval"] = int(val)
                elif key == "workspace":
                    config["workspace"] = val

    if not config["repos"] and not config["workspace"]:
        return None
    return config


def discover_workspace_repos(workspace_dir):
    """Scan workspace_dir for git repos belonging to the angellist GitHub org."""
    repos = []
    workspace = os.path.expanduser(workspace_dir)
    if not os.path.isdir(workspace):
        print(f"WARNING: workspace directory not found: {workspace}", file=sys.stderr)
        return repos

    entries = sorted(os.listdir(workspace))
    with ThreadPoolExecutor(max_workers=16) as pool:
        def check(entry):
            path = os.path.join(workspace, entry)
            if not os.path.isdir(path) or not os.path.exists(os.path.join(path, ".git")):
                return None
            try:
                result = subprocess.run(
                    ["git", "-C", path, "remote", "get-url", "origin"],
                    capture_output=True, text=True, timeout=5,
                )
                parsed = parse_repo_url(result.stdout.strip())
                if parsed and parsed[0].lower() == "angellist":
                    return parsed
            except Exception:
                pass
            return None

        # Dedupe by (owner, name): multiple local dirs can track the same repo
        # (e.g. a clone plus a worktree), which would otherwise render twice.
        seen = set()
        for parsed in pool.map(check, entries):
            if parsed and parsed not in seen:
                seen.add(parsed)
                repos.append(parsed)

    return repos


def detect_gh_user():
    """Detect the authenticated GitHub username."""
    result = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout.strip()


def gh_health():
    """Return None if the GitHub CLI can authenticate and reach GitHub,
    otherwise a short human-readable error string.

    A long-running daemon commonly loses access to the macOS login keychain
    where `gh` stores its token; afterwards every `gh` call fails and the
    dashboard would otherwise just show an empty "No open PRs found". This
    check lets the UI say what's actually wrong and that a restart is needed."""
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        return f"could not run gh: {e}"
    if result.returncode != 0:
        text = (result.stderr or result.stdout or "").strip()
        for line in text.splitlines():
            if line.strip():
                return line.strip()[:200]
        return "gh authentication failed"
    return None


def time_ago(iso_str):
    """Convert an ISO timestamp to a human-readable relative time string."""
    if not iso_str:
        return ""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


# ---------------------------------------------------------------------------
# Harness attribution
# ---------------------------------------------------------------------------
# Agent harnesses stamp the first line of the PR body with an attribution and
# usually a session link, e.g.
#   Generated by Claude Code — [Couchito session (local only)](http://localhost:8787/#/s/...)
#   🛋️ [couch session](https://couch.internal.angellist.com/sessions/...)
#   🤖 Created in Devin ([session](https://app.devin.ai/sessions/...)) by ...

# key -> (label, logo file). Logos are the same assets helios serves, vendored
# into ./logos and downscaled; claude.svg is the official transparent mark from
# claude.ai/favicon.svg rather than helios' dark-tile PNG. Order matters:
# "Claude Code" wins over the Couchito session link that follows it on the line.
HARNESS_SPECS = [
    ("claude", "Claude Code", "claude.svg", r"claude\s*code"),
    ("devin", "Devin", "devin.png", r"\bdevin\b"),
    ("sky", "Sky", "sky.svg", r"\bsky\b"),
    ("couch", "Couch", "couch.png", r"\bcouch(?:ito)?\b"),
    ("codex", "Codex", "openai.svg", r"\bcodex\b"),
    ("cursor", "Cursor", "cursor.png", r"\bcursor\b"),
]
HARNESS_BY_KEY = {key: (label, logo) for key, label, logo, _ in HARNESS_SPECS}
LOGO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logos")
LOGO_FILES = {logo for _, _, logo, _ in HARNESS_SPECS}

# Session-link hosts are the most reliable signal, so they're checked first.
HARNESS_HOSTS = [
    ("app.devin.ai", "devin"),
    ("couch.internal", "couch"),
    ("sky.internal", "sky"),
    ("localhost:8787", "claude"),  # Couchito = local Claude Code session viewer
]

_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
# Only an explicit attribution counts, so a PR that merely talks about Sky or
# Couch in its opening line isn't mistaken for one built by it.
_ATTRIBUTION_RE = re.compile(r"\b(?:generated|created|made|built|authored)\s+(?:by|with|in)\b", re.I)


def detect_harness(body):
    """Return (harness_key, session_url) parsed from a PR body's first line."""
    if not body:
        return None, ""
    first_line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    if not first_line or len(first_line) > 300:
        return None, ""

    session_url = ""
    for anchor, url in _MD_LINK_RE.findall(first_line):
        if "session" in anchor.lower():
            session_url = url
            break

    if session_url:
        low_url = session_url.lower()
        for fragment, key in HARNESS_HOSTS:
            if fragment in low_url:
                return key, session_url
    elif not _ATTRIBUTION_RE.search(first_line):
        return None, ""

    low = first_line.lower()
    for key, _label, _logo, pattern in HARNESS_SPECS:
        if re.search(pattern, low):
            return key, session_url
    return None, ""


def annotate_harness(prs):
    """Attach harness/session fields and drop the body -- it's only needed here."""
    for pr in prs:
        key, session_url = detect_harness(pr.pop("body", ""))
        pr["harness"] = key
        pr["harnessSession"] = session_url
    return prs


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_prs_for_repo(owner, name):
    """Fetch open (non-closed) PRs authored by the user for a single repo."""
    repo = f"{owner}/{name}"
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)

    fields = "number,title,body,isDraft,createdAt,updatedAt,reviewDecision,url,reviews,statusCheckRollup,mergeable,mergeStateStatus"

    result = subprocess.run(
        ["gh", "pr", "list", "--repo", repo, "--author", CONFIG["gh_user"],
         "--state", "open", "--limit", "100", "--json", fields],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        lines = (result.stderr or "").strip().splitlines()
        raise RuntimeError(lines[0] if lines else "gh pr list failed")
    open_prs = annotate_harness(json.loads(result.stdout) if result.stdout.strip() else [])
    for pr in open_prs:
        pr["reviewStates"] = [r.get("state", "") for r in pr.get("reviews", [])]

    open_numbers = [pr["number"] for pr in open_prs]
    graphql_data = fetch_pr_graphql_data(owner, name, open_numbers)
    for pr in open_prs:
        gql = graphql_data.get(pr["number"], {})
        pr["unresolvedThreads"] = gql.get("unresolved", [])
        pr["lastCommentAt"] = gql.get("lastCommentAt", "")

    active, drafts = [], []
    for pr in open_prs:
        if pr.get("isDraft"):
            created = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
            if created >= thirty_days_ago:
                drafts.append(pr)
        else:
            active.append(pr)

    return active, drafts


def fetch_review_prs_for_repo(owner, name):
    """Fetch open PRs waiting for the user's review (not authored by user, not draft, not approved)."""
    repo = f"{owner}/{name}"
    gh_user = CONFIG["gh_user"]
    now = datetime.now(timezone.utc)
    two_days_ago = now - timedelta(days=2)

    fields = "number,title,body,state,isDraft,createdAt,updatedAt,reviewDecision,url,reviews,author"
    search_query = f"is:open -review:approved -is:draft -author:{gh_user}"

    result = subprocess.run(
        ["gh", "pr", "list", "--repo", repo,
         "--search", search_query, "--limit", "50",
         "--json", fields],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        lines = (result.stderr or "").strip().splitlines()
        raise RuntimeError(lines[0] if lines else "gh pr list failed")
    if not result.stdout.strip():
        return []

    prs = annotate_harness(json.loads(result.stdout))

    # Filter to past 2 days and enrich with review info
    filtered = []
    for pr in prs:
        created = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
        if created < two_days_ago:
            continue
        reviews = pr.get("reviews", [])
        pr["reviewStates"] = [r.get("state", "") for r in reviews]
        # Count approvals from others
        approvals = sum(1 for r in reviews if r.get("state") == "APPROVED")
        pr["otherApprovals"] = approvals
        pr["authorLogin"] = pr.get("author", {}).get("login", "")
        filtered.append(pr)

    return filtered


def fetch_merged_prs_for_repo(owner, name):
    """Fetch the user's most recently merged PRs (up to 5) for a single repo."""
    repo = f"{owner}/{name}"
    fields = "number,title,body,url,createdAt,mergedAt"

    result = subprocess.run(
        ["gh", "pr", "list", "--repo", repo, "--author", CONFIG["gh_user"],
         "--state", "merged", "--limit", "5", "--json", fields],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        lines = (result.stderr or "").strip().splitlines()
        raise RuntimeError(lines[0] if lines else "gh pr list failed")
    if not result.stdout.strip():
        return []

    prs = annotate_harness(json.loads(result.stdout))
    # Show most-recently-merged first.
    prs.sort(key=lambda p: p.get("mergedAt", ""), reverse=True)
    return prs


def fetch_pr_graphql_data(owner, name, pr_numbers):
    """Fetch unresolved review thread authors and last comment time via GraphQL."""
    if not pr_numbers:
        return {}

    result = {}
    for i in range(0, len(pr_numbers), 10):
        batch = pr_numbers[i:i + 10]
        fragments = []
        for idx, num in enumerate(batch):
            fragments.append(f"""
                pr{idx}: pullRequest(number: {num}) {{
                    number
                    comments(last: 1) {{
                        nodes {{ createdAt }}
                    }}
                    reviews(last: 1) {{
                        nodes {{ createdAt }}
                    }}
                    reviewThreads(first: 100) {{
                        nodes {{
                            isResolved
                            comments(first: 1) {{
                                nodes {{
                                    author {{ login }}
                                }}
                            }}
                        }}
                    }}
                    latestComment: reviewThreads(last: 1) {{
                        nodes {{
                            comments(last: 1) {{
                                nodes {{ createdAt }}
                            }}
                        }}
                    }}
                }}
            """)

        query = f"""{{ repository(owner: "{owner}", name: "{name}") {{ {"".join(fragments)} }} }}"""

        try:
            proc = subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={query}"],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                continue
            data = json.loads(proc.stdout)
            repo_data = data.get("data", {}).get("repository", {})

            for idx in range(len(batch)):
                pr_data = repo_data.get(f"pr{idx}", {})
                num = pr_data.get("number", batch[idx])

                threads = pr_data.get("reviewThreads", {}).get("nodes", [])
                unresolved = []
                for t in threads:
                    if not t.get("isResolved"):
                        comments = t.get("comments", {}).get("nodes", [])
                        if comments:
                            author = comments[0].get("author", {}).get("login", "unknown")
                            unresolved.append(author)

                timestamps = []
                for c in pr_data.get("comments", {}).get("nodes", []):
                    if c.get("createdAt"):
                        timestamps.append(c["createdAt"])
                for r in pr_data.get("reviews", {}).get("nodes", []):
                    if r.get("createdAt"):
                        timestamps.append(r["createdAt"])
                for t in pr_data.get("latestComment", {}).get("nodes", []):
                    for c in t.get("comments", {}).get("nodes", []):
                        if c.get("createdAt"):
                            timestamps.append(c["createdAt"])

                last_comment = max(timestamps) if timestamps else ""
                result[num] = {"unresolved": unresolved, "lastCommentAt": last_comment}
        except Exception:
            continue

    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def approval_badge(pr):
    decision = pr.get("reviewDecision") or ""
    reviews = pr.get("reviewStates", [])
    approvals = sum(1 for r in reviews if r == "APPROVED")

    if decision == "APPROVED":
        return f'<span class="badge approved">Approved ({approvals})</span>'
    elif decision == "CHANGES_REQUESTED":
        return '<span class="badge changes">Changes Requested</span>'
    elif decision == "REVIEW_REQUIRED":
        if approvals > 0:
            return f'<span class="badge partial">Approved ({approvals}), needs more</span>'
        return '<span class="badge pending">Review Required</span>'
    else:
        if approvals > 0:
            return f'<span class="badge partial">Approved ({approvals})</span>'
        return '<span class="badge none">No Reviews</span>'


def unresolved_badge(pr):
    threads = pr.get("unresolvedThreads", [])
    if not threads:
        return '<span class="resolved">0</span>'

    counts = Counter(threads)
    total = len(threads)

    human_parts = []
    bot_parts = []
    for author, count in counts.most_common():
        label = f"{author} ({count})" if count > 1 else author
        if author.lower() in BOT_AUTHORS or "[bot]" in author.lower():
            bot_parts.append(html_mod.escape(label))
        else:
            human_parts.append(f'<strong>{html_mod.escape(label)}</strong>')

    all_parts = human_parts + bot_parts
    detail = ", ".join(all_parts)

    has_human = len(human_parts) > 0
    cls = "unresolved-human" if has_human else "unresolved-bot"
    return f'<span class="badge {cls}">{total}</span> <span class="comment-detail">{detail}</span>'


def ci_badge(pr):
    checks = pr.get("statusCheckRollup") or []
    if not checks:
        return '<span class="badge none">No CI</span>'

    failed = []
    pending = []
    for check in checks:
        status = check.get("status", "")
        conclusion = check.get("conclusion", "")
        if conclusion == "SKIPPED":
            continue
        if status != "COMPLETED":
            pending.append(check.get("name", ""))
        elif conclusion not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            failed.append(check.get("name", ""))

    if failed:
        names = ", ".join(failed[:3])
        extra = f" +{len(failed) - 3}" if len(failed) > 3 else ""
        return f'<span class="badge ci-fail">{html_mod.escape(names)}{extra}</span>'
    elif pending:
        return f'<span class="badge ci-pending">Running ({len(pending)})</span>'
    else:
        return '<span class="badge ci-pass">Passing</span>'


def merge_badge(pr):
    mergeable = pr.get("mergeable", "")
    merge_state = pr.get("mergeStateStatus", "")

    if merge_state == "BEHIND":
        return '<span class="badge merge-behind">Behind main</span>'
    elif merge_state == "DIRTY" or mergeable == "CONFLICTING":
        return '<span class="badge merge-conflict">Conflicts</span>'
    elif merge_state == "BLOCKED":
        return ""
    elif merge_state == "CLEAN" or mergeable == "MERGEABLE":
        return '<span class="badge merge-ok">Up to date</span>'
    elif mergeable == "UNKNOWN":
        return '<span class="badge none">Checking...</span>'
    return ""


def _approval_sort_key(pr):
    """0 = fully approved, 1 = partial approval, 2 = review required, 3 = changes requested, 4 = no reviews."""
    decision = pr.get("reviewDecision") or ""
    reviews = pr.get("reviewStates", [])
    approvals = sum(1 for r in reviews if r == "APPROVED")

    if decision == "APPROVED":
        return 0
    elif decision == "CHANGES_REQUESTED":
        return 3
    elif decision == "REVIEW_REQUIRED":
        return 1 if approvals > 0 else 2
    else:
        return 1 if approvals > 0 else 4


def _unresolved_sort_key(pr):
    """Number of unresolved threads."""
    return len(pr.get("unresolvedThreads", []))


def _ci_sort_key(pr):
    """0 = passing, 1 = pending/other, 2 = failing."""
    checks = pr.get("statusCheckRollup") or []
    if not checks:
        return 1
    for check in checks:
        conclusion = check.get("conclusion", "")
        if conclusion == "SKIPPED":
            continue
        if check.get("status", "") != "COMPLETED":
            continue
        if conclusion not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            return 2
    for check in checks:
        if check.get("conclusion") == "SKIPPED":
            continue
        if check.get("status", "") != "COMPLETED":
            return 1
    return 0


def default_sort_prs(prs):
    """Sort PRs: approved first, then by unresolved count, then by CI status, then oldest first."""
    return sorted(prs, key=lambda p: (
        _approval_sort_key(p),
        _unresolved_sort_key(p),
        _ci_sort_key(p),
        p.get("createdAt", ""),
    ))


# --- Shared table scaffolding -------------------------------------------------
# Each tab renders as ONE table: a single sticky header on top, then repo rows
# introduced by group rows. Column widths are fixed so everything lines up.

MY_PR_COLS = 9
MY_PR_HEAD = (
    '<colgroup><col style="width:6%"><col style="width:11%"><col style="width:28%">'
    '<col style="width:8%"><col style="width:9%"><col style="width:10%">'
    '<col style="width:11%"><col style="width:10%"><col style="width:7%"></colgroup>'
    '<thead><tr><th>PR</th><th>Harness</th><th>Title</th><th>Created</th><th>Last Comment</th>'
    '<th>Unresolved Comments</th><th>Approval</th><th>CI</th><th>Branch</th></tr></thead>'
)

REVIEW_COLS = 5
REVIEW_HEAD = (
    '<colgroup><col style="width:7%"><col style="width:11%"><col style="width:52%">'
    '<col style="width:15%"><col style="width:15%"></colgroup>'
    '<thead><tr><th>PR</th><th>Harness</th><th>Title</th><th>Created</th><th>Status</th></tr></thead>'
)

MERGED_COLS = 5
MERGED_HEAD = (
    '<colgroup><col style="width:7%"><col style="width:11%"><col style="width:52%">'
    '<col style="width:15%"><col style="width:15%"></colgroup>'
    '<thead><tr><th>PR</th><th>Harness</th><th>Title</th><th>Created</th><th>Merged</th></tr></thead>'
)


def harness_cell(pr):
    """Harness logo, plus the harness name linking to its session when one is stamped."""
    key = pr.get("harness")
    if not key:
        return '<td class="harness-col" data-sort="zzz"><span class="muted">--</span></td>'
    label, logo = HARNESS_BY_KEY[key]
    label_attr = html_mod.escape(label, quote=True)
    chip = (f'<img class="harness-logo" src="/logos/{logo}" alt="{label_attr}" '
            f'title="{label_attr}">')
    url = pr.get("harnessSession") or ""
    session = ""
    if url:
        session = (f'<a class="harness-session" href="{html_mod.escape(url, quote=True)}" '
                   f'target="_blank">{label_attr}</a>')
    return f'<td class="harness-col" data-sort="{key}">{chip}{session}</td>'


def repo_group_row(name, colspan):
    return (f'<tr class="group-row"><td colspan="{colspan}">'
            f'<span class="group-label">{html_mod.escape(name)}</span></td></tr>')


def subgroup_row(label_html, colspan):
    return f'<tr class="subgroup-row"><td colspan="{colspan}">{label_html}</td></tr>'


def note_row(text, colspan):
    return (f'<tr class="note-row"><td colspan="{colspan}">'
            f'<span class="empty">{html_mod.escape(text)}</span></td></tr>')


def wrap_table(head, body, empty_msg):
    if not body:
        return f'<p class="empty">{empty_msg}</p>'
    return f'<table class="sortable">{head}<tbody>{body}</tbody></table>'


def render_pr_rows(prs):
    rows = ""
    for pr in default_sort_prs(prs):
        num = pr["number"]
        title = pr["title"]
        url = pr["url"]
        approval = approval_badge(pr)
        draft_tag = ' <span class="badge draft">Draft</span>' if pr.get("isDraft") else ""

        unresolved_count = len(pr.get("unresolvedThreads", []))
        comments_col = f'<td data-sort="{unresolved_count}">{unresolved_badge(pr)}</td>'

        created_iso = pr.get("createdAt", "")
        created_col = f'<td class="time-col" data-sort="{created_iso}">{time_ago(created_iso)}</td>'

        last_comment = pr.get("lastCommentAt", "")
        last_comment_display = time_ago(last_comment) if last_comment else '<span class="muted">--</span>'
        last_comment_col = f'<td class="time-col" data-sort="{last_comment}">{last_comment_display}</td>'

        approval_td = f'<td data-sort="{_approval_sort_key(pr)}">{approval}</td>'
        ci_col = f'<td data-sort="{_ci_sort_key(pr)}">{ci_badge(pr)}</td>'
        merge_col = f"<td>{merge_badge(pr)}</td>"

        rows += f"""<tr>
            <td data-sort="{num}"><a href="{url}" target="_blank">#{num}</a></td>
            {harness_cell(pr)}
            <td data-sort="{html_mod.escape(title)}"><a href="{url}" target="_blank">{html_mod.escape(title)}</a>{draft_tag}</td>
            {created_col}
            {last_comment_col}
            {comments_col}
            {approval_td}
            {ci_col}
            {merge_col}
        </tr>"""
    return rows


def render_own_prs_rows(active, drafts):
    """Active PRs first (no label -- the repo group row says it all), then drafts
    under their own subgroup label since that window is time-boxed."""
    rows = render_pr_rows(active)
    if drafts:
        rows += subgroup_row(
            f'Drafts (past 30d) <span class="count">({len(drafts)})</span>', MY_PR_COLS)
        rows += render_pr_rows(drafts)
    return rows


def render_review_rows(prs):
    """Review PRs grouped by author, oldest first within a group, groups ordered
    by their oldest PR."""
    groups = {}
    for pr in prs:
        groups.setdefault(pr.get("authorLogin", "unknown"), []).append(pr)
    for author in groups:
        groups[author].sort(key=lambda p: p.get("createdAt", ""))
    sorted_groups = sorted(groups.items(), key=lambda g: g[1][0].get("createdAt", ""))

    rows = ""
    for author, author_prs in sorted_groups:
        author_esc = html_mod.escape(author)
        rows += subgroup_row(
            f'@{author_esc} <span class="count">({len(author_prs)})</span>', REVIEW_COLS)
        for pr in author_prs:
            num = pr["number"]
            url = pr["url"]
            title = html_mod.escape(pr["title"])
            created_iso = pr.get("createdAt", "")
            approvals = pr.get("otherApprovals", 0)
            if approvals > 0:
                approval_html = f'<span class="badge partial">Approved ({approvals})</span>'
            else:
                approval_html = '<span class="badge pending">No approvals</span>'
            rows += f"""<tr>
                <td data-sort="{num}"><a href="{url}" target="_blank">#{num}</a></td>
                {harness_cell(pr)}
                <td data-sort="{title}"><a href="{url}" target="_blank">{title}</a></td>
                <td class="time-col" data-sort="{created_iso}">{time_ago(created_iso)}</td>
                <td>{approval_html}</td>
            </tr>"""
    return rows


def render_merged_rows(prs):
    rows = ""
    for pr in prs:
        num = pr["number"]
        url = pr["url"]
        title = html_mod.escape(pr["title"])
        created_iso = pr.get("createdAt", "")
        merged_iso = pr.get("mergedAt", "")
        rows += f"""<tr>
            <td data-sort="{num}"><a href="{url}" target="_blank">#{num}</a></td>
            {harness_cell(pr)}
            <td data-sort="{title}"><a href="{url}" target="_blank">{title}</a></td>
            <td class="time-col" data-sort="{created_iso}">{time_ago(created_iso)}</td>
            <td class="time-col" data-sort="{merged_iso}">{time_ago(merged_iso)}</td>
        </tr>"""
    return rows


def _fetch_own_for_repo(owner, name):
    try:
        active, drafts = fetch_prs_for_repo(owner, name)
        return ("ok", owner, name, active, drafts)
    except Exception as e:
        return ("error", owner, name, str(e))


def _fetch_review_for_repo(owner, name):
    try:
        review_prs = fetch_review_prs_for_repo(owner, name)
        return ("ok", owner, name, review_prs)
    except Exception as e:
        return ("error", owner, name, str(e))


def _fetch_merged_for_repo(owner, name):
    try:
        merged_prs = fetch_merged_prs_for_repo(owner, name)
        return ("ok", owner, name, merged_prs)
    except Exception as e:
        return ("error", owner, name, str(e))


def gh_error_banner(msg):
    """Prominent red banner shown when the GitHub CLI can't authenticate."""
    return (
        '<div class="gh-error">'
        '⚠️ <strong>Can’t reach GitHub.</strong> '
        'The GitHub CLI couldn’t authenticate'
        f' (<code>{html_mod.escape(msg)}</code>). '
        'The background daemon most likely lost access to your login keychain — '
        'restart it from a terminal with <code>pr-status restart</code>.'
        '</div>'
    )


def build_my_prs_body():
    results = {}
    with ThreadPoolExecutor(max_workers=len(CONFIG["repos"])) as pool:
        futures = {
            pool.submit(_fetch_own_for_repo, owner, name): (owner, name)
            for owner, name in CONFIG["repos"]
        }
        for fut in as_completed(futures):
            owner, name = futures[fut]
            results[(owner, name)] = fut.result()

    body = ""
    for owner, name in CONFIG["repos"]:
        result = results.get((owner, name))
        if result is None:
            continue
        if result[0] == "error":
            body += repo_group_row(name, MY_PR_COLS) + note_row(f"Error: {result[3]}", MY_PR_COLS)
        else:
            _, _, _, active, drafts = result
            if not active and not drafts:
                continue
            body += repo_group_row(name, MY_PR_COLS) + render_own_prs_rows(active, drafts)

    return wrap_table(MY_PR_HEAD, body, "No open PRs found.")


def build_review_body():
    error = gh_health()
    if error:
        return gh_error_banner(error)
    results = {}
    with ThreadPoolExecutor(max_workers=len(CONFIG["repos"])) as pool:
        futures = {
            pool.submit(_fetch_review_for_repo, owner, name): (owner, name)
            for owner, name in CONFIG["repos"]
        }
        for fut in as_completed(futures):
            owner, name = futures[fut]
            results[(owner, name)] = fut.result()

    body = ""
    for owner, name in CONFIG["repos"]:
        result = results.get((owner, name))
        if result is None:
            continue
        if result[0] == "error":
            body += repo_group_row(name, REVIEW_COLS) + note_row(f"Error: {result[3]}", REVIEW_COLS)
        else:
            _, _, _, review_prs = result
            if not review_prs:
                continue
            body += repo_group_row(name, REVIEW_COLS) + render_review_rows(review_prs)

    return wrap_table(REVIEW_HEAD, body, "No PRs waiting for your review.")


def build_merged_body():
    error = gh_health()
    if error:
        return gh_error_banner(error)
    results = {}
    with ThreadPoolExecutor(max_workers=len(CONFIG["repos"])) as pool:
        futures = {
            pool.submit(_fetch_merged_for_repo, owner, name): (owner, name)
            for owner, name in CONFIG["repos"]
        }
        for fut in as_completed(futures):
            owner, name = futures[fut]
            results[(owner, name)] = fut.result()

    body = ""
    for owner, name in CONFIG["repos"]:
        result = results.get((owner, name))
        if result is None:
            continue
        if result[0] == "error":
            body += repo_group_row(name, MERGED_COLS) + note_row(f"Error: {result[3]}", MERGED_COLS)
        else:
            _, _, _, merged_prs = result
            if not merged_prs:
                continue
            body += repo_group_row(name, MERGED_COLS) + render_merged_rows(merged_prs)

    return wrap_table(MERGED_HEAD, body, "No recently merged PRs found.")


def build_full_page(my_prs_html, fetched_epoch, gh_error=None):
    gh_user = html_mod.escape(CONFIG["gh_user"])
    banner_html = gh_error_banner(gh_error) if gh_error else ""
    if gh_error:
        my_prs_html = '<p class="empty">PR data unavailable — see the warning above.</p>'
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>PR Status - {gh_user}</title>
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
           margin: 0; padding: 20px 40px; background: #fff; color: #1a1a1a; }}
    h1 {{ margin-bottom: 4px; }}
    .subtitle {{ color: #666; margin-bottom: 16px; font-size: 14px; display: flex; align-items: center; gap: 8px; }}
    .count {{ color: #666; font-weight: normal; }}
    table {{ width: 100%; border-collapse: collapse; margin-bottom: 12px; table-layout: fixed; }}
    th {{ text-align: left; padding: 6px 12px; background: #f6f8fa; border-bottom: 2px solid #d0d7de;
          font-size: 12px; color: #57606a; text-transform: uppercase; letter-spacing: 0.5px;
          position: sticky; top: 0; z-index: 2; }}
    td {{ padding: 6px 12px; border-bottom: 1px solid #e1e4e8; font-size: 14px; overflow-wrap: break-word; }}
    td:first-child, th:first-child {{ white-space: nowrap; }}
    tr:hover {{ background: #f6f8fa; }}
    /* Per-repo section header inside the shared table */
    .group-row td {{ padding: 28px 0 8px 0; border-bottom: none; }}
    tbody tr.group-row:first-child td {{ padding-top: 12px; }}
    .group-label {{ display: inline-block; font-size: 21px; font-weight: 700;
                    border-left: 4px solid #0969da; padding-left: 12px; }}
    .subgroup-row td {{ padding: 14px 12px 4px; font-size: 14px; font-weight: 600;
                        color: #57606a; border-bottom: 1px solid #e1e4e8; }}
    .group-row:hover, .subgroup-row:hover, .note-row:hover {{ background: none; }}
    a {{ color: #0969da; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 500; }}
    .approved {{ background: #dafbe1; color: #116329; }}
    .changes {{ background: #fff8c5; color: #7a4f01; }}
    .pending {{ background: #ddf4ff; color: #0550ae; }}
    .partial {{ background: #dafbe1; color: #116329; }}
    .none {{ background: #f0f0f0; color: #666; }}
    .draft {{ background: #e8e8e8; color: #57606a; }}
    .ci-pass {{ background: #dafbe1; color: #116329; }}
    .ci-fail {{ background: #ffebe9; color: #cf222e; }}
    .ci-pending {{ background: #fff8c5; color: #7a4f01; }}
    .merge-ok {{ background: #dafbe1; color: #116329; }}
    .merge-behind {{ background: #fff8c5; color: #7a4f01; }}
    .merge-conflict {{ background: #ffebe9; color: #cf222e; }}
    .resolved {{ color: #888; }}
    .unresolved-human {{ background: #ffebe9; color: #cf222e; }}
    .unresolved-bot {{ background: #fff8c5; color: #7a4f01; }}
    .comment-detail {{ font-size: 11px; color: #57606a; margin-left: 4px; }}
    .comment-detail strong {{ color: #cf222e; font-weight: 600; }}
    .time-col {{ white-space: nowrap; color: #57606a; font-size: 13px; }}
    .harness-col {{ white-space: nowrap; }}
    .harness-logo {{ width: 18px; height: 18px; border-radius: 4px; vertical-align: middle; }}
    .harness-session {{ font-size: 12px; margin-left: 6px; vertical-align: middle; }}
    .muted {{ color: #ccc; }}
    .empty {{ color: #888; font-style: italic; }}
    .gh-error {{ background: #ffebe9; border: 1px solid rgba(207,34,46,0.4); color: #cf222e;
                 padding: 12px 16px; border-radius: 8px; margin-bottom: 20px;
                 font-size: 14px; line-height: 1.5; }}
    .gh-error code {{ background: #fff0ef; padding: 1px 6px; border-radius: 4px;
                      font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }}
    /* Tab bar */
    .tabs {{ display: flex; gap: 0; border-bottom: 2px solid #d0d7de; margin-bottom: 24px; }}
    .tab-btn {{ background: none; border: none; border-bottom: 3px solid transparent; margin-bottom: -2px;
                padding: 8px 20px; font-size: 14px; font-weight: 500; color: #57606a;
                cursor: pointer; transition: color 0.15s, border-color 0.15s; }}
    .tab-btn:hover {{ color: #0969da; }}
    .tab-btn.active {{ color: #0969da; border-bottom-color: #0969da; }}
    .tab-content {{ display: none; }}
    .tab-content.active {{ display: block; }}
    /* Reload / action buttons */
    .action-btn {{ background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px;
                   padding: 2px 10px; font-size: 13px; cursor: pointer; color: #0969da;
                   transition: all 0.2s ease; }}
    .action-btn:hover {{ background: #e1e4e8; }}
    .action-btn:active {{ transform: scale(0.95); }}
    .action-btn.loading {{ color: #888; pointer-events: none; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .action-btn.loading::before {{ content: ''; display: inline-block; width: 10px; height: 10px;
                                   border: 2px solid #ccc; border-top-color: #0969da; border-radius: 50%;
                                   animation: spin 0.6s linear infinite; margin-right: 6px; vertical-align: middle; }}
    .review-toolbar {{ margin-bottom: 20px; display: flex; align-items: center; gap: 12px; }}
    .review-placeholder {{ padding: 40px 0; text-align: center; color: #888; }}
    .load-btn {{ background: #0969da; color: #fff; border: none; border-radius: 6px;
                 padding: 8px 20px; font-size: 14px; font-weight: 500; cursor: pointer;
                 transition: background 0.15s; }}
    .load-btn:hover {{ background: #0757ba; }}
    .load-btn.loading {{ background: #6c9fd8; pointer-events: none; }}
    /* No position:relative here -- th is sticky, which already positions the arrow. */
    .sortable th {{ cursor: pointer; user-select: none; padding-right: 20px; }}
    .sortable th::after {{ content: '\\2195'; position: absolute; right: 4px; opacity: 0.3; font-size: 11px; }}
    .sortable th.sort-asc::after {{ content: '\\25B2'; opacity: 0.7; }}
    .sortable th.sort-desc::after {{ content: '\\25BC'; opacity: 0.7; }}
</style>
</head>
<body>
    <h1>PR Status Dashboard</h1>
    <div class="subtitle">
        <span>@{gh_user}</span>
        <span>&middot;</span>
        <span id="age">Data from just now</span>
        <span>&middot;</span>
        <span>Auto-refreshes every {CONFIG["interval"] // 60} min</span>
        <span>&middot;</span>
        <button class="action-btn" id="reload-btn" onclick="doReload()">Reload</button>
    </div>
    {banner_html}
    <div class="tabs">
        <button class="tab-btn active" onclick="switchTab('my-prs', this)">My PRs</button>
        <button class="tab-btn" onclick="switchTab('review', this)">Review Queue</button>
        <button class="tab-btn" onclick="switchTab('merged', this)">Recently Merged</button>
    </div>
    <div id="my-prs" class="tab-content active">
        {my_prs_html}
    </div>
    <div id="review" class="tab-content">
        <div id="review-placeholder" class="review-placeholder">
            <p>Review queue is not loaded automatically.</p>
            <button class="load-btn" id="load-review-btn" onclick="loadReviews()">Load Review Queue</button>
        </div>
        <div id="review-content" style="display:none">
            <div class="review-toolbar">
                <button class="action-btn" onclick="loadReviews()">Refresh</button>
                <span id="review-age" style="color:#888;font-size:13px;"></span>
            </div>
            <div id="review-body"></div>
        </div>
    </div>
    <div id="merged" class="tab-content">
        <div id="merged-placeholder" class="review-placeholder">
            <p>Recently merged PRs are not loaded automatically.</p>
            <button class="load-btn" id="load-merged-btn" onclick="loadMerged()">Load Recently Merged</button>
        </div>
        <div id="merged-content" style="display:none">
            <div class="review-toolbar">
                <button class="action-btn" onclick="loadMerged()">Refresh</button>
                <span id="merged-age" style="color:#888;font-size:13px;"></span>
            </div>
            <div id="merged-body"></div>
        </div>
    </div>
    <script>
    (function() {{
        var fetchedAt = {fetched_epoch};
        var intervalSec = {CONFIG["interval"]};
        function update() {{
            var sec = Math.floor(Date.now()/1000 - fetchedAt);
            var el = document.getElementById('age');
            if (sec < 60) el.textContent = 'Data from just now';
            else if (sec < 120) el.textContent = 'Data from 1 min ago';
            else el.textContent = 'Data from ' + Math.floor(sec/60) + ' min ago';
        }}
        update();
        setInterval(update, 10000);
        setTimeout(function autoReload() {{
            var stale = Math.floor(Date.now()/1000 - fetchedAt);
            if (stale >= intervalSec) {{
                window.location.reload();
            }} else {{
                setTimeout(autoReload, (intervalSec - stale) * 1000);
            }}
        }}, intervalSec * 1000);
    }})();

    function doReload() {{
        var btn = document.getElementById('reload-btn');
        btn.classList.add('loading');
        btn.textContent = 'Refreshing...';
        window.location.href = '/refresh';
    }}

    function switchTab(name, btnEl) {{
        document.querySelectorAll('.tab-content').forEach(function(el) {{ el.classList.remove('active'); }});
        document.querySelectorAll('.tab-btn').forEach(function(el) {{ el.classList.remove('active'); }});
        document.getElementById(name).classList.add('active');
        btnEl.classList.add('active');
    }}

    function loadReviews() {{
        var loadBtn = document.getElementById('load-review-btn');
        var placeholder = document.getElementById('review-placeholder');
        var content = document.getElementById('review-content');
        var body = document.getElementById('review-body');
        var ageEl = document.getElementById('review-age');

        if (loadBtn) loadBtn.classList.add('loading');
        if (loadBtn) loadBtn.textContent = 'Loading...';
        document.querySelectorAll('#review-content .action-btn').forEach(function(b) {{
            b.classList.add('loading'); b.textContent = 'Refreshing...';
        }});

        fetch('/review-data')
            .then(function(r) {{ return r.text(); }})
            .then(function(html) {{
                placeholder.style.display = 'none';
                content.style.display = 'block';
                body.innerHTML = html;
                var now = new Date();
                ageEl.textContent = 'Loaded at ' + now.toLocaleTimeString();
                document.querySelectorAll('#review-content .action-btn').forEach(function(b) {{
                    b.classList.remove('loading'); b.textContent = 'Refresh';
                }});
                initSortable(body);
            }})
            .catch(function() {{
                body.innerHTML = '<p class="empty">Failed to load review queue.</p>';
                content.style.display = 'block';
                placeholder.style.display = 'none';
                document.querySelectorAll('#review-content .action-btn').forEach(function(b) {{
                    b.classList.remove('loading'); b.textContent = 'Retry';
                }});
            }});
    }}

    function loadMerged() {{
        var loadBtn = document.getElementById('load-merged-btn');
        var placeholder = document.getElementById('merged-placeholder');
        var content = document.getElementById('merged-content');
        var body = document.getElementById('merged-body');
        var ageEl = document.getElementById('merged-age');

        if (loadBtn) loadBtn.classList.add('loading');
        if (loadBtn) loadBtn.textContent = 'Loading...';
        document.querySelectorAll('#merged-content .action-btn').forEach(function(b) {{
            b.classList.add('loading'); b.textContent = 'Refreshing...';
        }});

        fetch('/merged-data')
            .then(function(r) {{ return r.text(); }})
            .then(function(html) {{
                placeholder.style.display = 'none';
                content.style.display = 'block';
                body.innerHTML = html;
                var now = new Date();
                ageEl.textContent = 'Loaded at ' + now.toLocaleTimeString();
                document.querySelectorAll('#merged-content .action-btn').forEach(function(b) {{
                    b.classList.remove('loading'); b.textContent = 'Refresh';
                }});
                initSortable(body);
            }})
            .catch(function() {{
                body.innerHTML = '<p class="empty">Failed to load recently merged PRs.</p>';
                content.style.display = 'block';
                placeholder.style.display = 'none';
                document.querySelectorAll('#merged-content .action-btn').forEach(function(b) {{
                    b.classList.remove('loading'); b.textContent = 'Retry';
                }});
            }});
    }}

    function initSortable(root) {{
        (root || document).querySelectorAll('table.sortable').forEach(function(table) {{
            var headers = table.querySelectorAll('thead th');
            headers.forEach(function(th, colIdx) {{
                th.addEventListener('click', function() {{
                    var tbody = table.querySelector('tbody');
                    var asc = !th.classList.contains('sort-asc');
                    headers.forEach(function(h) {{ h.classList.remove('sort-asc', 'sort-desc'); }});
                    th.classList.add(asc ? 'sort-asc' : 'sort-desc');

                    // Group/subgroup label rows split the body into segments; sort
                    // within each segment so repos and authors stay put.
                    var segments = [];
                    var current = {{ label: null, rows: [] }};
                    Array.from(tbody.children).forEach(function(tr) {{
                        if (tr.classList.contains('group-row') || tr.classList.contains('subgroup-row')) {{
                            segments.push(current);
                            current = {{ label: tr, rows: [] }};
                        }} else {{
                            current.rows.push(tr);
                        }}
                    }});
                    segments.push(current);

                    function cmp(a, b) {{
                        var cellA = a.cells[colIdx], cellB = b.cells[colIdx];
                        if (!cellA || !cellB) return 0;
                        var va = cellA.getAttribute('data-sort') || cellA.textContent.trim();
                        var vb = cellB.getAttribute('data-sort') || cellB.textContent.trim();
                        // Strict test: parseFloat('2026-07-30T...') is 2026, which
                        // would make every same-year timestamp compare equal.
                        var numeric = /^-?\\d+(\\.\\d+)?$/;
                        if (numeric.test(va) && numeric.test(vb)) {{
                            return asc ? va - vb : vb - va;
                        }}
                        return asc ? va.localeCompare(vb) : vb.localeCompare(va);
                    }}

                    segments.forEach(function(seg) {{
                        seg.rows.sort(cmp);
                        if (seg.label) tbody.appendChild(seg.label);
                        seg.rows.forEach(function(row) {{ tbody.appendChild(row); }});
                    }});
                }});
            }});
        }});
    }}
    initSortable();
    </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def refresh_repos():
    """Re-scan the workspace in workspace-discovery mode so repos cloned after
    startup appear without a restart. Keeps the existing list if a scan turns up
    nothing (e.g. transient git errors or the workspace being temporarily gone)."""
    workspace_dir = CONFIG.get("workspace")
    if not workspace_dir:
        return
    discovered = discover_workspace_repos(workspace_dir)
    if discovered:
        CONFIG["repos"] = discovered


def refresh_cache():
    refresh_repos()
    error = gh_health()
    my_prs_html = "" if error else build_my_prs_body()
    epoch = int(time.time())
    with _cache_lock:
        _cache["my_prs_html"] = my_prs_html
        _cache["fetched_epoch"] = epoch
        _cache["gh_error"] = error


def background_refresher():
    while True:
        time.sleep(CONFIG["interval"])
        with _last_visit_lock:
            idle_secs = time.time() - _last_visit["t"]
        # Skip refresh if no one has visited in the last 2 intervals
        if idle_secs > CONFIG["interval"] * 2:
            continue
        try:
            refresh_cache()
        except Exception as e:
            print(f"Cache refresh error: {e}", file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _last_visit_lock:
            _last_visit["t"] = time.time()

        if self.path == "/refresh":
            refresh_cache()
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if self.path.startswith("/logos/"):
            self.serve_logo(self.path[len("/logos/"):])
            return

        if self.path == "/review-data":
            html = build_review_body()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        if self.path == "/merged-data":
            html = build_merged_body()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        with _cache_lock:
            my_prs_html = _cache["my_prs_html"]
            epoch = _cache["fetched_epoch"]
            gh_error = _cache["gh_error"]
        if epoch == 0:
            page = "<html><body><h1>Loading...</h1><p>First refresh in progress, reload in a few seconds.</p></body></html>"
        else:
            page = build_full_page(my_prs_html, epoch, gh_error)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode("utf-8"))

    def serve_logo(self, filename):
        """Serve a vendored harness logo. Only the names in HARNESS_SPECS are
        served, so the path can't be used to read anything else."""
        if filename not in LOGO_FILES:
            self.send_error(404)
            return
        try:
            with open(os.path.join(LOGO_DIR, filename), "rb") as fh:
                blob = fh.read()
        except OSError:
            self.send_error(404)
            return
        ctype = "image/svg+xml" if filename.endswith(".svg") else "image/png"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(
        description="PR Status Dashboard - monitor your GitHub PRs locally.",
        epilog="Repos can be passed as arguments, configured in config.yml, or auto-discovered via --workspace.",
    )
    parser.add_argument(
        "repos", nargs="*", metavar="REPO",
        help="GitHub repo URLs (https://github.com/owner/name) or owner/name shorthand",
    )
    parser.add_argument("--port", type=int, default=None, help="Port to serve on (default: 9600)")
    parser.add_argument("--interval", type=int, default=None, help="Refresh interval in seconds (default: 300)")
    parser.add_argument("--workspace", metavar="DIR", default=None,
                        help="Scan this directory for angellist git repos (default: ~/workspace if no repos specified)")
    args = parser.parse_args()

    # Load config: CLI args take precedence over config.yml
    file_config = load_config_file()

    CONFIG["port"] = args.port or (file_config or {}).get("port", 9600)
    CONFIG["interval"] = args.interval or (file_config or {}).get("interval", 300)

    # Determine repo source: CLI > config.yml repos > workspace discovery
    if args.repos:
        for r in args.repos:
            parsed = parse_repo_url(r)
            if not parsed:
                print(f"ERROR: Cannot parse repo '{r}'. Use https://github.com/owner/name or owner/name", file=sys.stderr)
                sys.exit(1)
            CONFIG["repos"].append(parsed)
    elif file_config and file_config.get("repos"):
        for r in file_config["repos"]:
            parsed = parse_repo_url(r)
            if not parsed:
                print(f"ERROR: Cannot parse repo '{r}' from config.yml", file=sys.stderr)
                sys.exit(1)
            CONFIG["repos"].append(parsed)

    # Workspace discovery: explicit --workspace, or config workspace:, or default ~/workspace
    workspace_dir = args.workspace or (file_config or {}).get("workspace") or "~/workspace"
    if not CONFIG["repos"]:
        print(f"Scanning {workspace_dir} for angellist repos...")
        CONFIG["workspace"] = workspace_dir  # re-scanned on every refresh
        CONFIG["repos"] = discover_workspace_repos(workspace_dir)
        if not CONFIG["repos"]:
            print("ERROR: No angellist repos found in workspace and none specified.", file=sys.stderr)
            sys.exit(1)
        print(f"Discovered {len(CONFIG['repos'])} repos: {', '.join(n for _, n in CONFIG['repos'])}")

    # Detect user
    print("Detecting GitHub user...")
    gh_user = detect_gh_user()
    if not gh_user:
        print("ERROR: Could not detect GitHub user. Run 'gh auth login' first.", file=sys.stderr)
        sys.exit(1)
    CONFIG["gh_user"] = gh_user
    print(f"User: @{gh_user}")

    repos_str = ", ".join(f"{o}/{n}" for o, n in CONFIG["repos"])
    print(f"Repos: {repos_str}")
    print("Fetching PR data...")
    sys.stdout.flush()
    refresh_cache()

    t = threading.Thread(target=background_refresher, daemon=True)
    t.start()

    server = HTTPServer(("127.0.0.1", CONFIG["port"]), Handler)
    print(f"PR Status Dashboard running at http://localhost:{CONFIG['port']}")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
