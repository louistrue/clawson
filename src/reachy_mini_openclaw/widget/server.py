from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import timedelta
from typing import Any, Callable, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from ..briefing.dispatcher import EventDispatcher
from ..briefing.event_log import EventLog
from ..briefing.events import Event, EventBus
from ..briefing.mutes import MuteList, save_mutes
from ..briefing.standup import StandupRunner, is_within_active_hours
from ..clawson_config import FocusSettings
from ..focus.controller import FocusController
from ..focus.modes import FocusMode
from .page import PAGE

logger = logging.getLogger(__name__)


def _serialise_event(ev: Event) -> dict:
    return {
        "source": ev.source,
        "kind": ev.kind,
        "summary": ev.summary,
        "link": ev.link,
        "ts": ev.ts.isoformat(),
        "severity": ev.severity.value,
        "fingerprint": ev.fingerprint,
    }


class WidgetServer:
    """In-process FastAPI server bound to 127.0.0.1 by default.

    Routes through the same FocusController/StandupRunner the antennas
    use, so a widget click and an antenna press are indistinguishable
    from downstream's point of view.
    """

    def __init__(
        self,
        focus_controller: FocusController,
        event_dispatcher: EventDispatcher,
        standup_runner: StandupRunner,
        focus_settings: FocusSettings,
        *,
        host: str = "127.0.0.1",
        port: int = 7860,
        mute_list: Optional[MuteList] = None,
        event_log: Optional[EventLog] = None,
        event_bus: Optional[EventBus] = None,
        host_presence: Optional[Any] = None,
    ) -> None:
        self._focus = focus_controller
        self._dispatcher = event_dispatcher
        self._standup = standup_runner
        self._focus_settings = focus_settings
        self._mute_list = mute_list
        self._event_log = event_log
        self._event_bus = event_bus
        self._host_presence = host_presence
        self._host = host
        self._port = port
        self._app = FastAPI(title="Clawson Widget", docs_url=None, redoc_url=None)
        self._register_routes()

    def _register_routes(self) -> None:
        app = self._app

        @app.get("/", response_class=HTMLResponse)
        @app.get("/widget", response_class=HTMLResponse)
        async def page() -> HTMLResponse:
            return HTMLResponse(PAGE)

        @app.get("/api/state")
        async def state() -> JSONResponse:
            from datetime import datetime, timezone
            s = self._focus.state
            host_state = "unknown"
            host_url = None
            if self._host_presence is not None:
                host_state = self._host_presence.state
                host_url = self._host_presence.gateway_url
            return JSONResponse({
                "mode": s.mode.value,
                "snooze_until": s.snooze_until.isoformat() if s.snooze_until else None,
                "previous_mode": s.previous_mode.value if s.previous_mode else None,
                "queued": [_serialise_event(e) for e in self._dispatcher.queued_events],
                "recent": [_serialise_event(e) for e in self._dispatcher.recent_events],
                "active_hours_now": is_within_active_hours(
                    self._focus_settings, datetime.now(timezone.utc)
                ),
                "host_presence": host_state,
                "host_gateway": host_url,
            })

        @app.post("/api/host/recheck")
        async def host_recheck() -> JSONResponse:
            if self._host_presence is None:
                raise HTTPException(status_code=404, detail="host presence not enabled")
            await self._host_presence.force_recheck()
            return JSONResponse({"ok": True, "state": self._host_presence.state})

        @app.post("/api/mode/cycle")
        async def mode_cycle() -> JSONResponse:
            new_mode = await self._focus.request_cycle()
            return JSONResponse({"mode": new_mode.value})

        @app.post("/api/mode/{name}")
        async def mode_set(name: str) -> JSONResponse:
            try:
                target = FocusMode(name)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"unknown mode {name!r}")
            if target == FocusMode.SNOOZED:
                raise HTTPException(status_code=400, detail="use /api/snooze to snooze")
            await self._focus.request_set_mode(target)
            return JSONResponse({"mode": target.value})

        @app.post("/api/snooze")
        async def snooze(req: Request) -> JSONResponse:
            body = {}
            try:
                body = await req.json()
            except Exception:
                pass
            minutes = int(body.get("minutes", 15))
            if minutes <= 0 or minutes > 24 * 60:
                raise HTTPException(status_code=400, detail="minutes must be 1..1440")
            await self._focus.request_snooze(timedelta(minutes=minutes))
            return JSONResponse({"snoozed_minutes": minutes})

        @app.post("/api/snooze/cancel")
        async def unsnooze() -> JSONResponse:
            await self._focus.request_unsnooze()
            return JSONResponse({"mode": self._focus.mode.value})

        @app.post("/api/standup")
        async def standup() -> JSONResponse:
            await self._standup.run_now()
            return JSONResponse({"ok": True})

        @app.post("/api/queue/clear")
        async def queue_clear() -> JSONResponse:
            drained = self._dispatcher.drain_queued()
            return JSONResponse({"cleared": len(drained)})

        @app.get("/api/mutes")
        async def mutes_get() -> JSONResponse:
            if self._mute_list is None:
                return JSONResponse({"mutes": {}})
            return JSONResponse({"mutes": self._mute_list.to_dict()})

        @app.post("/api/mutes")
        async def mutes_add(req: Request) -> JSONResponse:
            if self._mute_list is None:
                raise HTTPException(status_code=503, detail="mute list unavailable")
            body = {}
            try:
                body = await req.json()
            except Exception:
                pass
            source = body.get("source")
            key = body.get("key")
            if not source or not key:
                raise HTTPException(status_code=400, detail="source and key required")
            self._mute_list.add(source, key)
            try:
                save_mutes(self._mute_list)
            except Exception as e:
                logger.debug("save_mutes failed: %s", e)
            return JSONResponse({"mutes": self._mute_list.to_dict()})

        @app.delete("/api/mutes")
        async def mutes_remove(req: Request) -> JSONResponse:
            if self._mute_list is None:
                raise HTTPException(status_code=503, detail="mute list unavailable")
            body = {}
            try:
                body = await req.json()
            except Exception:
                pass
            source = body.get("source")
            key = body.get("key")
            if not source or not key:
                raise HTTPException(status_code=400, detail="source and key required")
            removed = self._mute_list.remove(source, key)
            try:
                save_mutes(self._mute_list)
            except Exception as e:
                logger.debug("save_mutes failed: %s", e)
            return JSONResponse({"removed": removed, "mutes": self._mute_list.to_dict()})

        @app.get("/api/log")
        async def log_recent(
            days: int = 1,
            source: Optional[str] = None,
            kind: Optional[str] = None,
            limit: int = 200,
        ) -> JSONResponse:
            if self._event_log is None:
                return JSONResponse({"events": []})
            events = self._event_log.read_recent(
                days=max(1, min(days, 14)),
                source=source,
                kind=kind,
                limit=max(1, min(limit, 1000)),
            )
            return JSONResponse({"events": [_serialise_event(e) for e in events]})

        @app.post("/api/log/replay")
        async def log_replay(req: Request) -> JSONResponse:
            if self._event_log is None or self._event_bus is None:
                raise HTTPException(status_code=503, detail="event log/bus unavailable")
            body = {}
            try:
                body = await req.json()
            except Exception:
                pass
            fingerprint = body.get("fingerprint")
            if not fingerprint:
                raise HTTPException(status_code=400, detail="fingerprint required")
            event = self._event_log.find_by_fingerprint(fingerprint)
            if event is None:
                raise HTTPException(status_code=404, detail="event not found")
            await self._event_bus.publish(event)
            return JSONResponse({"replayed": event.fingerprint})

        @app.get("/api/pending_confirmation")
        async def pending_confirm() -> JSONResponse:
            confirmation = getattr(self._focus, "_confirmation", None)
            if confirmation is None or not confirmation.has_pending:
                return JSONResponse({"pending": None})
            return JSONResponse({"pending": confirmation.pending_description})

        @app.post("/api/restart")
        async def restart_endpoint() -> JSONResponse:
            """Hard restart via os.execv. Returns 202 before the new
            process takes over; the next /api/state will be served by
            the fresh interpreter."""
            import asyncio as _asyncio
            from ..briefing.voice_router import _restart_self
            _asyncio.get_event_loop().call_later(0.5, _restart_self)
            return JSONResponse({"restarting": True}, status_code=202)

    async def run(self, should_stop: Callable[[], bool]) -> None:
        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=self._port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)

        async def _watcher() -> None:
            while not should_stop():
                await asyncio.sleep(1.0)
            server.should_exit = True

        watcher_task = asyncio.create_task(_watcher(), name="widget-watcher")
        try:
            await server.serve()
        finally:
            watcher_task.cancel()
