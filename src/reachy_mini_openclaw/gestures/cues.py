"""Short audio cues paired with gestures (~200ms blips).

Synthesised on the fly with numpy and pushed straight to the robot's
audio output, bypassing the realtime handler. Three palettes:

  * sad   — descending low chirp, paired with droops (CI / deploy fail)
  * happy — ascending bright chirp, paired with bounces / nods
  * ack   — single soft blip, paired with mentions / reviews

Default volume is muted (~0.2) — these are notifications, not alarms.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_DURATION_S = 0.20
DEFAULT_VOLUME = 0.20


def _envelope(n: int) -> np.ndarray:
    """Quick attack, soft release — keeps clicks down."""
    attack = max(1, n // 20)
    release = max(1, n // 4)
    env = np.ones(n, dtype=np.float32)
    env[:attack] = np.linspace(0.0, 1.0, attack, dtype=np.float32)
    env[-release:] = np.linspace(1.0, 0.0, release, dtype=np.float32)
    return env


def _chirp(
    sr: int,
    duration_s: float,
    f0: float,
    f1: float,
    *,
    volume: float = DEFAULT_VOLUME,
) -> np.ndarray:
    n = max(1, int(sr * duration_s))
    t = np.linspace(0.0, duration_s, n, endpoint=False, dtype=np.float32)
    # Linear chirp.
    phase = 2 * math.pi * (f0 * t + 0.5 * (f1 - f0) / max(duration_s, 1e-6) * t * t)
    sig = np.sin(phase, dtype=np.float32)
    sig *= _envelope(n) * volume
    return sig


def _blip(
    sr: int,
    duration_s: float,
    freq: float,
    *,
    volume: float = DEFAULT_VOLUME,
) -> np.ndarray:
    n = max(1, int(sr * duration_s))
    t = np.linspace(0.0, duration_s, n, endpoint=False, dtype=np.float32)
    sig = np.sin(2 * math.pi * freq * t, dtype=np.float32)
    sig *= _envelope(n) * volume
    return sig


def cue_for_event(kind: str, sr: int) -> Optional[np.ndarray]:
    """Return float32 mono audio at `sr`, or None if no cue is configured."""
    if kind in {"ci_fail", "vercel_deploy_fail", "auth_failed"}:
        # Descending sad chirp — ~250 → 150 Hz.
        return _chirp(sr, DEFAULT_DURATION_S, 250.0, 150.0)
    if kind in {"ci_pass_after_fail", "pr_merged"}:
        # Ascending happy chirp — ~440 → 880 Hz.
        return _chirp(sr, DEFAULT_DURATION_S, 440.0, 880.0)
    if kind in {"vercel_deploy_success"}:
        # Quick high blip.
        return _blip(sr, 0.12, 660.0)
    if kind in {"review_requested", "mention", "issue_assigned"}:
        # Soft mid blip.
        return _blip(sr, 0.10, 440.0)
    return None
