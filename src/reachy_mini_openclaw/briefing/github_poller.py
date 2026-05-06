from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..mcp_clients.github import (
    GitHubClient,
    Notification,
    Repo,
    WorkflowRun,
)
from .backoff import Backoff
from .events import Event, EventBus, EventSeverity
from .filters import GitHubFilterState, github_event_should_pass
from .persistence import load_github_state, save_github_state

logger = logging.getLogger(__name__)


# Polling cadences (seconds).
NOTIFICATIONS_INTERVAL = 30
WORKFLOWS_INTERVAL = 30
REPOS_REFRESH_INTERVAL = 3600

# Cap concurrent watched repos to keep us well under rate limits.
MAX_WATCHED_REPOS = 10
# Skip repos that haven't been pushed to in this long.
REPO_FRESHNESS = timedelta(days=14)


def _api_to_web(api_url: str) -> str:
    """Crude api.github.com → github.com URL rewrite for notification subjects."""
    if not api_url:
        return api_url
    web = api_url.replace("api.github.com/repos", "github.com")
    web = web.replace("/pulls/", "/pull/")
    return web


def _notification_to_event(n: Notification) -> Optional[Event]:
    """Map a GitHub notification reason → Clawson Event kind."""
    reason_map = {
        "review_requested": ("review_requested", EventSeverity.NORMAL),
        "mention":          ("mention",          EventSeverity.NORMAL),
        "team_mention":     ("mention",          EventSeverity.NORMAL),
        "assign":           ("issue_assigned",   EventSeverity.INFO),
        "author":           (None, EventSeverity.INFO),
        "subscribed":       (None, EventSeverity.INFO),
        "comment":          (None, EventSeverity.INFO),
    }
    mapping = reason_map.get(n.reason)
    if not mapping or mapping[0] is None:
        return None
    kind, severity = mapping
    fingerprint = f"github:{kind}:{n.id}:{int(n.updated_at.timestamp())}"
    return Event(
        source="github",
        kind=kind,
        summary=f"{n.repo}: {n.title}",
        link=_api_to_web(n.url),
        ts=n.updated_at,
        fingerprint=fingerprint,
        severity=severity,
        raw={"repo": n.repo, "branch": None, "reason": n.reason, "id": n.id},
    )


def _workflow_run_to_event(
    run: WorkflowRun,
    *,
    previous_conclusion: Optional[str],
) -> Optional[Event]:
    """Classify a completed workflow run into ci_fail / ci_pass_after_fail / ci_pass."""
    if run.status != "completed" or run.conclusion is None:
        return None

    if run.conclusion in {"failure", "timed_out", "startup_failure"}:
        kind = "ci_fail"
        severity = EventSeverity.NORMAL
        summary = f"{run.repo} CI failed on {run.branch or 'unknown'}: {run.name}"
    elif run.conclusion == "success" and previous_conclusion == "failure":
        kind = "ci_pass_after_fail"
        severity = EventSeverity.NORMAL
        summary = f"{run.repo} CI green after fail on {run.branch or 'unknown'}"
    elif run.conclusion == "success":
        kind = "ci_pass"
        severity = EventSeverity.INFO
        summary = f"{run.repo} CI green on {run.branch or 'unknown'}"
    else:
        # cancelled / skipped / neutral / etc — ignore.
        return None

    fingerprint = f"github:{kind}:{run.repo}:{run.id}"
    return Event(
        source="github",
        kind=kind,
        summary=summary,
        link=run.html_url,
        ts=run.updated_at,
        fingerprint=fingerprint,
        severity=severity,
        raw={
            "repo": run.repo,
            "branch": run.branch,
            "run_id": run.id,
            "actor": run.actor_login,
        },
    )


class GitHubPoller:
    """Polls GitHub on multiple cadences and emits normalised Events to the bus.

    Three loops run concurrently:
      - notifications:  every 30s, all reviews/mentions/assignments
      - workflow runs:  every 30s for each watched repo, my actor only
      - repos refresh:  every hour, top-N most-recently-pushed
    """

    def __init__(
        self,
        client: GitHubClient,
        bus: EventBus,
        *,
        filter_state: Optional[GitHubFilterState] = None,
        persist: bool = True,
    ) -> None:
        self._client = client
        self._bus = bus
        # Load prior state from disk so a restart doesn't refire stale events.
        self._persist = persist
        if persist and filter_state is None:
            loaded_state, loaded_since = load_github_state()
            self._filter_state = loaded_state
            self._notifications_since: Optional[datetime] = loaded_since
        else:
            self._filter_state = filter_state or GitHubFilterState()
            self._notifications_since = None
        self._my_login: str = ""
        self._watched_repos: List[Repo] = []
        self._known_run_ids: Dict[str, set] = {}  # repo → seen run IDs
        self._my_recent_branches: Dict[Tuple[str, str], datetime] = {}
        # Backoffs are per-loop so a runs outage doesn't slow notifications.
        self._notif_backoff = Backoff()
        self._runs_backoff = Backoff()

    async def warm_up(self) -> None:
        """Run once before the loops start: identify and pull initial repos."""
        try:
            self._my_login = await self._client.get_authenticated_user()
        except Exception as e:
            logger.error("github: failed to identify user: %s", e)
            raise
        logger.info("github: authenticated as %s", self._my_login)
        await self._refresh_repos()

    async def run(self, should_stop: Callable[[], bool]) -> None:
        await self.warm_up()
        await asyncio.gather(
            self._notifications_loop(should_stop),
            self._workflows_loop(should_stop),
            self._repos_refresh_loop(should_stop),
        )

    # ------------------------------------------------------------------
    # Repos
    # ------------------------------------------------------------------
    async def _repos_refresh_loop(self, should_stop: Callable[[], bool]) -> None:
        # First refresh already happened in warm_up; sleep first.
        while not should_stop():
            await asyncio.sleep(REPOS_REFRESH_INTERVAL)
            if should_stop():
                return
            await self._safe_refresh_repos()

    async def _safe_refresh_repos(self) -> None:
        try:
            await self._refresh_repos()
        except Exception as e:
            logger.warning("github: repo refresh failed: %s", e)

    async def _refresh_repos(self) -> None:
        all_repos = await self._client.list_member_repos()
        cutoff = datetime.now(timezone.utc) - REPO_FRESHNESS
        fresh = [
            r for r in all_repos
            if not r.archived and r.pushed_at >= cutoff
        ]
        self._watched_repos = fresh[:MAX_WATCHED_REPOS]
        logger.info(
            "github: watching %d/%d fresh repos", len(self._watched_repos), len(all_repos)
        )

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    async def _notifications_loop(self, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            sleep_for = NOTIFICATIONS_INTERVAL
            try:
                notifications = await self._client.list_notifications(
                    since=self._notifications_since
                )
                for n in notifications:
                    ev = _notification_to_event(n)
                    if ev is None:
                        continue
                    if not github_event_should_pass(
                        ev,
                        state=self._filter_state,
                        my_recent_branches=self._my_recent_branches,
                    ):
                        continue
                    await self._bus.publish(ev)
                if notifications:
                    self._notifications_since = max(n.updated_at for n in notifications)
                self._notif_backoff.succeeded()
                self._save_state()
            except Exception as e:
                sleep_for = self._notif_backoff.failed()
                logger.warning(
                    "github notifications poll failed (attempt %d, backing off %.0fs): %s",
                    self._notif_backoff.fails, sleep_for, e,
                )
            await asyncio.sleep(sleep_for)

    # ------------------------------------------------------------------
    # Workflow runs
    # ------------------------------------------------------------------
    async def _workflows_loop(self, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            sleep_for = WORKFLOWS_INTERVAL
            any_failure = False
            for repo in list(self._watched_repos):
                if should_stop():
                    return
                try:
                    runs = await self._client.list_workflow_runs(
                        repo.full_name, actor=self._my_login or None, per_page=10
                    )
                except Exception as e:
                    any_failure = True
                    logger.debug("github runs poll failed for %s: %s", repo.full_name, e)
                    continue
                seen = self._known_run_ids.setdefault(repo.full_name, set())
                for run in runs:
                    if run.branch and run.actor_login == self._my_login:
                        self._my_recent_branches[(repo.full_name, run.branch)] = run.created_at
                    if run.id in seen:
                        continue
                    seen.add(run.id)
                    if run.status != "completed":
                        continue
                    prev = self._filter_state.last_ci_conclusion.get(
                        (repo.full_name, run.branch or "")
                    )
                    ev = _workflow_run_to_event(run, previous_conclusion=prev)
                    if ev is None:
                        continue
                    if not github_event_should_pass(
                        ev,
                        state=self._filter_state,
                        my_recent_branches=self._my_recent_branches,
                    ):
                        continue
                    await self._bus.publish(ev)
            if any_failure and self._watched_repos:
                sleep_for = self._runs_backoff.failed()
                logger.warning(
                    "github runs: per-repo failures (attempt %d, backing off %.0fs)",
                    self._runs_backoff.fails, sleep_for,
                )
            else:
                self._runs_backoff.succeeded()
                self._save_state()
            await asyncio.sleep(sleep_for)

    # ------------------------------------------------------------------
    # State save/load
    # ------------------------------------------------------------------
    def _save_state(self) -> None:
        if not self._persist:
            return
        save_github_state(self._filter_state, self._notifications_since)
