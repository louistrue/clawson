"""Daily cost cap for Clawson's out-of-band voice announcements.

OpenAI Realtime audio output is roughly $0.06/min. This tracker counts
say() invocations per day and refuses to fire once the daily cap is hit
— back-pressure that protects against runaway poller bugs without
silencing user-initiated conversation, which never goes through tick().

State persists to XDG_STATE_HOME so a restart in the same calendar day
keeps the count.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "clawson"


DEFAULT_USAGE_PATH = _state_root() / "usage.json"
DEFAULT_DAILY_MAX_SAYS = 200


@dataclass
class CostTracker:
    daily_max: int = DEFAULT_DAILY_MAX_SAYS
    path: Path = DEFAULT_USAGE_PATH
    today: Optional[str] = None
    today_count: int = 0
    _warned_high: bool = False

    def __post_init__(self) -> None:
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text())
        except Exception as e:
            logger.warning("cost tracker: could not parse %s: %s", self.path, e)
            return
        self.today = payload.get("date")
        self.today_count = int(payload.get("count", 0) or 0)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps({"date": self.today, "count": self.today_count}))
            tmp.replace(self.path)
        except Exception as e:
            logger.debug("cost tracker save failed: %s", e)

    def _ensure_today(self) -> None:
        today_iso = date.today().isoformat()
        if self.today != today_iso:
            self.today = today_iso
            self.today_count = 0
            self._warned_high = False

    def tick(self) -> bool:
        """Increment the daily counter. Return True if the announcement
        should be allowed; False if the cap is exhausted."""
        self._ensure_today()
        if self.today_count >= self.daily_max:
            return False
        self.today_count += 1
        self._save()
        if self.today_count >= int(self.daily_max * 0.9) and not self._warned_high:
            self._warned_high = True
            logger.warning(
                "cost tracker: %d/%d daily announcements used (90%% threshold)",
                self.today_count, self.daily_max,
            )
        return True

    @property
    def remaining(self) -> int:
        self._ensure_today()
        return max(0, self.daily_max - self.today_count)
