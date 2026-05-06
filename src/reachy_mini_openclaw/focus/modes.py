from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional


class FocusMode(str, Enum):
    DEEP = "deep"
    NORMAL = "normal"
    AVAILABLE = "available"
    SNOOZED = "snoozed"


_CYCLE_ORDER = (FocusMode.DEEP, FocusMode.NORMAL, FocusMode.AVAILABLE)


@dataclass
class FocusState:
    mode: FocusMode = FocusMode.NORMAL
    snooze_until: Optional[datetime] = None
    previous_mode: Optional[FocusMode] = None

    def cycle(self) -> FocusMode:
        if self.mode == FocusMode.SNOOZED:
            self._restore()
        try:
            idx = _CYCLE_ORDER.index(self.mode)
        except ValueError:
            idx = -1
        self.mode = _CYCLE_ORDER[(idx + 1) % len(_CYCLE_ORDER)]
        self.snooze_until = None
        self.previous_mode = None
        return self.mode

    def snooze(self, duration: timedelta, *, now: Optional[datetime] = None) -> datetime:
        now = now or datetime.now(timezone.utc)
        if self.mode != FocusMode.SNOOZED:
            self.previous_mode = self.mode
        self.mode = FocusMode.SNOOZED
        self.snooze_until = now + duration
        return self.snooze_until

    def maybe_expire(self, *, now: Optional[datetime] = None) -> bool:
        if self.mode != FocusMode.SNOOZED or self.snooze_until is None:
            return False
        now = now or datetime.now(timezone.utc)
        if now >= self.snooze_until:
            self._restore()
            return True
        return False

    def _restore(self) -> None:
        self.mode = self.previous_mode or FocusMode.NORMAL
        self.snooze_until = None
        self.previous_mode = None
