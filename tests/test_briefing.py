"""Tests for the briefing event bus, filters, and dispatcher."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from reachy_mini_openclaw.briefing.events import Event, EventBus, EventSeverity
from reachy_mini_openclaw.briefing.filters import (
    CI_FAIL_DEDUP_WINDOW,
    GitHubFilterState,
    github_event_should_pass,
)
from reachy_mini_openclaw.focus.modes import FocusMode

# The dispatcher transitively imports the reachy_mini SDK via gestures.
# Skip the dispatcher tests on machines without the SDK.
_sdk = pytest.importorskip("reachy_mini")
from reachy_mini_openclaw.briefing.dispatcher import EventDispatcher  # noqa: E402


def _make_event(kind: str, repo="o/r", branch="main", **kw) -> Event:
    return Event(
        source=kw.get("source", "github"),
        kind=kind,
        summary=f"{repo}:{kind}",
        link="http://x",
        ts=kw.get("ts", datetime.now(timezone.utc)),
        fingerprint=kw.get("fingerprint", f"github:{kind}:{repo}:{branch}"),
        severity=kw.get("severity", EventSeverity.NORMAL),
        raw={"repo": repo, "branch": branch},
    )


# -------------------- filters --------------------


def test_ci_fail_passes_when_branch_recent():
    state = GitHubFilterState()
    now = datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc)
    branches = {("o/r", "main"): now - timedelta(hours=1)}
    ev = _make_event("ci_fail")
    assert github_event_should_pass(ev, state=state, my_recent_branches=branches, now=now)
    assert state.last_ci_conclusion[("o/r", "main")] == "failure"


def test_ci_fail_dropped_when_branch_stale():
    state = GitHubFilterState()
    now = datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc)
    branches = {("o/r", "main"): now - timedelta(days=3)}
    ev = _make_event("ci_fail")
    assert not github_event_should_pass(ev, state=state, my_recent_branches=branches, now=now)


def test_ci_fail_dedup_within_window():
    state = GitHubFilterState()
    now = datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc)
    branches = {("o/r", "main"): now}
    ev1 = _make_event("ci_fail", fingerprint="a")
    assert github_event_should_pass(ev1, state=state, my_recent_branches=branches, now=now)
    ev2 = _make_event("ci_fail", fingerprint="b")  # different fingerprint, same repo/branch
    later_within = now + (CI_FAIL_DEDUP_WINDOW / 2)
    assert not github_event_should_pass(
        ev2, state=state, my_recent_branches=branches, now=later_within
    )


def test_ci_pass_after_fail_only_when_previous_was_fail():
    state = GitHubFilterState()
    now = datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc)
    branches = {("o/r", "main"): now}
    # No prior conclusion → should drop ci_pass_after_fail.
    ev = _make_event("ci_pass_after_fail")
    assert not github_event_should_pass(ev, state=state, my_recent_branches=branches, now=now)
    # After a fail recorded, pass_after_fail should pass.
    state.last_ci_conclusion[("o/r", "main")] = "failure"
    assert github_event_should_pass(ev, state=state, my_recent_branches=branches, now=now)
    # State now reflects success — second pass_after_fail is dropped.
    assert not github_event_should_pass(ev, state=state, my_recent_branches=branches, now=now)


def test_ci_pass_dropped_but_state_recorded():
    state = GitHubFilterState()
    branches = {("o/r", "main"): datetime.now(timezone.utc)}
    ev = _make_event("ci_pass")
    # Suppressed (we don't narrate plain success).
    assert not github_event_should_pass(ev, state=state, my_recent_branches=branches)
    # But state remembers it for pass-after-fail logic.
    assert state.last_ci_conclusion[("o/r", "main")] == "success"


def test_review_request_always_passes():
    state = GitHubFilterState()
    ev = _make_event("review_requested", branch=None)
    assert github_event_should_pass(ev, state=state)


# -------------------- dispatcher --------------------


class _FakeMovementManager:
    """Captures queued moves and exposes a synthetic last_primary_pose."""

    def __init__(self):
        import numpy as np
        self.state = type("S", (), {})()
        # Fake 4x4 identity-ish; downstream gestures only use it as a starting point.
        self.state.last_primary_pose = (np.eye(4, dtype=np.float32), (0.0, 0.0), 0.0)
        self.queued = []

    def queue_move(self, move):
        self.queued.append(move)


@pytest.mark.asyncio
async def test_dispatcher_normal_mode_fires_gesture():
    pytest.importorskip("reachy_mini")  # gesture import requires SDK
    bus = EventBus()
    mgr = _FakeMovementManager()
    disp = EventDispatcher(bus, lambda: FocusMode.NORMAL, mgr)
    await bus.publish(_make_event("ci_fail"))

    stop_after_one = {"count": 0}

    def should_stop():
        stop_after_one["count"] += 1
        return stop_after_one["count"] > 30 or len(mgr.queued) > 0

    await asyncio.wait_for(disp.run(should_stop), timeout=5.0)
    assert len(mgr.queued) == 1


@pytest.mark.asyncio
async def test_dispatcher_deep_mode_queues_non_critical():
    pytest.importorskip("reachy_mini")
    bus = EventBus()
    mgr = _FakeMovementManager()
    disp = EventDispatcher(bus, lambda: FocusMode.DEEP, mgr)
    ev = _make_event("ci_fail")
    await bus.publish(ev)

    stop_after = {"count": 0}

    def should_stop():
        stop_after["count"] += 1
        return stop_after["count"] > 30 or len(disp.queued_events) > 0

    await asyncio.wait_for(disp.run(should_stop), timeout=5.0)
    assert len(mgr.queued) == 0
    assert len(disp.queued_events) == 1
    assert disp.queued_events[0].fingerprint == ev.fingerprint


@pytest.mark.asyncio
async def test_dispatcher_dedup_drops_repeats():
    pytest.importorskip("reachy_mini")
    bus = EventBus()
    mgr = _FakeMovementManager()
    disp = EventDispatcher(bus, lambda: FocusMode.NORMAL, mgr)
    ev = _make_event("ci_fail", fingerprint="dup-1")
    await bus.publish(ev)
    await bus.publish(ev)  # exact same fingerprint

    seen = {"count": 0}

    def should_stop():
        seen["count"] += 1
        return seen["count"] > 50 or (len(mgr.queued) >= 1 and bus.pending == 0)

    await asyncio.wait_for(disp.run(should_stop), timeout=5.0)
    # Only the first should fire; the dup is suppressed.
    assert len(mgr.queued) == 1


@pytest.mark.asyncio
async def test_dispatcher_critical_overrides_deep_mode():
    pytest.importorskip("reachy_mini")
    bus = EventBus()
    mgr = _FakeMovementManager()
    disp = EventDispatcher(bus, lambda: FocusMode.DEEP, mgr)
    await bus.publish(
        _make_event("ci_fail", severity=EventSeverity.CRITICAL, fingerprint="crit")
    )

    seen = {"count": 0}

    def should_stop():
        seen["count"] += 1
        return seen["count"] > 30 or len(mgr.queued) > 0

    await asyncio.wait_for(disp.run(should_stop), timeout=5.0)
    assert len(mgr.queued) == 1
    assert len(disp.queued_events) == 0
