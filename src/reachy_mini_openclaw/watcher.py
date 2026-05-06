"""Tiny watchdog that owns the lifecycle of the main `clawson` service.

Runs as its own systemd unit (`clawson-watcher.service`), separate from
`clawson.service` itself. It TCP-probes the OpenClaw gateway every
POLL_S seconds and:

  - When the host has been unreachable for >= GRACE_S and clawson is
    running   →   `systemctl stop clawson`   (which sends SIGTERM,
                                              triggering the off-pose
                                              move + clean shutdown)

  - When the host is reachable and clawson is NOT running
                →   `systemctl start clawson`

By having two services we get a clean separation: `clawson` itself
exits fully when the desk PC is off (no idle CPU, no idle realtime
session), and only the watcher (a few KB of resident memory) keeps
poking the network.

systemctl invocations require sudo. We rely on a sudoers drop-in at
/etc/sudoers.d/clawson-watcher that allows the `pollen` user to run
exactly `systemctl {start,stop,is-active} clawson` with no password.

This module is intentionally standalone: no imports from the main
clawson package so the watcher can run from a tiny venv if ever
needed. Only Python stdlib + tcp probing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from typing import Optional, Tuple
from urllib.parse import urlparse

# Tunables. The big one is STABLE_ONLINE_PROBES — see comment below.
POLL_INTERVAL_S = 10.0
GRACE_S = 10.0                  # offline grace before stopping clawson
TCP_CONNECT_TIMEOUT_S = 3.0     # how long to wait for the SYN-ACK
APP_RESPONSE_TIMEOUT_S = 2.0    # how long to wait for actual HTTP bytes
# Hold-off for "online" — require this many consecutive successful
# L7 probes before declaring the host back online and starting clawson.
# Reason: macOS Power Nap and Wake-for-Network-Access can briefly bring
# Tailscale + the OpenClaw process up for ~10–30 s every few minutes
# while the lid is closed. A single successful probe in that window
# would otherwise restart clawson, which is exactly what the user just
# saw flap them back on. Three consecutive probes = ~30 s of sustained
# liveness, longer than any Power Nap window.
STABLE_ONLINE_PROBES = 3
# Hold-off after a stop — refuse to start clawson within this many
# seconds of the last stop, even if the host appears online. Belt and
# braces against immediate flap.
START_AFTER_STOP_HOLDOFF_S = 30.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] watcher: %(message)s",
)
logger = logging.getLogger("clawson-watcher")


def parse_endpoint(url: str) -> Tuple[str, int]:
    p = urlparse(url)
    host = p.hostname or "127.0.0.1"
    port = p.port
    if port is None:
        scheme = (p.scheme or "").lower()
        port = 443 if scheme in {"wss", "https"} else 80
    return host, int(port)


async def probe(host: str, port: int) -> bool:
    """L7 probe: open TCP, send a HEAD request, require ANY response.

    Why L7 and not just TCP-connect: when macOS is asleep (with Power
    Nap or Wake-for-Network-Access on), the kernel's TCP stack can
    complete SYN-ACK handshakes for ports it has open — but the
    application behind those ports is suspended and won't read or
    respond to actual data. A pure TCP-connect probe would falsely
    report 'online'. Forcing an L7 round trip filters that out: if
    OpenClaw is actually running it'll respond to a stray HTTP HEAD
    with 400 / 405 / 426 (the WebSocket-upgrade endpoint complains
    about non-upgrade requests). If the process is suspended, the
    socket goes silent and we time out.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=TCP_CONNECT_TIMEOUT_S,
        )
    except (asyncio.TimeoutError, OSError):
        return False
    try:
        # Minimal HTTP request. We don't care what status the server
        # sends back — just that *something* comes back, proving the
        # application thread is live.
        writer.write(b"HEAD / HTTP/1.0\r\nHost: probe\r\n\r\n")
        await writer.drain()
        data = await asyncio.wait_for(
            reader.read(64), timeout=APP_RESPONSE_TIMEOUT_S
        )
        return len(data) > 0
    except (asyncio.TimeoutError, OSError):
        return False
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def _systemctl(*args: str) -> Tuple[int, str]:
    """Run `sudo systemctl <args>`. Returns (returncode, stdout+stderr).
    Quiet on success, logs on failure."""
    try:
        r = subprocess.run(
            ["sudo", "-n", "systemctl", *args],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 127, str(e)


def is_clawson_active() -> bool:
    rc, out = _systemctl("is-active", "clawson")
    return rc == 0 and out.strip() == "active"


def start_clawson() -> None:
    rc, out = _systemctl("start", "clawson")
    if rc == 0:
        logger.info("started clawson")
    else:
        logger.warning("start clawson failed (rc=%d): %s", rc, out.strip()[:200])


def stop_clawson() -> None:
    # `systemctl stop` blocks until the service is fully stopped (or
    # TimeoutStopSec fires), so by the time this returns the off-pose
    # move has played out and the process has exited.
    rc, out = _systemctl("stop", "clawson")
    if rc == 0:
        logger.info("stopped clawson")
    else:
        logger.warning("stop clawson failed (rc=%d): %s", rc, out.strip()[:200])


async def main() -> None:
    gateway_url = os.getenv("OPENCLAW_GATEWAY_URL", "ws://localhost:18789")
    host, port = parse_endpoint(gateway_url)
    logger.info(
        "armed: probing %s:%d every %ds, offline grace %ds, "
        "online stability %d probes (~%ds), post-stop hold-off %ds",
        host, port,
        int(POLL_INTERVAL_S),
        int(GRACE_S),
        STABLE_ONLINE_PROBES,
        int(STABLE_ONLINE_PROBES * POLL_INTERVAL_S),
        int(START_AFTER_STOP_HOLDOFF_S),
    )
    unreachable_since: Optional[float] = None
    consecutive_ok: int = 0
    last_stop_t: Optional[float] = None
    state: str = "unknown"

    while True:
        ok = await probe(host, port)
        active = is_clawson_active()
        now = time.monotonic()

        if ok:
            consecutive_ok += 1
            if unreachable_since is not None:
                logger.info(
                    "host probe succeeded after %.0fs offline; "
                    "need %d consecutive to declare online (have %d)",
                    now - unreachable_since,
                    STABLE_ONLINE_PROBES,
                    consecutive_ok,
                )
                unreachable_since = None

            if consecutive_ok >= STABLE_ONLINE_PROBES:
                # Sustained connectivity. Safe to flip to online.
                if state != "online":
                    logger.info(
                        "host: %s → online (after %d consecutive probes)",
                        state, consecutive_ok,
                    )
                    state = "online"
                if not active:
                    # Respect the post-stop hold-off so we don't flap
                    # back on within seconds of having just stopped.
                    if (
                        last_stop_t is not None
                        and now - last_stop_t < START_AFTER_STOP_HOLDOFF_S
                    ):
                        remaining = START_AFTER_STOP_HOLDOFF_S - (now - last_stop_t)
                        logger.info(
                            "host online but in post-stop hold-off "
                            "(%.0fs remaining); not starting yet",
                            remaining,
                        )
                    else:
                        start_clawson()
            # else: building up confidence; keep clawson stopped if it is.
        else:
            consecutive_ok = 0
            if unreachable_since is None:
                unreachable_since = now
                logger.info(
                    "host probe failed; grace period begins (%ds)",
                    int(GRACE_S),
                )
            elapsed = now - unreachable_since
            if elapsed >= GRACE_S and active:
                logger.info(
                    "host offline for %.0fs (>= %ds); stopping clawson",
                    elapsed, int(GRACE_S),
                )
                stop_clawson()
                last_stop_t = time.monotonic()
                if state != "offline":
                    state = "offline"

        await asyncio.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("watcher exiting on KeyboardInterrupt")
        sys.exit(0)
