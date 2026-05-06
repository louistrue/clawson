"""Tests for backoff helper + GitHub/Vercel persistence."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reachy_mini_openclaw.briefing.backoff import Backoff
from reachy_mini_openclaw.briefing.filters import GitHubFilterState
from reachy_mini_openclaw.briefing.persistence import (
    load_github_state,
    load_vercel_seen,
    save_github_state,
    save_vercel_seen,
)


# -------------------- Backoff --------------------


def test_backoff_first_failure_returns_at_least_base():
    b = Backoff(base_s=5, max_s=300, factor=2, jitter=0)
    delay = b.failed()
    assert delay >= 5
    assert b.fails == 1


def test_backoff_grows_exponentially_until_capped():
    b = Backoff(base_s=10, max_s=80, factor=2, jitter=0)
    seq = [b.failed() for _ in range(8)]
    # 10, 20, 40, 80, 80, 80, 80, 80
    assert seq[0] == 10
    assert seq[1] == 20
    assert seq[2] == 40
    assert seq[3] == 80
    assert all(s == 80 for s in seq[4:])


def test_backoff_succeeded_resets_counter():
    b = Backoff()
    b.failed()
    b.failed()
    assert b.fails == 2
    b.succeeded()
    assert b.fails == 0


def test_backoff_jitter_keeps_delay_at_or_above_base():
    b = Backoff(base_s=5, max_s=100, factor=2, jitter=0.5)
    # drive enough samples to exercise the jitter range
    delays = [b.failed() for _ in range(20)]
    assert all(d >= 5 for d in delays)


# -------------------- GitHub state persistence --------------------


def test_github_state_roundtrip(tmp_path: Path):
    path = tmp_path / "github.json"
    state = GitHubFilterState()
    state.last_ci_conclusion[("o/r", "main")] = "failure"
    state.last_ci_fail_at[("o/r", "main")] = datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc)
    since = datetime(2026, 5, 6, 8, 30, tzinfo=timezone.utc)
    save_github_state(state, since, path)
    assert path.exists()
    loaded_state, loaded_since = load_github_state(path)
    assert loaded_state.last_ci_conclusion[("o/r", "main")] == "failure"
    assert loaded_state.last_ci_fail_at[("o/r", "main")].isoformat() == \
        "2026-05-06T09:00:00+00:00"
    assert loaded_since == since


def test_github_state_load_missing_file_returns_empty(tmp_path: Path):
    state, since = load_github_state(tmp_path / "absent.json")
    assert state.last_ci_conclusion == {}
    assert state.last_ci_fail_at == {}
    assert since is None


def test_github_state_load_corrupt_returns_empty(tmp_path: Path):
    path = tmp_path / "github.json"
    path.write_text("not json {")
    state, since = load_github_state(path)
    assert state.last_ci_conclusion == {}
    assert since is None


# -------------------- Vercel seen-set --------------------


def test_vercel_seen_roundtrip(tmp_path: Path):
    path = tmp_path / "vercel.json"
    save_vercel_seen({"dpl_a", "dpl_b", "dpl_c"}, path)
    loaded = load_vercel_seen(path)
    assert loaded == {"dpl_a", "dpl_b", "dpl_c"}


def test_vercel_seen_empty_when_missing(tmp_path: Path):
    assert load_vercel_seen(tmp_path / "nope.json") == set()


def test_vercel_seen_corrupt_file_returns_empty(tmp_path: Path):
    path = tmp_path / "vercel.json"
    path.write_text("{not: json")
    assert load_vercel_seen(path) == set()
