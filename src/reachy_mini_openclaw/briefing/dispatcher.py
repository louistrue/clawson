from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Deque, Optional

from ..focus.modes import FocusMode
from ..gestures import gesture_for_event
from .events import Event, EventBus, EventSeverity

logger = logging.getLogger(__name__)


# Cross-event dedup window (different filter from the source-specific one in
# filters.py). Catches the same fingerprint arriving twice from a poller burst.
DISPATCH_DEDUP_WINDOW = timedelta(minutes=5)
DEDUP_HISTORY_CAP = 256


class EventDispatcher:
    """Single consumer of the EventBus.

    Responsibilities:
      - dedup repeats by fingerprint within DISPATCH_DEDUP_WINDOW
      - gate by focus mode (deep/snoozed silence non-critical events)
      - dispatch a gesture via MovementManager (Phase 2)
      - hand off to TTS preview (Phase 3, hook ready)
      - drop unhandled into a queue accessible via .queued_events for the
        rollup ("what's queued?") that Phase 5's widget will read.
    """

    def __init__(
        self,
        bus: EventBus,
        focus_mode_provider: Callable[[], FocusMode],
        movement_manager: Any,                       # MovementManager
        *,
        on_announce: Optional[Callable[[Event], Awaitable[None]]] = None,
        active_hours_provider: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._bus = bus
        self._focus_mode_provider = focus_mode_provider
        self._movement_manager = movement_manager
        self._on_announce = on_announce
        self._active_hours_provider = active_hours_provider
        self._recent: Deque[tuple[str, datetime]] = deque(maxlen=DEDUP_HISTORY_CAP)
        self._queued: Deque[Event] = deque(maxlen=DEDUP_HISTORY_CAP)

    @property
    def queued_events(self) -> list[Event]:
        return list(self._queued)

    def drain_queued(self) -> list[Event]:
        out = list(self._queued)
        self._queued.clear()
        return out

    async def run(self, should_stop: Callable[[], bool]) -> None:
        logger.info("event dispatcher started")
        while not should_stop():
            try:
                event = await asyncio.wait_for(self._bus.consume(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._handle(event)
            except Exception as e:
                logger.exception("dispatcher handler failed: %s", e)
            finally:
                self._bus.task_done()

    async def _handle(self, event: Event) -> None:
        if self._is_recent_duplicate(event):
            logger.debug("dispatcher: dedup'd %s", event.fingerprint)
            return
        self._recent.append((event.fingerprint, datetime.now(timezone.utc)))

        mode = self._focus_mode_provider()
        action = self._decide(event, mode)
        logger.info(
            "dispatcher: %s/%s sev=%s mode=%s → %s",
            event.source, event.kind, event.severity.value, mode.value, action,
        )

        if action == "drop":
            return
        if action == "queue":
            self._queued.append(event)
            return
        if action == "gesture_only":
            await self._fire_gesture(event)
            return
        if action == "gesture_and_announce":
            await self._fire_gesture(event)
            if self._on_announce is not None:
                try:
                    await self._on_announce(event)
                except Exception as e:
                    logger.warning("dispatcher: announce failed: %s", e)
            return

    def _decide(self, event: Event, mode: FocusMode) -> str:
        critical = event.severity == EventSeverity.CRITICAL
        # Active-hours gate: outside the configured window we silence
        # non-critical events regardless of focus mode (the morning
        # standup at 07:30 is a separate path that doesn't go through
        # the dispatcher, so it's not affected).
        if self._active_hours_provider is not None and not critical:
            try:
                if not self._active_hours_provider():
                    return "queue"
            except Exception as e:
                logger.debug("active_hours_provider failed: %s", e)
        if mode in (FocusMode.DEEP, FocusMode.SNOOZED):
            return "gesture_only" if critical else "queue"
        if mode == FocusMode.NORMAL:
            return "gesture_only"
        if mode == FocusMode.AVAILABLE:
            return "gesture_and_announce"
        return "queue"

    def _is_recent_duplicate(self, event: Event) -> bool:
        cutoff = datetime.now(timezone.utc) - DISPATCH_DEDUP_WINDOW
        # Trim out-of-window entries so the deque doesn't lie.
        while self._recent and self._recent[0][1] < cutoff:
            self._recent.popleft()
        return any(fp == event.fingerprint for fp, _ in self._recent)

    async def _fire_gesture(self, event: Event) -> None:
        try:
            current = self._movement_manager.state.last_primary_pose
            if current is None:
                logger.debug("dispatcher: no current pose, skipping gesture for %s", event.fingerprint)
                return
            head, antennas, _yaw = current
            move = gesture_for_event(event.kind, head, antennas)
            if move is None:
                return
            self._movement_manager.queue_move(move)
        except Exception as e:
            logger.warning("dispatcher: queue_move failed for %s: %s", event.fingerprint, e)
