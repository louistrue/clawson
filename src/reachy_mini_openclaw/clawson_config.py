"""Clawson-specific configuration loader.

Kept separate from upstream's `config.py` so we don't muddy the inherited
env-var schema. Reads `~/.config/clawson/config.toml` first, then layers
environment variables on top — env wins on collision.

Schema (everything optional, sensible defaults if missing):

    [github]
    token = "ghp_..."

    [focus]
    timezone        = "Europe/Zurich"
    active_hours    = ["09:00", "18:00"]
    standup_time    = "07:30"
    standup_days    = ["mon", "tue", "wed", "thu", "fri"]
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import tomllib  # py3.11+
except ImportError:                                  # pragma: no cover
    import tomli as tomllib                          # type: ignore

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "clawson" / "config.toml"

# Defaults (Europe/Zurich; weekdays; 09-18 active; 07:30 standup).
DEFAULT_TIMEZONE = "Europe/Zurich"
DEFAULT_ACTIVE_HOURS = ("09:00", "18:00")
DEFAULT_STANDUP_TIME = "07:30"
DEFAULT_STANDUP_DAYS = ("mon", "tue", "wed", "thu", "fri")

_DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_hhmm(value: str) -> time:
    h, m = value.strip().split(":")
    return time(int(h), int(m))


def _parse_days(values: List[str]) -> Tuple[int, ...]:
    out: List[int] = []
    for v in values:
        key = v.strip().lower()[:3]
        if key in _DAY_INDEX:
            out.append(_DAY_INDEX[key])
    return tuple(sorted(set(out)))


@dataclass
class FocusSettings:
    timezone: str = DEFAULT_TIMEZONE
    active_start: time = field(default_factory=lambda: _parse_hhmm(DEFAULT_ACTIVE_HOURS[0]))
    active_end: time = field(default_factory=lambda: _parse_hhmm(DEFAULT_ACTIVE_HOURS[1]))
    standup_time: time = field(default_factory=lambda: _parse_hhmm(DEFAULT_STANDUP_TIME))
    standup_days: Tuple[int, ...] = DEFAULT_STANDUP_DAYS  # weekday() indices

    def tzinfo(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            logger.warning("unknown timezone %r, falling back to UTC", self.timezone)
            return ZoneInfo("UTC")


@dataclass
class ClawsonConfig:
    github_token: Optional[str] = None
    focus: FocusSettings = field(default_factory=FocusSettings)

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_token)


def _focus_from_section(section: dict) -> FocusSettings:
    settings = FocusSettings()
    if "timezone" in section:
        settings.timezone = str(section["timezone"])
    active = section.get("active_hours")
    if isinstance(active, list) and len(active) == 2:
        try:
            settings.active_start = _parse_hhmm(active[0])
            settings.active_end = _parse_hhmm(active[1])
        except Exception as e:
            logger.warning("bad active_hours %r: %s", active, e)
    standup_time = section.get("standup_time")
    if isinstance(standup_time, str):
        try:
            settings.standup_time = _parse_hhmm(standup_time)
        except Exception as e:
            logger.warning("bad standup_time %r: %s", standup_time, e)
    standup_days = section.get("standup_days")
    if isinstance(standup_days, list):
        days = _parse_days(standup_days)
        if days:
            settings.standup_days = days
    return settings


def load_clawson_config(path: Path = DEFAULT_CONFIG_PATH) -> ClawsonConfig:
    """Load clawson config from TOML, then layer env vars on top."""
    raw: dict = {}
    if path.exists():
        try:
            raw = tomllib.loads(path.read_text())
        except Exception as e:
            logger.warning("could not parse %s: %s", path, e)
            raw = {}

    github_section = raw.get("github") or {}
    focus_settings = _focus_from_section(raw.get("focus") or {})

    return ClawsonConfig(
        github_token=os.environ.get("GITHUB_TOKEN") or github_section.get("token"),
        focus=focus_settings,
    )
