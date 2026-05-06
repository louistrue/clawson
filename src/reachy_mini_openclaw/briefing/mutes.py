"""Per-source mute list for the dispatcher.

A mute targets either a github repo (`owner/name`) or a vercel project
(`project-name`). Muted events still go to the queued list so the rollup
and widget can surface them, but they don't trigger a gesture / cue /
announce.

State at ~/.local/state/clawson/mutes.json. Widget endpoints (phase 6)
read and edit it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Optional, Set

from .events import Event

logger = logging.getLogger(__name__)


def _state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "clawson"


DEFAULT_MUTES_PATH = _state_root() / "mutes.json"


@dataclass
class MuteList:
    """Per-source mute keys (repo full names / project names)."""
    by_source: Dict[str, Set[str]] = field(default_factory=dict)

    def add(self, source: str, key: str) -> None:
        self.by_source.setdefault(source, set()).add(key)

    def remove(self, source: str, key: str) -> bool:
        existing = self.by_source.get(source)
        if not existing or key not in existing:
            return False
        existing.remove(key)
        if not existing:
            self.by_source.pop(source, None)
        return True

    def is_muted(self, event: Event) -> bool:
        keys = self.by_source.get(event.source)
        if not keys:
            return False
        # source-specific mute key
        if event.source == "github":
            target = (event.raw or {}).get("repo")
        elif event.source == "vercel":
            target = (event.raw or {}).get("project")
        else:
            target = None
        return bool(target and target in keys)

    def to_dict(self) -> dict:
        return {src: sorted(keys) for src, keys in self.by_source.items()}

    @classmethod
    def from_dict(cls, raw: dict) -> "MuteList":
        out = cls()
        for src, keys in (raw or {}).items():
            if isinstance(keys, list):
                out.by_source[str(src)] = set(str(k) for k in keys)
        return out


def load_mutes(path: Path = DEFAULT_MUTES_PATH) -> MuteList:
    if not path.exists():
        return MuteList()
    try:
        return MuteList.from_dict(json.loads(path.read_text()))
    except Exception as e:
        logger.warning("mutes: could not parse %s: %s", path, e)
        return MuteList()


def save_mutes(mutes: MuteList, path: Path = DEFAULT_MUTES_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(mutes.to_dict(), indent=2))
    tmp.replace(path)
