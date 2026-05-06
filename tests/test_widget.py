"""Tests for the widget HTTP API.

Uses FastAPI's TestClient against a wired-up FocusController +
EventDispatcher + StandupRunner. The dispatcher is not actually consuming
events here (no run loop), but the API surface still works against its
queue / recent_events / drain_queued contract.
"""

from datetime import datetime, time, timezone
from typing import List

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("reachy_mini")  # gestures.vocabulary needs SDK

from fastapi.testclient import TestClient                 # noqa: E402

from reachy_mini_openclaw.briefing.dispatcher import EventDispatcher  # noqa: E402
from reachy_mini_openclaw.briefing.events import (         # noqa: E402
    Event,
    EventBus,
    EventSeverity,
)
from reachy_mini_openclaw.briefing.standup import StandupRunner  # noqa: E402
from reachy_mini_openclaw.clawson_config import FocusSettings    # noqa: E402
from reachy_mini_openclaw.focus.controller import FocusController  # noqa: E402
from reachy_mini_openclaw.focus.modes import FocusMode    # noqa: E402
from reachy_mini_openclaw.widget.server import WidgetServer  # noqa: E402


class _FakeMovementManager:
    def __init__(self):
        import numpy as np
        self.state = type("S", (), {})()
        self.state.last_primary_pose = (np.eye(4, dtype=np.float32), (0.0, 0.0), 0.0)
        self.queued = []

    def queue_move(self, move):
        self.queued.append(move)


@pytest.fixture
def wired(tmp_path):
    state_path = tmp_path / "state.json"
    focus = FocusController(state_path=state_path)
    bus = EventBus()
    mgr = _FakeMovementManager()
    dispatcher = EventDispatcher(bus, lambda: focus.mode, mgr)
    settings = FocusSettings(
        timezone="UTC",
        active_start=time(0, 0),
        active_end=time(23, 59),
        standup_time=time(7, 30),
        standup_days=(0, 1, 2, 3, 4),
    )
    standup = StandupRunner(settings, on_announce=None, drain_queued=dispatcher.drain_queued)
    server = WidgetServer(focus, dispatcher, standup, settings)
    return server, focus, dispatcher


def test_state_endpoint_reports_default_mode(wired):
    server, _, _ = wired
    with TestClient(server._app) as c:
        r = c.get("/api/state")
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "normal"
        assert body["queued"] == []
        assert body["recent"] == []


def test_mode_cycle_advances_state(wired):
    server, focus, _ = wired
    with TestClient(server._app) as c:
        c.post("/api/mode/cycle")
        # cycle from NORMAL → AVAILABLE.
        assert focus.mode == FocusMode.AVAILABLE


def test_mode_set_changes_state(wired):
    server, focus, _ = wired
    with TestClient(server._app) as c:
        r = c.post("/api/mode/deep")
        assert r.status_code == 200
        assert focus.mode == FocusMode.DEEP


def test_mode_set_rejects_snoozed(wired):
    server, _, _ = wired
    with TestClient(server._app) as c:
        r = c.post("/api/mode/snoozed")
        assert r.status_code == 400


def test_mode_set_unknown_returns_400(wired):
    server, _, _ = wired
    with TestClient(server._app) as c:
        r = c.post("/api/mode/turbo")
        assert r.status_code == 400


def test_snooze_updates_state(wired):
    server, focus, _ = wired
    with TestClient(server._app) as c:
        r = c.post("/api/snooze", json={"minutes": 30})
        assert r.status_code == 200
        assert focus.mode == FocusMode.SNOOZED
        assert focus.state.snooze_until is not None


def test_snooze_rejects_invalid_duration(wired):
    server, _, _ = wired
    with TestClient(server._app) as c:
        assert c.post("/api/snooze", json={"minutes": 0}).status_code == 400
        assert c.post("/api/snooze", json={"minutes": 99999}).status_code == 400


def test_unsnooze_restores_previous_mode(wired):
    server, focus, _ = wired
    with TestClient(server._app) as c:
        c.post("/api/mode/available")
        c.post("/api/snooze", json={"minutes": 15})
        assert focus.mode == FocusMode.SNOOZED
        c.post("/api/snooze/cancel")
        assert focus.mode == FocusMode.AVAILABLE


def test_queue_clear_empties_queue(wired):
    server, _, dispatcher = wired
    ev = Event(
        source="github", kind="ci_fail", summary="x", link="", ts=datetime.now(timezone.utc),
        fingerprint="fp1", severity=EventSeverity.NORMAL,
    )
    dispatcher._queued.append(ev)
    with TestClient(server._app) as c:
        r = c.post("/api/queue/clear")
        assert r.json()["cleared"] == 1
        assert len(dispatcher.queued_events) == 0


def test_widget_page_serves_html(wired):
    server, _, _ = wired
    with TestClient(server._app) as c:
        r = c.get("/widget")
        assert r.status_code == 200
        assert "Clawson" in r.text
        assert "Cycle mode" in r.text
