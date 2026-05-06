"""Sleep / wake / snore Move classes for Clawson.

Modeled after the SDK's `goto_sleep` pose but gentler — head only nods
~15° down (vs SDK's ~24°), antennas at the SDK's full-back position so
it reads like a sleepy robot. Plus a long-running `SleepHoldMove` that
holds the pose AND fires periodic antenna twitches that read as
'snoring'. WakeUpMove eases back to neutral with the SDK's awake
antenna init position.
"""

from __future__ import annotations

import math
import random
from typing import Tuple

import numpy as np
from numpy.typing import NDArray

from reachy_mini.motion.move import Move
from reachy_mini.utils import create_head_pose
from reachy_mini.utils.interpolation import linear_pose_interpolation


# Pose constants. SDK-derived but tuned to be less dramatic.
_SLEEP_PITCH_DEG = -15.0       # gentle nod down (SDK uses ~-24°)
_SLEEP_Z_MM = -4.0             # tiny drop so head visibly droops
_SLEEP_HEAD_POSE = create_head_pose(
    x=0.0, y=0.0, z=_SLEEP_Z_MM,
    roll=0.0, pitch=_SLEEP_PITCH_DEG, yaw=0.0,
    degrees=True, mm=True,
)
# SDK's SLEEP_ANTENNAS_JOINT_POSITIONS = [-3.05, 3.05] — antennas folded
# all the way back. Same here.
_SLEEP_ANTENNAS = np.array([-3.05, 3.05], dtype=np.float64)
# Awake target = breathing neutral (0, 0). Matches BreathingMove's
# `neutral_antennas` so the wake-then-breathe handoff produces no
# additional antenna travel. Earlier we used the SDK's INIT_ANTENNAS
# (-0.1745, 0.1745) but it caused a perceptible second hop after
# WakeUpMove finished and breathing kicked in.
_AWAKE_ANTENNAS = np.array([0.0, 0.0], dtype=np.float64)
_NEUTRAL_HEAD_POSE = create_head_pose(0, 0, 0, 0, 0, 0, degrees=True, mm=True)

# Canonical "off / power-down" pose, copied verbatim from the upstream
# Reachy Mini SDK (pollen-robotics/reachy-mini, daemon/backend/abstract.py
# at SLEEP_HEAD_POSE). The daemon commands this exact 4x4 transform when
# shutting down. Decodes to: pitch ≈ +24.4° (chin forward / head bowed
# down), x = -21 mm, z = -44 mm — visibly more pronounced than the
# gentler interactive sleep pose above.
#
# We hardcode the matrix rather than going through create_head_pose so
# pitch sign-convention differences between the SDK's matrix builder
# and ours can't cause the head to bow the wrong way.
_OFF_HEAD_POSE = np.array(
    [
        [ 0.911,  0.004,  0.413, -0.021],
        [-0.004,  1.0,   -0.001,  0.001],
        [-0.413, -0.001,  0.911, -0.044],
        [ 0.0,    0.0,    0.0,    1.0  ],
    ],
    dtype=np.float32,
)
# Same antennas as gentle sleep — folded fully back.
_OFF_ANTENNAS = _SLEEP_ANTENNAS


def _smoothstep(a: float) -> float:
    """3a²-2a³ — gentle in/out, no overshoot."""
    a = max(0.0, min(1.0, a))
    return a * a * (3.0 - 2.0 * a)


class GoToSleepMove(Move):
    """Smoothly transitions head + antennas to the sleep pose."""

    def __init__(
        self,
        start_pose: NDArray[np.float32],
        start_antennas: Tuple[float, float] = (0.0, 0.0),
        *,
        duration: float = 2.0,
    ) -> None:
        self.start_pose = start_pose
        self.start_antennas = np.array(start_antennas, dtype=np.float64)
        self._duration = duration

    @property
    def duration(self) -> float:
        return self._duration

    def evaluate(self, t: float) -> tuple:
        alpha = _smoothstep(min(1.0, t / self._duration))
        head = linear_pose_interpolation(self.start_pose, _SLEEP_HEAD_POSE, alpha)
        ant = (1.0 - alpha) * self.start_antennas + alpha * _SLEEP_ANTENNAS
        return (head, ant.astype(np.float64), 0.0)


class SleepHoldMove(Move):
    """Holds the sleep pose for a long time with periodic 'snore' wiggles
    of the antennas. Default duration is 5 minutes — re-queue if you
    want sleep to last longer than that.
    """

    SNORE_MIN_S = 8.0
    SNORE_MAX_S = 18.0
    SNORE_DURATION_S = 0.7      # one full breath out
    SNORE_AMPLITUDE_RAD = 0.18  # ~10° antenna twitch around the back-pose

    def __init__(self, *, duration: float = 5 * 60.0) -> None:
        self._duration = duration
        self._next_snore_at = random.uniform(self.SNORE_MIN_S, self.SNORE_MAX_S)

    @property
    def duration(self) -> float:
        return self._duration

    def evaluate(self, t: float) -> tuple:
        ant = _SLEEP_ANTENNAS.copy()
        elapsed_in_snore = t - self._next_snore_at
        if 0.0 <= elapsed_in_snore < self.SNORE_DURATION_S:
            # Half-sine envelope so the wiggle eases in and out.
            phase = elapsed_in_snore / self.SNORE_DURATION_S * math.pi
            wiggle = self.SNORE_AMPLITUDE_RAD * math.sin(phase)
            ant = ant + np.array([wiggle, -wiggle], dtype=np.float64)
        elif elapsed_in_snore >= self.SNORE_DURATION_S:
            # Schedule next snore.
            self._next_snore_at = t + random.uniform(
                self.SNORE_MIN_S, self.SNORE_MAX_S
            )
        return (_SLEEP_HEAD_POSE, ant.astype(np.float64), 0.0)


class OffPoseMove(Move):
    """Smoothly transitions to the SDK's canonical off / shutdown pose.

    Used by the SIGTERM handler so the robot visibly powers down (head
    bowed forward, antennas folded back) before the process exits.
    Different from GoToSleepMove — this is the *terminal* shutdown
    pose, not the interactive 'I'm idle' pose. Duration matches the
    SDK's `goto_sleep` (2.0 s).
    """

    def __init__(
        self,
        start_pose: NDArray[np.float32] = _NEUTRAL_HEAD_POSE,
        start_antennas: Tuple[float, float] = (0.0, 0.0),
        *,
        duration: float = 2.0,
    ) -> None:
        self.start_pose = start_pose
        self.start_antennas = np.array(start_antennas, dtype=np.float64)
        self._duration = duration

    @property
    def duration(self) -> float:
        return self._duration

    def evaluate(self, t: float) -> tuple:
        alpha = _smoothstep(min(1.0, t / self._duration))
        head = linear_pose_interpolation(self.start_pose, _OFF_HEAD_POSE, alpha)
        ant = (1.0 - alpha) * self.start_antennas + alpha * _OFF_ANTENNAS
        return (head, ant.astype(np.float64), 0.0)


class WakeUpMove(Move):
    """Smoothly returns from sleep pose to neutral with antennas forward."""

    def __init__(
        self,
        start_pose: NDArray[np.float32] = _SLEEP_HEAD_POSE,
        start_antennas: Tuple[float, float] = (-3.05, 3.05),
        *,
        duration: float = 1.4,
    ) -> None:
        self.start_pose = start_pose
        self.start_antennas = np.array(start_antennas, dtype=np.float64)
        self._duration = duration

    @property
    def duration(self) -> float:
        return self._duration

    def evaluate(self, t: float) -> tuple:
        alpha = _smoothstep(min(1.0, t / self._duration))
        head = linear_pose_interpolation(self.start_pose, _NEUTRAL_HEAD_POSE, alpha)
        ant = (1.0 - alpha) * self.start_antennas + alpha * _AWAKE_ANTENNAS
        return (head, ant.astype(np.float64), 0.0)
