from datetime import datetime, timedelta, timezone

import pytest

from reachy_mini_openclaw.focus.modes import FocusMode, FocusState
from reachy_mini_openclaw.focus.store import _deserialize, _serialize


def test_default_state_is_normal():
    s = FocusState()
    assert s.mode == FocusMode.NORMAL
    assert s.snooze_until is None
    assert s.previous_mode is None


def test_cycle_walks_three_modes_in_order():
    s = FocusState()
    assert s.cycle() == FocusMode.AVAILABLE
    assert s.cycle() == FocusMode.DEEP
    assert s.cycle() == FocusMode.NORMAL


def test_cycle_from_snoozed_restores_then_advances():
    s = FocusState(mode=FocusMode.AVAILABLE)
    s.snooze(timedelta(minutes=15))
    assert s.mode == FocusMode.SNOOZED
    # cycle from snoozed should restore to previous (available) then step to deep
    assert s.cycle() == FocusMode.DEEP


def test_snooze_records_previous_mode_and_deadline():
    now = datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc)
    s = FocusState(mode=FocusMode.DEEP)
    until = s.snooze(timedelta(hours=1), now=now)
    assert s.mode == FocusMode.SNOOZED
    assert s.previous_mode == FocusMode.DEEP
    assert until == now + timedelta(hours=1)


def test_snooze_twice_keeps_first_previous_mode():
    s = FocusState(mode=FocusMode.AVAILABLE)
    s.snooze(timedelta(minutes=15))
    s.snooze(timedelta(hours=4))
    assert s.previous_mode == FocusMode.AVAILABLE


def test_maybe_expire_restores_previous_mode():
    now = datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc)
    s = FocusState(mode=FocusMode.AVAILABLE)
    s.snooze(timedelta(minutes=15), now=now)
    # Before deadline: no change.
    assert s.maybe_expire(now=now + timedelta(minutes=14)) is False
    assert s.mode == FocusMode.SNOOZED
    # After deadline: restored.
    assert s.maybe_expire(now=now + timedelta(minutes=16)) is True
    assert s.mode == FocusMode.AVAILABLE
    assert s.snooze_until is None
    assert s.previous_mode is None


def test_maybe_expire_noop_when_not_snoozed():
    s = FocusState(mode=FocusMode.NORMAL)
    assert s.maybe_expire() is False


def test_serialize_roundtrip_preserves_state():
    now = datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc)
    s = FocusState(mode=FocusMode.SNOOZED, previous_mode=FocusMode.DEEP, snooze_until=now)
    out = _deserialize(_serialize(s))
    assert out.mode == FocusMode.SNOOZED
    assert out.previous_mode == FocusMode.DEEP
    assert out.snooze_until == now


def test_serialize_roundtrip_handles_none_fields():
    s = FocusState()
    out = _deserialize(_serialize(s))
    assert out.mode == FocusMode.NORMAL
    assert out.previous_mode is None
    assert out.snooze_until is None
