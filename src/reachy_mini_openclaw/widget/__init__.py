"""Desktop widget — FastAPI side-server bound to localhost.

Bookmarkable single-page UI at http://127.0.0.1:7860/widget that mirrors
the antenna controls (mode cycle, snooze, standup-now, queued events).
Routes through the same FocusController + StandupRunner as the antennas.
"""

from .server import WidgetServer

__all__ = ["WidgetServer"]
