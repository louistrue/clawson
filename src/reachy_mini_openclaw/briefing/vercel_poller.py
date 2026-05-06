from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional, Set

from ..mcp_clients.vercel import Deployment, VercelClient
from .backoff import Backoff
from .events import Event, EventBus, EventSeverity
from .persistence import load_vercel_seen, save_vercel_seen

logger = logging.getLogger(__name__)


VERCEL_POLL_INTERVAL = 60   # plan §config: poll_interval_seconds = 60
TERMINAL_STATES = {"READY", "ERROR"}


def _deployment_to_event(d: Deployment) -> Optional[Event]:
    """Map a deployment in a terminal state to an Event."""
    if d.state == "READY":
        kind = "vercel_deploy_success"
        severity = EventSeverity.INFO
        target = f" ({d.target})" if d.target else ""
        summary = f"{d.project_name}{target} deployed: {d.url}"
    elif d.state == "ERROR":
        kind = "vercel_deploy_fail"
        severity = EventSeverity.NORMAL
        target = f" ({d.target})" if d.target else ""
        summary = f"{d.project_name}{target} deploy failed"
    else:
        return None

    fingerprint = f"vercel:{kind}:{d.uid}"
    return Event(
        source="vercel",
        kind=kind,
        summary=summary,
        link=d.inspector_url or f"https://{d.url}" if d.url else "",
        ts=d.created_at,
        fingerprint=fingerprint,
        severity=severity,
        raw={
            "project": d.project_name,
            "deployment_id": d.uid,
            "state": d.state,
            "target": d.target,
        },
    )


class VercelPoller:
    """Polls Vercel deployments and emits state-transition events.

    Tracks already-seen deployment UIDs in memory (phase-6 polish will
    persist this across restarts). On startup we backfill the seen set
    with the current snapshot so we don't fire a flurry of historical
    events; only deployments observed AFTER startup emit Events.
    """

    def __init__(
        self,
        client: VercelClient,
        bus: EventBus,
        *,
        poll_interval_s: float = VERCEL_POLL_INTERVAL,
        persist: bool = True,
    ) -> None:
        self._client = client
        self._bus = bus
        self._poll_interval = poll_interval_s
        # Restored seen-set means a restart doesn't re-emit historical events
        # already seen in a previous session.
        self._persist = persist
        self._seen: Set[str] = load_vercel_seen() if persist else set()
        self._warmed = False
        self._backoff = Backoff()

    async def warm_up(self) -> None:
        """Snapshot current deployments without emitting Events. We add the
        live snapshot to whatever was loaded from disk, so first-run still
        suppresses history."""
        try:
            current = await self._client.list_recent_deployments(limit=50)
        except Exception as e:
            logger.warning("vercel warm_up failed: %s", e)
            return
        self._seen.update(d.uid for d in current if d.state in TERMINAL_STATES)
        self._warmed = True
        if self._persist:
            save_vercel_seen(self._seen)
        logger.info(
            "vercel: armed (%d known deployments suppressed)", len(self._seen)
        )

    async def run(self, should_stop: Callable[[], bool]) -> None:
        await self.warm_up()
        while not should_stop():
            sleep_for = self._poll_interval
            try:
                deployments = await self._client.list_recent_deployments(limit=20)
                self._backoff.succeeded()
            except Exception as e:
                deployments = []
                sleep_for = self._backoff.failed()
                logger.warning(
                    "vercel poll failed (attempt %d, backing off %.0fs): %s",
                    self._backoff.fails, sleep_for, e,
                )
            new_uids: list[str] = []
            for d in deployments:
                if d.state not in TERMINAL_STATES:
                    continue
                if d.uid in self._seen:
                    continue
                self._seen.add(d.uid)
                new_uids.append(d.uid)
                ev = _deployment_to_event(d)
                if ev is not None:
                    await self._bus.publish(ev)
            if new_uids and self._persist:
                save_vercel_seen(self._seen)
            await asyncio.sleep(sleep_for)
