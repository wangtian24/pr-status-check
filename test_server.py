#!/usr/bin/env python3
"""Self-check for the status classification and harness detection: python3 test_server.py"""
import server


def item(**kw):
    base = {"isDraft": False, "merge": "ok", "reviewDecision": "REVIEW_REQUIRED",
            "approvers": [], "changesRequestedBy": [], "approvals": 0, "unresolved": [],
            "ci": {"state": "pass", "total": 2, "failed": [], "pending": []}}
    base.update(kw)
    if base["approvers"]:
        base["approvals"] = len(base["approvers"])
    server.classify_mine(base)
    return base


def test_classification():
    assert item(isDraft=True)["status"] == "draft"
    assert item(merge="conflict")["ask"] == "Resolve merge conflicts"
    failing = item(ci={"state": "fail", "total": 3, "failed": [{"name": "lint", "url": ""}], "pending": []})
    assert (failing["status"], failing["ask"]) == ("action", "Fix CI: lint")
    assert item(changesRequestedBy=["bob"])["ask"] == "Address changes requested by @bob"
    threads = item(unresolved=[{"author": "cursor", "human": False}, {"author": "alice", "human": True}])
    assert threads["ask"] == "Reply to 2 unresolved comments from @alice, @cursor"
    assert threads["summary"].startswith("2 unresolved")
    # Conflicts beat everything else that also needs the author.
    assert item(merge="conflict", changesRequestedBy=["bob"])["ask"] == "Resolve merge conflicts"
    ready = item(reviewDecision="APPROVED", approvers=["bob"])
    assert (ready["status"], ready["ask"]) == ("ready", "Merge it")
    assert item(reviewDecision="APPROVED", approvers=["bob"], merge="behind")["ask"] == "Update branch, then merge"
    pending = {"state": "pending", "total": 2, "failed": [], "pending": ["build"]}
    assert item(reviewDecision="APPROVED", approvers=["bob"], ci=pending)["status"] == "running"
    assert item(ci=pending)["status"] == "running"
    waiting = item()
    assert (waiting["status"], waiting["ask"]) == ("waiting", "")
    assert waiting["summary"] == "Review required · CI passing · up to date"


def test_own_review_threads_do_not_count():
    server.CONFIG["gh_user"] = "me"
    pr = {"repository": {"nameWithOwner": "AngelList/Repo"}, "number": 1, "title": "t", "url": "u",
          "createdAt": "2026-01-01T00:00:00Z", "author": {"login": "me"},
          "reviewThreads": {"nodes": [
              {"isResolved": False, "comments": {"nodes": [{"author": {"login": "me"}, "body": "note to self"}]}},
              {"isResolved": False, "comments": {"nodes": [{"author": {"login": "bob"}, "body": "<!-- x -->fix this"}]}},
              {"isResolved": True, "comments": {"nodes": [{"author": {"login": "bob"}, "body": "done"}]}},
          ]}}
    server.CONFIG["repos"] = [("angellist", "repo")]
    server.gh_search_prs = lambda *a, **k: [pr]
    items = server.fetch_mine()
    assert len(items) == 1 and [t["author"] for t in items[0]["unresolved"]] == ["bob"]
    assert items[0]["unresolved"][0]["body"] == "fix this"
    assert items[0]["ask"] == "Reply to 1 unresolved comment from @bob"


def test_fingerprint_changes_on_new_activity():
    a = item(); a["updatedAt"] = "2026-01-01T00:00:00Z"
    b = dict(a, updatedAt="2026-01-02T00:00:00Z")
    assert server.fingerprint(a) != server.fingerprint(b)
    assert server.fingerprint(a) == server.fingerprint(dict(a))


def test_harness_detection():
    assert server.detect_harness("🛋️ [couch session](https://couch.internal.angellist.com/sessions/abc)\n\nbody") == \
        ("couch", "https://couch.internal.angellist.com/sessions/abc")
    assert server.detect_harness("🤖 Created in Claude Code - [Couchito session](http://localhost:8787/#/s/x) (local only)") == \
        ("claude", "http://localhost:8787/#/s/x")
    assert server.detect_harness("Created by Devin") == ("devin", "")
    assert server.detect_harness("This PR touches the couch deploy") == (None, "")
    assert server.detect_harness("") == (None, "")


def test_ci_summary_ignores_skipped_and_reads_legacy_statuses():
    ci = server.ci_summary([
        {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": "u"},
        {"name": "docs", "status": "COMPLETED", "conclusion": "SKIPPED"},
        {"context": "buildkite/nova", "state": "FAILURE", "targetUrl": "b"},
        {"name": "e2e", "status": "IN_PROGRESS", "conclusion": None},
    ])
    assert ci["state"] == "fail" and ci["total"] == 3
    assert [c["name"] for c in ci["failed"]] == ["buildkite/nova"] and ci["pending"] == ["e2e"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
