#!/usr/bin/env python3
"""PR inbox - a local web dashboard showing your GitHub PR status.

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
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Known bot accounts - comments from anyone else are "human"
BOT_AUTHORS = {
    "yupp-reviews", "gemini-code-assist", "copilot", "github-actions",
    "dependabot", "renovate", "codecov", "sonarcloud", "vercel",
    "cursor", "devin-ai-integration",
}

# Populated at startup
CONFIG = {
    "repos": [],       # list of (owner, name)
    "workspace": None, # workspace dir to re-scan each refresh (None = fixed repo list)
    "gh_user": "",
    "port": 9600,
    "interval": 300,
}

# Global cache of the user's own PRs (the only scope refreshed in the background)
_cache_lock = threading.Lock()
_cache = {"items": [], "errors": [], "fetched_epoch": 0, "gh_error": None}

# Track last page visit so background refresh pauses when page is closed
_last_visit_lock = threading.Lock()
_last_visit = {"t": 0.0}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


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
    config_path = os.path.join(SCRIPT_DIR, "config.yml")

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
    dashboard would otherwise just show an empty inbox. This check lets the
    UI say what's actually wrong and that a restart is needed."""
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
HARNESSES = {key: {"label": label, "logo": logo} for key, label, logo, _ in HARNESS_SPECS}
LOGO_DIR = os.path.join(SCRIPT_DIR, "logos")
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


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def gh_search_prs(query, fields, page_size=50, max_results=1000):
    """Run a GraphQL PR search across all repos, following pages.

    One search replaces a per-repo `gh pr list` loop: GitHub bills GraphQL by
    requested nodes, so per-repo loops over a large workspace exhaust the hourly budget."""
    nodes, cursor = [], None
    while True:
        after = f", after: {json.dumps(cursor)}" if cursor else ""
        q = (f"{{ search(query: {json.dumps(query)}, type: ISSUE, first: {page_size}{after}) {{ "
             f"pageInfo {{ hasNextPage endCursor }} "
             f"nodes {{ ... on PullRequest {{ repository {{ nameWithOwner }} {fields} }} }} }} }}")
        proc = subprocess.run(["gh", "api", "graphql", "-f", f"query={q}"],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            lines = (proc.stderr or "").strip().splitlines()
            raise RuntimeError(lines[0] if lines else "gh api graphql failed")
        search = json.loads(proc.stdout)["data"]["search"]
        nodes += [n for n in search["nodes"] if n]
        if not search["pageInfo"]["hasNextPage"] or len(nodes) >= max_results:
            return nodes
        cursor = search["pageInfo"]["endCursor"]


def wanted_repos():
    return {f"{o}/{n}".lower(): (o, n) for o, n in CONFIG["repos"]}


def owner_qualifiers():
    return " ".join(sorted({f"user:{o}" for o, _ in CONFIG["repos"]}))


def _nodes(obj, *path):
    for p in path:
        obj = (obj or {}).get(p)
    return (obj or {}).get("nodes") or []


def _login(obj):
    return ((obj or {}).get("login") or "").strip()


def is_bot(login):
    low = (login or "").lower()
    return low in BOT_AUTHORS or "[bot]" in low


def _check(c):
    """Normalise a CheckRun or legacy StatusContext into {name, state, url}
    where state is pass | fail | pending | skipped."""
    if "context" in c:
        st = c.get("state", "")
        state = ("pending" if st in ("PENDING", "EXPECTED") else
                 "pass" if st == "SUCCESS" else "fail")
        return {"name": c["context"], "state": state, "url": c.get("targetUrl") or ""}
    conclusion = c.get("conclusion") or ""
    if c.get("status") != "COMPLETED":
        state = "pending"
    elif conclusion in ("SUCCESS", "NEUTRAL"):
        state = "pass"
    elif conclusion == "SKIPPED":
        state = "skipped"
    else:
        state = "fail"
    return {"name": c.get("name", ""), "state": state, "url": c.get("detailsUrl") or ""}


def ci_summary(rollup_nodes):
    checks = [_check(c) for c in rollup_nodes if c]
    checks = [c for c in checks if c["state"] != "skipped"]
    failed = [c for c in checks if c["state"] == "fail"]
    pending = [c for c in checks if c["state"] == "pending"]
    state = "none" if not checks else "fail" if failed else "pending" if pending else "pass"
    return {"state": state, "total": len(checks), "failed": failed, "pending": [c["name"] for c in pending]}


def merge_state(pr):
    mergeable = pr.get("mergeable", "")
    ms = pr.get("mergeStateStatus", "")
    if ms == "BEHIND":
        return "behind"
    if ms == "DIRTY" or mergeable == "CONFLICTING":
        return "conflict"
    if ms == "BLOCKED":
        return "blocked"
    if ms == "CLEAN" or mergeable == "MERGEABLE":
        return "ok"
    if mergeable == "UNKNOWN":
        return "unknown"
    return ""


BODY_LIMIT = 6000
COMMENT_LIMIT = 400
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def clip(text, limit):
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def base_item(pr, scope):
    repo_full = pr["repository"]["nameWithOwner"]
    owner, name = repo_full.split("/", 1)
    body = pr.get("body") or ""
    harness, session = detect_harness(body)
    return {
        "id": f"{repo_full}#{pr['number']}",
        "scope": scope,
        "owner": owner, "repo": name, "number": pr["number"],
        "title": pr.get("title") or "", "url": pr.get("url") or "",
        "author": _login(pr.get("author")),
        "isDraft": bool(pr.get("isDraft")),
        "createdAt": pr.get("createdAt") or "",
        "updatedAt": pr.get("updatedAt") or pr.get("createdAt") or "",
        "mergedAt": pr.get("mergedAt") or "",
        "harness": harness, "harnessSession": session,
        "body": clip(body, BODY_LIMIT),
        "additions": pr.get("additions", 0), "deletions": pr.get("deletions", 0),
        "changedFiles": pr.get("changedFiles", 0),
    }


def review_fields(pr, item):
    reviews = _nodes(pr, "reviews")
    # Latest review per author decides their stance, so an old "changes
    # requested" that was later approved doesn't keep blocking.
    latest = {}
    for r in reviews:
        login = _login(r.get("author"))
        if login and r.get("state") in ("APPROVED", "CHANGES_REQUESTED"):
            latest[login] = r["state"]
    item["reviewDecision"] = pr.get("reviewDecision") or ""
    item["approvers"] = sorted(l for l, s in latest.items() if s == "APPROVED")
    item["changesRequestedBy"] = sorted(l for l, s in latest.items() if s == "CHANGES_REQUESTED")
    item["approvals"] = len(item["approvers"])


def names(logins, limit=3):
    logins = list(logins)
    shown = ", ".join("@" + l for l in logins[:limit])
    return shown + (f" +{len(logins) - limit}" if len(logins) > limit else "")


def approval_text(item):
    if item["changesRequestedBy"]:
        return f"Changes requested by {names(item['changesRequestedBy'])}"
    if item["approvals"]:
        txt = f"Approved by {names(item['approvers'])}"
        if item["reviewDecision"] == "REVIEW_REQUIRED":
            txt += ", needs more"
        return txt
    if item["reviewDecision"] == "REVIEW_REQUIRED":
        return "Review required"
    return "No reviews"


def ci_text(ci):
    if ci["state"] == "none":
        return "No CI"
    if ci["state"] == "fail":
        shown = ", ".join(c["name"] for c in ci["failed"][:3])
        extra = f" +{len(ci['failed']) - 3}" if len(ci["failed"]) > 3 else ""
        return f"CI failing: {shown}{extra}"
    if ci["state"] == "pending":
        return f"CI running ({len(ci['pending'])})"
    return "CI passing"


MERGE_TEXT = {"behind": "behind main", "conflict": "merge conflicts", "ok": "up to date",
              "blocked": "", "unknown": "mergeability unknown", "": ""}


def classify_mine(item):
    """Set status, ask (what the user must do, empty when nothing) and summary."""
    ci, merge = item["ci"], item["merge"]
    unresolved = item["unresolved"]
    parts = [approval_text(item), ci_text(ci)]
    if MERGE_TEXT[merge]:
        parts.append(MERGE_TEXT[merge])
    if unresolved:
        parts.insert(0, f"{len(unresolved)} unresolved")
    item["summary"] = " · ".join(parts)

    if item["isDraft"]:
        item["status"], item["ask"] = "draft", ""
        return
    if merge == "conflict":
        item["status"], item["ask"] = "action", "Resolve merge conflicts"
        return
    if ci["state"] == "fail":
        shown = ", ".join(c["name"] for c in ci["failed"][:2])
        extra = f" +{len(ci['failed']) - 2}" if len(ci["failed"]) > 2 else ""
        item["status"], item["ask"] = "action", f"Fix CI: {shown}{extra}"
        return
    if item["changesRequestedBy"]:
        item["status"], item["ask"] = "action", f"Address changes requested by {names(item['changesRequestedBy'])}"
        return
    if unresolved:
        humans = sorted({t["author"] for t in unresolved if t["human"]})
        bots = sorted({t["author"] for t in unresolved if not t["human"]})
        n = len(unresolved)
        who = names(humans + bots)
        item["status"], item["ask"] = "action", f"Reply to {n} unresolved comment{'s' if n > 1 else ''} from {who}"
        return
    if item["reviewDecision"] == "APPROVED":
        if ci["state"] == "pending":
            item["status"], item["ask"] = "running", ""
        elif merge == "behind":
            item["status"], item["ask"] = "ready", "Update branch, then merge"
        else:
            item["status"], item["ask"] = "ready", "Merge it"
        return
    if ci["state"] == "pending":
        item["status"], item["ask"] = "running", ""
        return
    item["status"], item["ask"] = "waiting", ""


def fingerprint(item):
    return "|".join(str(x) for x in (
        item["updatedAt"], item.get("status"), len(item.get("unresolved", [])),
        item.get("ci", {}).get("state"), item.get("reviewDecision"), item.get("merge"),
    ))


OWN_PR_FIELDS = """
    number title body isDraft createdAt updatedAt url reviewDecision mergeable mergeStateStatus
    additions deletions changedFiles
    author { login }
    reviews(last: 50) { nodes { state createdAt author { login } } }
    comments(last: 1) { nodes { createdAt } }
    commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 50) { nodes {
        ... on CheckRun { name status conclusion detailsUrl }
        ... on StatusContext { context state targetUrl } } } } } } }
    reviewThreads(first: 20) { nodes { isResolved
        comments(first: 1) { nodes { author { login } body url path createdAt } } } }
    latestThread: reviewThreads(last: 1) { nodes { comments(last: 1) { nodes { createdAt } } } }
"""


def fetch_mine():
    """The user's open PRs (drafts only from the past 30 days), as inbox items."""
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    prs = gh_search_prs(
        f"is:pr is:open archived:false author:{CONFIG['gh_user']} {owner_qualifiers()}",
        OWN_PR_FIELDS, page_size=25)
    wanted = wanted_repos()
    items = []
    for pr in prs:
        if pr["repository"]["nameWithOwner"].lower() not in wanted:
            continue
        item = base_item(pr, "mine")
        if item["isDraft"] and datetime.fromisoformat(item["createdAt"].replace("Z", "+00:00")) < thirty_days_ago:
            continue
        review_fields(pr, item)
        commits = _nodes(pr, "commits")
        rollup = (commits[0]["commit"].get("statusCheckRollup") if commits else None) or {}
        item["ci"] = ci_summary(_nodes(rollup, "contexts"))
        item["merge"] = merge_state(pr)
        item["unresolved"] = []
        for t in _nodes(pr, "reviewThreads"):
            first = (_nodes(t, "comments") or [None])[0]
            if t.get("isResolved") or not first:
                continue
            login = _login(first.get("author")) or "unknown"
            if login == CONFIG["gh_user"]:  # the author's own notes don't block the author
                continue
            item["unresolved"].append({
                "author": login, "human": not is_bot(login),
                "body": clip(_HTML_COMMENT_RE.sub("", first.get("body") or ""), COMMENT_LIMIT),
                "url": first.get("url") or "", "path": first.get("path") or "",
                "createdAt": first.get("createdAt") or "",
            })
        timestamps = [c.get("createdAt") for c in _nodes(pr, "comments")]
        timestamps += [r.get("createdAt") for r in _nodes(pr, "reviews")[-1:]]
        for t in _nodes(pr, "latestThread"):
            timestamps += [c.get("createdAt") for c in _nodes(t, "comments")]
        item["lastCommentAt"] = max((t for t in timestamps if t), default="")
        classify_mine(item)
        item["fp"] = fingerprint(item)
        items.append(item)
    return items


REVIEW_FIELDS = """
    number title body isDraft createdAt updatedAt url reviewDecision
    additions deletions changedFiles
    author { login }
    reviews(last: 30) { nodes { state author { login } } }
    reviewRequests(first: 20) { nodes { requestedReviewer {
        ... on User { login } ... on Team { slug } } } }
"""


def fetch_review():
    """Open PRs from the past 2 days by others, not yet approved, waiting for review."""
    gh_user = CONFIG["gh_user"]
    since = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = (f"is:pr is:open -review:approved -is:draft -author:{gh_user} "
             f"created:>={since} {owner_qualifiers()}")
    wanted = wanted_repos()
    items = []
    for pr in gh_search_prs(query, REVIEW_FIELDS):
        if pr["repository"]["nameWithOwner"].lower() not in wanted:
            continue
        item = base_item(pr, "review")
        review_fields(pr, item)
        requested = [_login(r.get("requestedReviewer")) for r in _nodes(pr, "reviewRequests")]
        item["requested"] = gh_user in requested
        item["ci"] = {"state": "none", "total": 0, "failed": [], "pending": []}
        item["merge"] = ""
        item["unresolved"] = []
        item["lastCommentAt"] = ""
        item["status"] = "review"
        item["ask"] = ("Review requested from you" if item["requested"]
                       else f"Review @{item['author']}'s PR")
        size = f"+{item['additions']} −{item['deletions']} in {item['changedFiles']} file{'s' if item['changedFiles'] != 1 else ''}"
        item["summary"] = f"{approval_text(item)} · {size}"
        item["fp"] = fingerprint(item)
        items.append(item)
    return items


def fetch_merged():
    """The user's 5 most recently merged PRs per repo."""
    # ponytail: only the 100 most recently updated merged PRs are scanned; a repo
    # whose last merge is older drops off. Raise max_results if that matters.
    prs = gh_search_prs(
        f"is:pr is:merged author:{CONFIG['gh_user']} sort:updated-desc {owner_qualifiers()}",
        "number title body url createdAt updatedAt mergedAt author { login } additions deletions changedFiles",
        max_results=100)
    wanted = wanted_repos()
    per_repo = {}
    for pr in prs:
        full = pr["repository"]["nameWithOwner"]
        if full.lower() in wanted:
            per_repo.setdefault(full, []).append(pr)
    items = []
    for full, repo_prs in per_repo.items():
        repo_prs.sort(key=lambda p: p.get("mergedAt", ""), reverse=True)
        for pr in repo_prs[:5]:
            item = base_item(pr, "merged")
            item.update({"reviewDecision": "", "approvers": [], "changesRequestedBy": [], "approvals": 0,
                         "ci": {"state": "none", "total": 0, "failed": [], "pending": []},
                         "merge": "", "unresolved": [], "lastCommentAt": "",
                         "status": "done", "ask": "", "summary": "Merged"})
            item["fp"] = fingerprint(item)
            items.append(item)
    return items


FETCHERS = {"mine": fetch_mine, "review": fetch_review, "merged": fetch_merged}


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
    errors, fetched = [], None
    if not error:
        try:
            fetched = fetch_mine()
        except Exception as e:
            # Keep the last good data (e.g. while rate-limited) and surface the failure.
            errors = [{"scope": "mine", "message": str(e)}]
    with _cache_lock:
        if fetched is not None:
            _cache["items"] = fetched
            _cache["fetched_epoch"] = int(time.time())
        elif not _cache["fetched_epoch"]:
            _cache["fetched_epoch"] = int(time.time())
        _cache["errors"] = errors
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


def api_prs(scopes, force):
    """Items for the requested scopes. "mine" comes from the background cache
    unless force=1; the on-demand scopes are always fetched live, in parallel."""
    with _cache_lock:
        age = time.time() - _cache["fetched_epoch"]
    if "mine" in scopes and (force or age > CONFIG["interval"]):
        refresh_cache()
    with _cache_lock:
        payload = {"items": list(_cache["items"]) if "mine" in scopes else [],
                   "errors": list(_cache["errors"]) if "mine" in scopes else [],
                   "fetchedAt": _cache["fetched_epoch"], "ghError": _cache["gh_error"]}
    live = [s for s in scopes if s in FETCHERS and s != "mine"]
    if live and not payload["ghError"]:
        def run(scope):
            try:
                return scope, FETCHERS[scope](), None
            except Exception as e:
                return scope, [], str(e)
        with ThreadPoolExecutor(max_workers=len(live)) as pool:
            for scope, items, err in pool.map(run, live):
                payload["items"] += items
                if err:
                    payload["errors"].append({"scope": scope, "message": err})
    payload.update({"user": CONFIG["gh_user"], "interval": CONFIG["interval"],
                    "repos": [n for _, n in CONFIG["repos"]], "harnesses": HARNESSES,
                    "now": int(time.time())})
    return payload


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _last_visit_lock:
            _last_visit["t"] = time.time()
        url = urlparse(self.path)
        qs = parse_qs(url.query)

        if url.path == "/":
            return self.serve_file("index.html", "text/html; charset=utf-8", cache=False)
        if url.path.startswith("/logos/"):
            name = url.path[len("/logos/"):]
            if name not in LOGO_FILES:  # only the vendored harness logos are served
                return self.send_error(404)
            ctype = "image/svg+xml" if name.endswith(".svg") else "image/png"
            return self.serve_file(os.path.join("logos", name), ctype, cache=True)
        if url.path == "/api/prs":
            scopes = [s for s in (qs.get("scope", ["mine"])[0]).split(",") if s]
            force = qs.get("refresh", ["0"])[0] == "1"
            with _cache_lock:
                loading = _cache["fetched_epoch"] == 0
            if "mine" in scopes and loading and not force:
                return self.send_json({"loading": True}, status=503)
            return self.send_json(api_prs(scopes, force))
        self.send_error(404)

    def send_json(self, obj, status=200):
        blob = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def serve_file(self, rel, ctype, cache):
        try:
            with open(os.path.join(SCRIPT_DIR, rel), "rb") as fh:
                blob = fh.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(
        description="PR inbox - monitor your GitHub PRs locally.",
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
    print(f"PR inbox running at http://localhost:{CONFIG['port']}")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
