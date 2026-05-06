from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .antennas import AntennaEvent, AntennaPoller, PositionReader
from .modes import FocusMode, FocusState
from .store import DEFAULT_STATE_PATH, load_state, save_state

logger = logging.getLogger(__name__)

# Snooze durations bound to gestures.
SNOOZE_TAP = timedelta(minutes=15)
SNOOZE_DOUBLE = timedelta(hours=1)
SNOOZE_HOLD = timedelta(hours=4)

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
    ) -> None:
        self._state_path = state_path
        self._state: FocusState = load_state(state_path)
        self._poller: Optional[AntennaPoller] = None
        self._on_change = on_change
        self._on_announce = on_announce
        self._on_rollup_request = on_rollup_request
        self._on_standup_request = on_standup_request

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
        logger.info("antenna event: side=%s kind=%s", ev.side, ev.kind)

        if ev.side == "right" and ev.kind == "tap":
            await self.request_cycle()
        elif ev.side == "left" and ev.kind == "tap":
            await self.request_snooze(SNOOZE_TAP, label="fifteen minutes")
        elif ev.side == "left" and ev.kind == "double":
            await self.request_snooze(SNOOZE_DOUBLE, label="one hour")
        elif ev.side == "left" and ev.kind == "hold":
            await self.request_snooze(SNOOZE_HOLD, label="four hours")
        elif ev.side == "both" and ev.kind == "tap":
            await self._fire_rollup()
        elif ev.side == "right" and ev.kind == "hold":
            # Right-hold = "trigger standup now" (4th plan trigger).
            await self._fire_standup()
        elif ev.side == "right" and ev.kind == "double":
            # Reserved for future bindings; no-op for v1.
            await self._announce("right double not yet bound")

    # ------------------------------------------------------------------
    # Public action API — used by antenna handler AND the widget.
    # ------------------------------------------------------------------
    async def request_cycle(self) -> FocusMode:
        previous = self._state.mode
        new_mode = self._state.cycle()
        await self._announce(f"focus mode {new_mode.value}")
        save_state(self._state, self._state_path)
        await self._fire_change(previous)
        return new_mode

    async def request_snooze(self, duration: timedelta, *, label: Optional[str] = None) -> None:
        previous = self._state.mode
        until = self._state.snooze(duration)
        spoken = label or _humanise_duration(duration)
        await self._announce(f"snoozing {spoken}")
        logger.info("snoozed until %s", until.isoformat())
        save_state(self._state, self._state_path)
        await self._fire_change(previous)

    async def request_unsnooze(self) -> None:
        if self._state.mode != FocusMode.SNOOZED:
            return
        previous = self._state.mode
        # Cycle path triggers the restore + advance; instead, just restore.
        self._state._restore()  # type: ignore[attr-defined]
        await self._announce(f"snooze cancelled; back to {self._state.mode.value}")
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
        await self._announce(f"focus mode {target.value}")
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
