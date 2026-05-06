"""Standup runner: scheduling, script building, active-hours gate."""

from datetime import datetime, time, timedelta, timezone
from typing import List

from reachy_mini_openclaw.briefing.events import Event, EventSeverity
from reachy_mini_openclaw.briefing.standup import (
    _format_script,
    is_within_active_hours,
    next_standup_datetime,
)
from reachy_mini_openclaw.clawson_config import FocusSettings


def _focus(
    *,
    timezone_str: str = "Europe/Zurich",
    active_hours=("09:00", "18:00"),
    standup_time_str: str = "07:30",
    standup_days=(0, 1, 2, 3, 4),  # mon–fri
) -> FocusSettings:
    return FocusSettings(
        timezone=timezone_str,
        active_start=time.fromisoformat(active_hours[0]),
        active_end=time.fromisoformat(active_hours[1]),
        standup_time=time.fromisoformat(standup_time_str),
        standup_days=standup_days,
    )


def _evt(kind: str, fp: str = None) -> Event:
    return Event(
        source="github", kind=kind, summary=f"x:{kind}", link="",
        ts=datetime.now(timezone.utc),
        fingerprint=fp or f"github:{kind}:x",
        severity=EventSeverity.NORMAL,
    )


# ---------- next_standup_datetime ----------


def test_next_standup_after_today_is_tomorrow_same_time_if_weekday():
    f = _focus()
    # Tuesday 08:00 local — next is Wednesday 07:30.
    now = datetime(2026, 5, 5, 8, 0, tzinfo=f.tzinfo())
    nxt = next_standup_datetime(f, now)
    assert nxt.date() == datetime(2026, 5, 6).date()
    assert nxt.hour == 7 and nxt.minute == 30


def test_next_standup_skips_weekend():
    f = _focus()
    # Friday 09:00 local — next is Monday 07:30.
    now = datetime(2026, 5, 8, 9, 0, tzinfo=f.tzinfo())
    nxt = next_standup_datetime(f, now)
    assert nxt.weekday() == 0  # Monday
    assert nxt.date() == datetime(2026, 5, 11).date()


def test_next_standup_before_today_time_returns_today():
    f = _focus()
    # Tuesday 06:00 local — today is a weekday and 07:30 is still ahead.
    now = datetime(2026, 5, 5, 6, 0, tzinfo=f.tzinfo())
    nxt = next_standup_datetime(f, now)
    assert nxt.date() == now.date()
    assert nxt.hour == 7 and nxt.minute == 30


def test_next_standup_at_exact_time_advances_to_next_day():
    f = _focus()
    now = datetime(2026, 5, 5, 7, 30, tzinfo=f.tzinfo())  # exactly standup time
    nxt = next_standup_datetime(f, now)
    assert nxt > now
    assert nxt.date() == datetime(2026, 5, 6).date()


# ---------- is_within_active_hours ----------


def test_active_hours_inclusive_start_exclusive_end():
    f = _focus(active_hours=("09:00", "18:00"))
    tz = f.tzinfo()
    # 08:59 → false; 09:00 → true; 17:59 → true; 18:00 → false
    assert not is_within_active_hours(f, datetime(2026, 5, 5, 8, 59, tzinfo=tz))
    assert is_within_active_hours(f, datetime(2026, 5, 5, 9, 0, tzinfo=tz))
    assert is_within_active_hours(f, datetime(2026, 5, 5, 17, 59, tzinfo=tz))
    assert not is_within_active_hours(f, datetime(2026, 5, 5, 18, 0, tzinfo=tz))


def test_active_hours_works_when_now_is_in_utc():
    f = _focus()
    # 07:30 UTC = 09:30 Europe/Zurich (CEST May), inside window.
    now_utc = datetime(2026, 5, 5, 7, 30, tzinfo=timezone.utc)
    assert is_within_active_hours(f, now_utc)


# ---------- _format_script ----------


def test_format_script_quiet_when_no_events():
    out = _format_script([])
    assert "all quiet" in out.lower()


def test_format_script_groups_kinds_with_pluralisation():
    events: List[Event] = [
        _evt("ci_fail", "1"),
        _evt("ci_fail", "2"),
        _evt("review_requested", "3"),
    ]
    out = _format_script(events)
    assert "2 CI failures" in out
    assert "1 pull request" in out


def test_format_script_handles_pr_merged_and_recovery():
    events = [_evt("ci_pass_after_fail", "x"), _evt("pr_merged", "y")]
    out = _format_script(events)
    assert "1 recovery" in out or "1 recoveries" in out  # tolerate either form
    assert "1 of your pull request" in out
