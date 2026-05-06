from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from .modes import FocusMode, FocusState

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path.home() / ".config" / "clawson" / "state.json"


def _serialize(state: FocusState) -> dict:
    return {
        "mode": state.mode.value,
        "snooze_until": state.snooze_until.isoformat() if state.snooze_until else None,
        "previous_mode": state.previous_mode.value if state.previous_mode else None,
    }


def _deserialize(raw: dict) -> FocusState:
    snooze_until_raw = raw.get("snooze_until")
    previous_raw = raw.get("previous_mode")
    return FocusState(
        mode=FocusMode(raw.get("mode", FocusMode.NORMAL.value)),
        snooze_until=datetime.fromisoformat(snooze_until_raw) if snooze_until_raw else None,
        previous_mode=FocusMode(previous_raw) if previous_raw else None,
    )


def load_state(path: Path = DEFAULT_STATE_PATH) -> FocusState:
    if not path.exists():
        return FocusState()
    try:
        return _deserialize(json.loads(path.read_text()))
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        logger.warning("Could not parse %s, using defaults: %s", path, e)
        return FocusState()


def save_state(state: FocusState, path: Path = DEFAULT_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_serialize(state), indent=2))
    tmp.replace(path)
