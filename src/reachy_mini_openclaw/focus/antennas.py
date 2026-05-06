from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Tuple

logger = logging.getLogger(__name__)

# Press detection — radians of antenna deflection.
PRESS_THRESHOLD_RAD = 0.30      # ~17°
RELEASE_THRESHOLD_RAD = 0.18    # hysteresis

# Timing windows (seconds).
POLL_INTERVAL_S = 0.05          # 20 Hz
MIN_PRESS_S = 0.05              # below this is mechanical noise
HOLD_S = 1.0                    # press >= this duration ⇒ "hold"
BOTH_WINDOW_S = 0.20            # both antennas must overlap within this window
DOUBLE_GAP_S = 0.40             # gap between two taps to count as a double-tap

# Antenna index convention. Reachy SDK returns (idx0, idx1); flip if the
# physical mapping is inverted on this hardware.
LEFT_INDEX = 0
RIGHT_INDEX = 1


@dataclass
class AntennaEvent:
    side: str   # "left" | "right" | "both"
    kind: str   # "tap" | "double" | "hold"


@dataclass
class _AntennaTracker:
    """Single-antenna press/release state machine."""

    side: str
    pressed: bool = False
    press_started_at: float = 0.0
    hold_emitted: bool = False
    consumed: bool = False  # this press was absorbed by a "both" event

    def feed(self, magnitude: float, now: float) -> Tuple[Optional[str], Optional[AntennaEvent]]:
        """Update with current |angle|; return (transition, event).

        transition ∈ {"down", "up", None}; event is emitted on release-as-tap
        or on hold-threshold crossing while still pressed.
        """
        if not self.pressed and magnitude >= PRESS_THRESHOLD_RAD:
            self.pressed = True
            self.press_started_at = now
            self.hold_emitted = False
            self.consumed = False
            return ("down", None)

        if self.pressed and magnitude < RELEASE_THRESHOLD_RAD:
            duration = now - self.press_started_at
            self.pressed = False
            if duration < MIN_PRESS_S or self.consumed or self.hold_emitted:
                return ("up", None)
            return ("up", AntennaEvent(side=self.side, kind="tap"))

        if self.pressed and not self.hold_emitted and not self.consumed:
            if (now - self.press_started_at) >= HOLD_S:
                self.hold_emitted = True
                return (None, AntennaEvent(side=self.side, kind="hold"))

        return (None, None)


PositionReader = Callable[[], "Optional[Tuple[float, float]]"]
EventCallback = Callable[[AntennaEvent], "Awaitable[None]"]


@dataclass
class AntennaPoller:
    """Polls two antenna positions and dispatches debounced events.

    `read_positions` returns (left_rad, right_rad) per the LEFT_INDEX /
    RIGHT_INDEX constants in this module, or None if read fails.
    """

    read_positions: PositionReader
    on_event: EventCallback
    poll_interval_s: float = POLL_INTERVAL_S
    _left: _AntennaTracker = field(default_factory=lambda: _AntennaTracker("left"))
    _right: _AntennaTracker = field(default_factory=lambda: _AntennaTracker("right"))
    # Pending tap timestamps per side — committed as "tap" after DOUBLE_GAP_S
    # without a follow-up, or upgraded to "double" if a second tap arrives in
    # time on the same side.
    _pending_tap: dict = field(default_factory=lambda: {"left": None, "right": None})

    async def run_until(self, should_stop: Callable[[], bool]) -> None:
        logger.info("antenna poller started (interval=%.2fs)", self.poll_interval_s)
        _next_diag = 0.0
        while not should_stop():
            try:
                pos = self.read_positions()
            except Exception as e:
                logger.debug("antenna read failed: %s", e)
                pos = None

            now = time.monotonic()
            if pos is not None and now >= _next_diag:
                logger.debug("antenna_raw left=%.4f right=%.4f", pos[0], pos[1])
                _next_diag = now + 1.0
            if pos is not None:
                left_pos, right_pos = pos
                ltrans, lev = self._left.feed(abs(left_pos), now)
                rtrans, rev = self._right.feed(abs(right_pos), now)

                both = self._detect_both(ltrans, rtrans, now)
                if both is not None:
                    # Both-tap consumes any pending single taps so we don't
                    # fire a stale tap right after the both-event.
                    self._pending_tap["left"] = None
                    self._pending_tap["right"] = None
                    lev = rev = None
                    await self.on_event(both)

                # Holds bypass the double-tap buffer; taps go through it.
                for ev in (lev, rev):
                    if ev is None:
                        continue
                    if ev.kind == "hold":
                        self._pending_tap[ev.side] = None
                        await self.on_event(ev)
                    elif ev.kind == "tap":
                        last = self._pending_tap[ev.side]
                        if last is not None and (now - last) <= DOUBLE_GAP_S:
                            self._pending_tap[ev.side] = None
                            await self.on_event(AntennaEvent(side=ev.side, kind="double"))
                        else:
                            self._pending_tap[ev.side] = now

            # Flush any tap that's been pending past the double-tap window.
            for side in ("left", "right"):
                last = self._pending_tap[side]
                if last is not None and (now - last) > DOUBLE_GAP_S:
                    self._pending_tap[side] = None
                    await self.on_event(AntennaEvent(side=side, kind="tap"))

            await asyncio.sleep(self.poll_interval_s)

    def _detect_both(
        self, ltrans: Optional[str], rtrans: Optional[str], now: float
    ) -> Optional[AntennaEvent]:
        # Same-tick concurrent down.
        if ltrans == "down" and rtrans == "down":
            self._left.consumed = True
            self._right.consumed = True
            return AntennaEvent(side="both", kind="tap")

        # Late-comer joins an in-progress press within the window.
        if ltrans == "down" and self._right.pressed and not self._right.consumed:
            if (now - self._right.press_started_at) <= BOTH_WINDOW_S:
                self._left.consumed = True
                self._right.consumed = True
                return AntennaEvent(side="both", kind="tap")
        if rtrans == "down" and self._left.pressed and not self._left.consumed:
            if (now - self._left.press_started_at) <= BOTH_WINDOW_S:
                self._left.consumed = True
                self._right.consumed = True
                return AntennaEvent(side="both", kind="tap")

        return None


def make_robot_antenna_reader(robot) -> PositionReader:
    """Adapter: return a closure that reads antenna positions from the SDK.

    Reachy Mini's `get_current_joint_positions()` returns (head_joints, antennas)
    where antennas is a length-2 sequence indexed by LEFT_INDEX / RIGHT_INDEX.
    Returns None on failure so the poller can swallow it.
    """

    def _read() -> Optional[Tuple[float, float]]:
        try:
            _, antennas = robot.get_current_joint_positions()
        except Exception:
            return None
        if antennas is None or len(antennas) < 2:
            return None
        return (float(antennas[LEFT_INDEX]), float(antennas[RIGHT_INDEX]))

    return _read
