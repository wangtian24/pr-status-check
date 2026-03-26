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
    "gh_user": "",
    "port": 9600,
    "interval": 300,
}

# Global cache
_cache_lock = threading.Lock()
_cache = {"html_body": "", "fetched_epoch": 0}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_repo_url(url):
    """Parse 'https://github.com/owner/name' or 'owner/name' into (owner, name)."""
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    m = re.match(r"(?:https?://github\.com/)?([^/]+)/([^/]+)$", url)
    if not m:
        return None
    return m.group(1), m.group(2)


def load_config_file():
    """Load config.yml from the same directory as this script. Returns dict or None."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "config.yml")
    example_path = os.path.join(script_dir, "config.yml.example")

    if not os.path.exists(config_path):
        return None

    # Minimal YAML parser for our simple config (no dependency needed)
    config = {"repos": []}
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

    return config if config["repos"] else None


def detect_gh_user():
    """Detect the authenticated GitHub username."""
    result = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout.strip()


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
# Data fetching
# ---------------------------------------------------------------------------

def fetch_prs_for_repo(owner, name):
    """Fetch PRs for a single repo using gh CLI."""
    repo = f"{owner}/{name}"
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)
    seven_days_ago = now - timedelta(days=7)

    fields = "number,title,state,isDraft,createdAt,closedAt,updatedAt,reviewDecision,url,reviews,statusCheckRollup,mergeable,mergeStateStatus"

    def run_gh(state):
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--author", CONFIG["gh_user"],
             "--state", state, "--limit", "100",
             "--json", fields],
            capture_output=True, text=True, timeout=30,
        )
        if not result.stdout.strip():
            return []
        prs = json.loads(result.stdout)
        for pr in prs:
            pr["reviewStates"] = [r.get("state", "") for r in pr.get("reviews", [])]
        return prs

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_open = pool.submit(run_gh, "open")
        fut_closed = pool.submit(run_gh, "closed")
        open_prs = fut_open.result()
        closed_prs = fut_closed.result()

    # Fetch unresolved threads + last comment timestamp for open PRs via GraphQL
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

    recent_closed = []
    for pr in closed_prs:
        closed_at = pr.get("closedAt")
        if closed_at:
            closed_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
            if closed_dt >= seven_days_ago:
                recent_closed.append(pr)

    return active, drafts, recent_closed


def fetch_review_prs_for_repo(owner, name):
    """Fetch open PRs waiting for the user's review (not authored by user, not draft, not approved)."""
    repo = f"{owner}/{name}"
    gh_user = CONFIG["gh_user"]
    now = datetime.now(timezone.utc)
    two_days_ago = now - timedelta(days=2)

    fields = "number,title,state,isDraft,createdAt,updatedAt,reviewDecision,url,reviews,author"
    search_query = f"is:open -review:approved -is:draft -author:{gh_user}"

    result = subprocess.run(
        ["gh", "pr", "list", "--repo", repo,
         "--search", search_query, "--limit", "50",
         "--json", fields],
        capture_output=True, text=True, timeout=30,
    )
    if not result.stdout.strip():
        return []

    prs = json.loads(result.stdout)

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


def render_table(prs, show_ci=True):
    if not prs:
        return ""
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

        approval_key = _approval_sort_key(pr)
        approval_td = f'<td data-sort="{approval_key}">{approval}</td>'

        ci_col = ""
        merge_col = ""
        if show_ci:
            ci_key = _ci_sort_key(pr)
            ci_col = f'<td data-sort="{ci_key}">{ci_badge(pr)}</td>'
            merge_col = f"<td>{merge_badge(pr)}</td>"

        rows += f"""<tr>
            <td data-sort="{num}"><a href="{url}" target="_blank">#{num}</a></td>
            <td data-sort="{html_mod.escape(title)}"><a href="{url}" target="_blank">{html_mod.escape(title)}</a>{draft_tag}</td>
            {created_col}
            {last_comment_col}
            {comments_col}
            {approval_td}
            {ci_col}
            {merge_col}
        </tr>"""

    ci_headers = "<th>CI</th><th>Branch</th>" if show_ci else ""
    return f"""<table class="sortable">
        <thead><tr><th>PR</th><th>Title</th><th>Created</th><th>Last Comment</th><th>Unresolved Comments</th><th>Approval</th>{ci_headers}</tr></thead>
        <tbody>{rows}</tbody>
    </table>"""


def render_review_grouped(prs):
    """Render review PRs grouped by author as sections, oldest first within groups, groups ordered by oldest PR."""
    if not prs:
        return ""

    # Group by author
    groups = {}
    for pr in prs:
        author = pr.get("authorLogin", "unknown")
        groups.setdefault(author, []).append(pr)

    # Within each group, sort oldest first
    for author in groups:
        groups[author].sort(key=lambda p: p.get("createdAt", ""))

    # Order groups by oldest PR in each group
    sorted_groups = sorted(groups.items(), key=lambda g: g[1][0].get("createdAt", ""))

    html = ""
    for author, author_prs in sorted_groups:
        author_esc = html_mod.escape(author)
        html += f'<h4 class="review-author">@{author_esc} <span class="count">({len(author_prs)})</span></h4>'
        html += '<div class="review-author-prs">'

        rows = ""
        for pr in author_prs:
            num = pr["number"]
            title = pr["title"]
            url = pr["url"]
            created = time_ago(pr.get("createdAt"))
            created_iso = pr.get("createdAt", "")

            approvals = pr.get("otherApprovals", 0)
            if approvals > 0:
                approval_html = f'<span class="badge partial">Approved ({approvals})</span>'
            else:
                approval_html = '<span class="badge pending">No approvals</span>'

            rows += f"""<tr>
                <td><a href="{url}" target="_blank">#{num}</a></td>
                <td><a href="{url}" target="_blank">{html_mod.escape(title)}</a></td>
                <td class="time-col" data-sort="{created_iso}">{created}</td>
                <td>{approval_html}</td>
            </tr>"""

        html += f"""<table class="sortable">
            <thead><tr><th>PR</th><th>Title</th><th>Created</th><th>Status</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>"""
        html += '</div>'

    return html


def render_review_subsection(prs):
    """Render a per-repo 'Waiting for My Review' subsection."""
    if not prs:
        return ""
    html = '<div class="review-subsection">'
    html += f'<h3 class="review-sub-header">Waiting for My Review <span class="count">({len(prs)})</span></h3>'
    html += render_review_grouped(prs)
    html += "</div>"
    return html


def render_repo_section(owner, name, active, drafts, recent_closed, review_prs):
    if not active and not drafts and not recent_closed and not review_prs:
        return ""
    html = f'<div class="repo-section"><h2 class="repo-name">{html_mod.escape(name)}</h2>'

    # My Own PRs
    has_own = active or drafts or recent_closed
    if has_own:
        html += '<h3 class="section-label">My Own PRs</h3>'
        if active:
            html += f'<h3>Open <span class="count">({len(active)})</span></h3>'
            html += render_table(active, show_ci=True)
        if drafts:
            html += f'<h3>Drafts (past 30d) <span class="count">({len(drafts)})</span></h3>'
            html += render_table(drafts, show_ci=True)
        if recent_closed:
            html += f'<h3>Recently Closed (past 7d) <span class="count">({len(recent_closed)})</span></h3>'
            html += render_table(recent_closed, show_ci=False)

    # Waiting for My Review
    if review_prs:
        html += render_review_subsection(review_prs)

    html += "</div>"
    return html


def _fetch_all_for_repo(owner, name):
    """Fetch own PRs and review PRs for a repo in parallel."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_own = pool.submit(fetch_prs_for_repo, owner, name)
        fut_review = pool.submit(fetch_review_prs_for_repo, owner, name)

        try:
            active, drafts, recent_closed = fut_own.result()
        except Exception as e:
            return ("error", owner, name, str(e))

        try:
            review_prs = fut_review.result()
        except Exception:
            review_prs = []

    return ("ok", owner, name, active, drafts, recent_closed, review_prs)


def build_body():
    # Fetch all repos in parallel
    results = {}
    with ThreadPoolExecutor(max_workers=len(CONFIG["repos"])) as pool:
        futures = {
            pool.submit(_fetch_all_for_repo, owner, name): (owner, name)
            for owner, name in CONFIG["repos"]
        }
        for fut in as_completed(futures):
            owner, name = futures[fut]
            results[(owner, name)] = fut.result()

    # Render in config order
    sections = ""
    for owner, name in CONFIG["repos"]:
        result = results.get((owner, name))
        if result is None:
            continue
        if result[0] == "error":
            sections += f'<div class="repo-section"><h2>{html_mod.escape(name)}</h2><p class="empty">Error: {result[3]}</p></div>'
        else:
            _, _, _, active, drafts, recent_closed, review_prs = result
            sections += render_repo_section(owner, name, active, drafts, recent_closed, review_prs)

    if not sections:
        sections = '<p class="empty">No PRs found across any repo.</p>'
    return sections


def build_full_page(body, fetched_epoch):
    gh_user = html_mod.escape(CONFIG["gh_user"])
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>PR Status - {gh_user}</title>
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
           margin: 0; padding: 20px 40px; background: #fff; color: #1a1a1a; }}
    h1 {{ margin-bottom: 4px; }}
    .subtitle {{ color: #666; margin-bottom: 24px; font-size: 14px; display: flex; align-items: center; gap: 8px; }}
    .repo-section {{ margin-bottom: 36px; }}
    .repo-name {{ margin-top: 28px; margin-bottom: 4px; font-size: 22px;
                  border-left: 4px solid #0969da; padding-left: 12px; }}
    h3 {{ margin-top: 16px; margin-bottom: 6px; font-size: 15px; color: #444;
          border-bottom: 1px solid #e1e4e8; padding-bottom: 4px; }}
    .count {{ color: #666; font-weight: normal; }}
    table {{ width: 100%; border-collapse: collapse; margin-bottom: 12px; }}
    th {{ text-align: left; padding: 6px 12px; background: #f6f8fa; border-bottom: 2px solid #d0d7de;
          font-size: 12px; color: #57606a; text-transform: uppercase; letter-spacing: 0.5px; }}
    td {{ padding: 6px 12px; border-bottom: 1px solid #e1e4e8; font-size: 14px; }}
    tr:hover {{ background: #f6f8fa; }}
    a {{ color: #0969da; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .center {{ text-align: center; }}
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
    .muted {{ color: #ccc; }}
    .empty {{ color: #888; font-style: italic; }}
    .review-section {{ margin-bottom: 36px; padding: 16px 20px; background: #fff8f0; border: 1px solid #f0dcc8;
                       border-radius: 8px; }}
    .review-header {{ margin-top: 0; font-size: 20px; color: #9a6700;
                      border-left: 4px solid #d4a72c; padding-left: 12px; }}
    .section-header {{ margin-bottom: 8px; }}
    .reload-btn {{ background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px;
                   padding: 2px 10px; font-size: 13px; cursor: pointer; color: #0969da;
                   transition: all 0.2s ease; }}
    .reload-btn:hover {{ background: #e1e4e8; }}
    .reload-btn:active {{ transform: scale(0.95); }}
    .reload-btn.loading {{ color: #888; pointer-events: none; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .reload-btn.loading::before {{ content: ''; display: inline-block; width: 10px; height: 10px;
                                   border: 2px solid #ccc; border-top-color: #0969da; border-radius: 50%;
                                   animation: spin 0.6s linear infinite; margin-right: 6px; vertical-align: middle; }}
    .sortable th {{ cursor: pointer; user-select: none; position: relative; padding-right: 20px; }}
    .sortable th::after {{ content: '\\2195'; position: absolute; right: 4px; opacity: 0.3; font-size: 11px; }}
    .sortable th.sort-asc::after {{ content: '\\25B2'; opacity: 0.7; }}
    .sortable th.sort-desc::after {{ content: '\\25BC'; opacity: 0.7; }}
    .section-label {{ font-size: 16px; color: #0969da; margin-top: 12px; margin-bottom: 4px;
                      border-bottom: none; padding-bottom: 0; }}
    .review-subsection {{ margin-top: 16px; padding: 12px 16px; background: #fff8f0; border: 1px solid #f0dcc8;
                          border-radius: 6px; }}
    .review-sub-header {{ margin-top: 0; font-size: 16px; color: #9a6700;
                          border-bottom: 1px solid #f0dcc8; padding-bottom: 4px; }}
    .review-author {{ font-size: 14px; font-weight: 600; color: #57606a; margin: 12px 0 4px 0;
                      padding-left: 4px; border-bottom: none; }}
    .review-author-prs {{ margin-left: 16px; }}
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
        <button class="reload-btn" id="reload-btn" onclick="doReload()">Reload</button>
    </div>
    {body}
    <script>
    (function() {{
        var fetchedAt = {fetched_epoch};
        function update() {{
            var sec = Math.floor(Date.now()/1000 - fetchedAt);
            var el = document.getElementById('age');
            if (sec < 60) el.textContent = 'Data from just now';
            else if (sec < 120) el.textContent = 'Data from 1 min ago';
            else el.textContent = 'Data from ' + Math.floor(sec/60) + ' min ago';
        }}
        update();
        setInterval(update, 10000);
    }})();
    function doReload() {{
        var btn = document.getElementById('reload-btn');
        btn.classList.add('loading');
        btn.textContent = 'Refreshing...';
        window.location.href = '/refresh';
    }}

    // Sortable tables
    document.querySelectorAll('table.sortable').forEach(function(table) {{
        var headers = table.querySelectorAll('th');
        headers.forEach(function(th, colIdx) {{
            th.addEventListener('click', function() {{
                var tbody = table.querySelector('tbody');
                var rows = Array.from(tbody.querySelectorAll('tr'));
                var asc = !th.classList.contains('sort-asc');

                // Clear all sort indicators in this table
                headers.forEach(function(h) {{ h.classList.remove('sort-asc', 'sort-desc'); }});
                th.classList.add(asc ? 'sort-asc' : 'sort-desc');

                rows.sort(function(a, b) {{
                    var cellA = a.cells[colIdx], cellB = b.cells[colIdx];
                    if (!cellA || !cellB) return 0;
                    var va = cellA.getAttribute('data-sort') || cellA.textContent.trim();
                    var vb = cellB.getAttribute('data-sort') || cellB.textContent.trim();
                    // Try numeric comparison
                    var na = parseFloat(va), nb = parseFloat(vb);
                    if (!isNaN(na) && !isNaN(nb)) return asc ? na - nb : nb - na;
                    return asc ? va.localeCompare(vb) : vb.localeCompare(va);
                }});
                rows.forEach(function(row) {{ tbody.appendChild(row); }});
            }});
        }});
    }});
    </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def refresh_cache():
    body = build_body()
    epoch = int(time.time())
    with _cache_lock:
        _cache["html_body"] = body
        _cache["fetched_epoch"] = epoch


def background_refresher():
    while True:
        try:
            refresh_cache()
        except Exception as e:
            print(f"Cache refresh error: {e}", file=sys.stderr)
        time.sleep(CONFIG["interval"])


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/refresh":
            # Force a fresh data fetch, then redirect to /
            refresh_cache()
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return

        with _cache_lock:
            body = _cache["html_body"]
            epoch = _cache["fetched_epoch"]
        if not body:
            page = "<html><body><h1>Loading...</h1><p>First refresh in progress, reload in a few seconds.</p></body></html>"
        else:
            page = build_full_page(body, epoch)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode("utf-8"))

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(
        description="PR Status Dashboard - monitor your GitHub PRs locally.",
        epilog="Repos can be passed as arguments or configured in config.yml (see config.yml.example).",
    )
    parser.add_argument(
        "repos", nargs="*", metavar="REPO",
        help="GitHub repo URLs (https://github.com/owner/name) or owner/name shorthand",
    )
    parser.add_argument("--port", type=int, default=None, help="Port to serve on (default: 9600)")
    parser.add_argument("--interval", type=int, default=None, help="Refresh interval in seconds (default: 300)")
    args = parser.parse_args()

    # Load config: CLI args take precedence over config.yml
    file_config = load_config_file()
    repo_args = args.repos if args.repos else (file_config.get("repos", []) if file_config else [])

    if not repo_args:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        print("ERROR: No repos specified.", file=sys.stderr)
        print(f"Either pass repos as arguments or create config.yml (see config.yml.example):", file=sys.stderr)
        print(f"  cp {script_dir}/config.yml.example {script_dir}/config.yml", file=sys.stderr)
        sys.exit(1)

    for r in repo_args:
        parsed = parse_repo_url(r)
        if not parsed:
            print(f"ERROR: Cannot parse repo '{r}'. Use https://github.com/owner/name or owner/name", file=sys.stderr)
            sys.exit(1)
        CONFIG["repos"].append(parsed)

    CONFIG["port"] = args.port or (file_config or {}).get("port", 9600)
    CONFIG["interval"] = args.interval or (file_config or {}).get("interval", 300)

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
