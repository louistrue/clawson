"""Sleep / wake / re-discover behaviour driven by face presence.

Three observable states:
  AWAKE      — face tracker on, head follows you
  DOZING     — face missing >DOZE_AFTER seconds; tracker still scanning,
               but no rediscovery wiggle yet (just a quiet absence)
  SLEEPING   — face missing >SLEEP_AFTER seconds; tracker DISABLED,
               head goes to a soft droop, breathing keeps the body
               alive but no scanning. Saves CPU and feels deliberate.

Transitions:
  AWAKE   → DOZING   when face hasn't been seen in DOZE_AFTER s
  DOZING  → SLEEPING when face hasn't been seen in SLEEP_AFTER s
  *       → AWAKE    when a face appears (with a friendly antenna wiggle)
                     If transitioning from SLEEPING, also nudge head up.

Rediscovery wiggle (without going to sleep first): if face was missing
>REDISCOVER_GAP seconds and reappears, also fire the wiggle even if we
hadn't reached DOZING yet. Keeps it likeable.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from ..clawson_config import FocusSettings

logger = logging.getLogger(__name__)


TICK_INTERVAL_S = 1.0

# How fresh a face has to be to count as "present".
FACE_FRESHNESS_S = 3.0

# Tier thresholds.
REDISCOVER_GAP_S = 5.0       # >5 s gone → wiggle on return
DOZE_AFTER_S = 60.0          # 1 min no face → quiet doze
SLEEP_AFTER_S = 5 * 60       # 5 min no face → sleep (tracker off)


class CompanionPresence:
    """Background loop that toggles tracking + plays wake animations.

    Doesn't fight user-set state: only manages camera_worker tracking
    and queues short wake/wiggle moves on transitions.
    """

    def __init__(
        self,
        camera_worker: Optional[Any],
        movement_manager: Any,
        focus_settings: FocusSettings,
        *,
        wiggle_factory: Optional[Callable[[str], Any]] = None,
        on_say: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        self._camera = camera_worker
        self._mm = movement_manager
        self._focus = focus_settings
        self._wiggle_factory = wiggle_factory
        self._say = on_say
        self._state: str = "awake"
        self._last_face_seen: float = _time.monotonic()
        # Hysteresis: only wake if the absence actually crossed a threshold.
        self._was_absent_long: bool = False

    @property
    def state(self) -> str:
        return self._state

    async def run(self, should_stop: Callable[[], bool]) -> None:
        if self._camera is None:
            logger.info("companion presence disabled (no camera)")
            return
        logger.info("companion presence armed")
        while not should_stop():
            try:
                await self._tick()
            except Exception as e:
                logger.debug("companion tick failed: %s", e)
            await asyncio.sleep(TICK_INTERVAL_S)

    async def _tick(self) -> None:
        last = getattr(self._camera, "last_face_detected_time", None)
        now = _time.monotonic()
        # Update our own tracking of when we last actually saw a face.
        if last is not None and (now - float(last)) <= FACE_FRESHNESS_S:
            elapsed_since = 0.0
            self._last_face_seen = float(last)
        else:
            elapsed_since = now - self._last_face_seen

        # ---- Transitions ----
        if self._state == "awake":
            if elapsed_since >= SLEEP_AFTER_S:
                await self._enter_sleeping()
            elif elapsed_since >= DOZE_AFTER_S:
                await self._enter_dozing()
            elif elapsed_since >= REDISCOVER_GAP_S:
                # We're awake but face is briefly gone — track absence so
                # a rediscovery wiggle fires when they come back.
                self._was_absent_long = True
            elif elapsed_since == 0.0 and self._was_absent_long:
                self._was_absent_long = False
                await self._wiggle_hello("welcome back")

        elif self._state == "dozing":
            if elapsed_since >= SLEEP_AFTER_S:
                await self._enter_sleeping()
            elif elapsed_since == 0.0:
                await self._enter_awake(from_long_absence=True)

        elif self._state == "sleeping":
            if elapsed_since == 0.0:
                await self._enter_awake(from_long_absence=True)

    # ---- State entries ----

    async def _enter_dozing(self) -> None:
        if self._state == "dozing":
            return
        self._state = "dozing"
        logger.info("companion: dozing (face missing %.0fs)",
                    _time.monotonic() - self._last_face_seen)

    async def _enter_sleeping(self) -> None:
        if self._state == "sleeping":
            return
        self._state = "sleeping"
        logger.info("companion: sleeping — disabling face tracking")
        try:
            if hasattr(self._camera, "set_head_tracking_enabled"):
                self._camera.set_head_tracking_enabled(False)
            else:
                self._camera.is_head_tracking_enabled = False
        except Exception as e:
            logger.debug("companion: tracking disable failed: %s", e)

    async def _enter_awake(self, *, from_long_absence: bool) -> None:
        prev = self._state
        self._state = "awake"
        self._was_absent_long = False
        logger.info("companion: waking (was %s)", prev)
        try:
            if hasattr(self._camera, "set_head_tracking_enabled"):
                self._camera.set_head_tracking_enabled(True)
            else:
                self._camera.is_head_tracking_enabled = True
        except Exception as e:
            logger.debug("companion: tracking enable failed: %s", e)
        if from_long_absence:
            await self._wiggle_hello("hey, there you are")

    async def _wiggle_hello(self, blurb: str) -> None:
        """Two-side antenna wiggle + optional spoken hi."""
        if self._wiggle_factory is not None:
            try:
                # Right then left for a friendly "wave".
                self._mm.queue_move(self._wiggle_factory("right"))
                self._mm.queue_move(self._wiggle_factory("left"))
                self._mm.queue_move(self._wiggle_factory("right"))
            except Exception as e:
                logger.debug("companion: wiggle queue failed: %s", e)
        # Brief verbal — only if user is in active hours.
        from .. import clawson_config as _cfg
        if self._say is not None:
            try:
                from ..briefing.standup import is_within_active_hours
                if is_within_active_hours(self._focus, datetime.now(timezone.utc)):
                    await self._say(blurb)
            except Exception as e:
                logger.debug("companion: say-hello failed: %s", e)
