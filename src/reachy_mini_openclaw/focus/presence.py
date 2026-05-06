"""Presence-aware auto-snooze.

Watches CameraWorker.last_face_detected_time. When the user has been
absent for ABSENCE_THRESHOLD during active hours and the focus mode
is NORMAL or AVAILABLE (i.e. we're not already in deep / user-set
snooze), auto-snoozes for AUTO_SNOOZE_DURATION. When a fresh face
appears, restores the prior mode — but only if Clawson was the one
that snoozed; user-set snoozes are sacred.

Designed to fail gracefully — if there's no camera worker (Reachy Mini
without face tracking), the loop logs and exits cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from ..clawson_config import FocusSettings
from .modes import FocusMode

logger = logging.getLogger(__name__)


# Tunables — phase-6 polish can move these to config.toml later.
TICK_INTERVAL_S = 30.0
ABSENCE_THRESHOLD = timedelta(minutes=15)
AUTO_SNOOZE_DURATION = timedelta(minutes=30)
FACE_FRESHNESS_S = 60.0


class PresenceAutoSnooze:
    """Background task that auto-snoozes during absence and restores on
    return. Fights nothing the user has done deliberately."""

    def __init__(
        self,
        focus_controller: Any,                       # FocusController
        camera_worker: Optional[Any],
        focus_settings: FocusSettings,
        is_within_active_hours: Callable[[FocusSettings, datetime], bool],
        *,
        absence_threshold: timedelta = ABSENCE_THRESHOLD,
        auto_snooze_duration: timedelta = AUTO_SNOOZE_DURATION,
        face_freshness_s: float = FACE_FRESHNESS_S,
        tick_interval_s: float = TICK_INTERVAL_S,
    ) -> None:
        self._focus = focus_controller
        self._camera = camera_worker
        self._focus_settings = focus_settings
        self._active_hours_check = is_within_active_hours
        self._absence_threshold = absence_threshold
        self._auto_snooze_duration = auto_snooze_duration
        self._face_freshness_s = face_freshness_s
        self._tick_interval_s = tick_interval_s
        self._auto_snoozed = False

    async def run(self, should_stop: Callable[[], bool]) -> None:
        if self._camera is None:
            logger.info("presence auto-snooze disabled (no camera)")
            return
        logger.info("presence auto-snooze armed")
        while not should_stop():
            try:
                await self._tick()
            except Exception as e:
                logger.debug("presence tick failed: %s", e)
            await asyncio.sleep(self._tick_interval_s)

    async def _tick(self) -> None:
        from datetime import timezone
        now = datetime.now(timezone.utc)
        if not self._active_hours_check(self._focus_settings, now):
            return
        last_seen = getattr(self._camera, "last_face_detected_time", None)
        face_recent = (
            last_seen is not None
            and (_time.monotonic() - float(last_seen)) <= self._face_freshness_s
        )
        face_absent_long = (
            last_seen is None
            or (_time.monotonic() - float(last_seen)) >= self._absence_threshold.total_seconds()
        )
        mode = self._focus.mode

        # Auto-snooze when away during active hours.
        if face_absent_long and mode in (FocusMode.NORMAL, FocusMode.AVAILABLE):
            self._auto_snoozed = True
            logger.info("presence: auto-snoozing (absent %.0fs)",
                        self._absence_threshold.total_seconds())
            await self._focus.request_snooze(
                self._auto_snooze_duration, label="presence pause"
            )
            return

        # Restore on return — but only if WE snoozed.
        if face_recent and self._auto_snoozed and mode == FocusMode.SNOOZED:
            logger.info("presence: face seen — cancelling auto-snooze")
            self._auto_snoozed = False
            await self._focus.request_unsnooze()
