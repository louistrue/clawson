"""On-disk persistence for poller state.

Filters and 'seen' sets sit in memory, which means a restart re-emits
stale events that fired before shutdown. This module gives each poller
a tiny JSON file under XDG_STATE_HOME (~/.local/state/clawson/) so we
can reload the last-known truth at startup.

Atomic writes (write tempfile + replace) so a crash mid-write can't
leave a half-written file behind.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from .filters import GitHubFilterState

logger = logging.getLogger(__name__)


def _state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "clawson"


GITHUB_STATE_PATH = _state_root() / "github_filters.json"
VERCEL_STATE_PATH = _state_root() / "vercel_seen.json"


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    tmp.replace(path)


# ----------------------------------------------------------------------
# GitHub filter state
# ----------------------------------------------------------------------
def save_github_state(
    state: GitHubFilterState,
    notifications_since: Optional[datetime],
    path: Path = GITHUB_STATE_PATH,
) -> None:
    payload = {
        "last_ci_conclusion": [
            [list(k), v] for k, v in state.last_ci_conclusion.items()
        ],
        "last_ci_fail_at": [
            [list(k), v.isoformat()] for k, v in state.last_ci_fail_at.items()
        ],
        "notifications_since": (
            notifications_since.isoformat() if notifications_since else None
        ),
    }
    try:
        _atomic_write(path, json.dumps(payload, indent=2))
    except Exception as e:
        logger.warning("github state save failed: %s", e)


def load_github_state(
    path: Path = GITHUB_STATE_PATH,
) -> Tuple[GitHubFilterState, Optional[datetime]]:
    state = GitHubFilterState()
    if not path.exists():
        return state, None
    try:
        payload = json.loads(path.read_text())
    except Exception as e:
        logger.warning("github state read failed: %s", e)
        return state, None
    for key, value in payload.get("last_ci_conclusion") or []:
        if isinstance(key, list) and len(key) == 2:
            state.last_ci_conclusion[(key[0], key[1])] = value
    for key, ts_iso in payload.get("last_ci_fail_at") or []:
        if isinstance(key, list) and len(key) == 2:
            try:
                state.last_ci_fail_at[(key[0], key[1])] = datetime.fromisoformat(ts_iso)
            except ValueError:
                continue
    since_iso = payload.get("notifications_since")
    notifications_since: Optional[datetime] = None
    if since_iso:
        try:
            notifications_since = datetime.fromisoformat(since_iso)
        except ValueError:
            notifications_since = None
    return state, notifications_since


# ----------------------------------------------------------------------
# Vercel seen-set
# ----------------------------------------------------------------------
def save_vercel_seen(seen: Iterable[str], path: Path = VERCEL_STATE_PATH) -> None:
    try:
        _atomic_write(path, json.dumps({"seen": list(seen)}, indent=2))
    except Exception as e:
        logger.warning("vercel state save failed: %s", e)


def load_vercel_seen(path: Path = VERCEL_STATE_PATH) -> set[str]:
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text())
        return set(payload.get("seen") or [])
    except Exception as e:
        logger.warning("vercel state read failed: %s", e)
        return set()
