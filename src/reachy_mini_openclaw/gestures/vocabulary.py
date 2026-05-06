"""Custom Move subclasses for Clawson event gestures.

These plug into the existing MovementManager queue alongside HeadLookMove
and friends. Each gesture is structured ease-in → hold → ease-out so the
viewer perceives a clear beat. Face tracking offsets continue to apply
on top per the upstream design (see moves.py:343-350).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from numpy.typing import NDArray

from reachy_mini.motion.move import Move
from reachy_mini.utils import create_head_pose
from reachy_mini.utils.interpolation import linear_pose_interpolation


def _smoothstep(alpha: float) -> float:
    """3α²-2α³ — gentler than linear, no overshoot."""
    a = max(0.0, min(1.0, alpha))
    return a * a * (3 - 2 * a)


def _three_phase(t: float, ease_in: float, hold: float, ease_out: float) -> Tuple[str, float]:
    """Return ('in'|'hold'|'out'|'done', alpha-within-phase)."""
    if t < ease_in:
        return ("in", t / ease_in if ease_in > 0 else 1.0)
    t -= ease_in
    if t < hold:
        return ("hold", 1.0)
    t -= hold
    if t < ease_out:
        return ("out", t / ease_out if ease_out > 0 else 1.0)
    return ("done", 1.0)


@dataclass
class _GestureBase(Move):
    start_pose: NDArray[np.float32]
    start_antennas: Tuple[float, float] = (0.0, 0.0)

    @property
    def duration(self) -> float:  # type: ignore[override]
        raise NotImplementedError


class HeadDroopMove(_GestureBase):
    """Head droops down + slightly forward, holds, then recovers.

    Mapped to ci_fail / ci_failure-like events. Total ~1.8s.
    """

    def __init__(
        self,
        start_pose: NDArray[np.float32],
        start_antennas: Tuple[float, float] = (0.0, 0.0),
        *,
        ease_in: float = 0.35,
        hold: float = 1.0,
        ease_out: float = 0.55,
    ) -> None:
        self.start_pose = start_pose
        self.start_antennas = start_antennas
        self._ease_in = ease_in
        self._hold = hold
        self._ease_out = ease_out
        self._target_pose = create_head_pose(
            x=0, y=0, z=-8, roll=0, pitch=-22, yaw=0, degrees=True, mm=True
        )
        self._target_antennas = np.array([-0.20, -0.20], dtype=np.float64)
        self._neutral_pose = create_head_pose(0, 0, 0, 0, 0, 0, degrees=True, mm=True)

    @property
    def duration(self) -> float:
        return self._ease_in + self._hold + self._ease_out

    def evaluate(self, t: float) -> tuple:
        phase, alpha = _three_phase(t, self._ease_in, self._hold, self._ease_out)
        if phase == "in":
            a = _smoothstep(alpha)
            head = linear_pose_interpolation(self.start_pose, self._target_pose, a)
            ant = (1 - a) * np.array(self.start_antennas) + a * self._target_antennas
        elif phase == "hold":
            head = self._target_pose
            ant = self._target_antennas
        else:  # out / done
            a = _smoothstep(alpha)
            head = linear_pose_interpolation(self._target_pose, self._neutral_pose, a)
            ant = (1 - a) * self._target_antennas + a * np.array([0.0, 0.0])
        return (head, ant.astype(np.float64), 0.0)


class HappyBounceMove(_GestureBase):
    """Two quick upward nods. Mapped to ci_pass_after_fail / pr_merged."""

    def __init__(
        self,
        start_pose: NDArray[np.float32],
        start_antennas: Tuple[float, float] = (0.0, 0.0),
        *,
        bounces: int = 2,
        bounce_period: float = 0.30,
        amplitude_deg: float = 18.0,
    ) -> None:
        self.start_pose = start_pose
        self.start_antennas = start_antennas
        self._bounces = bounces
        self._period = bounce_period
        self._amplitude = amplitude_deg
        self._neutral = create_head_pose(0, 0, 0, 0, 0, 0, degrees=True, mm=True)

    @property
    def duration(self) -> float:
        return self._bounces * self._period + 0.15  # tail to settle

    def evaluate(self, t: float) -> tuple:
        # Sine envelope clipped to up-only — head goes up then back to centre,
        # repeated `bounces` times.
        cycle_t = t % self._period
        phase = cycle_t / self._period * 2 * math.pi
        # full cosine wave: starts at 0, peaks negative-cos at π → invert sign.
        pitch_deg = self._amplitude * max(0.0, math.sin(phase))
        z_mm = 6.0 * max(0.0, math.sin(phase))
        head = create_head_pose(
            x=0, y=0, z=z_mm, roll=0, pitch=pitch_deg, yaw=0, degrees=True, mm=True
        )
        ant_sway = 0.15 * math.sin(phase)
        ant = np.array([ant_sway, -ant_sway], dtype=np.float64)
        return (head, ant, 0.0)


class SmallNodMove(_GestureBase):
    """Brief acknowledging nod. Mapped to review_requested / mention / issue_assigned."""

    def __init__(
        self,
        start_pose: NDArray[np.float32],
        start_antennas: Tuple[float, float] = (0.0, 0.0),
        *,
        amplitude_deg: float = 10.0,
        period: float = 0.45,
    ) -> None:
        self.start_pose = start_pose
        self.start_antennas = start_antennas
        self._amplitude = amplitude_deg
        self._period = period

    @property
    def duration(self) -> float:
        return self._period + 0.10

    def evaluate(self, t: float) -> tuple:
        # One full down-up-return arc.
        alpha = min(1.0, t / self._period) if self._period > 0 else 1.0
        pitch = -self._amplitude * math.sin(alpha * math.pi)
        head = create_head_pose(
            x=0, y=0, z=0, roll=0, pitch=pitch, yaw=0, degrees=True, mm=True
        )
        return (head, np.array([0.0, 0.0], dtype=np.float64), 0.0)


# Event-kind → gesture-factory mapping. Factories take (start_pose, start_antennas)
# and return a Move ready to queue.
_GESTURE_MAP = {
    "ci_fail":              lambda p, a: HeadDroopMove(p, a),
    "ci_pass_after_fail":   lambda p, a: HappyBounceMove(p, a),
    "pr_merged":            lambda p, a: HappyBounceMove(p, a),
    "review_requested":     lambda p, a: SmallNodMove(p, a),
    "mention":              lambda p, a: SmallNodMove(p, a),
    "issue_assigned":       lambda p, a: SmallNodMove(p, a),
    # Vercel — distinct gesture per plan: longer droop than CI fail.
    "vercel_deploy_fail":    lambda p, a: HeadDroopMove(p, a, hold=1.6),
    "vercel_deploy_success": lambda p, a: SmallNodMove(p, a),
    # Auth failure: deep, slow droop. Critical events bypass mode gating.
    "auth_failed":           lambda p, a: HeadDroopMove(p, a, ease_in=0.5, hold=2.0, ease_out=0.7),
}


def gesture_for_event(
    kind: str,
    start_pose: NDArray[np.float32],
    start_antennas: Tuple[float, float] = (0.0, 0.0),
) -> Optional[Move]:
    """Return a queueable Move for the given event kind, or None if no gesture."""
    factory = _GESTURE_MAP.get(kind)
    if factory is None:
        return None
    return factory(start_pose, start_antennas)
