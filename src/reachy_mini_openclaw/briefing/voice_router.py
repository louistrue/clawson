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
_RESTART = re.compile(
    r"\b(restart yourself|reboot yourself|restart clawson|reboot clawson|reload yourself|kick yourself)\b",
    re.I,
)
_STATUS = re.compile(
    r"\b(status report|status check|how are you doing|are you (alive|there)|sit rep|sitrep)\b",
    re.I,
)
_WHAT_TIME = re.compile(
    r"\bwhat('?s)? (the )?time\b|\b(current time|tell me the time)\b", re.I,
)
_WHAT_MODE = re.compile(
    r"\bwhat('?s)? (the )?mode\b|\b(current mode|which mode|what mode am i in)\b", re.I,
)
_CLEAR_QUEUE = re.compile(
    r"\b(clear (the )?queue|empty (the )?queue|drop queued|forget queued)\b", re.I,
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
        event_dispatcher: Any = None,
        focus_settings: Any = None,
    ) -> None:
        self._focus = focus_controller
        self._standup = standup_runner
        self._handler = handler
        self._say = say
        self._dispatcher = event_dispatcher
        self._focus_settings = focus_settings

    async def __call__(self, transcript: str) -> bool:
        """Return True if the transcript matched a command (and was handled),
        False otherwise. The realtime handler uses this to decide whether
        to fire the LLM auto-response."""
        if not transcript:
            return False
        try:
            return await self._dispatch(transcript)
        except Exception as e:
            logger.exception("voice command router failed: %s", e)
            return False

    async def _dispatch(self, t: str) -> bool:
        # Snooze cancel must beat plain snooze regex.
        if _UNSNOOZE.search(t):
            logger.info("voice: unsnooze")
            await self._focus.request_unsnooze()
            return True

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
            return True

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
            return True

        if _STANDUP.search(t):
            logger.info("voice: standup")
            await self._standup.run_now()
            return True

        if _QUIET.search(t):
            logger.info("voice: quiet")
            try:
                await self._handler.cancel_speaking()
            except Exception as e:
                logger.debug("cancel_speaking failed: %s", e)
            return True

        if _REPEAT.search(t):
            logger.info("voice: repeat")
            try:
                await self._handler.repeat_last_say()
            except Exception as e:
                logger.debug("repeat_last_say failed: %s", e)
            return True

        if _STATUS.search(t):
            logger.info("voice: status")
            await self._speak_status()
            return True

        if _WHAT_MODE.search(t):
            logger.info("voice: what mode")
            if self._say is not None:
                await self._say(f"Mode is {self._focus.mode.value}.")
            return True

        if _WHAT_TIME.search(t):
            logger.info("voice: what time")
            await self._speak_time()
            return True

        if _CLEAR_QUEUE.search(t):
            logger.info("voice: clear queue")
            cleared = 0
            if self._dispatcher is not None:
                cleared = len(self._dispatcher.drain_queued())
            if self._say is not None:
                await self._say(
                    f"Cleared {cleared} queued events."
                    if cleared else "Queue is already empty."
                )
            return True

        if _RESTART.search(t):
            logger.info("voice: restart")
            if self._say is not None:
                await self._say("Restarting now.")
            # Schedule the exec on the next tick so the say() can flush.
            import asyncio
            asyncio.get_event_loop().call_later(1.0, _restart_self)
            return True

        # No match → return False so handler fires LLM response.create.
        return False

    async def _speak_status(self) -> None:
        if self._say is None:
            return
        mode = self._focus.mode.value
        snooze_until = self._focus.state.snooze_until
        queued = len(self._dispatcher.queued_events) if self._dispatcher else 0
        recent = len(self._dispatcher.recent_events) if self._dispatcher else 0
        parts = [f"Mode {mode}."]
        if snooze_until is not None:
            from datetime import datetime, timezone
            tz = self._focus_settings.tzinfo() if self._focus_settings else timezone.utc
            local = snooze_until.astimezone(tz)
            parts.append(f"Snooze until {local.strftime('%H:%M')}.")
        if queued:
            parts.append(f"{queued} queued.")
        if recent:
            parts.append(f"{recent} recent.")
        if not queued and not recent:
            parts.append("No recent events.")
        await self._say(" ".join(parts))

    async def _speak_time(self) -> None:
        if self._say is None:
            return
        from datetime import datetime, timezone
        tz = self._focus_settings.tzinfo() if self._focus_settings else timezone.utc
        now = datetime.now(tz)
        await self._say(now.strftime("It is %H:%M, %A."))


def _restart_self() -> None:
    """Replace the current process image with a fresh exec of the same
    command line. PID is preserved; in-flight WebSockets close ungracefully
    but the new process opens fresh ones."""
    import logging as _logging
    import os as _os
    import sys as _sys
    _logging.getLogger(__name__).warning("restart requested — exec'ing fresh process")
    _os.execv(_sys.executable, [_sys.executable, *_sys.argv])
