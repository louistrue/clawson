"""Cross-source event aggregation: GitHub today, Vercel next, more later.

The event bus is a single asyncio.Queue. Producers (pollers) push normalised
Events; the dispatcher consumes, applies focus-mode gating, then triggers a
gesture and (later) a TTS preview. New sources just need to emit Events with
a unique fingerprint; the dispatcher is source-agnostic.
"""

"""Briefing package — events, filters, dispatcher, pollers.

We deliberately avoid importing `dispatcher` here: it transitively pulls
in the Reachy Mini SDK via gestures.vocabulary, which is only available
on-robot. Callers should `from .dispatcher import EventDispatcher`
directly when they need it (after the SDK is on the path).
"""

from .events import Event, EventBus, EventSeverity
from .filters import GitHubFilterState, github_event_should_pass

__all__ = [
    "Event",
    "EventBus",
    "EventSeverity",
    "GitHubFilterState",
    "github_event_should_pass",
]
