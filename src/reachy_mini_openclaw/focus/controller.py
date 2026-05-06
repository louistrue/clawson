from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .antennas import AntennaEvent, AntennaPoller, PositionReader
from .modes import FocusMode, FocusState
from .store import DEFAULT_STATE_PATH, load_state, save_state

logger = logging.getLogger(__name__)

# Snooze durations bound to gestures.
SNOOZE_TAP = timedelta(minutes=15)
SNOOZE_DOUBLE = timedelta(hours=1)
SNOOZE_HOLD = timedelta(hours=4)


# Short contextual descriptions spoken when a mode change fires.
# Kept tight so the robot's reply doesn't drag.
_MODE_BLURB = {
    FocusMode.DEEP: "Deep mode. Quiet until something critical.",
    FocusMode.NORMAL: "Normal mode. Quick gestures and brief alerts.",
    FocusMode.AVAILABLE: "Available mode. I'll narrate events.",
    FocusMode.SNOOZED: "Snoozed.",
}


def _snooze_blurb(label: str) -> str:
    return f"Snoozing {label}. Catch up later."

# How often to check for snooze expiry when no antenna events fire.
EXPIRY_CHECK_INTERVAL_S = 15.0


def _humanise_duration(d: timedelta) -> str:
    minutes = int(d.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    rest = minutes % 60
    if rest == 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours} hour{'s' if hours != 1 else ''} {rest} minutes"


class FocusController:
    """Wires antenna events to FocusState transitions and persists changes.

    Optional callbacks let other subsystems react without coupling:
        on_change(new_mode, previous_mode)  – fires after every transition
        on_announce(message)                – fires for snooze/cycle vocal cues
        on_rollup_request()                 – fires on a "both" antenna tap

    None of these are awaited inside the antenna hot loop's critical path —
    they run as background tasks so a slow callback can't stall input.
    """

    def __init__(
        self,
        position_reader: Optional[PositionReader] = None,
        *,
        state_path: Path = DEFAULT_STATE_PATH,
        on_change: Optional[Callable[[FocusMode, FocusMode], Awaitable[None]]] = None,
        on_announce: Optional[Callable[[str], Awaitable[None]]] = None,
        on_rollup_request: Optional[Callable[[], Awaitable[None]]] = None,
        on_standup_request: Optional[Callable[[], Awaitable[None]]] = None,
        confirmation: Optional[Any] = None,            # actions.ConfirmationSystem
    ) -> None:
        self._state_path = state_path
        self._state: FocusState = load_state(state_path)
        self._poller: Optional[AntennaPoller] = None
        self._on_change = on_change
        self._on_announce = on_announce
        self._on_rollup_request = on_rollup_request
        self._on_standup_request = on_standup_request
        self._confirmation = confirmation

        if position_reader is not None:
            self._poller = AntennaPoller(
                read_positions=position_reader,
                on_event=self._handle_event,
            )

        # Restore any expired snooze immediately on startup.
        if self._state.maybe_expire():
            save_state(self._state, self._state_path)

    @property
    def state(self) -> FocusState:
        return self._state

    @property
    def mode(self) -> FocusMode:
        return self._state.mode

    async def run(self, should_stop: Callable[[], bool]) -> None:
        """Run the antenna poller and the snooze-expiry watchdog concurrently."""
        tasks = [asyncio.create_task(self._expiry_watchdog(should_stop), name="focus-expiry")]
        if self._poller is not None:
            tasks.append(asyncio.create_task(self._poller.run_until(should_stop), name="antenna-poller"))
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                if not t.done():
                    t.cancel()
            raise

    async def _expiry_watchdog(self, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            previous = self._state.mode
            if self._state.maybe_expire():
                logger.info("snooze expired → mode=%s", self._state.mode.value)
                save_state(self._state, self._state_path)
                await self._fire_change(previous)
                await self._announce(f"snooze ended; back to {self._state.mode.value}")
            await asyncio.sleep(EXPIRY_CHECK_INTERVAL_S)

    async def _handle_event(self, ev: AntennaEvent) -> None:
        """Antennas are now confirmation-only: right tap = YES, left tap = NO.
        Mode switches, snooze, standup, rollup all go through voice or the
        widget. Holds / doubles / both are no-ops here so the antennas
        can't accidentally toggle state.
        """
        logger.debug("antenna event: side=%s kind=%s", ev.side, ev.kind)
        if ev.kind != "tap":
            return
        if self._confirmation is None or not self._confirmation.has_pending:
            return  # no pending question → ignore the tap
        if ev.side == "right":
            self._confirmation.confirm()
        elif ev.side == "left":
            self._confirmation.deny()

    # ------------------------------------------------------------------
    # Public action API — used by antenna handler AND the widget.
    # ------------------------------------------------------------------
    async def request_cycle(self) -> FocusMode:
        previous = self._state.mode
        new_mode = self._state.cycle()
        await self._announce(_MODE_BLURB.get(new_mode, new_mode.value))
        save_state(self._state, self._state_path)
        await self._fire_change(previous)
        return new_mode

    async def request_snooze(self, duration: timedelta, *, label: Optional[str] = None) -> None:
        previous = self._state.mode
        until = self._state.snooze(duration)
        spoken = label or _humanise_duration(duration)
        await self._announce(_snooze_blurb(spoken))
        logger.info("snoozed until %s", until.isoformat())
        save_state(self._state, self._state_path)
        await self._fire_change(previous)

    async def request_unsnooze(self) -> None:
        if self._state.mode != FocusMode.SNOOZED:
            return
        previous = self._state.mode
        # Cycle path triggers the restore + advance; instead, just restore.
        self._state._restore()  # type: ignore[attr-defined]
        restored_blurb = _MODE_BLURB.get(self._state.mode, self._state.mode.value)
        await self._announce(f"Back online. {restored_blurb}")
        save_state(self._state, self._state_path)
        await self._fire_change(previous)

    async def request_set_mode(self, target: FocusMode) -> None:
        if target == FocusMode.SNOOZED:
            # Use request_snooze for snoozing; refuse to set SNOOZED directly.
            return
        previous = self._state.mode
        self._state.mode = target
        self._state.snooze_until = None
        self._state.previous_mode = None
        await self._announce(_MODE_BLURB.get(target, target.value))
        save_state(self._state, self._state_path)
        await self._fire_change(previous)

    async def _fire_change(self, previous: FocusMode) -> None:
        if self._on_change is None or previous == self._state.mode:
            return
        try:
            await self._on_change(self._state.mode, previous)
        except Exception as e:
            logger.exception("on_change callback failed: %s", e)

    async def _announce(self, message: str) -> None:
        if self._on_announce is None:
            return
        try:
            await self._on_announce(message)
        except Exception as e:
            logger.exception("on_announce callback failed: %s", e)

    async def _fire_rollup(self) -> None:
        if self._on_rollup_request is None:
            await self._announce("rollup not yet wired")
            return
        try:
            await self._on_rollup_request()
        except Exception as e:
            logger.exception("on_rollup_request failed: %s", e)

    async def _fire_standup(self) -> None:
        if self._on_standup_request is None:
            await self._announce("standup not yet wired")
            return
        try:
            await self._on_standup_request()
        except Exception as e:
            logger.exception("on_standup_request failed: %s", e)
