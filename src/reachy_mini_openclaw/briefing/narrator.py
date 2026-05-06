"""Pattern detection over recent events.

Folds into the morning standup and the both-tap rollup. Pure heuristics
(no LLM) — keeps the rollup grounded and predictable. Each rule returns
one short string when it triggers; rules that don't match return None.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import timedelta
from typing import List, Optional

from .events import Event

logger = logging.getLogger(__name__)


# Cluster threshold — N+ same-kind events on same target within window.
CLUSTER_MIN_COUNT = 3
CLUSTER_WINDOW = timedelta(hours=2)


def narrate(events: List[Event]) -> List[str]:
    """Return zero or more short narration lines for the given events."""
    out: List[str] = []
    line = _detect_ci_cluster(events)
    if line:
        out.append(line)
    line = _detect_recovery(events)
    if line:
        out.append(line)
    line = _detect_review_pile(events)
    if line:
        out.append(line)
    line = _detect_overdue_pile(events)
    if line:
        out.append(line)
    return out


def _detect_ci_cluster(events: List[Event]) -> Optional[str]:
    """3+ ci_fail on the same (repo, branch) inside CLUSTER_WINDOW."""
    by_target: defaultdict = defaultdict(list)
    for e in events:
        if e.kind != "ci_fail":
            continue
        repo = (e.raw or {}).get("repo")
        branch = (e.raw or {}).get("branch")
        if repo and branch:
            by_target[(repo, branch)].append(e)
    for (repo, branch), group in by_target.items():
        if len(group) < CLUSTER_MIN_COUNT:
            continue
        latest = max(group, key=lambda e: e.ts)
        earliest = min(group, key=lambda e: e.ts)
        if (latest.ts - earliest.ts) <= CLUSTER_WINDOW:
            return (
                f"CI keeps failing on {branch} in {repo} — {len(group)} fails in "
                f"the last {CLUSTER_WINDOW.seconds // 3600} hours. Same root cause?"
            )
    return None


def _detect_recovery(events: List[Event]) -> Optional[str]:
    """ci_pass_after_fail without further fails afterward — green for now."""
    last_pass = None
    last_fail_after = False
    for e in sorted(events, key=lambda e: e.ts):
        if e.kind == "ci_pass_after_fail":
            last_pass = e
            last_fail_after = False
        elif e.kind == "ci_fail" and last_pass is not None:
            last_fail_after = True
    if last_pass is not None and not last_fail_after:
        repo = (last_pass.raw or {}).get("repo", "")
        branch = (last_pass.raw or {}).get("branch", "")
        if repo and branch:
            return f"CI is green again on {branch} in {repo}."
    return None


def _detect_review_pile(events: List[Event]) -> Optional[str]:
    n = sum(1 for e in events if e.kind == "review_requested")
    if n >= 4:
        return f"You're on the hook for {n} reviews — heavy day."
    return None


def _detect_overdue_pile(events: List[Event]) -> Optional[str]:
    n = sum(1 for e in events if e.kind == "todoist_overdue")
    if n >= 5:
        return f"{n} tasks slipping into overdue — worth a triage pass."
    return None
