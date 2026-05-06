from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, List, Optional

from ..clawson_config import FocusSettings
from .events import Event

logger = logging.getLogger(__name__)


# How often we wake to check the next-fire time. Short enough that a
# day-boundary slip (DST, suspend/resume) gets corrected within a minute.
TICK_S = 60.0


def next_standup_datetime(focus: FocusSettings, now: datetime) -> datetime:
    """Compute the next datetime in `focus.timezone` that hits standup time
    on a configured standup day. Always strictly in the future."""
    tz = focus.tzinfo()
    now_tz = now.astimezone(tz)
    candidate = now_tz.replace(
        hour=focus.standup_time.hour,
        minute=focus.standup_time.minute,
        second=0,
        microsecond=0,
    )
    if candidate <= now_tz:
        candidate += timedelta(days=1)
    # Walk forward until we land on a standup weekday.
    safety = 0
    while candidate.weekday() not in focus.standup_days:
        candidate += timedelta(days=1)
        safety += 1
        if safety > 14:
            break
    return candidate


def is_within_active_hours(focus: FocusSettings, now: datetime) -> bool:
    """True if `now` (in any tz) falls inside [active_start, active_end] in
    the configured timezone. End is exclusive so 18:00 means 'until 18:00'."""
    tz = focus.tzinfo()
    local = now.astimezone(tz)
    minutes = local.hour * 60 + local.minute
    start = focus.active_start.hour * 60 + focus.active_start.minute
    end = focus.active_end.hour * 60 + focus.active_end.minute
    return start <= minutes < end


class StandupRunner:
    """Runs the morning standup at the configured time/days.

    Phase 3 ships the schedule loop, the script builder, and the announce
    hand-off. Triggers other than the cron — face-detect, widget button,
    voice — plug in by calling `run_now()` directly; phase 5 will wire
    them up.
    """

    def __init__(
        self,
        focus: FocusSettings,
        *,
        on_announce: Optional[Callable[[str], Awaitable[None]]] = None,
        queued_events_provider: Optional[Callable[[], List[Event]]] = None,
        drain_queued: Optional[Callable[[], List[Event]]] = None,
    ) -> None:
        self._focus = focus
        self._on_announce = on_announce
        self._queued_events_provider = queued_events_provider
        self._drain_queued = drain_queued
        self._last_fired_at: Optional[datetime] = None

    async def run(self, should_stop: Callable[[], bool]) -> None:
        logger.info(
            "standup runner started — next %s",
            next_standup_datetime(self._focus, datetime.now(self._focus.tzinfo())).isoformat(),
        )
        while not should_stop():
            now = datetime.now(self._focus.tzinfo())
            target = next_standup_datetime(self._focus, now)
            wait_s = (target - now).total_seconds()

            # Sleep in TICK_S chunks so shutdown is responsive.
            while wait_s > 0 and not should_stop():
                chunk = min(TICK_S, wait_s)
                await asyncio.sleep(chunk)
                wait_s -= chunk

            if should_stop():
                return

            await self._fire(target)

    async def run_now(self) -> None:
        """Manual trigger (face-detect, widget button, voice). No-op if
        already fired in the last hour to avoid double-rollups."""
        now = datetime.now(self._focus.tzinfo())
        if self._last_fired_at is not None and (now - self._last_fired_at) < timedelta(hours=1):
            logger.info("standup: skipped manual fire (recently ran at %s)", self._last_fired_at)
            return
        await self._fire(now)

    async def _fire(self, scheduled_for: datetime) -> None:
        self._last_fired_at = scheduled_for
        script = self.build_script()
        logger.info("standup: %s", script[:120])
        if self._on_announce is None:
            return
        try:
            await self._on_announce(script)
        except Exception as e:
            logger.warning("standup announce failed: %s", e)

    # ------------------------------------------------------------------
    # Script building
    # ------------------------------------------------------------------
    def build_script(self) -> str:
        """Compose the spoken rollup. Pulls from queued events; phase 4
        will fold in Vercel summaries; phase 6 will add weather/calendar."""
        events = self._collect_queued_events()
        return _format_script(events)

    def _collect_queued_events(self) -> List[Event]:
        if self._drain_queued is not None:
            return self._drain_queued()
        if self._queued_events_provider is not None:
            return list(self._queued_events_provider())
        return []


def format_rollup(events: List[Event]) -> str:
    """Public alias for the script formatter so the antennas-rollup path
    (both-tap) and the morning-standup path can share output style."""
    return _format_script(events)


def _format_script(events: List[Event]) -> str:
    """Render queued events into a short spoken rollup."""
    if not events:
        return "Good morning. All quiet overnight."

    by_kind: dict[str, List[Event]] = {}
    for ev in events:
        by_kind.setdefault(ev.kind, []).append(ev)

    parts: List[str] = ["Good morning. Overnight rollup."]
    if "ci_fail" in by_kind:
        n = len(by_kind["ci_fail"])
        parts.append(f"{n} CI failure{'s' if n != 1 else ''} on watched branches.")
    if "ci_pass_after_fail" in by_kind:
        n = len(by_kind["ci_pass_after_fail"])
        parts.append(f"{n} recover{'ies' if n != 1 else 'y'} from earlier red.")
    if "review_requested" in by_kind:
        n = len(by_kind["review_requested"])
        parts.append(f"{n} pull request{'s' if n != 1 else ''} waiting on your review.")
    if "mention" in by_kind:
        n = len(by_kind["mention"])
        parts.append(f"{n} mention{'s' if n != 1 else ''}.")
    if "issue_assigned" in by_kind:
        n = len(by_kind["issue_assigned"])
        parts.append(f"{n} issue{'s' if n != 1 else ''} assigned to you.")
    if "pr_merged" in by_kind:
        n = len(by_kind["pr_merged"])
        parts.append(f"{n} of your pull request{'s' if n != 1 else ''} merged.")
    return " ".join(parts)
