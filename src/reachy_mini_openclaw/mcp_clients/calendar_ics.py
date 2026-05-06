"""Minimal ICS calendar reader.

Fetches an .ics URL and parses VEVENT entries with start time. Handles
the bare-minimum feature set for our use case: today's events, with
DTSTART parsed as UTC. Recurring rules and multi-line folded fields
get a basic implementation; exotic ICS features (VTIMEZONE blocks,
exception dates, etc.) are out of scope and silently ignored.

No external deps beyond httpx.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, List, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0


@dataclass(frozen=True)
class CalEvent:
    uid: str
    summary: str
    start: datetime          # tz-aware UTC
    end: Optional[datetime]
    all_day: bool
    location: str = ""


def _unfold_lines(raw: str) -> List[str]:
    """ICS lines starting with a space/tab are continuations of the prior."""
    out: List[str] = []
    for line in raw.splitlines():
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


_DT_RE = re.compile(r"^(?P<key>[A-Z]+)(?:;[^:]+)?:(?P<val>.+)$")


def _parse_dt(value: str, *, all_day: bool) -> datetime:
    """Parse ICS DATE / DATE-TIME into tz-aware UTC datetime.

    Forms supported:
        20260506        — date-only (all-day)
        20260506T093000Z — UTC datetime
        20260506T093000  — local/floating; assumed UTC for v1
    """
    value = value.strip()
    if all_day or len(value) == 8:
        d = date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
        return datetime.combine(d, time.min, tzinfo=timezone.utc)
    is_utc = value.endswith("Z")
    if is_utc:
        value = value[:-1]
    dt = datetime(
        int(value[0:4]), int(value[4:6]), int(value[6:8]),
        int(value[9:11]), int(value[11:13]), int(value[13:15] or "0"),
    )
    return dt.replace(tzinfo=timezone.utc)


def parse_ics(text: str) -> List[CalEvent]:
    """Parse a raw ICS body into a list of CalEvent. Recurring events are
    only returned for their original DTSTART (no expansion of RRULE)."""
    events: List[CalEvent] = []
    in_event = False
    cur: dict = {}
    for line in _unfold_lines(text):
        if line == "BEGIN:VEVENT":
            in_event = True
            cur = {}
            continue
        if line == "END:VEVENT":
            in_event = False
            try:
                summary = cur.get("SUMMARY", "")
                uid = cur.get("UID", "")
                start_raw, start_all_day = cur.get("DTSTART", ("", False))
                end_raw, end_all_day = cur.get("DTEND", ("", False))
                if not start_raw:
                    continue
                start = _parse_dt(start_raw, all_day=start_all_day)
                end = _parse_dt(end_raw, all_day=end_all_day) if end_raw else None
                events.append(CalEvent(
                    uid=uid,
                    summary=summary,
                    start=start,
                    end=end,
                    all_day=start_all_day,
                    location=cur.get("LOCATION", ""),
                ))
            except Exception as e:
                logger.debug("ics parse skipped event: %s", e)
            continue
        if not in_event:
            continue

        # KEY[;PARAM=...]:VALUE
        if ":" not in line:
            continue
        key_section, _, value = line.partition(":")
        key, _, params = key_section.partition(";")
        key = key.strip().upper()

        if key in {"SUMMARY", "UID", "LOCATION"}:
            cur[key] = value.strip()
        elif key in {"DTSTART", "DTEND"}:
            all_day = "VALUE=DATE" in params.upper()
            cur[key] = (value.strip(), all_day)
    return events


class CalendarIcsClient:
    """Tiny async ICS fetcher."""

    def __init__(self, ics_url: str) -> None:
        self._url = ics_url
        self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_events(self) -> List[CalEvent]:
        resp = await self._client.get(self._url)
        resp.raise_for_status()
        return parse_ics(resp.text)

    @staticmethod
    def events_in_range(
        events: Iterable[CalEvent],
        start: datetime,
        end: datetime,
    ) -> List[CalEvent]:
        return sorted(
            (e for e in events if start <= e.start < end),
            key=lambda e: e.start,
        )
