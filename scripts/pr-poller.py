#!/usr/bin/env python3
"""
PR Poller — NexusMBD automated PR review, merge, and deployment.

Every 5 minutes (via cron):
  1. Iterate NexusMBD repos for open PRs.
  2. Skip if CI is failing.
  3. If CI passes and no Code Reviewer child issue exists → create one.
  4. If Code Reviewer child issue is done → merge, deploy, cleanup.
"""
import json
import logging
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
API     = "http://localhost:3100"
TOKEN   = "pcp_board_5b391fab83204689721526535d3600a0e289bb9e560c834a"
COMPANY = "f7949f00-0ccb-407a-a440-8955b29c06ca"
PROJECT = "2ea13380-f87d-46c5-8d2f-da056aa5da48"
PARENT  = "ff696646-1ae6-402b-b680-3711787fb129"   # MBD-2850

CODE_REVIEWER = "2763d121-89c5-4271-9fd1-85470b98894f"
VPS_USER      = "nexusmbd"
VPS_HOST      = "192.168.88.53"

REPOS = [
    "creative-analyzer",
    "radarbox-connectors",
    "ads",
    "company-website",
    "mbdaaa-app",
    "glorified-dashboard",
    "mbd-client-portal",
    "campaign-opt-platform",
    "external-market-data",
    "mbd-dsr-tools",
]

HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("pr-poller")


# ── Paperclip API helpers ─────────────────────────────────────────────────────
def papi(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(f"{API}{path}", data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.warning("Paperclip %s %s → %s: %s", method, path, e.code, e.read()[:200])
        return None


def get_child_issues():
    """Return all child issues of MBD-2850."""
    result = papi("GET", f"/api/companies/{COMPANY}/issues?parentId={PARENT}&limit=200")
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("issues", result.get("data", []))
    return []


def find_review_issue(pr_url, children):
    """Find an existing Code Reviewer child issue for a given PR URL."""
    for issue in children:
        if pr_url in (issue.get("title", "") + issue.get("description", "")):
            return issue
    return None


def create_review_issue(repo, pr_number, pr_url, branch):
    """Create a Code Reviewer child issue for a PR."""
    payload = {
        "title": f"[Code Review] {repo}#{pr_number}: {branch}",
        "description": (
            f"Review and approve this PR before auto-merge.\n\n"
            f"- **PR:** {pr_url}\n"
            f"- **Repo:** NexusMBD/{repo}\n"
            f"- **Branch:** {branch}\n"
            f"- **PR #:** {pr_number}\n\n"
            f"Mark this issue **done** to trigger automated merge and deployment."
        ),
        "status": "todo",
        "projectId": PROJECT,
        "parentId": PARENT,
        "assigneeId": CODE_REVIEWER,
        "priority": "medium",
        "tags": ["infrastructure", "feature"],
    }
    result = papi("POST", f"/api/companies/{COMPANY}/issues", payload)
    if result:
        log.info("Created review issue %s for %s#%d", result.get("identifier"), repo, pr_number)
    return result


def post_comment(issue_id, body):
    papi("POST", f"/api/issues/{issue_id}/comments", {"body": body})


# ── GitHub helpers ────────────────────────────────────────────────────────────
def gh(args, timeout=30):
    """Run gh CLI and return (stdout, returncode)."""
    try:
        r = subprocess.run(
            ["gh"] + args,
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", 1
    except Exception as e:
        return str(e), 1


def get_open_prs(repo):
    out, rc = gh([
        "pr", "list",
        "--repo", f"NexusMBD/{repo}",
        "--state", "open",
        "--json", "number,title,headRefName,url",
    ])
    if rc != 0 or not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def ci_passing(repo, pr_number):
    """Return True if all CI checks pass (or there are none = treat as pending)."""
    out, rc = gh([
        "pr", "checks",
        "--repo", f"NexusMBD/{repo}",
        str(pr_number),
        "--json", "name,status,conclusion",
    ])
    if rc != 0 or not out:
        return False
    try:
        checks = json.loads(out)
    except json.JSONDecodeError:
        return False

    if not checks:
        return False  # No checks → CI not configured or not yet run

    # All must be PASS / success
    for c in checks:
        conclusion = (c.get("conclusion") or "").lower()
        status = (c.get("status") or "").lower()
        if status in ("pending", "queued", "in_progress"):
            return False
        if conclusion not in ("success", "pass"):
            return False
    return True


def merge_pr(repo, pr_number):
    _, rc = gh([
        "pr", "merge",
        "--repo", f"NexusMBD/{repo}",
        str(pr_number),
        "--squash", "--delete-branch",
    ], timeout=60)
    return rc == 0


def get_merge_sha(repo, pr_number):
    out, rc = gh([
        "pr", "view",
        "--repo", f"NexusMBD/{repo}",
        str(pr_number),
        "--json", "mergeCommit",
    ])
    if rc != 0 or not out:
        return "unknown"
    try:
        oid = json.loads(out).get("mergeCommit", {}).get("oid", "unknown")
        return oid[:8] if oid else "unknown"
    except Exception:
        return "unknown"


# ── Deployment ────────────────────────────────────────────────────────────────
def deploy(repo):
    cmd = (
        f"cd /home/nexusmbd/projects/{repo} && "
        f"git pull && "
        f"docker compose up -d --build"
    )
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
             f"{VPS_USER}@{VPS_HOST}", cmd],
            capture_output=True, text=True, timeout=300
        )
        if r.returncode == 0:
            log.info("Deployed %s successfully", repo)
            return True
        log.error("Deploy %s failed: %s", repo, r.stderr[:500])
        return False
    except subprocess.TimeoutExpired:
        log.error("Deploy %s timed out", repo)
        return False
    except Exception as e:
        log.error("Deploy %s exception: %s", repo, e)
        return False


def cleanup_worktrees(branch):
    """Remove any local git worktrees checked out on the given branch."""
    try:
        r = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True
        )
        worktrees = []
        current = {}
        for line in r.stdout.splitlines():
            if line.startswith("worktree "):
                current = {"path": line.split(" ", 1)[1]}
            elif line.startswith("branch "):
                current["branch"] = line.split("refs/heads/", 1)[-1]
                worktrees.append(current)

        for wt in worktrees:
            if wt.get("branch") == branch:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", wt["path"]],
                    capture_output=True
                )
                log.info("Removed worktree %s", wt["path"])
    except Exception as e:
        log.warning("Worktree cleanup error: %s", e)


# ── Main poll loop ─────────────────────────────────────────────────────────────
def poll():
    log.info("PR Poller cycle start — %s UTC", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))

    # Cache all child issues once per cycle
    children = get_child_issues()
    log.info("Found %d existing review child issues", len(children))

    for repo in REPOS:
        prs = get_open_prs(repo)
        if not prs:
            continue
        log.info("%s: %d open PR(s)", repo, len(prs))

        for pr in prs:
            num    = pr["number"]
            url    = pr["url"]
            branch = pr["headRefName"]
            title  = pr["title"]
            log.info("  PR #%d: %s (%s)", num, title, branch)

            # Step 1: CI check
            if not ci_passing(repo, num):
                log.info("    CI not passing — skipping")
                continue

            log.info("    CI passing")

            # Step 2: Find existing review issue
            review = find_review_issue(url, children)

            if not review:
                # Step 3: Create Code Reviewer child issue
                issue = create_review_issue(repo, num, url, branch)
                if issue:
                    children.append(issue)   # avoid duplicate creation in same cycle
                continue

            review_id     = review.get("id")
            review_status = review.get("status", "")
            review_ident  = review.get("identifier", review_id)

            if review_status != "done":
                log.info("    Review %s not done yet (status=%s)", review_ident, review_status)
                continue

            # Step 4: Merge, deploy, cleanup
            log.info("    Review %s done → merging", review_ident)

            if not merge_pr(repo, num):
                log.error("    Merge failed for %s#%d", repo, num)
                post_comment(review_id, f"⚠️ Auto-merge of PR #{num} failed. Manual merge required.")
                continue

            sha    = get_merge_sha(repo, num)
            log.info("    Merged → %s", sha)

            ok     = deploy(repo)
            status = "✅ Success" if ok else "⚠️ Failed (manual redeploy needed)"

            cleanup_worktrees(branch)

            post_comment(
                review_id,
                f"✅ PR #{num} merged and deployed.\n\n"
                f"- **Merge SHA:** `{sha}`\n"
                f"- **Deployment:** {status}\n"
                f"- **Repo:** NexusMBD/{repo}\n"
                f"- **Branch:** `{branch}` (deleted)"
            )
            log.info("    Done — merge sha=%s deploy=%s", sha, status)

    log.info("PR Poller cycle complete")


if __name__ == "__main__":
    try:
        poll()
    except Exception as e:
        log.error("Fatal error: %s", e, exc_info=True)
        sys.exit(1)
