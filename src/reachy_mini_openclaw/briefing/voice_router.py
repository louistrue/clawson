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

# 'sleep' is the user-preferred token (clearer phonetically, less STT
# drift than 'snooze', and reads naturally as a sleep-pose command).
# Snooze and its STT mishears stay in for backwards compat.
_SNOOZE_TOKEN = (
    r"(?:sleep|go to sleep|sleeping"
    r"|snooze|snews|snows|snuze|snuse|snoos|sloose|sluice"
    r"|on the snooze|the snooze)"
)
_SNOOZE_15 = re.compile(rf"\b{_SNOOZE_TOKEN}\b.*\b(15|fifteen)\b", re.I)
_SNOOZE_1H = re.compile(rf"\b{_SNOOZE_TOKEN}\b.*\b(1 hour|one hour|hour|60)\b", re.I)
_SNOOZE_4H = re.compile(rf"\b{_SNOOZE_TOKEN}\b.*\b(4 hours?|four hours?|240)\b", re.I)
_SNOOZE_BARE = re.compile(rf"\b{_SNOOZE_TOKEN}\b(?!.*?(?:cancel|stop))", re.I)
_UNSNOOZE = re.compile(
    r"\b(wake up|wake|unsnooze|cancel snooze|stop snoozing|end snooze"
    r"|stop sleeping|end sleep|no more sleep)\b", re.I
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
    r"\b(restart yourself|reboot yourself|restart clawson|reboot clawson"
    r"|reload yourself|kick yourself|^restart\.?$|^reboot\.?$)\b",
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
_OPEN_PRS = re.compile(
    r"\b(open (my )?p\.?r\.?s?|show (my )?p\.?r\.?s?|my pull requests|open pull requests)\b",
    re.I,
)
_WHATS_BROKEN = re.compile(
    r"\b(what'?s? broken|what is broken|whats? broken|recent failures|last failures|any failures)\b",
    re.I,
)
# 'focus on owner/repo' — captures the slash-separated repo path.
_FOCUS_REPO = re.compile(
    r"\bfocus on (?:repo )?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b", re.I,
)
_UNFOCUS = re.compile(
    r"\b(unfocus|stop focusing|clear focus|focus on everything)\b", re.I,
)
# 'timer N (minutes|seconds|hours)' — captures number + unit.
_TIMER = re.compile(
    r"\btimer (\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b", re.I,
)
_DRAFT_COMMIT = re.compile(
    r"\b(draft (a )?commit( message)?|commit message|write (a )?commit)\b", re.I,
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

        if _OPEN_PRS.search(t):
            logger.info("voice: open PRs")
            await self._open_prs()
            return True

        if _WHATS_BROKEN.search(t):
            logger.info("voice: what's broken")
            await self._whats_broken()
            return True

        m = _FOCUS_REPO.search(t)
        if m:
            repo = m.group(1)
            logger.info("voice: focus on %s", repo)
            await self._focus_on_repo(repo)
            return True

        if _UNFOCUS.search(t):
            logger.info("voice: unfocus")
            await self._unfocus_repo()
            return True

        m = _TIMER.search(t)
        if m:
            n, unit = int(m.group(1)), m.group(2).lower()
            seconds = n
            if unit.startswith(("min",)):
                seconds = n * 60
            elif unit.startswith(("hour", "hr")):
                seconds = n * 3600
            logger.info("voice: timer %ds", seconds)
            await self._set_timer(seconds, n, unit)
            return True

        if _DRAFT_COMMIT.search(t):
            logger.info("voice: draft commit")
            await self._draft_commit_message(t)
            return True

        # No match → return False so handler fires LLM response.create.
        return False

    # ------------------------------------------------------------------
    # Action helpers (kept here so the dispatch table stays scannable)
    # ------------------------------------------------------------------

    async def _open_prs(self) -> None:
        """Speak the GitHub PR review URL. The widget can also open it
        in the user's browser via the announce text — a future Mac-side
        helper can pop this in webbrowser.open() automatically."""
        url = "https://github.com/pulls/review-requested"
        if self._say is not None:
            await self._say("Opening your reviews. Link is in the widget.")
        # Stash on the dispatcher so the widget can show it.
        if self._dispatcher is not None:
            try:
                from .events import Event, EventSeverity
                from datetime import datetime, timezone
                ev = Event(
                    source="self", kind="link", summary="Your GitHub reviews",
                    link=url, ts=datetime.now(timezone.utc),
                    fingerprint=f"self:link:prs:{int(datetime.now().timestamp())}",
                    severity=EventSeverity.INFO,
                )
                self._dispatcher._recent_events.append(ev)
            except Exception as e:
                logger.debug("link injection failed: %s", e)

    async def _whats_broken(self) -> None:
        if self._dispatcher is None or self._say is None:
            return
        recent = list(self._dispatcher.recent_events)[-50:]
        fails = [
            e for e in recent
            if e.kind in ("ci_fail", "vercel_deploy_fail")
        ][-3:]
        if not fails:
            await self._say("Nothing broken in the recent window.")
            return
        parts = [f"{len(fails)} recent {'failure' if len(fails) == 1 else 'failures'}."]
        for ev in reversed(fails):  # newest first
            parts.append(ev.summary + ".")
        await self._say(" ".join(parts))

    async def _focus_on_repo(self, repo: str) -> None:
        """Set a soft focus filter — only events from `repo` pass through."""
        # Stored on the dispatcher as a single-allowed-repo override.
        if self._dispatcher is not None:
            self._dispatcher._focus_repo = repo  # type: ignore[attr-defined]
            self._dispatcher._focus_repo_until = (
                __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                + __import__("datetime").timedelta(hours=1)
            )
        if self._say is not None:
            await self._say(f"Focused on {repo} for one hour.")

    async def _unfocus_repo(self) -> None:
        if self._dispatcher is not None:
            self._dispatcher._focus_repo = None  # type: ignore[attr-defined]
        if self._say is not None:
            await self._say("Focus cleared.")

    async def _set_timer(self, seconds: int, n: int, unit: str) -> None:
        if self._say is not None:
            await self._say(f"Timer set for {n} {unit}.")
        import asyncio

        async def _fire() -> None:
            await asyncio.sleep(seconds)
            if self._say is not None:
                await self._say(f"Timer up — {n} {unit} elapsed.")

        asyncio.create_task(_fire(), name=f"timer-{seconds}s")

    async def _draft_commit_message(self, transcript: str) -> None:
        """Ask OpenClaw for a commit message draft based on recent context."""
        bridge = getattr(self._handler, "openclaw_bridge", None)
        if bridge is None or not getattr(bridge, "is_connected", False):
            if self._say is not None:
                await self._say("OpenClaw isn't connected, can't draft.")
            return
        prompt = (
            "Draft a single short git commit message (one line, imperative "
            "mood, under 70 chars) based on what we've been discussing. "
            "Reply with only the commit message text, no quotes or "
            "explanation."
        )
        try:
            resp = await bridge.chat(prompt)
        except Exception as e:
            logger.warning("draft commit chat failed: %s", e)
            if self._say is not None:
                await self._say("Couldn't reach OpenClaw for the draft.")
            return
        msg = (resp.content or "").strip().splitlines()[0] if resp and resp.content else ""
        if not msg:
            if self._say is not None:
                await self._say("OpenClaw didn't return a draft.")
            return
        if self._say is not None:
            await self._say(f"Draft commit: {msg}")

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
