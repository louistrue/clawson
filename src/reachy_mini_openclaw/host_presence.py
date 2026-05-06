"""Track whether the desk PC (the OpenClaw gateway host) is reachable.

Lifecycle goal: Clawson should run as long as the desk PC is on, and
quietly go to sleep when the PC is off. Implementation: poll a TCP
connection to the gateway host:port. After GRACE_S of unreachability
fire on_host_offline; on first reach again fire on_host_online.

Why TCP-connect rather than rely on `openclaw_bridge.is_connected`:
- The bridge does its own reconnect attempts that can succeed/fail in
  rapid succession during transient network blips. We want clean,
  debounced "PC is away vs PC is back" semantics.
- TCP-connect is protocol-agnostic — works whether the gateway speaks
  WebSocket-only or also serves HTTP on the same port.

The state machine has hysteresis: probe failures must persist for
GRACE_S before flipping to offline. Probe successes flip to online
immediately (no grace) so the user perceives "wake up" as instant when
they're back at the desk.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# Tuned for "desk PC sleeps and wakes" cadence:
#   POLL_INTERVAL_S — how often we open a TCP socket. Cheap (~ms).
#   GRACE_S         — how long unreachability must persist before
#                     flipping to offline. Avoids flapping during
#                     short network blips or PC sleep transitions.
#   PROBE_TIMEOUT_S — TCP connect timeout. Short so polls don't pile up.
POLL_INTERVAL_S = 10.0
GRACE_S = 10.0
PROBE_TIMEOUT_S = 3.0


class HostPresenceMonitor:
    """Background task that fires online/offline callbacks based on TCP
    reachability of the OpenClaw gateway host. Idempotent — repeated
    transitions to the same state don't re-fire callbacks.
    """

    def __init__(
        self,
        gateway_url: str,
        *,
        on_host_offline: Callable[[], Awaitable[None]],
        on_host_online: Callable[[], Awaitable[None]],
    ) -> None:
        self._gateway_url = gateway_url
        self._host, self._port = self._parse_endpoint(gateway_url)
        self._on_offline = on_host_offline
        self._on_online = on_host_online
        # State: "unknown" | "online" | "offline".
        # Starts "unknown" so the first probe result fires whichever
        # callback matches reality. We don't pre-bias to "online".
        self._state: str = "unknown"
        self._first_unreachable_t: Optional[float] = None
        self._last_probe_ok: Optional[bool] = None
        self._last_transition_t: Optional[float] = None
        self._wake_event = asyncio.Event()  # poked by force_recheck()

    @property
    def state(self) -> str:
        return self._state

    @property
    def last_probe_ok(self) -> Optional[bool]:
        return self._last_probe_ok

    @property
    def last_transition_t(self) -> Optional[float]:
        return self._last_transition_t

    @property
    def gateway_url(self) -> str:
        return self._gateway_url

    @staticmethod
    def _parse_endpoint(url: str) -> tuple[str, int]:
        """Extract (host, port) from ws[s]://host:port or http[s]://host:port.
        Falls back to scheme defaults if no explicit port.
        """
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port
        if port is None:
            scheme = (parsed.scheme or "").lower()
            port = 443 if scheme in {"wss", "https"} else 80
        return host, int(port)

    async def _probe(self) -> bool:
        """L7 probe: open TCP, send a HEAD request, require ANY response.

        Pure TCP-connect lies on macOS: when the laptop is asleep with
        Power Nap or Wake-for-Network-Access on, the kernel can complete
        SYN-ACK while the actual OpenClaw process is suspended. Forcing
        a small HTTP round trip filters that out — if the application
        thread isn't running, nobody reads our bytes and we time out.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=PROBE_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, OSError) as e:
            logger.debug(
                "host probe %s:%d connect failed: %s",
                self._host, self._port, e,
            )
            return False
        try:
            writer.write(b"HEAD / HTTP/1.0\r\nHost: probe\r\n\r\n")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(64), timeout=2.0)
            return len(data) > 0
        except (asyncio.TimeoutError, OSError) as e:
            logger.debug(
                "host probe %s:%d L7 failed: %s",
                self._host, self._port, e,
            )
            return False
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def force_recheck(self) -> None:
        """Skip the next sleep and run a probe immediately. Used by the
        widget 'Reconnect now' button so the user gets sub-second
        feedback instead of waiting up to POLL_INTERVAL_S."""
        self._wake_event.set()

    async def run(self, should_stop: Callable[[], bool]) -> None:
        logger.info(
            "host presence monitor armed (gateway=%s:%d, poll=%ds, grace=%ds)",
            self._host, self._port, int(POLL_INTERVAL_S), int(GRACE_S),
        )
        while not should_stop():
            ok = await self._probe()
            self._last_probe_ok = ok
            now = time.monotonic()
            try:
                if ok:
                    self._first_unreachable_t = None
                    if self._state != "online":
                        await self._transition("online")
                else:
                    if self._first_unreachable_t is None:
                        self._first_unreachable_t = now
                        logger.info(
                            "host probe failed; grace period begins (%ds)",
                            int(GRACE_S),
                        )
                    elif (
                        self._state != "offline"
                        and now - self._first_unreachable_t >= GRACE_S
                    ):
                        await self._transition("offline")
            except Exception as e:
                logger.exception("host presence transition failed: %s", e)
            # Sleep with early-wakeup support for force_recheck().
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(), timeout=POLL_INTERVAL_S
                )
                self._wake_event.clear()
                logger.debug("host presence: force_recheck fired")
            except asyncio.TimeoutError:
                pass

    async def _transition(self, new_state: str) -> None:
        prev = self._state
        self._state = new_state
        self._last_transition_t = time.monotonic()
        logger.info("host presence: %s → %s", prev, new_state)
        if new_state == "online":
            await self._on_online()
        elif new_state == "offline":
            await self._on_offline()
