"""Coordinator for the sleep / wake animation.

Both the FocusController (when mode → SNOOZED) and the CompanionPresence
(when face has been absent for SLEEP_AFTER_S) want to put the robot in
the same sleepy pose. This module owns the actual queueing of GoToSleep
+ SleepHold + WakeUp moves and toggles face tracking accordingly, so
the two triggers can't fight each other.

Idempotent — calling enter_sleep when already asleep, or exit_sleep when
already awake, is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


# How often to re-queue SleepHoldMove so the robot stays in the sleep
# pose across days. SleepHoldMove.duration default is 5 min; we refresh
# at 4 min so there's always one in flight.
HOLD_REFRESH_S = 4 * 60


class SleepAnimator:
    """Single source of truth for is-the-robot-sleeping. Use enter_sleep /
    exit_sleep as a toggle from any subsystem; internal flag prevents
    duplicate animations and lets multiple callers share state."""

    def __init__(
        self,
        movement_manager: Any,
        camera_worker: Optional[Any] = None,
    ) -> None:
        self._mm = movement_manager
        self._camera = camera_worker
        self._sleeping: bool = False
        self._refresh_task: Optional[asyncio.Task] = None

    @property
    def is_sleeping(self) -> bool:
        return self._sleeping

    async def enter_sleep(self, *, reason: str = "") -> None:
        if self._sleeping:
            return
        self._sleeping = True
        logger.info("sleep animator: entering sleep (%s)", reason or "unknown")
        # Lazy import — depends on reachy_mini SDK that's only on robot.
        from ..gestures.sleep import GoToSleepMove, SleepHoldMove

        # Pause face tracking while asleep so the head doesn't drift.
        if self._camera is not None:
            try:
                if hasattr(self._camera, "set_head_tracking_enabled"):
                    self._camera.set_head_tracking_enabled(False)
                else:
                    self._camera.is_head_tracking_enabled = False
            except Exception as e:
                logger.debug("sleep animator: tracking disable failed: %s", e)

        try:
            pose = self._mm.state.last_primary_pose
            head, ant, _yaw = pose if pose is not None else (None, (0.0, 0.0), 0.0)
            if head is not None:
                self._mm.queue_move(GoToSleepMove(head, ant))
            self._mm.queue_move(SleepHoldMove())
        except Exception as e:
            logger.warning("sleep animator: queue sleep failed: %s", e)

        # Periodically re-queue SleepHoldMove so we never run out of
        # primary moves and snap back to breathing.
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(
                self._refresh_loop(), name="sleep-hold-refresh"
            )

    async def exit_sleep(self, *, reason: str = "") -> None:
        if not self._sleeping:
            return
        self._sleeping = False
        logger.info("sleep animator: waking (%s)", reason or "unknown")
        # Lazy import.
        from ..gestures.sleep import WakeUpMove

        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
            self._refresh_task = None

        try:
            self._mm.clear_move_queue()
            # Use the actual current pose as the start so the wake move
            # doesn't snap from sleep_pose if a snore was mid-wiggle.
            pose = self._mm.state.last_primary_pose
            if pose is not None:
                head, ant, _yaw = pose
                logger.info("sleep animator: queue WakeUpMove (start ant=%s)", ant)
                self._mm.queue_move(WakeUpMove(start_pose=head, start_antennas=ant))
            else:
                logger.info("sleep animator: queue WakeUpMove (default start)")
                self._mm.queue_move(WakeUpMove())
        except Exception as e:
            logger.warning("sleep animator: queue wake failed: %s", e)

        if self._camera is not None:
            try:
                if hasattr(self._camera, "set_head_tracking_enabled"):
                    self._camera.set_head_tracking_enabled(True)
                else:
                    self._camera.is_head_tracking_enabled = True
            except Exception as e:
                logger.debug("sleep animator: tracking enable failed: %s", e)

    async def _refresh_loop(self) -> None:
        """Keep re-queueing SleepHoldMove every HOLD_REFRESH_S so the
        primary-move queue never empties out and the breathing animation
        doesn't quietly resume during sleep."""
        from ..gestures.sleep import SleepHoldMove
        try:
            while self._sleeping:
                await asyncio.sleep(HOLD_REFRESH_S)
                if not self._sleeping:
                    return
                try:
                    self._mm.queue_move(SleepHoldMove())
                except Exception as e:
                    logger.debug("sleep animator: refresh queue failed: %s", e)
        except asyncio.CancelledError:
            pass
