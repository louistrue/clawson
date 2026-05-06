"""ClawBody - Give your OpenClaw AI agent a physical robot body.

This module provides the main application that connects:
- OpenAI Realtime API for voice I/O (speech recognition + TTS)
- OpenClaw Gateway for AI intelligence (Clawson's brain)
- Reachy Mini robot for physical embodiment

Usage:
    # Console mode (direct audio)
    clawbody

    # With Gradio UI
    clawbody --gradio

    # With debug logging
    clawbody --debug
"""

import os
import sys
import time
import asyncio
import logging
import argparse
import threading
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

# Load environment from project root (override=True ensures .env takes precedence)
_project_root = Path(__file__).parent.parent.parent
load_dotenv(_project_root / ".env", override=True)

logger = logging.getLogger(__name__)


def setup_logging(debug: bool = False) -> None:
    """Configure logging for the application.
    
    Args:
        debug: Enable debug level logging
    """
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    
    # Reduce noise from libraries
    if not debug:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("openai").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.
    
    Returns:
        Parsed arguments namespace
    """
    parser = argparse.ArgumentParser(
        description="ClawBody - Give your OpenClaw AI agent a physical robot body",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Run in console mode
    clawbody

    # Run with Gradio web UI
    clawbody --gradio

    # Connect to specific robot
    clawbody --robot-name my-reachy

    # Use different OpenClaw gateway
    clawbody --gateway-url http://192.168.1.100:18790
        """
    )
    
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--gradio",
        action="store_true",
        help="Launch Gradio web UI instead of console mode"
    )
    parser.add_argument(
        "--robot-name",
        type=str,
        help="Robot name for connection (default: auto-discover)"
    )
    parser.add_argument(
        "--gateway-url",
        type=str,
        default=os.getenv("OPENCLAW_GATEWAY_URL", "ws://localhost:18789"),
        help="OpenClaw gateway URL (from OPENCLAW_GATEWAY_URL env or default)"
    )
    parser.add_argument(
        "--no-camera",
        action="store_true",
        help="Disable camera functionality"
    )
    parser.add_argument(
        "--no-openclaw",
        action="store_true",
        help="Disable OpenClaw integration"
    )
    parser.add_argument(
        "--no-face-tracking",
        action="store_true",
        help="Disable face tracking"
    )
    parser.add_argument(
        "--local-vision",
        action="store_true",
        help="Enable local vision processing with SmolVLM2"
    )
    parser.add_argument(
        "--profile",
        type=str,
        help="Custom personality profile to use"
    )
    
    return parser.parse_args()


class ClawBodyCore:
    """ClawBody core application controller.
    
    This class orchestrates all components:
    - Reachy Mini robot connection and movement control
    - OpenAI Realtime API for voice I/O
    - OpenClaw gateway bridge for AI intelligence
    - Audio input/output loops
    """
    
    def __init__(
        self,
        gateway_url: str = "ws://localhost:18789",
        robot_name: Optional[str] = None,
        enable_camera: bool = True,
        enable_openclaw: bool = True,
        robot: Optional["ReachyMini"] = None,
        external_stop_event: Optional[threading.Event] = None,
    ):
        """Initialize the application.
        
        Args:
            gateway_url: OpenClaw gateway URL
            robot_name: Optional robot name for connection
            enable_camera: Whether to enable camera functionality
            enable_openclaw: Whether to enable OpenClaw integration
            robot: Optional pre-initialized robot (for app framework)
            external_stop_event: Optional external stop event
        """
        from reachy_mini import ReachyMini
        from reachy_mini_openclaw.config import config
        from reachy_mini_openclaw.moves import MovementManager
        from reachy_mini_openclaw.audio.head_wobbler import HeadWobbler
        from reachy_mini_openclaw.openclaw_bridge import OpenClawBridge
        from reachy_mini_openclaw.tools.core_tools import ToolDependencies
        from reachy_mini_openclaw.openai_realtime import OpenAIRealtimeHandler
        from reachy_mini_openclaw.focus import (
            FocusController,
            make_robot_antenna_reader,
        )
        from reachy_mini_openclaw.briefing import EventBus
        from reachy_mini_openclaw.briefing.dispatcher import EventDispatcher
        from reachy_mini_openclaw.briefing.github_poller import GitHubPoller
        from reachy_mini_openclaw.briefing.vercel_poller import VercelPoller
        from reachy_mini_openclaw.mcp_clients.vercel import VercelClient
        from reachy_mini_openclaw.widget import WidgetServer
        from reachy_mini_openclaw.briefing.standup import (
            StandupRunner,
            format_rollup,
            is_within_active_hours,
        )
        from reachy_mini_openclaw.briefing.mutes import load_mutes
        from reachy_mini_openclaw.briefing.cost_tracker import CostTracker
        from reachy_mini_openclaw.briefing.triggers import (
            FaceDetectStandupTrigger,
            make_voice_trigger,
        )
        from reachy_mini_openclaw.briefing.event_log import EventLog
        from reachy_mini_openclaw.briefing.todoist_poller import TodoistPoller
        from reachy_mini_openclaw.briefing.calendar_poller import CalendarPoller
        from reachy_mini_openclaw.mcp_clients.todoist import TodoistClient
        from reachy_mini_openclaw.mcp_clients.calendar_ics import CalendarIcsClient
        from reachy_mini_openclaw.actions import (
            Action,
            ActionRegistry,
            ConfirmationSystem,
        )
        from reachy_mini_openclaw.focus.presence import PresenceAutoSnooze
        from reachy_mini_openclaw.focus.head_gesture import (
            HeadGestureDetector,
            HeadGestureEvent,
            make_head_joints_reader,
            make_imu_reader,
        )
        from reachy_mini_openclaw.focus.companion import CompanionPresence
        from reachy_mini_openclaw.focus.sleep_animator import SleepAnimator
        from reachy_mini_openclaw.mcp_clients.github import GitHubClient
        from reachy_mini_openclaw.clawson_config import load_clawson_config
        
        self.gateway_url = gateway_url
        self._external_stop_event = external_stop_event
        self._owns_robot = robot is None
        
        # Validate configuration
        errors = config.validate()
        if errors:
            for error in errors:
                logger.error("Config error: %s", error)
            sys.exit(1)
        
        # Connect to robot
        if robot is not None:
            self.robot = robot
            logger.info("Using provided Reachy Mini instance")
        else:
            logger.info("Connecting to Reachy Mini...")
            robot_kwargs = {}
            if robot_name:
                robot_kwargs["robot_name"] = robot_name
                
            try:
                self.robot = ReachyMini(**robot_kwargs)
            except TimeoutError as e:
                logger.error("Connection timeout: %s", e)
                logger.error("Check that the robot is powered on and reachable.")
                sys.exit(1)
            except Exception as e:
                logger.error("Robot connection failed: %s", e)
                sys.exit(1)
                
            logger.info("Connected to robot: %s", self.robot.client.get_status())
        
        # Initialize movement system
        logger.info("Initializing movement system...")
        self.movement_manager = MovementManager(current_robot=self.robot)
        self.head_wobbler = HeadWobbler(
            set_speech_offsets=self.movement_manager.set_speech_offsets
        )
        
        # Initialize OpenClaw bridge
        self.openclaw_bridge = None
        if enable_openclaw:
            logger.info("Initializing OpenClaw bridge...")
            self.openclaw_bridge = OpenClawBridge(
                gateway_url=gateway_url,
                gateway_token=config.OPENCLAW_TOKEN,
            )
        
        # Camera worker for video streaming and frame capture
        self.camera_worker = None
        self.head_tracker = None
        self.vision_manager = None
        
        if enable_camera:
            logger.info("Initializing camera worker...")
            from reachy_mini_openclaw.camera_worker import CameraWorker
            
            # Initialize head tracker for local face tracking
            if config.ENABLE_FACE_TRACKING:
                self.head_tracker = self._initialize_head_tracker(config.HEAD_TRACKER_TYPE)
            
            # Initialize camera worker with head tracker
            self.camera_worker = CameraWorker(
                reachy_mini=self.robot,
                head_tracker=self.head_tracker,
            )
            
            # Enable/disable head tracking based on whether we have a tracker
            self.camera_worker.set_head_tracking_enabled(self.head_tracker is not None)
            
            # Initialize local vision processor if enabled
            if config.ENABLE_LOCAL_VISION:
                self.vision_manager = self._initialize_vision_manager()
        
        # Create tool dependencies
        self.deps = ToolDependencies(
            movement_manager=self.movement_manager,
            head_wobbler=self.head_wobbler,
            robot=self.robot,
            camera_worker=self.camera_worker,
            openclaw_bridge=self.openclaw_bridge,
            vision_manager=self.vision_manager,
        )
        
        # Initialize OpenAI Realtime handler with OpenClaw bridge
        self.handler = OpenAIRealtimeHandler(
            deps=self.deps,
            openclaw_bridge=self.openclaw_bridge,
        )
        
        # State
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

        # Clawson config drives standup time, active hours, GitHub auth.
        self.clawson_cfg = load_clawson_config()

        # Daily voice budget — protects against runaway poller bugs.
        self.cost_tracker = CostTracker()

        # Action mode — confirmation system + registry for write tools.
        self.confirmation = ConfirmationSystem()
        self.action_registry = ActionRegistry()
        self.handler.confirmation = self.confirmation
        self.handler.clawson_actions = self.action_registry

        # While a confirmation is pending, pause face tracking so the head
        # doesn't drift as the user moves — that drift was producing
        # gyro impulses indistinguishable from a deliberate nod/shake.
        async def _face_pause_for_confirm(is_pending: bool) -> None:
            cw = self.camera_worker
            if cw is None:
                return
            try:
                cw.is_head_tracking_enabled = not is_pending
                logger.info(
                    "face tracking %s for pending confirmation",
                    "paused" if is_pending else "resumed",
                )
            except Exception as e:
                logger.debug("face-track toggle failed: %s", e)
        self.confirmation.add_listener(_face_pause_for_confirm)

        # Spoken announcement bridge — wired through OpenAIRealtimeHandler.say().
        async def _say(message: str) -> None:
            if not self.cost_tracker.tick():
                logger.warning(
                    "daily say cap reached (%d), skipping: %s",
                    self.cost_tracker.daily_max, message[:80],
                )
                return
            try:
                await self.handler.say(message)
            except Exception as e:
                logger.debug("say bridge failed: %s", e)

        # Sleep animator — queued sleep / hold / wake animations + face
        # tracking toggle. Single source of truth shared between the
        # FocusController on_change hook (mode → SNOOZED) and the
        # CompanionPresence absence loop (no face for 5 min).
        self.sleep_animator = SleepAnimator(
            movement_manager=self.movement_manager,
            camera_worker=self.camera_worker,
        )

        # Clawson focus controller — antenna input → mode state machine.
        async def _announce_focus(message: str) -> None:
            logger.info("[focus] %s", message)
            await _say(message)

        async def _on_focus_change(new_mode, previous_mode) -> None:
            logger.info("[focus] mode %s → %s", previous_mode.value, new_mode.value)
            from reachy_mini_openclaw.focus.modes import FocusMode as _FM
            if new_mode == _FM.SNOOZED and previous_mode != _FM.SNOOZED:
                await self.sleep_animator.enter_sleep(reason="snoozed")
            elif new_mode != _FM.SNOOZED and previous_mode == _FM.SNOOZED:
                await self.sleep_animator.exit_sleep(reason="unsnoozed")

        # The two cross-references (rollup → dispatcher, standup → standup_runner)
        # are wired after both objects exist; we close over self for the lookup.
        async def _on_rollup_request() -> None:
            queued = self.event_dispatcher.queued_events
            await _say(format_rollup(queued))

        async def _on_standup_request() -> None:
            await self.standup_runner.run_now()

        # Wiggle factory used by both head-gesture confirmation feedback
        # and the antenna confirmation feedback. Imported lazily so the
        # gestures module's reachy_mini deps stay scoped.
        from reachy_mini_openclaw.gestures import WiggleAntennaMove as _WiggleMove

        def _make_wiggle(side: str):
            pose = self.movement_manager.state.last_primary_pose
            head, ant, _yaw = pose if pose is not None else (None, (0.0, 0.0), 0.0)
            if head is None:
                return None
            return _WiggleMove(side, head, ant)

        async def _antenna_confirm_feedback(side: str) -> None:
            move = _make_wiggle(side)
            if move is None:
                return
            try:
                self.movement_manager.queue_move(move)
            except Exception as e:
                logger.debug("antenna confirm wiggle failed: %s", e)

        self.focus_controller = FocusController(
            position_reader=make_robot_antenna_reader(self.robot),
            on_announce=_announce_focus,
            on_change=_on_focus_change,
            on_rollup_request=_on_rollup_request,
            on_standup_request=_on_standup_request,
            confirmation=self.confirmation,
            on_antenna_confirm_feedback=_antenna_confirm_feedback,
        )

        # Event-pipeline announce hook (Phase 3): summary spoken when
        # AVAILABLE; in NORMAL the gesture is the whole signal.
        from reachy_mini_openclaw.briefing.events import Event as _ClawsonEvent

        async def _announce_event(event: "_ClawsonEvent") -> None:
            await _say(event.summary)

        # Clawson event pipeline: bus + dispatcher (always on),
        # GitHub poller (only if a token is configured).
        from datetime import datetime as _dt, timezone as _tz

        def _active_hours_now() -> bool:
            return is_within_active_hours(self.clawson_cfg.focus, _dt.now(_tz.utc))

        self.event_bus = EventBus()
        self.mute_list = load_mutes()
        self.event_log = EventLog()
        self.event_dispatcher = EventDispatcher(
            self.event_bus,
            focus_mode_provider=lambda: self.focus_controller.mode,
            movement_manager=self.movement_manager,
            on_announce=_announce_event,
            active_hours_provider=_active_hours_now,
            audio_sink=self.robot.media,
            mute_list=self.mute_list,
            event_log=self.event_log,
        )

        # Morning standup. Drains queued events from the dispatcher into a
        # spoken rollup at standup time on standup days.
        self.standup_runner = StandupRunner(
            self.clawson_cfg.focus,
            on_announce=_say,
            drain_queued=self.event_dispatcher.drain_queued,
        )

        # Voice command router: catches mode/snooze/standup/quiet/repeat
        # commands on user transcripts and dispatches them. Anything that
        # isn't a command falls through to the LLM normally.
        from reachy_mini_openclaw.briefing.voice_router import VoiceCommandRouter
        self.voice_router = VoiceCommandRouter(
            focus_controller=self.focus_controller,
            standup_runner=self.standup_runner,
            handler=self.handler,
            say=_say,
            event_dispatcher=self.event_dispatcher,
            focus_settings=self.clawson_cfg.focus,
        )
        self.handler.on_user_transcript = self.voice_router

        # Face-detect trigger: morning face appearance in 06:00–08:00ish window.
        self.face_trigger = FaceDetectStandupTrigger(
            camera_worker=self.camera_worker,
            standup_runner=self.standup_runner,
            focus_settings=self.clawson_cfg.focus,
        )

        # Desktop widget — localhost-only by default, optional via env.
        widget_disabled = os.getenv("CLAWSON_WIDGET_DISABLED", "").lower() in {"1", "true", "yes"}
        self.widget_server: Optional[Any] = None
        if not widget_disabled:
            self.widget_server = WidgetServer(
                self.focus_controller,
                self.event_dispatcher,
                self.standup_runner,
                self.clawson_cfg.focus,
                host=os.getenv("CLAWSON_WIDGET_HOST", "127.0.0.1"),
                port=int(os.getenv("CLAWSON_WIDGET_PORT", "7860")),
                mute_list=self.mute_list,
                event_log=self.event_log,
                event_bus=self.event_bus,
            )
        self.github_client: Optional[Any] = None
        self.github_poller: Optional[Any] = None
        if self.clawson_cfg.github_enabled:
            self.github_client = GitHubClient(self.clawson_cfg.github_token)
            self.github_poller = GitHubPoller(self.github_client, self.event_bus)
            logger.info("Clawson: GitHub poller armed")
        else:
            logger.info(
                "Clawson: GitHub disabled (no token in ~/.config/clawson/config.toml or $GITHUB_TOKEN)"
            )

        # Vercel poller: optional, off by default. Token gates startup.
        self.vercel_client: Optional[Any] = None
        self.vercel_poller: Optional[Any] = None
        if self.clawson_cfg.vercel_enabled:
            self.vercel_client = VercelClient(self.clawson_cfg.vercel_token)
            self.vercel_poller = VercelPoller(self.vercel_client, self.event_bus)
            logger.info("Clawson: Vercel poller armed")

        # Todoist poller + add-task action.
        self.todoist_client: Optional[Any] = None
        self.todoist_poller: Optional[Any] = None
        if self.clawson_cfg.todoist_enabled:
            self.todoist_client = TodoistClient(self.clawson_cfg.todoist_token)
            self.todoist_poller = TodoistPoller(self.todoist_client, self.event_bus)
            logger.info("Clawson: Todoist poller armed")
            # Register add-task as a confirm-gated action.
            async def _todoist_add(args: dict) -> dict:
                content = args.get("content") or ""
                due_string = args.get("due_string")
                if not content:
                    return {"status": "error", "error": "content required"}
                task = await self.todoist_client.create_task(
                    content, due_string=due_string,
                )
                return {"status": "ok", "id": task.id, "url": task.url}

            self.action_registry.register(Action(
                name="todoist_add_task",
                tool_spec={
                    "type": "function",
                    "name": "todoist_add_task",
                    "description": (
                        "Add a new Todoist task. Requires user confirmation via "
                        "antenna press before it executes."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "Task title",
                            },
                            "due_string": {
                                "type": "string",
                                "description": (
                                    "Natural-language due date, e.g. 'tomorrow at 9am'"
                                ),
                            },
                        },
                        "required": ["content"],
                    },
                },
                executor=_todoist_add,
                requires_confirmation=True,
                preview=lambda args: (
                    f"Add Todoist task: '{args.get('content', '?')}'"
                    + (f" due {args.get('due_string')}" if args.get("due_string") else "")
                ),
            ))

        # GitHub issue tools (read-only, no confirmation needed).
        if self.clawson_cfg.github_enabled and self.github_client is not None:
            async def _list_repo_issues(args: dict) -> dict:
                repo = (args.get("repo") or "").strip()
                if "/" not in repo:
                    return {"status": "error", "error": "repo must be 'owner/name'"}
                state = args.get("state") or "open"
                limit = int(args.get("limit") or 10)
                try:
                    issues = await self.github_client.list_repo_issues(
                        repo, state=state, per_page=min(max(limit, 1), 50),
                    )
                except Exception as e:
                    return {"status": "error", "error": str(e)}
                # Strip out PRs (GitHub returns them in /issues).
                issues = [i for i in issues if not i.is_pull_request][:limit]
                return {
                    "status": "ok",
                    "repo": repo,
                    "state": state,
                    "count": len(issues),
                    "issues": [
                        {
                            "number": i.number,
                            "title": i.title,
                            "labels": i.labels,
                            "assignee": i.assignee_login,
                            "url": i.html_url,
                            "updated_at": i.updated_at.isoformat(),
                        }
                        for i in issues
                    ],
                }

            self.action_registry.register(Action(
                name="list_repo_issues",
                tool_spec={
                    "type": "function",
                    "name": "list_repo_issues",
                    "description": (
                        "List GitHub issues for one specific repository. Use "
                        "for 'open issues in owner/repo', 'what's in repo X', "
                        "'show me issues for X'. Read-only, no confirmation."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "repo": {
                                "type": "string",
                                "description": "owner/name format, e.g. 'louistrue/clawson'",
                            },
                            "state": {
                                "type": "string",
                                "enum": ["open", "closed", "all"],
                                "default": "open",
                            },
                            "limit": {
                                "type": "integer",
                                "default": 10,
                                "description": "max issues to return (1-50)",
                            },
                        },
                        "required": ["repo"],
                    },
                },
                executor=_list_repo_issues,
                requires_confirmation=False,
            ))

            async def _list_my_open_issues(args: dict) -> dict:
                filt = (args.get("filter") or "assigned")
                state = args.get("state") or "open"
                limit = int(args.get("limit") or 20)
                try:
                    issues = await self.github_client.list_my_issues(
                        filter_str=filt, state=state,
                        per_page=min(max(limit, 1), 100),
                    )
                except Exception as e:
                    return {"status": "error", "error": str(e)}
                issues = [i for i in issues if not i.is_pull_request][:limit]
                return {
                    "status": "ok",
                    "filter": filt,
                    "state": state,
                    "count": len(issues),
                    "issues": [
                        {
                            "repo": i.repo,
                            "number": i.number,
                            "title": i.title,
                            "labels": i.labels,
                            "url": i.html_url,
                            "updated_at": i.updated_at.isoformat(),
                        }
                        for i in issues
                    ],
                }

            self.action_registry.register(Action(
                name="list_my_open_issues",
                tool_spec={
                    "type": "function",
                    "name": "list_my_open_issues",
                    "description": (
                        "List GitHub issues across ALL repositories that "
                        "involve the user (assigned, created, mentioned). Use "
                        "for 'what issues do I have', 'what's on my plate', "
                        "cross-repo issue queries. Read-only, no confirmation."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filter": {
                                "type": "string",
                                "enum": ["assigned", "created", "mentioned",
                                         "subscribed", "all"],
                                "default": "assigned",
                            },
                            "state": {
                                "type": "string",
                                "enum": ["open", "closed", "all"],
                                "default": "open",
                            },
                            "limit": {
                                "type": "integer",
                                "default": 20,
                            },
                        },
                    },
                },
                executor=_list_my_open_issues,
                requires_confirmation=False,
            ))

            async def _open_repo(args: dict) -> dict:
                repo = (args.get("repo") or "").strip()
                if "/" not in repo:
                    return {"status": "error", "error": "repo must be 'owner/name'"}
                url = f"https://github.com/{repo}"
                # Drop a clickable into the widget recent panel.
                try:
                    from reachy_mini_openclaw.briefing.events import (
                        Event as _Ev, EventSeverity as _Sev,
                    )
                    from datetime import datetime as _dt, timezone as _tz
                    self.event_dispatcher._recent_events.append(_Ev(
                        source="self", kind="link",
                        summary=f"Opened repo {repo}",
                        link=url, ts=_dt.now(_tz.utc),
                        fingerprint=f"self:open:{repo}:{int(_dt.now().timestamp())}",
                        severity=_Sev.INFO,
                    ))
                except Exception:
                    pass
                return {"status": "ok", "repo": repo, "url": url}

            self.action_registry.register(Action(
                name="open_repo",
                tool_spec={
                    "type": "function",
                    "name": "open_repo",
                    "description": (
                        "Surface a specific GitHub repo to the user (URL "
                        "logged into widget). Use for 'open louistrue/clawson', "
                        "'show me the X repo'. Read-only, no confirmation."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "repo": {
                                "type": "string",
                                "description": "owner/name format",
                            },
                        },
                        "required": ["repo"],
                    },
                },
                executor=_open_repo,
                requires_confirmation=False,
            ))

        # Calendar poller (ICS feed).
        self.calendar_client: Optional[Any] = None
        self.calendar_poller: Optional[Any] = None
        if self.clawson_cfg.calendar_enabled:
            self.calendar_client = CalendarIcsClient(self.clawson_cfg.calendar_ics_url)
            self.calendar_poller = CalendarPoller(self.calendar_client, self.event_bus)
            logger.info("Clawson: Calendar poller armed")

        # Presence-aware auto-snooze.
        self.presence_auto_snooze = PresenceAutoSnooze(
            focus_controller=self.focus_controller,
            camera_worker=self.camera_worker,
            focus_settings=self.clawson_cfg.focus,
            is_within_active_hours=is_within_active_hours,
        )

        # Companion presence: dozes / sleeps when no face is around,
        # wiggles antennas when a face reappears.
        self.companion_presence = CompanionPresence(
            camera_worker=self.camera_worker,
            movement_manager=self.movement_manager,
            focus_settings=self.clawson_cfg.focus,
            wiggle_factory=_make_wiggle,
            on_say=_say,
            sleep_animator=self.sleep_animator,
        )

        # Head-gesture detector (nod = yes, shake = no).
        # Snappy feedback path: queue a single-antenna wiggle FIRST so the
        # user sees acknowledgement within ~10ms, then handle semantics
        # (confirmation route or spoken ack) async without blocking.
        from reachy_mini_openclaw.gestures import WiggleAntennaMove as _WiggleMove

        async def _on_head_gesture(ev: "HeadGestureEvent") -> None:
            # Head gestures are confirmation-only, same as antennas.
            # Without a pending question we ignore them entirely — the
            # face tracker rotates the head all the time, and we don't
            # want every face-tracker movement to wiggle antennas /
            # speak "yes" / "no". Mode/snooze/standup all go through
            # voice + widget instead.
            if self.confirmation is None or not self.confirmation.has_pending:
                logger.debug("head gesture %s ignored (no pending confirmation)", ev.kind)
                return
            logger.info("head gesture: %s (confirming)", ev.kind)
            # Snappy visual ack — right antenna for YES, left for NO.
            try:
                pose = self.movement_manager.state.last_primary_pose
                if pose is not None:
                    head, ant, _yaw = pose
                    side = "right" if ev.kind == "nod" else "left"
                    self.movement_manager.queue_move(_WiggleMove(side, head, ant))
            except Exception as e:
                logger.debug("wiggle queue failed: %s", e)
            if ev.kind == "nod":
                self.confirmation.confirm()
            elif ev.kind == "shake":
                self.confirmation.deny()

        self.head_gesture_detector = HeadGestureDetector(
            imu_reader=make_imu_reader(self.robot),
            head_joints_reader=make_head_joints_reader(self.robot),
            on_event=_on_head_gesture,
        )
        
    def _initialize_vision_manager(self) -> Optional[Any]:
        """Initialize local vision processor (SmolVLM2).
        
        Returns:
            VisionManager instance or None if initialization fails
        """
        if self.camera_worker is None:
            logger.warning("Cannot initialize vision manager without camera worker")
            return None
        
        try:
            from reachy_mini_openclaw.vision.processors import (
                VisionConfig, 
                initialize_vision_manager,
            )
            from reachy_mini_openclaw.config import config
            
            vision_config = VisionConfig(
                model_path=config.LOCAL_VISION_MODEL,
                device_preference=config.VISION_DEVICE,
                hf_home=config.HF_HOME,
            )
            
            logger.info("Initializing local vision processor (SmolVLM2)...")
            vision_manager = initialize_vision_manager(self.camera_worker, vision_config)
            
            if vision_manager is not None:
                logger.info("Local vision processor initialized")
            else:
                logger.warning("Local vision processor failed to initialize")
            
            return vision_manager
            
        except ImportError as e:
            logger.warning(f"Local vision not available: {e}")
            logger.warning("Install with: pip install torch transformers")
            return None
        except Exception as e:
            logger.error(f"Failed to initialize vision manager: {e}")
            return None
    
    def _initialize_head_tracker(self, tracker_type: Optional[str] = None) -> Optional[Any]:
        """Initialize head tracker for local face tracking.
        
        Args:
            tracker_type: Type of tracker ("yolo", "mediapipe", or None for auto)
            
        Returns:
            Initialized head tracker or None if initialization fails
        """
        # Default to YOLO if not specified
        if tracker_type is None:
            tracker_type = "yolo"
        
        if tracker_type == "yolo":
            try:
                from reachy_mini_openclaw.vision.yolo_head_tracker import HeadTracker
                logger.info("Initializing YOLO face tracker...")
                tracker = HeadTracker(device="cpu")  # CPU is fast enough for face detection
                logger.info("YOLO face tracker initialized")
                return tracker
            except ImportError as e:
                logger.warning(f"YOLO tracker not available: {e}")
                logger.warning("Install with: pip install ultralytics supervision")
            except Exception as e:
                logger.error(f"Failed to initialize YOLO tracker: {e}")
        
        elif tracker_type == "mediapipe":
            try:
                from reachy_mini_openclaw.vision.mediapipe_tracker import HeadTracker
                logger.info("Initializing MediaPipe face tracker...")
                tracker = HeadTracker()
                logger.info("MediaPipe face tracker initialized")
                return tracker
            except ImportError as e:
                logger.warning(f"MediaPipe tracker not available: {e}")
            except Exception as e:
                logger.error(f"Failed to initialize MediaPipe tracker: {e}")
        
        logger.warning("No face tracker available - face tracking disabled")
        return None
        
    def _should_stop(self) -> bool:
        """Check if we should stop."""
        if self._stop_event.is_set():
            return True
        if self._external_stop_event is not None and self._external_stop_event.is_set():
            return True
        return False
        
    async def record_loop(self) -> None:
        """Read audio from robot microphone and send to handler."""
        input_sr = self.robot.media.get_input_audio_samplerate()
        logger.info("Recording at %d Hz", input_sr)
        
        while not self._should_stop():
            audio_frame = self.robot.media.get_audio_sample()
            if audio_frame is not None:
                await self.handler.receive((input_sr, audio_frame))
            await asyncio.sleep(0.01)
            
    async def play_loop(self) -> None:
        """Play audio from handler through robot speakers."""
        output_sr = self.robot.media.get_output_audio_samplerate()
        logger.info("Playing at %d Hz", output_sr)
        
        while not self._should_stop():
            output = await self.handler.emit()
            if output is not None:
                if isinstance(output, tuple):
                    input_sr, audio_data = output
                    
                    # Convert to float32 and normalize (OpenAI sends int16)
                    audio_data = audio_data.flatten().astype("float32") / 32768.0
                    
                    # Reduce volume to prevent distortion (0.5 = 50% volume)
                    audio_data = audio_data * 0.5
                    
                    # Resample if needed
                    if input_sr != output_sr:
                        from scipy.signal import resample
                        num_samples = int(len(audio_data) * output_sr / input_sr)
                        audio_data = resample(audio_data, num_samples).astype("float32")
                        
                    self.robot.media.push_audio_sample(audio_data)
                # else: it's an AdditionalOutputs (transcript) - handle in UI mode
                
            await asyncio.sleep(0.01)
            
    async def run(self) -> None:
        """Run the main application loop."""
        # Test OpenClaw connection
        if self.openclaw_bridge is not None:
            connected = await self.openclaw_bridge.connect()
            if connected:
                logger.info("OpenClaw gateway connected")
            else:
                logger.warning("OpenClaw gateway not available - some features disabled")
        
        # Enable motors and move to neutral pose
        logger.info("Enabling motors and moving to neutral position...")
        try:
            self.robot.enable_motors()
            from reachy_mini.utils import create_head_pose
            neutral = create_head_pose(0, 0, 0, 0, 0, 0, degrees=True)
            self.robot.goto_target(
                head=neutral,
                antennas=[0.0, 0.0],
                duration=2.0,
                body_yaw=0.0,
            )
            time.sleep(2)  # Wait for goto to complete
            logger.info("Robot at neutral position with motors enabled")
        except Exception as e:
            logger.error("Failed to initialize robot pose: %s", e)
        
        # Wire up camera worker to movement manager for face tracking
        if self.camera_worker is not None:
            self.movement_manager.camera_worker = self.camera_worker
            logger.info("Face tracking connected to movement system")
        
        # Start movement system
        logger.info("Starting movement system...")
        self.movement_manager.start()
        self.head_wobbler.start()
        
        # Start camera worker for video streaming
        if self.camera_worker is not None:
            logger.info("Starting camera worker...")
            self.camera_worker.start()
        
        # Start local vision processor if available
        if self.vision_manager is not None:
            logger.info("Starting local vision processor...")
            self.vision_manager.start()
        
        # Start audio
        logger.info("Starting audio...")
        self.robot.media.start_recording()
        self.robot.media.start_playing()
        time.sleep(1)  # Let pipelines initialize
        
        logger.info("Ready! Speak to me...")
        
        # Start OpenAI handler in background
        handler_task = asyncio.create_task(self.handler.start_up(), name="openai-handler")
        
        # Start audio loops + focus controller (antenna input → state machine)
        # + Clawson event pipeline (bus dispatcher always; GitHub poller if armed).
        self._tasks = [
            handler_task,
            asyncio.create_task(self.record_loop(), name="record-loop"),
            asyncio.create_task(self.play_loop(), name="play-loop"),
            asyncio.create_task(
                self.focus_controller.run(self._should_stop), name="focus-controller"
            ),
            asyncio.create_task(
                self.event_dispatcher.run(self._should_stop), name="event-dispatcher"
            ),
            asyncio.create_task(
                self.standup_runner.run(self._should_stop), name="standup-runner"
            ),
            asyncio.create_task(
                self.face_trigger.run(self._should_stop), name="face-detect-trigger"
            ),
        ]
        if self.github_poller is not None:
            self._tasks.append(
                asyncio.create_task(
                    self.github_poller.run(self._should_stop), name="github-poller"
                )
            )
        if self.vercel_poller is not None:
            self._tasks.append(
                asyncio.create_task(
                    self.vercel_poller.run(self._should_stop), name="vercel-poller"
                )
            )
        if self.todoist_poller is not None:
            self._tasks.append(
                asyncio.create_task(
                    self.todoist_poller.run(self._should_stop), name="todoist-poller"
                )
            )
        if self.calendar_poller is not None:
            self._tasks.append(
                asyncio.create_task(
                    self.calendar_poller.run(self._should_stop), name="calendar-poller"
                )
            )
        self._tasks.append(
            asyncio.create_task(
                self.presence_auto_snooze.run(self._should_stop), name="presence"
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self.companion_presence.run(self._should_stop), name="companion"
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self.head_gesture_detector.run_until(self._should_stop),
                name="head-gesture",
            )
        )
        if self.widget_server is not None:
            self._tasks.append(
                asyncio.create_task(
                    self.widget_server.run(self._should_stop), name="widget-server"
                )
            )
        
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            logger.info("Tasks cancelled")
            
    def stop(self) -> None:
        """Stop everything."""
        logger.info("Stopping...")
        self._stop_event.set()
        
        # Cancel tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
                
        # Stop movement system
        self.head_wobbler.stop()
        self.movement_manager.stop()

        # Close Clawson HTTP clients.
        for client_attr in (
            "github_client", "vercel_client", "todoist_client", "calendar_client",
        ):
            client = getattr(self, client_attr, None)
            if client is None:
                continue
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(client.aclose())
                loop.close()
            except Exception as e:
                logger.warning("%s close failed: %s", client_attr, e)
        
        # Stop vision manager
        if self.vision_manager is not None:
            self.vision_manager.stop()
        
        # Stop camera worker
        if self.camera_worker is not None:
            self.camera_worker.stop()
        
        # Disconnect OpenClaw bridge
        if self.openclaw_bridge is not None:
            try:
                asyncio.get_event_loop().run_until_complete(
                    self.openclaw_bridge.disconnect()
                )
            except Exception as e:
                logger.debug("OpenClaw disconnect: %s", e)
        
        # Close resources if we own them
        if self._owns_robot:
            try:
                self.robot.media.close()
            except Exception as e:
                logger.debug("Media close: %s", e)
            self.robot.client.disconnect()
            
        logger.info("Stopped")


class ClawBodyApp:
    """ClawBody - Reachy Mini Apps entry point.
    
    This class allows ClawBody to be installed and run from
    the Reachy Mini dashboard as a Reachy Mini App.
    """
    
    # No custom settings UI
    custom_app_url: Optional[str] = None
    
    def run(self, reachy_mini, stop_event: threading.Event) -> None:
        """Run ClawBody as a Reachy Mini App.
        
        Args:
            reachy_mini: Pre-initialized ReachyMini instance
            stop_event: Threading event to signal stop
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        gateway_url = os.getenv("OPENCLAW_GATEWAY_URL", "ws://localhost:18789")
        
        app = ClawBodyCore(
            gateway_url=gateway_url,
            robot=reachy_mini,
            external_stop_event=stop_event,
        )
        
        try:
            loop.run_until_complete(app.run())
        except Exception as e:
            logger.error("Error running app: %s", e)
        finally:
            app.stop()
            loop.close()


def main() -> None:
    """Main entry point."""
    args = parse_args()
    setup_logging(args.debug)
    
    # Set custom profile if specified
    if args.profile:
        from reachy_mini_openclaw.config import set_custom_profile
        set_custom_profile(args.profile)
    
    # Configure face tracking and local vision from args
    from reachy_mini_openclaw.config import (
        set_face_tracking_enabled, 
        set_local_vision_enabled,
    )
    if args.no_face_tracking:
        set_face_tracking_enabled(False)
    if args.local_vision:
        set_local_vision_enabled(True)
    
    if args.gradio:
        # Launch Gradio UI
        logger.info("Starting Gradio UI...")
        from reachy_mini_openclaw.gradio_app import launch_gradio
        launch_gradio(
            gateway_url=args.gateway_url,
            robot_name=args.robot_name,
            enable_camera=not args.no_camera,
            enable_openclaw=not args.no_openclaw,
        )
    else:
        # Console mode
        app = ClawBodyCore(
            gateway_url=args.gateway_url,
            robot_name=args.robot_name,
            enable_camera=not args.no_camera,
            enable_openclaw=not args.no_openclaw,
        )
        
        try:
            asyncio.run(app.run())
        except KeyboardInterrupt:
            logger.info("Interrupted")
        finally:
            app.stop()


if __name__ == "__main__":
    main()
