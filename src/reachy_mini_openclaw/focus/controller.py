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

# Snooze durations bound to gestures. Tap = 15m, hold = 4h. Double-tap (1h)
# is intentionally deferred to phase 1.5 along with widget control.
SNOOZE_TAP = timedelta(minutes=15)
SNOOZE_HOLD = timedelta(hours=4)

# How often to check for snooze expiry when no antenna events fire.
EXPIRY_CHECK_INTERVAL_S = 15.0


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
    ) -> None:
        self._state_path = state_path
        self._state: FocusState = load_state(state_path)
        self._poller: Optional[AntennaPoller] = None
        self._on_change = on_change
        self._on_announce = on_announce
        self._on_rollup_request = on_rollup_request

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
        previous = self._state.mode

        if ev.side == "right" and ev.kind == "tap":
            new_mode = self._state.cycle()
            await self._announce(f"focus mode {new_mode.value}")

        elif ev.side == "left" and ev.kind == "tap":
            until = self._state.snooze(SNOOZE_TAP)
            await self._announce("snoozing fifteen minutes")
            logger.info("snoozed until %s", until.isoformat())

        elif ev.side == "left" and ev.kind == "hold":
            until = self._state.snooze(SNOOZE_HOLD)
            await self._announce("snoozing four hours")
            logger.info("snoozed until %s", until.isoformat())

        elif ev.side == "both" and ev.kind == "tap":
            await self._fire_rollup()
            return  # rollup doesn't change mode

        elif ev.side == "right" and ev.kind == "hold":
            # Right-hold reserved (e.g. "what's queued?" or trigger standup).
            # No-op for v1; surface via TTS so user knows it's a deliberate gap.
            await self._announce("hold not yet bound")
            return

        else:
            return

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
