from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from .events import Event, EventSeverity

logger = logging.getLogger(__name__)


# CI fail dedup window — same fail on same (repo, branch) inside this is muted.
CI_FAIL_DEDUP_WINDOW = timedelta(minutes=5)

# How recent a "branch I pushed" must be to count as actively monitored.
RECENT_PUSH_HORIZON = timedelta(hours=24)


@dataclass
class GitHubFilterState:
    """Tracks per-(repo, branch) history needed for CI filtering rules.

    All keyed by (repo_full_name, branch). Values held in memory only — phase
    6 will add disk persistence so a restart doesn't re-fire stale events.
    """

    last_ci_conclusion: Dict[Tuple[str, str], str] = field(default_factory=dict)
    last_ci_fail_at: Dict[Tuple[str, str], datetime] = field(default_factory=dict)


def github_event_should_pass(
    event: Event,
    *,
    state: GitHubFilterState,
    my_recent_branches: Optional[Dict[Tuple[str, str], datetime]] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Apply per-source filter rules.

    Caller supplies the filter state (mutated in place when an event passes
    so future calls remember the latest CI conclusion). `my_recent_branches`
    maps (repo, branch) → last_pushed_at; only branches I've pushed within
    RECENT_PUSH_HORIZON are eligible for CI filtering.
    """
    now = now or datetime.now(timezone.utc)

    if event.source != "github":
        return True

    # Always-pass kinds: review request, mention, issue assignment, PR merge.
    if event.kind in {"review_requested", "mention", "issue_assigned", "pr_merged"}:
        return True

    raw = event.raw or {}
    repo = raw.get("repo")
    branch = raw.get("branch")

    if event.kind == "ci_fail":
        if not _branch_recently_pushed(repo, branch, my_recent_branches, now):
            logger.debug("filter: ci_fail dropped (branch not recent): %s", event.fingerprint)
            return False
        # Mute identical repeated fail within window.
        last_fail = state.last_ci_fail_at.get((repo, branch))
        if last_fail is not None and (now - last_fail) < CI_FAIL_DEDUP_WINDOW:
            logger.debug("filter: ci_fail dropped (repeat in window): %s", event.fingerprint)
            return False
        state.last_ci_fail_at[(repo, branch)] = now
        state.last_ci_conclusion[(repo, branch)] = "failure"
        return True

    if event.kind == "ci_pass_after_fail":
        if not _branch_recently_pushed(repo, branch, my_recent_branches, now):
            return False
        # Only emit if the previous conclusion on this (repo, branch) was fail.
        prev = state.last_ci_conclusion.get((repo, branch))
        state.last_ci_conclusion[(repo, branch)] = "success"
        if prev != "failure":
            logger.debug("filter: ci_pass_after_fail dropped (prev was %s)", prev)
            return False
        return True

    if event.kind == "ci_pass":
        # Plain green-after-green is muted by the rule "never narrate two
        # successes in a row". The poller emits ci_pass; the filter
        # remembers but suppresses it.
        if repo and branch:
            state.last_ci_conclusion[(repo, branch)] = "success"
        return False

    # Unknown github kinds default to pass — better to over-share than to
    # silently drop new event types we add later.
    return True


def _branch_recently_pushed(
    repo: Optional[str],
    branch: Optional[str],
    my_recent_branches: Optional[Dict[Tuple[str, str], datetime]],
    now: datetime,
) -> bool:
    if not repo or not branch:
        return False
    if my_recent_branches is None:
        # No info ⇒ assume yes; better to false-positive than silently drop.
        return True
    pushed_at = my_recent_branches.get((repo, branch))
    if pushed_at is None:
        return False
    return (now - pushed_at) <= RECENT_PUSH_HORIZON
