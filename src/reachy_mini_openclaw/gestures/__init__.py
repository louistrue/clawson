"""Gesture vocabulary for event-triggered robot motion.

Each gesture is a Move subclass we can queue on MovementManager. The
dispatcher in `briefing` maps Event kinds to gesture factories so we
can add new event types without touching motion code.
"""

from .vocabulary import (
    HeadDroopMove,
    HappyBounceMove,
    SmallNodMove,
    gesture_for_event,
)

__all__ = [
    "HeadDroopMove",
    "HappyBounceMove",
    "SmallNodMove",
    "gesture_for_event",
]
