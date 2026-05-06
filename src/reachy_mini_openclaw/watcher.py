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

# Tunables match host_presence.py in the main process so the two
# observers see the world the same way (60 s grace, 10 s poll, 3 s
# probe timeout). If you bump one, bump both.
POLL_INTERVAL_S = 10.0
GRACE_S = 60.0
PROBE_TIMEOUT_S = 3.0

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
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=PROBE_TIMEOUT_S,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except (asyncio.TimeoutError, OSError):
        return False


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
        "armed: probing %s:%d every %ds, grace %ds",
        host, port, int(POLL_INTERVAL_S), int(GRACE_S),
    )
    unreachable_since: Optional[float] = None
    state = "unknown"
    while True:
        ok = await probe(host, port)
        active = is_clawson_active()
        now = time.monotonic()
        if ok:
            if unreachable_since is not None:
                logger.info("host reachable again after %.0fs", now - unreachable_since)
            unreachable_since = None
            if state != "online":
                logger.info("host: %s → online (clawson active=%s)", state, active)
                state = "online"
            if not active:
                start_clawson()
        else:
            if unreachable_since is None:
                unreachable_since = now
                logger.info("host probe failed; grace period begins (%ds)", int(GRACE_S))
            elapsed = now - unreachable_since
            if elapsed >= GRACE_S and active:
                logger.info(
                    "host offline for %.0fs (>= %ds); stopping clawson",
                    elapsed, int(GRACE_S),
                )
                stop_clawson()
                if state != "offline":
                    state = "offline"
        await asyncio.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("watcher exiting on KeyboardInterrupt")
        sys.exit(0)
