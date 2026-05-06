from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional, Set

import httpx

from ..mcp_clients.todoist import Task, TodoistClient
from .backoff import Backoff
from .events import Event, EventBus, EventSeverity

logger = logging.getLogger(__name__)

# Tasks change slower than CI; poll less often.
TODOIST_POLL_INTERVAL = 300  # 5 min

# Severity per kind. Overdue is louder; due-today is informational.
SEVERITY = {
    "todoist_overdue": EventSeverity.NORMAL,
    "todoist_due_today": EventSeverity.INFO,
}


def _today_event(t: Task) -> Event:
    return Event(
        source="todoist",
        kind="todoist_due_today",
        summary=f"Today: {t.content}",
        link=t.url,
        ts=datetime.now(timezone.utc),
        fingerprint=f"todoist:due_today:{t.id}",
        severity=SEVERITY["todoist_due_today"],
        raw={"task_id": t.id, "content": t.content},
    )


def _overdue_event(t: Task) -> Event:
    return Event(
        source="todoist",
        kind="todoist_overdue",
        summary=f"Overdue: {t.content}",
        link=t.url,
        ts=datetime.now(timezone.utc),
        fingerprint=f"todoist:overdue:{t.id}",
        severity=SEVERITY["todoist_overdue"],
        raw={"task_id": t.id, "content": t.content},
    )


class TodoistPoller:
    """Fires today / overdue events the first time we see a given task in
    that state. Tasks reverting from overdue back to scheduled (because the
    user nudged the due date) won't refire — only newly-overdue tasks do.

    State is in-memory; phase-6-polish-style persistence can be layered
    later if double-firing across restarts becomes a problem in practice.
    """

    def __init__(
        self,
        client: TodoistClient,
        bus: EventBus,
        *,
        poll_interval_s: float = TODOIST_POLL_INTERVAL,
    ) -> None:
        self._client = client
        self._bus = bus
        self._poll_interval = poll_interval_s
        self._seen_today: Set[str] = set()
        self._seen_overdue: Set[str] = set()
        self._backoff = Backoff()
        self._auth_failed_announced = False

    async def warm_up(self) -> None:
        """Snapshot current today/overdue tasks without emitting events."""
        try:
            today = await self._client.list_tasks(filter_str="today")
            overdue = await self._client.list_tasks(filter_str="overdue")
        except Exception as e:
            logger.warning("todoist warm_up failed: %s", e)
            return
        self._seen_today.update(t.id for t in today)
        self._seen_overdue.update(t.id for t in overdue)
        logger.info(
            "todoist: armed (%d today, %d overdue suppressed)",
            len(self._seen_today), len(self._seen_overdue),
        )

    async def run(self, should_stop: Callable[[], bool]) -> None:
        await self.warm_up()
        while not should_stop():
            sleep_for = self._poll_interval
            try:
                today = await self._client.list_tasks(filter_str="today")
                overdue = await self._client.list_tasks(filter_str="overdue")
                self._backoff.succeeded()
            except Exception as e:
                today = overdue = []
                sleep_for = self._backoff.failed()
                logger.warning(
                    "todoist poll failed (attempt %d, backing off %.0fs): %s",
                    self._backoff.fails, sleep_for, e,
                )
                await self._maybe_announce_auth_failure(e)

            for t in today:
                if t.id in self._seen_today:
                    continue
                self._seen_today.add(t.id)
                await self._bus.publish(_today_event(t))
            for t in overdue:
                if t.id in self._seen_overdue:
                    continue
                self._seen_overdue.add(t.id)
                await self._bus.publish(_overdue_event(t))
            await asyncio.sleep(sleep_for)

    async def _maybe_announce_auth_failure(self, exc: BaseException) -> None:
        if self._auth_failed_announced:
            return
        if not isinstance(exc, httpx.HTTPStatusError):
            return
        if exc.response.status_code != 401:
            return
        self._auth_failed_announced = True
        await self._bus.publish(Event(
            source="todoist",
            kind="auth_failed",
            summary="Todoist token rejected — update ~/.config/clawson/config.toml",
            link="https://todoist.com/app/settings/integrations/developer",
            ts=datetime.now(timezone.utc),
            fingerprint="todoist:auth_failed",
            severity=EventSeverity.CRITICAL,
            raw={"status": 401},
        ))
