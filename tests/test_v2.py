"""Tests for v2 modules: event log, calendar parsing, narrator, todoist."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from reachy_mini_openclaw.briefing.event_log import EventLog
from reachy_mini_openclaw.briefing.events import Event, EventSeverity
from reachy_mini_openclaw.briefing.narrator import narrate
from reachy_mini_openclaw.briefing.todoist_poller import (
    _overdue_event,
    _today_event,
)
from reachy_mini_openclaw.mcp_clients.calendar_ics import (
    CalEvent,
    parse_ics,
)
from reachy_mini_openclaw.mcp_clients.todoist import Task, _parse_task


def _evt(kind: str, fp: str = None, **raw) -> Event:
    return Event(
        source=raw.pop("source", "github"),
        kind=kind,
        summary=f"x:{kind}",
        link="",
        ts=raw.pop("ts", datetime.now(timezone.utc)),
        fingerprint=fp or f"{kind}:{raw}",
        severity=EventSeverity.NORMAL,
        raw=raw or None,
    )


# -------------------- EventLog --------------------


def test_event_log_append_and_read(tmp_path: Path):
    log = EventLog(log_dir=tmp_path)
    ev = _evt("ci_fail", fp="a", repo="o/r", branch="main")
    log.append(ev)
    out = log.read_recent(days=1)
    assert len(out) == 1
    assert out[0].fingerprint == "a"


def test_event_log_filters_by_source(tmp_path: Path):
    log = EventLog(log_dir=tmp_path)
    log.append(_evt("ci_fail", fp="g1"))
    log.append(Event(
        source="vercel", kind="vercel_deploy_fail", summary="x", link="",
        ts=datetime.now(timezone.utc), fingerprint="v1",
        severity=EventSeverity.NORMAL,
    ))
    out = log.read_recent(days=1, source="vercel")
    assert len(out) == 1
    assert out[0].source == "vercel"


def test_event_log_find_by_fingerprint(tmp_path: Path):
    log = EventLog(log_dir=tmp_path)
    ev = _evt("ci_fail", fp="findme")
    log.append(ev)
    found = log.find_by_fingerprint("findme")
    assert found is not None
    assert found.fingerprint == "findme"
    assert log.find_by_fingerprint("nope") is None


def test_event_log_skips_malformed_lines(tmp_path: Path):
    log = EventLog(log_dir=tmp_path)
    log.append(_evt("ci_fail", fp="ok"))
    today = datetime.now(timezone.utc).date().isoformat()
    (tmp_path / f"{today}.jsonl").open("a").write("garbage line\n")
    out = log.read_recent(days=1)
    fps = {e.fingerprint for e in out}
    assert "ok" in fps


# -------------------- Calendar ICS --------------------


_SAMPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//x//
BEGIN:VEVENT
UID:abc-1
SUMMARY:Standup with team
DTSTART:20260506T080000Z
DTEND:20260506T083000Z
LOCATION:Zoom
END:VEVENT
BEGIN:VEVENT
UID:abc-2
SUMMARY:All-day offsite
DTSTART;VALUE=DATE:20260507
DTEND;VALUE=DATE:20260508
END:VEVENT
END:VCALENDAR
"""


def test_parse_ics_basic_event():
    events = parse_ics(_SAMPLE_ICS)
    assert len(events) == 2
    summaries = sorted(e.summary for e in events)
    assert summaries == ["All-day offsite", "Standup with team"]


def test_parse_ics_handles_all_day_dates():
    events = parse_ics(_SAMPLE_ICS)
    all_day = next(e for e in events if e.uid == "abc-2")
    assert all_day.all_day is True
    assert all_day.start.hour == 0


def test_parse_ics_unfolds_continuation_lines():
    # Per RFC 5545, the wrap whitespace itself is stripped — producers
    # insert it inside content if they want a space across the boundary.
    folded = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1\n"
        "SUMMARY:Long title that\n  wraps once\n"
        "DTSTART:20260506T080000Z\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    events = parse_ics(folded)
    assert len(events) == 1
    # Continuation line strips one wrap-whitespace char; remaining " wraps once"
    # joins onto the prior, giving "Long title that wraps once".
    assert events[0].summary == "Long title that wraps once"


def test_parse_ics_skips_event_without_dtstart():
    bad = "BEGIN:VEVENT\nUID:1\nSUMMARY:x\nEND:VEVENT\n"
    events = parse_ics(bad)
    assert events == []


# -------------------- Todoist --------------------


def test_todoist_parse_task_with_due():
    raw = {
        "id": "1234",
        "content": "Buy milk",
        "description": "",
        "is_completed": False,
        "project_id": "p1",
        "due": {"date": "2026-05-06", "is_recurring": False, "datetime": None},
        "url": "https://todoist.com/task/1234",
    }
    t = _parse_task(raw)
    assert t.id == "1234"
    assert t.content == "Buy milk"
    assert t.due_iso == "2026-05-06"
    assert t.due_is_recurring is False


def test_todoist_today_event_uses_correct_kind():
    t = Task(
        id="1", content="Write tests", description="", project_id="p",
        is_completed=False, due_iso="2026-05-06", due_is_recurring=False,
        url="http://x", raw={},
    )
    ev = _today_event(t)
    assert ev.kind == "todoist_due_today"
    assert ev.fingerprint == "todoist:due_today:1"


def test_todoist_overdue_event_uses_correct_kind():
    t = Task(
        id="2", content="Old task", description="", project_id="p",
        is_completed=False, due_iso="2026-05-01", due_is_recurring=False,
        url="http://x", raw={},
    )
    ev = _overdue_event(t)
    assert ev.kind == "todoist_overdue"
    assert ev.severity == EventSeverity.NORMAL


# -------------------- Narrator --------------------


def test_narrator_detects_ci_cluster():
    now = datetime.now(timezone.utc)
    events = [
        Event(
            source="github", kind="ci_fail", summary="x", link="",
            ts=now - timedelta(minutes=i * 10),
            fingerprint=f"f{i}", severity=EventSeverity.NORMAL,
            raw={"repo": "o/r", "branch": "feat"},
        )
        for i in range(4)
    ]
    lines = narrate(events)
    assert any("keeps failing" in line for line in lines)


def test_narrator_detects_recovery():
    now = datetime.now(timezone.utc)
    events = [
        Event(
            source="github", kind="ci_fail", summary="x", link="",
            ts=now - timedelta(minutes=20),
            fingerprint="f1", severity=EventSeverity.NORMAL,
            raw={"repo": "o/r", "branch": "feat"},
        ),
        Event(
            source="github", kind="ci_pass_after_fail", summary="x", link="",
            ts=now,
            fingerprint="p1", severity=EventSeverity.NORMAL,
            raw={"repo": "o/r", "branch": "feat"},
        ),
    ]
    lines = narrate(events)
    assert any("green again" in line for line in lines)


def test_narrator_detects_review_pile():
    events = [_evt("review_requested", fp=f"r{i}") for i in range(5)]
    lines = narrate(events)
    assert any("reviews" in line for line in lines)


def test_narrator_quiet_when_nothing_significant():
    events = [_evt("issue_assigned", fp="x")]
    lines = narrate(events)
    assert lines == []
