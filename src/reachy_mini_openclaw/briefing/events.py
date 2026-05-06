from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class EventSeverity(str, Enum):
    CRITICAL = "critical"   # interrupts even DEEP mode
    NORMAL = "normal"       # default
    INFO = "info"           # background; gentle in NORMAL, full in AVAILABLE


@dataclass(frozen=True)
class Event:
    """A normalised notification from any monitored source.

    `fingerprint` is the dedup key — the dispatcher drops repeats within a
    short window. `raw` carries the source-specific payload for debugging.
    """

    source: str           # "github" | "vercel" | …
    kind: str             # "ci_fail" | "pr_merged" | …
    summary: str
    link: str
    ts: datetime
    fingerprint: str
    severity: EventSeverity = EventSeverity.NORMAL
    raw: Optional[dict] = field(default=None, repr=False)

    @classmethod
    def now(cls, **kwargs) -> "Event":
        kwargs.setdefault("ts", datetime.now(timezone.utc))
        return cls(**kwargs)


class EventBus:
    """Single-producer/single-consumer in-memory bus.

    For phase-2 we have one producer (GitHub poller) and one consumer
    (EventDispatcher). When Vercel lands in phase-4 we'll add a second
    producer; the bus is unbounded so a slow consumer never blocks a
    poller, but events get debounced by the dispatcher before fan-out.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue()

    async def publish(self, event: Event) -> None:
        logger.debug("publish: %s/%s %s", event.source, event.kind, event.fingerprint)
        await self._queue.put(event)

    async def consume(self) -> Event:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    @property
    def pending(self) -> int:
        return self._queue.qsize()
