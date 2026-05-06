"""Action mode — write-tool gating with antenna confirmation.

When the LLM calls a tool that mutates external state (add Todoist
task, send a message, mark something done), Clawson interrupts the
flow and asks for an antenna confirmation. Right tap = go ahead,
left tap = cancel, no tap = timeout-cancel after N seconds. The
voice loop only sees the executed result (or a "user cancelled"
sentinel) so the LLM behaves correctly downstream.

Read-only tools (queries, lookups) skip confirmation entirely.
"""

from .confirm import ConfirmationSystem
from .registry import Action, ActionRegistry

__all__ = ["Action", "ActionRegistry", "ConfirmationSystem"]
