"""MuteList: per-source mute keys for the dispatcher."""

import json
from datetime import datetime, timezone
from pathlib import Path

from reachy_mini_openclaw.briefing.events import Event, EventSeverity
from reachy_mini_openclaw.briefing.mutes import MuteList, load_mutes, save_mutes


def _evt(source: str, repo_or_project: str | None) -> Event:
    raw = {}
    if source == "github":
        raw = {"repo": repo_or_project, "branch": "main"}
    elif source == "vercel":
        raw = {"project": repo_or_project}
    return Event(
        source=source, kind="ci_fail", summary="x", link="",
        ts=datetime.now(timezone.utc), fingerprint=f"{source}:x",
        severity=EventSeverity.NORMAL, raw=raw,
    )


def test_unmuted_event_is_not_muted():
    m = MuteList()
    assert m.is_muted(_evt("github", "o/r")) is False


def test_add_then_is_muted():
    m = MuteList()
    m.add("github", "o/r")
    assert m.is_muted(_evt("github", "o/r")) is True
    assert m.is_muted(_evt("github", "o/other")) is False


def test_remove_returns_true_then_false():
    m = MuteList()
    m.add("vercel", "site")
    assert m.remove("vercel", "site") is True
    assert m.remove("vercel", "site") is False
    assert m.is_muted(_evt("vercel", "site")) is False


def test_event_with_empty_raw_never_muted():
    m = MuteList()
    m.add("github", "o/r")
    ev = Event(
        source="github", kind="x", summary="", link="",
        ts=datetime.now(timezone.utc), fingerprint="z",
        severity=EventSeverity.NORMAL,
    )
    assert m.is_muted(ev) is False  # event.raw is None


def test_save_and_load_roundtrip(tmp_path: Path):
    m = MuteList()
    m.add("github", "o/repo1")
    m.add("github", "o/repo2")
    m.add("vercel", "siteA")
    save_mutes(m, tmp_path / "mutes.json")
    loaded = load_mutes(tmp_path / "mutes.json")
    assert loaded.is_muted(_evt("github", "o/repo1")) is True
    assert loaded.is_muted(_evt("vercel", "siteA")) is True


def test_load_missing_file_is_empty(tmp_path: Path):
    m = load_mutes(tmp_path / "nope.json")
    assert m.is_muted(_evt("github", "x")) is False


def test_load_corrupt_file_is_empty(tmp_path: Path):
    p = tmp_path / "mutes.json"
    p.write_text("not json {")
    m = load_mutes(p)
    assert m.is_muted(_evt("github", "x")) is False
