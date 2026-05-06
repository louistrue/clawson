"""Append-only JSONL log of every dispatched Event.

The dispatcher writes one line per event after dedup and before mode
gating, so the log captures the full delivered stream regardless of
whether the gesture/announce fired. This unlocks the widget's history
panel and a debug 'replay' endpoint.

Daily rotation by filename; old files stay on disk for the user to
prune. No size cap — this is small text and the path is theirs.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, List, Optional

from .events import Event, EventSeverity

logger = logging.getLogger(__name__)


def _state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "clawson" / "event_log"


DEFAULT_LOG_DIR = _state_root()


def _serialise(ev: Event) -> str:
    return json.dumps({
        "source": ev.source,
        "kind": ev.kind,
        "summary": ev.summary,
        "link": ev.link,
        "ts": ev.ts.isoformat(),
        "fingerprint": ev.fingerprint,
        "severity": ev.severity.value,
        "raw": ev.raw or None,
    })


def _deserialise(line: str) -> Optional[Event]:
    try:
        payload = json.loads(line)
        return Event(
            source=payload["source"],
            kind=payload["kind"],
            summary=payload["summary"],
            link=payload.get("link", ""),
            ts=datetime.fromisoformat(payload["ts"]),
            fingerprint=payload["fingerprint"],
            severity=EventSeverity(payload.get("severity", "normal")),
            raw=payload.get("raw"),
        )
    except Exception as e:
        logger.debug("event log: skipped malformed line: %s", e)
        return None


@dataclass
class EventLog:
    """Append-only JSONL log with daily file rotation."""
    log_dir: Path = DEFAULT_LOG_DIR

    def _path_for(self, when: datetime) -> Path:
        return self.log_dir / f"{when.astimezone(timezone.utc).date().isoformat()}.jsonl"

    def append(self, ev: Event) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with self._path_for(ev.ts).open("a", encoding="utf-8") as f:
                f.write(_serialise(ev))
                f.write("\n")
        except Exception as e:
            logger.debug("event log append failed: %s", e)

    def read_recent(
        self,
        *,
        days: int = 1,
        source: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 200,
    ) -> List[Event]:
        """Return events from the last `days` (newest first), filtered."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        out: List[Event] = []
        for d in self._iter_days(days):
            path = self.log_dir / f"{d.isoformat()}.jsonl"
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for line in lines:
                ev = _deserialise(line)
                if ev is None:
                    continue
                if ev.ts < cutoff:
                    continue
                if source is not None and ev.source != source:
                    continue
                if kind is not None and ev.kind != kind:
                    continue
                out.append(ev)
        out.sort(key=lambda e: e.ts, reverse=True)
        return out[:limit]

    def find_by_fingerprint(self, fingerprint: str, *, days: int = 7) -> Optional[Event]:
        """Return the most-recent event whose fingerprint matches."""
        for d in self._iter_days(days):
            path = self.log_dir / f"{d.isoformat()}.jsonl"
            if not path.exists():
                continue
            for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                ev = _deserialise(line)
                if ev is not None and ev.fingerprint == fingerprint:
                    return ev
        return None

    def _iter_days(self, days: int) -> Iterator[date]:
        today = datetime.now(timezone.utc).date()
        for delta in range(days):
            yield today - timedelta(days=delta)
