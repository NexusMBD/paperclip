"""
P0/P1 Escalation Monitor — comprehensive escalation with suppression rules and cross-agent fallback.

PRIORITY SCOPE: This monitor is STRICTLY scoped to `critical` (P0) and `high` (P1) issues only.
Issues with priority `low` or `medium` MUST be skipped entirely.

Thresholds:
  P0 (critical): blocked > 30 minutes → @CEO
  P1 (high):     blocked > 4 hours    → @PM @CTO

Suppression rules implemented:
  - Rule 1: CEO-acknowledged hold (blockerAttention.state="covered" + stalledBlockerCount=0 + CEO comment within window)
  - Rule 2: Pending-interaction hold (blocker in_review + pending request_confirmation/suggest_tasks/ask_user_questions)
  - Rule 3: Permanent human-gate hold (blocker blocked + CEO/CTO human-gate comment within 48h)
  - Dedup: Recent ESCALATION comment OR cross-agent escalation notice within threshold window

Escalation modes:
  - Stage 1: POST comment directly on blocked issue (preferred)
  - Stage 2: 403 fallback creates cross-agent escalation notice issue

TRI-COPY WARNING — this file exists in three locations:
  SOURCE (edit here first): /home/nexusmbd/paperclip/scripts/p0p1_escalation.py
  RUNNING (cron via routine): /home/nexusmbd/scripts/p0p1-escalation-monitor.py
  DEPLOYED (Paperclip project): /home/nexusmbd/.paperclip/instances/default/projects/
                                   f7949f00-0ccb-407a-a440-8955b29c06ca/
                                   dae0859c-b529-4139-9ff9-69596885d9a6/_default/scripts/p0p1_escalation.py

When making any change:
  1. Edit the SOURCE file (source of truth)
  2. Sync to both deployed paths:
       cp /home/nexusmbd/paperclip/scripts/p0p1_escalation.py /home/nexusmbd/scripts/p0p1-escalation-monitor.py
       cp /home/nexusmbd/paperclip/scripts/p0p1_escalation.py \\
          /home/nexusmbd/.paperclip/instances/default/projects/f7949f00-0ccb-407a-a440-8955b29c06ca/dae0859c-b529-4139-9ff9-69596885d9a6/_default/scripts/p0p1_escalation.py
  3. Verify: diff the three files (should produce no output)
  4. Restart the routine so it picks up the new deployed copy.
"""
import json, sys, time, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone

API     = "http://localhost:3100"
TOKEN   = __import__("os").environ["PAPERCLIP_API_KEY"]
COMPANY = "f7949f00-0ccb-407a-a440-8955b29c06ca"
MONITORING_PROJECT_ID = "dae0859c-b529-4139-9ff9-69596885d9a6"
ESCALATION_NOTICE_PARENT_ID = "3d084828-69a5-4f73-8ca5-845479bd379b"

CEO = "b30408d8-6a35-4677-a85b-8c23a0a46d14"
CTO = "6ce3717b-412c-4ab7-a546-598206864622"
PM  = "b3bd2d64-113e-4ad9-bcc2-2dc05a288851"

P0_THRESHOLD_S      = 30 * 60        # 30 minutes
P1_THRESHOLD_S      = 4  * 60 * 60   # 4 hours
DEDUP_WINDOW_P0_S   = 30 * 60        # P0 dedup: 30 min
DEDUP_WINDOW_P1_S   = 4  * 60 * 60   # P1 dedup: 4 h
CEO_ACK_WINDOW_S    = 30 * 60        # P0: CEO comment within 30 min
CEO_ACK_WINDOW_P1_S = 4  * 60 * 60   # P1: CEO comment within 4 h
HUMAN_GATE_HOLD_S   = 48 * 60 * 60   # 48 hours — Rule 3 human-gate suppression window (legacy; not used when Condition A is active)
HUMAN_GATE_HOLD_DURABLE_S = 30 * 24 * 60 * 60  # 30 days — Rule 3 lookback when blocker is still blocked

HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

# Label IDs for escalation notice issues
# "monitoring" project tag (notices are always created in the monitoring project)
LABEL_MONITORING = "0623c0c2-5447-4283-840a-fe8b4a5b0bd8"
# "task" type tag
LABEL_TASK = "54650751-2ca3-4662-8e88-f6300acecf7b"


def req(method, path, body=None):
    """Make HTTP request to Paperclip API."""
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(f"{API}{path}", data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise PermissionError(f"403 Forbidden: {e}")
        raise


def get_blocked_issues():
    """Fetch all blocked critical/high issues with MANDATORY priority filter."""
    path = f"/api/companies/{COMPANY}/issues?status=blocked&priority=critical,high&limit=100"
    try:
        page = req("GET", path)
        results = page if isinstance(page, list) else page.get("issues", page.get("data", []))

        # Step 0 client-side validation
        unexpected = [i for i in results if i.get("priority") not in ("critical", "high")]
        if unexpected:
            print(f"WARNING: API priority filter may be broken — {len(unexpected)} issues with unexpected priority detected. Applying manual client-side filter.", file=sys.stderr)
            results = [i for i in results if i.get("priority") in ("critical", "high")]

        return results
    except Exception as e:
        print(f"ERROR fetching blocked issues: {e}", file=sys.stderr)
        return []


def now_ts():
    return datetime.now(timezone.utc).timestamp()


def parse_ts(s):
    if not s:
        return 0.0
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return 0.0


def get_comments(issue_id):
    try:
        result = req("GET", f"/api/issues/{issue_id}/comments?order=desc&limit=200")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("items", "comments", "data", "results"):
                if key in result and isinstance(result[key], list):
                    return result[key]
        return result if isinstance(result, list) else []
    except Exception as e:
        print(f"  [warn] get_comments({issue_id}): {e}", file=sys.stderr)
        return None  # None signals fetch failure (vs [] for empty)


def get_full_issue(issue_id):
    """Fetch full issue object to get blockedBy array (Step 3.5)."""
    try:
        return req("GET", f"/api/issues/{issue_id}")
    except Exception as e:
        print(f"  [warn] get_full_issue({issue_id}): {e}", file=sys.stderr)
        return None


def get_interactions(blocker_id):
    """Fetch interactions for a blocker (Step 5)."""
    try:
        return req("GET", f"/api/issues/{blocker_id}/interactions")
    except Exception as e:
        print(f"  [warn] get_interactions({blocker_id}): {e}", file=sys.stderr)
        return []


def search_issues(q):
    """Search for issues by title/identifier."""
    try:
        path = f"/api/companies/{COMPANY}/issues?q={urllib.parse.quote(q)}&status=todo,in_progress,in_review,done,cancelled,blocked,backlog&limit=50"
        page = req("GET", path)
        return page if isinstance(page, list) else page.get("issues", page.get("data", []))
    except Exception as e:
        print(f"  [warn] search_issues({q}): {e}", file=sys.stderr)
        return []


def check_escalation_notice_dedup(issue_identifier, priority):
    """Check for existing escalation notice (Step 3 Part B)."""
    now = now_ts()
    dedup_window = DEDUP_WINDOW_P0_S if priority == "critical" else DEDUP_WINDOW_P1_S

    # Search 1: specific title search
    search1 = search_issues(f"ESCALATION NOTICE {issue_identifier}")

    # Search 2: fallback broad search
    search2 = search_issues(issue_identifier) if not search1 else []

    results = search1 + search2
    for notice in results:
        title = notice.get("title", "")
        if not title.startswith("[ESCALATION NOTICE]"):
            continue
        if issue_identifier not in title:
            continue
        created = parse_ts(notice.get("createdAt"))
        if now - created < dedup_window:
            return True

    return False


def should_suppress(issue, full_issue, comments, priority):
    """Comprehensive suppression logic (Steps 3-5.75)."""
    now = now_ts()
    dedup_window = DEDUP_WINDOW_P0_S if priority == "critical" else DEDUP_WINDOW_P1_S
    ident = issue.get("identifier", "")
    issue_id = issue.get("id")
    blockers = full_issue.get("blockedBy", []) if full_issue else []
    blocker_attention = issue.get("blockerAttention", {})

    # comments=None signals a fetch error; treat as empty for dedup checks
    # but carry the flag into Rule 3 for fail-safe suppress
    comments_fetch_failed = (comments is None)
    comment_list = comments if isinstance(comments, list) else []

    # Step 3 Part A: Recent ESCALATION comment dedup
    for c in comment_list:
        body = c.get("body", "")
        created = parse_ts(c.get("createdAt"))
        if "ESCALATION" in body and now - created < dedup_window:
            return ("recent-escalation-comment", None)

    # Step 3 Part B: Cross-agent escalation notice dedup
    if check_escalation_notice_dedup(ident, priority):
        return ("escalation-notice-dedup", None)

    # Step 4: Rule 1 — CEO-acknowledged hold
    if blocker_attention.get("state") == "covered" and blocker_attention.get("stalledBlockerCount", 0) == 0:
        ceo_ack_window = CEO_ACK_WINDOW_S if priority == "critical" else CEO_ACK_WINDOW_P1_S
        for c in comment_list:
            if c.get("authorAgentId") == CEO:
                created = parse_ts(c.get("createdAt"))
                if now - created < ceo_ack_window:
                    return ("ceo-acknowledged-hold", None)

    # Step 5: Rule 2 — Pending-interaction hold
    if blockers:
        for blocker in blockers:
            blocker_id = blocker.get("id")
            blocker_status = blocker.get("status")

            # Condition 1: blocker must be in_review
            if blocker_status != "in_review":
                continue

            # Condition 2: blocker must have pending interaction
            interactions = get_interactions(blocker_id)
            pending_interaction = False
            for interaction in (interactions if isinstance(interactions, list) else []):
                if interaction.get("status") == "pending" and interaction.get("resolvedAt") is None:
                    if interaction.get("kind") in ("request_confirmation", "suggest_tasks", "ask_user_questions"):
                        pending_interaction = True
                        break

            if not pending_interaction:
                continue

            # Condition 3: blocker must have no unresolved blockers of its own
            blocker_full = get_full_issue(blocker_id)
            blocker_blockers = blocker_full.get("blockedBy", []) if blocker_full else []
            has_unresolved = any(bb.get("status") not in ("done", "cancelled") for bb in blocker_blockers)

            if not has_unresolved:
                return ("rule-2-pending-interaction", blocker.get("identifier", blocker_id))

    # Step 5.5 / 5.75: Rule 3 — Permanent human-gate hold
    # Condition A: at least one direct blocker has status == "blocked"
    # Condition B: CEO or CTO posted a keyword comment on this issue within HUMAN_GATE_HOLD_DURABLE_S
    # Fail-safe: if comment fetch failed AND Condition A is met, default to SUPPRESS
    if blockers:
        any_blocker_blocked = any(bl.get("status") == "blocked" for bl in blockers)
        if any_blocker_blocked:
            blocked_blocker = next((bl.get("identifier", bl.get("id")) for bl in blockers if bl.get("status") == "blocked"), None)

            # Fail-safe: comment fetch error + Condition A → suppress to avoid false positives
            if comments_fetch_failed:
                print(f"  [warn] Rule 3 fail-safe: comment fetch failed for {ident}, suppressing since blocker is blocked", file=sys.stderr)
                return ("rule-3-failsafe-suppress", blocked_blocker)

            # Normal Rule 3: look back HUMAN_GATE_HOLD_DURABLE_S (30 days) for keyword comments.
            # No shorter window — the blocker being still blocked is the true guard.
            GATE_KEYWORDS = ("human-gate", "board action required", "escalation chain exhausted")
            for c in comment_list:
                if c.get("authorAgentId") not in (CEO, CTO):
                    continue
                created = parse_ts(c.get("createdAt"))
                if now - created > HUMAN_GATE_HOLD_DURABLE_S:
                    continue
                body_lower = c.get("body", "").lower()
                if any(kw in body_lower for kw in GATE_KEYWORDS):
                    return ("rule-3-human-gate-permanent-hold", blocked_blocker)

    return (None, None)


def build_comment(issue, priority, blocked_for_s):
    """Build escalation comment."""
    hrs = blocked_for_s / 3600
    mins = blocked_for_s / 60
    duration = f"{hrs:.1f}h" if hrs >= 1 else f"{int(mins)}m"

    ident = issue.get("identifier", issue["id"])
    title = issue.get("title", "")

    if priority == "critical":
        mention = f"[@CEO](agent://{CEO})"
        level = "P0"
        note = "Immediate attention required."
    else:
        mention = f"[@PM](agent://{PM}) [@CTO](agent://{CTO})"
        level = "P1"
        note = "Please triage and unblock."

    lines = [
        f"⚠️ **ESCALATION [{level}]** — {mention}",
        "",
        f"**[{ident}](/MBD/issues/{ident}) — {title}** has been `blocked` for **{duration}**.",
        "",
        note,
        "",
        "_Auto-generated by P0/P1 Escalation Monitor. Resolve the blocker or add a `human-gate` comment to suppress future escalations._",
    ]
    return "\n".join(lines)


def post_comment(issue_id, body):
    """Post comment directly on issue (Stage 1)."""
    req("POST", f"/api/issues/{issue_id}/comments", {"body": body})


def create_escalation_notice(blocked_issue, priority, blocked_for_s, blockers):
    """Create cross-agent escalation notice issue (Stage 2 fallback)."""
    ident = blocked_issue.get("identifier", blocked_issue["id"])
    title = blocked_issue.get("title", "")

    hrs = blocked_for_s / 3600
    duration = f"{hrs:.1f}h" if hrs >= 1 else f"{int(blocked_for_s / 60)}m"

    if priority == "critical":
        level = "P0"
        mention = f"[@CEO](agent://{CEO})"
    else:
        level = "P1"
        mention = f"[@PM](agent://{PM}) [@CTO](agent://{CTO})"

    blocker_list = "\n".join([f"- [{bl.get('identifier', bl['id'])}](/MBD/issues/{bl.get('identifier', bl['id'])}) — {bl.get('title', '')}" for bl in blockers])

    description = f"""## Cross-Agent Escalation Notice

**Blocked Issue:** [{ident}](/MBD/issues/{ident})
**Title:** {title}

**Blocker(s):**
{blocker_list}

**Duration:** Blocked for {duration}

**Responsible Agent:** {blocked_issue.get('assignee', {}).get('name', 'Unknown')}

{mention}

---
_This is a cross-agent escalation notice created because the monitor could not post directly on the blocked issue. Review and escalate as needed._
"""

    notice_title = f"[ESCALATION NOTICE] {level}: {ident} blocked >{int(blocked_for_s / 3600)}h"

    req("POST", f"/api/companies/{COMPANY}/issues", {
        "title": notice_title,
        "description": description,
        "projectId": MONITORING_PROJECT_ID,
        "parentId": ESCALATION_NOTICE_PARENT_ID,
        "priority": priority,
        "status": "todo",
        "labelIds": [LABEL_MONITORING, LABEL_TASK]
    })


def main():
    now = now_ts()
    issues = get_blocked_issues()
    print(f"Found {len(issues)} blocked critical/high issues")

    if not issues:
        print("No P0/P1 blocked issues found.")
        return

    escalated = []
    suppressed = []
    not_threshold = []
    diagnostic_rows = []

    for issue in issues:
        ident = issue.get("identifier", "?")
        priority = issue.get("priority")
        updated = parse_ts(issue.get("updatedAt") or issue.get("statusChangedAt"))
        blocked_s = now - updated
        threshold = P0_THRESHOLD_S if priority == "critical" else P1_THRESHOLD_S

        # Step 2: Check threshold
        if blocked_s < threshold:
            not_threshold.append((ident, blocked_s))
            continue

        # Step 3.5: Enrich with full issue
        full_issue = get_full_issue(issue["id"])
        if not full_issue:
            print(f"  [warn] Could not fetch full issue {ident}, skipping", file=sys.stderr)
            continue

        blockers = full_issue.get("blockedBy", [])

        # Step 3-5.75: Check suppression
        comments = get_comments(issue["id"])
        suppression_rule, blocker_detail = should_suppress(issue, full_issue, comments, priority)

        if suppression_rule:
            suppressed.append((ident, blockers, suppression_rule, blocker_detail))

            # Diagnostic row
            if blockers:
                blocker_id = blockers[0].get("id")
                blocker_ident = blockers[0].get("identifier", blocker_id)
                blocker_status = blockers[0].get("status")
                interactions_url = f"/api/issues/{blocker_id}/interactions"
                pending = "yes" if suppression_rule == "rule-2-pending-interaction" else "no"
            else:
                blocker_ident = "—"
                blocker_status = "—"
                interactions_url = "—"
                pending = "—"

            diagnostic_rows.append({
                "issue": ident,
                "blocker": blocker_ident,
                "blocker_status": blocker_status,
                "interactions_url": interactions_url,
                "pending": pending,
                "rule": suppression_rule,
                "outcome": "suppressed"
            })
            continue

        # Step 6: Escalate
        level = "P0" if priority == "critical" else "P1"
        comment_body = build_comment(issue, priority, blocked_s)

        # Diagnostic row for escalation attempt
        if blockers:
            blocker_id = blockers[0].get("id")
            blocker_ident = blockers[0].get("identifier", blocker_id)
            blocker_status = blockers[0].get("status")
            interactions_url = f"/api/issues/{blocker_id}/interactions"
        else:
            blocker_ident = "—"
            blocker_status = "—"
            interactions_url = "—"

        diagnostic_rows.append({
            "issue": ident,
            "blocker": blocker_ident,
            "blocker_status": blocker_status,
            "interactions_url": interactions_url,
            "pending": "—",
            "rule": "—",
            "outcome": "escalating..."
        })

        try:
            # Stage 1: Try direct comment
            post_comment(issue["id"], comment_body)
            print(f"  ✓ ESCALATED {level} {ident} — {issue.get('title', '')[:60]} (blocked {blocked_s/3600:.1f}h)")
            escalated.append((ident, "direct-comment", blockers))
            # Update diagnostic row
            diagnostic_rows[-1]["outcome"] = "escalated (direct comment)"
        except PermissionError:
            # Stage 2: Create cross-agent escalation notice
            try:
                create_escalation_notice(issue, priority, blocked_s, blockers)
                print(f"  ✓ ESCALATED {level} {ident} via notice — {issue.get('title', '')[:60]} (blocked {blocked_s/3600:.1f}h)")
                escalated.append((ident, "escalation-notice", blockers))
                # Update diagnostic row
                diagnostic_rows[-1]["outcome"] = "escalated (notice)"
            except Exception as e:
                print(f"  ✗ FAILED {level} {ident}: {e}", file=sys.stderr)
                diagnostic_rows[-1]["outcome"] = f"failed: {e}"
        except Exception as e:
            print(f"  ✗ FAILED {level} {ident}: {e}", file=sys.stderr)
            diagnostic_rows[-1]["outcome"] = f"failed: {e}"

    # Step 7: Wrap up with summary and diagnostic table
    print("\n" + "="*80)
    print("ESCALATION MONITOR SUMMARY")
    print("="*80)

    print(f"\n✓ Escalated: {len(escalated)}")
    for ident, method, _ in escalated:
        print(f"  - {ident} ({method})")

    print(f"\n⊘ Suppressed: {len(suppressed)}")
    for ident, _, rule, detail in suppressed:
        detail_str = f" (blocker: {detail})" if detail else ""
        print(f"  - {ident} ({rule}){detail_str}")

    print(f"\n⌛ Not at threshold: {len(not_threshold)}")
    for ident, blocked_s in not_threshold[:5]:
        print(f"  - {ident} (blocked {blocked_s/3600:.1f}h)")
    if len(not_threshold) > 5:
        print(f"  ... and {len(not_threshold)-5} more")

    print("\n" + "="*80)
    print("DIAGNOSTIC TABLE")
    print("="*80)
    print("| Issue | Blocker | Blocker Status | Interactions URL | Pending | Rule | Outcome |")
    print("|-------|---------|----------------|------------------|---------|------|---------|")
    for row in diagnostic_rows:
        print(f"| {row['issue']} | {row['blocker']} | {row['blocker_status']} | {row['interactions_url']} | {row['pending']} | {row['rule']} | {row['outcome']} |")


if __name__ == "__main__":
    main()
