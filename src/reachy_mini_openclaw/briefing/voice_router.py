"""Voice-command router for Clawson.

The realtime handler hands every completed user transcript to this
router. We pattern-match against a curated command vocabulary:

    mode    → deep / normal / available
    snooze  → 15m / 1h / 4h, or cancel
    standup → run now (alias: rollup, what's queued, briefing)
    quiet   → cancel current TTS in flight
    repeat  → say the last announcement again

Anything that doesn't match a command falls through to the LLM as a
normal conversational turn — the router is non-destructive.

Matches are intentionally generous (substring, case-insensitive) since
STT often drops articles or punctuation. The single-fire guarantee
comes from the matched-action shortcut: once a command fires we return,
so the LLM doesn't ALSO answer the same utterance.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any, Awaitable, Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Compiled regexes — cheap-ish to evaluate per transcript at human speed.
_MODE_DEEP = re.compile(r"\b(deep mode|go deep|focus mode|deep focus|deep)\b", re.I)
_MODE_NORMAL = re.compile(r"\b(normal mode|normal|default mode)\b", re.I)
_MODE_AVAILABLE = re.compile(r"\b(available mode|available|open up|open mode)\b", re.I)

_SNOOZE_15 = re.compile(r"\bsnooze\b.*\b(15|fifteen)\b", re.I)
_SNOOZE_1H = re.compile(r"\bsnooze\b.*\b(1 hour|one hour|hour|60)\b", re.I)
_SNOOZE_4H = re.compile(r"\bsnooze\b.*\b(4 hours?|four hours?|240)\b", re.I)
_SNOOZE_BARE = re.compile(r"\bsnooze\b(?!.*?(?:cancel|stop))", re.I)
_UNSNOOZE = re.compile(
    r"\b(wake up|unsnooze|cancel snooze|stop snoozing|end snooze)\b", re.I
)

_STANDUP = re.compile(
    r"\b(standup|stand up|rollup|roll up|briefing|morning brief|what'?s queued|whats queued|what do i have)\b",
    re.I,
)
_QUIET = re.compile(
    r"\b(stop talking|shut up|be quiet|quiet please|stop speaking|cancel speaking|hush)\b",
    re.I,
)
_REPEAT = re.compile(
    r"\b(say again|repeat( that)?|what (was that|did you say)|sorry( what)?)\b",
    re.I,
)


class VoiceCommandRouter:
    """Routes completed user transcripts to focus/standup/handler actions.

    All callbacks are awaitable so the router can be plugged into the
    existing OpenAIRealtimeHandler.on_user_transcript hook without
    changing its signature.
    """

    def __init__(
        self,
        *,
        focus_controller: Any,
        standup_runner: Any,
        handler: Any,
        say: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        self._focus = focus_controller
        self._standup = standup_runner
        self._handler = handler
        self._say = say

    async def __call__(self, transcript: str) -> None:
        if not transcript:
            return
        try:
            await self._dispatch(transcript)
        except Exception as e:
            logger.exception("voice command router failed: %s", e)

    async def _dispatch(self, t: str) -> None:
        # Snooze cancel must beat plain snooze regex.
        if _UNSNOOZE.search(t):
            logger.info("voice: unsnooze")
            await self._focus.request_unsnooze()
            return

        if _SNOOZE_4H.search(t):
            logger.info("voice: snooze 4h")
            await self._focus.request_snooze(timedelta(hours=4), label="four hours")
            return
        if _SNOOZE_1H.search(t):
            logger.info("voice: snooze 1h")
            await self._focus.request_snooze(timedelta(hours=1), label="one hour")
            return
        if _SNOOZE_15.search(t):
            logger.info("voice: snooze 15m")
            await self._focus.request_snooze(timedelta(minutes=15), label="fifteen minutes")
            return
        if _SNOOZE_BARE.search(t):
            logger.info("voice: snooze bare → 15m")
            await self._focus.request_snooze(timedelta(minutes=15), label="fifteen minutes")
            return

        # Mode commands — order matters: 'deep' shouldn't fire if the
        # transcript was 'normal' (it isn't, but defensively the more-
        # specific match goes first).
        if _MODE_AVAILABLE.search(t):
            logger.info("voice: mode available")
            from ..focus.modes import FocusMode
            await self._focus.request_set_mode(FocusMode.AVAILABLE)
            return
        if _MODE_DEEP.search(t):
            logger.info("voice: mode deep")
            from ..focus.modes import FocusMode
            await self._focus.request_set_mode(FocusMode.DEEP)
            return
        if _MODE_NORMAL.search(t):
            logger.info("voice: mode normal")
            from ..focus.modes import FocusMode
            await self._focus.request_set_mode(FocusMode.NORMAL)
            return

        if _STANDUP.search(t):
            logger.info("voice: standup")
            await self._standup.run_now()
            return

        if _QUIET.search(t):
            logger.info("voice: quiet")
            try:
                await self._handler.cancel_speaking()
            except Exception as e:
                logger.debug("cancel_speaking failed: %s", e)
            return

        if _REPEAT.search(t):
            logger.info("voice: repeat")
            try:
                await self._handler.repeat_last_say()
            except Exception as e:
                logger.debug("repeat_last_say failed: %s", e)
            return

        # No match → let the LLM handle the turn normally.
