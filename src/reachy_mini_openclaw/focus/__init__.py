"""Focus modes + antenna input for Clawson.

Pure state-machine and antenna-event modules. The orchestrator
(`controller.FocusController`) wires antenna events into mode transitions
and persists state to ~/.config/clawson/state.json.
"""

from .modes import FocusMode, FocusState
from .antennas import AntennaEvent, AntennaPoller, make_robot_antenna_reader
from .controller import FocusController
from .store import DEFAULT_STATE_PATH, load_state, save_state

__all__ = [
    "FocusMode",
    "FocusState",
    "AntennaEvent",
    "AntennaPoller",
    "FocusController",
    "DEFAULT_STATE_PATH",
    "load_state",
    "save_state",
    "make_robot_antenna_reader",
]
