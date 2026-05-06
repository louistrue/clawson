"""Antenna tracker / poller tests with synthetic position streams."""

import asyncio
import math
from typing import List, Optional, Tuple

import pytest

from reachy_mini_openclaw.focus.antennas import (
    AntennaEvent,
    AntennaPoller,
    HOLD_S,
    MIN_PRESS_S,
    PRESS_THRESHOLD_RAD,
    RELEASE_THRESHOLD_RAD,
    _AntennaTracker,
)


PRESS_MAG = PRESS_THRESHOLD_RAD + 0.05
RELEASE_MAG = 0.0


def _drive(tracker: _AntennaTracker, samples: List[Tuple[float, float]]):
    """Feed (magnitude, time) pairs; collect emitted events."""
    out = []
    for mag, t in samples:
        _, ev = tracker.feed(mag, t)
        if ev is not None:
            out.append(ev)
    return out


def test_tap_short_press_emits_tap():
    tr = _AntennaTracker("right")
    events = _drive(tr, [
        (RELEASE_MAG, 0.0),
        (PRESS_MAG, 0.10),     # press down
        (PRESS_MAG, 0.15),     # still pressed
        (RELEASE_MAG, 0.25),   # release after 150ms
    ])
    assert events == [AntennaEvent(side="right", kind="tap")]


def test_below_min_press_is_filtered_as_noise():
    tr = _AntennaTracker("left")
    events = _drive(tr, [
        (RELEASE_MAG, 0.0),
        (PRESS_MAG, 0.10),
        (RELEASE_MAG, 0.10 + MIN_PRESS_S / 2),  # too short
    ])
    assert events == []


def test_hold_emits_at_threshold_then_release_does_not_emit_tap():
    tr = _AntennaTracker("left")
    events = _drive(tr, [
        (PRESS_MAG, 0.0),
        (PRESS_MAG, HOLD_S - 0.1),   # not yet hold
        (PRESS_MAG, HOLD_S + 0.05),  # crosses hold threshold
        (RELEASE_MAG, HOLD_S + 0.5),  # release after hold
    ])
    assert events == [AntennaEvent(side="left", kind="hold")]


def test_release_below_hysteresis_only():
    """Magnitudes between RELEASE and PRESS must not toggle pressed state."""
    tr = _AntennaTracker("right")
    events = _drive(tr, [
        (PRESS_MAG, 0.0),
        ((PRESS_THRESHOLD_RAD + RELEASE_THRESHOLD_RAD) / 2, 0.10),  # in hysteresis band
        (RELEASE_MAG, 0.20),
    ])
    assert events == [AntennaEvent(side="right", kind="tap")]


@pytest.mark.asyncio
async def test_poller_emits_both_when_concurrent():
    """When both antennas press within the BOTH window, only 'both' fires."""
    seen: List[AntennaEvent] = []

    # Scripted position stream: (left, right) pairs, then None to stop.
    script = [
        (0.0, 0.0),
        (PRESS_MAG, PRESS_MAG),  # both press concurrently
        (PRESS_MAG, PRESS_MAG),
        (0.0, 0.0),              # both release
    ]
    idx = {"i": 0}

    def reader():
        i = idx["i"]
        idx["i"] += 1
        if i < len(script):
            return script[i]
        return None

    async def on_event(e: AntennaEvent):
        seen.append(e)

    poller = AntennaPoller(read_positions=reader, on_event=on_event, poll_interval_s=0.001)

    stopped = {"v": False}
    def should_stop():
        return idx["i"] > len(script) + 5 or stopped["v"]

    task = asyncio.create_task(poller.run_until(should_stop))
    await asyncio.sleep(0.05)
    stopped["v"] = True
    await task

    kinds = [(e.side, e.kind) for e in seen]
    assert ("both", "tap") in kinds
    # Per-antenna taps were absorbed.
    assert ("left", "tap") not in kinds
    assert ("right", "tap") not in kinds


@pytest.mark.asyncio
async def test_poller_emits_single_side_when_alone():
    """A solo press flushes as 'tap' after the double-tap window expires."""
    seen: List[AntennaEvent] = []
    # 12 PRESS samples then idle samples that span the double-gap window.
    script = [(0.0, 0.0)]
    script.extend((PRESS_MAG, 0.0) for _ in range(12))
    script.extend((0.0, 0.0) for _ in range(80))  # ~800ms idle > DOUBLE_GAP_S
    idx = {"i": 0}

    def reader():
        i = idx["i"]
        idx["i"] += 1
        if i < len(script):
            return script[i]
        return None

    async def on_event(e: AntennaEvent):
        seen.append(e)

    poller = AntennaPoller(read_positions=reader, on_event=on_event, poll_interval_s=0.010)

    stopped = {"v": False}

    def should_stop():
        return idx["i"] > len(script) + 5 or stopped["v"]

    task = asyncio.create_task(poller.run_until(should_stop))
    await asyncio.sleep(1.20)
    stopped["v"] = True
    await task

    kinds = [(e.side, e.kind) for e in seen]
    assert ("left", "tap") in kinds
    assert all(s != "both" for s, _ in kinds)


@pytest.mark.asyncio
async def test_poller_collapses_two_quick_taps_into_double():
    """Two taps within DOUBLE_GAP_S on the same side fire 'double', not two taps."""
    seen: List[AntennaEvent] = []
    script = [(0.0, 0.0)]
    # First tap
    script.extend((PRESS_MAG, 0.0) for _ in range(10))
    script.append((0.0, 0.0))
    # ~150ms gap (well under DOUBLE_GAP_S=400ms)
    script.extend((0.0, 0.0) for _ in range(15))
    # Second tap
    script.extend((PRESS_MAG, 0.0) for _ in range(10))
    script.extend((0.0, 0.0) for _ in range(80))  # idle long enough to flush
    idx = {"i": 0}

    def reader():
        i = idx["i"]
        idx["i"] += 1
        if i < len(script):
            return script[i]
        return None

    async def on_event(e: AntennaEvent):
        seen.append(e)

    poller = AntennaPoller(read_positions=reader, on_event=on_event, poll_interval_s=0.010)

    stopped = {"v": False}

    def should_stop():
        return idx["i"] > len(script) + 5 or stopped["v"]

    task = asyncio.create_task(poller.run_until(should_stop))
    await asyncio.sleep(1.50)
    stopped["v"] = True
    await task

    kinds = [(e.side, e.kind) for e in seen]
    assert ("left", "double") in kinds
    # No stale single tap from the buffer.
    assert kinds.count(("left", "tap")) == 0
