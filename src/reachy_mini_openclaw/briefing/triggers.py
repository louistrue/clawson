"""Manual standup triggers — voice command + face detection.

The cron trigger lives in StandupRunner.run(). The widget trigger goes
through the /api/standup endpoint. Antenna right-hold goes through
FocusController. This module wires the two remaining triggers from
plan.md §morning-standup so all four are live.

* `make_voice_trigger`  — listens for keywords on user transcripts.
* `FaceDetectStandupTrigger` — async loop that watches CameraWorker's
  `last_face_detected_time` and calls run_now() when a face appears
  inside the morning window.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime, time, timedelta
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# Keywords that fire the voice-command trigger. Case-insensitive substring
# match — keep the surface narrow so casual conversation doesn't fire it.
_VOICE_KEYWORDS = ("standup", "stand up", "rollup", "what's queued", "whats queued")


def make_voice_trigger(
    standup_runner: Any,
) -> Callable[[str], Awaitable[None]]:
    """Return an async callback suitable for OpenAIRealtimeHandler.on_user_transcript."""

    async def _trigger(transcript: str) -> None:
        lower = transcript.lower()
        if not any(kw in lower for kw in _VOICE_KEYWORDS):
            return
        logger.info("standup: voice trigger matched in %r", transcript[:60])
        try:
            await standup_runner.run_now()
        except Exception as e:
            logger.warning("standup voice trigger failed: %s", e)

    return _trigger


class FaceDetectStandupTrigger:
    """Polls CameraWorker.last_face_detected_time and fires standup once
    per morning when a face appears inside the morning trigger window.

    Default window: 06:00 → standup_time + 30 minutes (in the configured
    timezone). After firing, the trigger is armed-disabled until the next
    day. Cron firings also disable today's window so we don't double up.
    """

    def __init__(
        self,
        camera_worker: Optional[Any],
        standup_runner: Any,
        focus_settings: Any,                       # FocusSettings, duck-typed
        *,
        window_start: time = time(6, 0),
        post_window_grace: timedelta = timedelta(minutes=30),
        face_freshness_s: float = 5.0,
    ) -> None:
        self._camera = camera_worker
        self._standup = standup_runner
        self._focus = focus_settings
        self._window_start = window_start
        self._post_window_grace = post_window_grace
        self._face_freshness_s = face_freshness_s
        self._fired_for_date: Optional[str] = None

    async def run(self, should_stop: Callable[[], bool]) -> None:
        if self._camera is None:
            logger.info("face-detect trigger disabled (no camera worker)")
            return
        logger.info("face-detect standup trigger armed")
        while not should_stop():
            try:
                await self._tick()
            except Exception as e:
                logger.debug("face trigger tick failed: %s", e)
            await asyncio.sleep(15.0)

    async def _tick(self) -> None:
        tz = self._focus.tzinfo()
        now_local = datetime.now(tz)
        today_iso = now_local.date().isoformat()
        if self._fired_for_date == today_iso:
            return
        # Inside [window_start, standup_time + grace] window?
        window_end = (
            datetime.combine(now_local.date(), self._focus.standup_time, tzinfo=tz)
            + self._post_window_grace
        )
        window_open = (
            now_local.time() >= self._window_start
            and now_local <= window_end
        )
        if not window_open:
            return
        # Only fire on standup days.
        if now_local.weekday() not in self._focus.standup_days:
            return

        last = getattr(self._camera, "last_face_detected_time", None)
        if last is None:
            return
        # CameraWorker's timestamps are time.monotonic(); compare in that frame.
        if (_time.monotonic() - float(last)) > self._face_freshness_s:
            return

        self._fired_for_date = today_iso
        logger.info("face-detect: triggering morning standup")
        try:
            await self._standup.run_now()
        except Exception as e:
            logger.warning("face-detect standup fire failed: %s", e)
