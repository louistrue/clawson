from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Action:
    """A tool the LLM can call. Write actions go through ConfirmationSystem.

    `tool_spec` is the JSON the realtime API sees (name + description +
    parameters). `executor` runs the action and returns a JSON-serialisable
    dict. `preview` renders a human-readable string for the confirmation
    prompt — short, ideally fits in one breath of TTS.
    """

    name: str
    tool_spec: Dict[str, Any]                       # OpenAI tool schema
    executor: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]
    requires_confirmation: bool = False
    preview: Callable[[Dict[str, Any]], str] = lambda args: ""  # noqa: E731


class ActionRegistry:
    """Maps tool name → Action. Empty registry is fine (zero registered
    Clawson actions ⇒ realtime handler falls through to upstream dispatch).
    """

    def __init__(self) -> None:
        self._by_name: Dict[str, Action] = {}

    def register(self, action: Action) -> None:
        if action.name in self._by_name:
            logger.warning("action %r is being overwritten", action.name)
        self._by_name[action.name] = action

    def get(self, name: str) -> Optional[Action]:
        return self._by_name.get(name)

    def names(self) -> List[str]:
        return list(self._by_name.keys())

    def tool_specs(self) -> List[Dict[str, Any]]:
        """All tool specs (read + write)."""
        return [a.tool_spec for a in self._by_name.values()]
