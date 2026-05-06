"""Daily say-cap tracker."""

import json
from datetime import date, timedelta
from pathlib import Path

from reachy_mini_openclaw.briefing.cost_tracker import CostTracker


def test_under_cap_allows_ticks(tmp_path: Path):
    t = CostTracker(daily_max=5, path=tmp_path / "u.json")
    for _ in range(5):
        assert t.tick() is True
    # 6th tick should fail.
    assert t.tick() is False


def test_remaining_decrements(tmp_path: Path):
    t = CostTracker(daily_max=3, path=tmp_path / "u.json")
    assert t.remaining == 3
    t.tick()
    assert t.remaining == 2


def test_persists_across_instances_same_day(tmp_path: Path):
    path = tmp_path / "u.json"
    t1 = CostTracker(daily_max=10, path=path)
    t1.tick()
    t1.tick()
    t2 = CostTracker(daily_max=10, path=path)
    assert t2.today_count == 2


def test_resets_on_new_day(tmp_path: Path):
    path = tmp_path / "u.json"
    # Pre-seed with yesterday.
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    path.write_text(json.dumps({"date": yesterday, "count": 5}))
    t = CostTracker(daily_max=10, path=path)
    # First tick today should succeed; today_count starts at 0.
    assert t.tick() is True
    assert t.today_count == 1


def test_corrupt_file_starts_clean(tmp_path: Path):
    path = tmp_path / "u.json"
    path.write_text("not json {{{")
    t = CostTracker(daily_max=3, path=path)
    assert t.tick() is True
