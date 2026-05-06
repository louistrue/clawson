from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Set

from ..mcp_clients.calendar_ics import CalEvent, CalendarIcsClient
from .backoff import Backoff
from .events import Event, EventBus, EventSeverity

logger = logging.getLogger(__name__)


# ICS feeds change rarely; poll every 5 min.
CALENDAR_POLL_INTERVAL = 300

# Fire `calendar_starting_soon` when an event is this close to starting.
STARTING_SOON_WINDOW = timedelta(minutes=5)


def _starting_soon_event(ev: CalEvent) -> Event:
    location = f" — {ev.location}" if ev.location else ""
    return Event(
        source="calendar",
        kind="calendar_starting_soon",
        summary=f"Up next: {ev.summary}{location}",
        link="",
        ts=datetime.now(timezone.utc),
        fingerprint=f"calendar:starting_soon:{ev.uid}:{ev.start.isoformat()}",
        severity=EventSeverity.NORMAL,
        raw={"uid": ev.uid, "summary": ev.summary, "start": ev.start.isoformat()},
    )


class CalendarPoller:
    """Watches an ICS feed; emits `calendar_starting_soon` once per event
    at the moment it crosses STARTING_SOON_WINDOW. Past events and
    far-future ones are ignored."""

    def __init__(
        self,
        client: CalendarIcsClient,
        bus: EventBus,
        *,
        poll_interval_s: float = CALENDAR_POLL_INTERVAL,
    ) -> None:
        self._client = client
        self._bus = bus
        self._poll_interval = poll_interval_s
        self._fired: Set[str] = set()
        self._backoff = Backoff()

    def list_today(self, events: list[CalEvent], now: datetime) -> list[CalEvent]:
        """Public helper for the standup script."""
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return CalendarIcsClient.events_in_range(
            events, midnight, midnight + timedelta(days=1)
        )

    async def run(self, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            sleep_for = self._poll_interval
            try:
                events = await self._client.fetch_events()
                self._backoff.succeeded()
            except Exception as e:
                events = []
                sleep_for = self._backoff.failed()
                logger.warning(
                    "calendar poll failed (attempt %d, backing off %.0fs): %s",
                    self._backoff.fails, sleep_for, e,
                )

            now = datetime.now(timezone.utc)
            for ev in events:
                key = f"{ev.uid}:{ev.start.isoformat()}"
                if key in self._fired:
                    continue
                delta = (ev.start - now).total_seconds()
                if 0 <= delta <= STARTING_SOON_WINDOW.total_seconds():
                    self._fired.add(key)
                    await self._bus.publish(_starting_soon_event(ev))
            await asyncio.sleep(sleep_for)

    @property
    def fired_count(self) -> int:
        return len(self._fired)
