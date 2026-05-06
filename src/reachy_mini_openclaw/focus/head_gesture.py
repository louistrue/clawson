"""Head-gesture detector — nod (yes) and shake (no).

When the user manually nods or shakes the Reachy Mini head, the robot's
IMU registers angular velocity oscillations:
  * nod  → pitch axis (gyroscope Y)
  * shake → yaw axis  (gyroscope Z)

This module polls IMU + head joints at 20Hz, logs the raw signal under
CLAWSON_DEBUG_HEAD_GESTURE=1 (so we can tune thresholds against real
data), and emits 'nod' / 'shake' events when the windowed signal crosses
the configured pattern.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time as _time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Deque, Optional, Tuple

logger = logging.getLogger(__name__)

# Polling cadence.
POLL_INTERVAL_S = 0.05            # 20 Hz
WINDOW_S = 1.2                    # rolling window for oscillation detection

# Detection thresholds (rad/s on the relevant gyro axis).
# Manual nodding/shaking peaks well above the breathing/idle noise floor;
# these will be tuned on real data, see CLAWSON_DEBUG_HEAD_GESTURE=1.
PEAK_THRESHOLD_RAD_S = 1.5
ZERO_CROSSINGS_REQUIRED = 3       # → at least 1.5 oscillation cycles
MIN_AXIS_DOMINANCE = 1.6          # primary axis must beat the other by this ×

# Cooldown between consecutive emissions so a single gesture doesn't fire
# multiple events.
GESTURE_COOLDOWN_S = 0.8


@dataclass
class HeadGestureEvent:
    kind: str    # "nod" | "shake"


# Sample = (t, gyro_y, gyro_z, head_pitch, head_yaw)
_Sample = Tuple[float, float, float, float, float]


def _zero_crossings(values: list[float]) -> int:
    n = 0
    for i in range(1, len(values)):
        if values[i - 1] == 0 or values[i] == 0:
            continue
        if (values[i - 1] > 0) != (values[i] > 0):
            n += 1
    return n


def _peak_abs(values: list[float]) -> float:
    return max((abs(v) for v in values), default=0.0)


def detect_gesture(samples: list[_Sample]) -> Optional[HeadGestureEvent]:
    """Run nod/shake heuristics over a window of samples."""
    if len(samples) < 6:
        return None
    gyro_y = [s[1] for s in samples]   # pitch rate
    gyro_z = [s[2] for s in samples]   # yaw rate

    peak_y = _peak_abs(gyro_y)
    peak_z = _peak_abs(gyro_z)
    cross_y = _zero_crossings(gyro_y)
    cross_z = _zero_crossings(gyro_z)

    nod_strong = (
        peak_y >= PEAK_THRESHOLD_RAD_S
        and cross_y >= ZERO_CROSSINGS_REQUIRED
        and peak_y >= MIN_AXIS_DOMINANCE * peak_z
    )
    shake_strong = (
        peak_z >= PEAK_THRESHOLD_RAD_S
        and cross_z >= ZERO_CROSSINGS_REQUIRED
        and peak_z >= MIN_AXIS_DOMINANCE * peak_y
    )
    if nod_strong and not shake_strong:
        return HeadGestureEvent(kind="nod")
    if shake_strong and not nod_strong:
        return HeadGestureEvent(kind="shake")
    return None


def _diag_enabled() -> bool:
    return os.environ.get("CLAWSON_DEBUG_HEAD_GESTURE", "").lower() in {"1", "true", "yes"}


class HeadGestureDetector:
    """Polls IMU + head joints; emits nod/shake events to a callback.

    `imu_reader` returns a dict like {'gyroscope': [x, y, z], ...}.
    `head_joints_reader` returns a sequence whose first element is pitch
    and whose third element is yaw (Reachy Mini convention) — we use them
    only for diagnostic logging; the gyro signal is the source of truth.
    """

    def __init__(
        self,
        imu_reader: Callable[[], Optional[dict]],
        head_joints_reader: Callable[[], Optional[Any]],
        on_event: Callable[[HeadGestureEvent], Awaitable[None]],
        *,
        poll_interval_s: float = POLL_INTERVAL_S,
        window_s: float = WINDOW_S,
        cooldown_s: float = GESTURE_COOLDOWN_S,
    ) -> None:
        self._imu_reader = imu_reader
        self._head_reader = head_joints_reader
        self._on_event = on_event
        self._poll_interval_s = poll_interval_s
        self._window_s = window_s
        self._cooldown_s = cooldown_s
        self._samples: Deque[_Sample] = deque()
        self._last_event_at: float = -1e9
        self._diag = _diag_enabled()

    async def run_until(self, should_stop: Callable[[], bool]) -> None:
        logger.info(
            "head-gesture detector armed (interval=%.2fs window=%.1fs diag=%s)",
            self._poll_interval_s, self._window_s, self._diag,
        )
        next_diag = 0.0
        while not should_stop():
            now = _time.monotonic()
            sample = self._read_sample(now)
            if sample is not None:
                self._samples.append(sample)
                cutoff = now - self._window_s
                while self._samples and self._samples[0][0] < cutoff:
                    self._samples.popleft()
                if self._diag and now >= next_diag:
                    _, gy, gz, hp, hy = sample
                    logger.info(
                        "head_gyro y=%+.3f z=%+.3f head_pitch=%+.3f yaw=%+.3f",
                        gy, gz, hp, hy,
                    )
                    next_diag = now + 0.2  # 5 Hz diag log

                if (now - self._last_event_at) >= self._cooldown_s:
                    event = detect_gesture(list(self._samples))
                    if event is not None:
                        self._last_event_at = now
                        self._samples.clear()
                        try:
                            await self._on_event(event)
                        except Exception as e:
                            logger.warning("head gesture callback failed: %s", e)
            await asyncio.sleep(self._poll_interval_s)

    def _read_sample(self, now: float) -> Optional[_Sample]:
        try:
            imu = self._imu_reader()
        except Exception as e:
            logger.debug("imu read failed: %s", e)
            return None
        if imu is None:
            return None
        gyro = imu.get("gyroscope") if isinstance(imu, dict) else None
        if not gyro or len(gyro) < 3:
            return None
        try:
            head_joints = self._head_reader()
        except Exception:
            head_joints = None
        # Reachy Mini head: best-effort extraction of pitch / yaw for diag.
        head_pitch = float(head_joints[0]) if head_joints and len(head_joints) > 0 else 0.0
        head_yaw = float(head_joints[2]) if head_joints and len(head_joints) > 2 else 0.0
        return (now, float(gyro[1]), float(gyro[2]), head_pitch, head_yaw)


def make_imu_reader(robot: Any) -> Callable[[], Optional[dict]]:
    """Adapter: closure that reads the robot's IMU dict each tick."""

    def _read() -> Optional[dict]:
        try:
            return robot.imu
        except Exception:
            return None

    return _read


def make_head_joints_reader(robot: Any) -> Callable[[], Optional[Any]]:
    """Adapter: closure that returns just the head_joints list."""

    def _read() -> Optional[Any]:
        try:
            head, _ = robot.get_current_joint_positions()
        except Exception:
            return None
        return head

    return _read
