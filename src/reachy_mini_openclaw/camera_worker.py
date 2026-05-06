"""Camera worker thread with frame buffering and face tracking.

Provides:
- 30Hz+ camera polling with thread-safe frame buffering
- Face tracking integration with smooth interpolation
- Room scanning when no face is detected
- Latest frame always available for tools
- Smooth return to neutral when face is lost

Based on pollen-robotics/reachy_mini_conversation_app camera worker.
"""

import time
import logging
import threading
from typing import Any, List, Tuple, Optional

import numpy as np
from numpy.typing import NDArray
from scipy.spatial.transform import Rotation as R

from reachy_mini import ReachyMini
from reachy_mini.utils.interpolation import linear_pose_interpolation


logger = logging.getLogger(__name__)


class CameraWorker:
    """Thread-safe camera worker with frame buffering and face tracking.
    
    State machine for face tracking:
        SCANNING  -- no face known, sweeping the room to find one
        TRACKING  -- face detected, following it with head offsets
        WAITING   -- face just lost, holding position briefly
        RETURNING -- interpolating back to neutral before scanning again
    """

    def __init__(self, reachy_mini: ReachyMini, head_tracker: Any = None) -> None:
        """Initialize camera worker.
        
        Args:
            reachy_mini: Connected ReachyMini instance
            head_tracker: Optional head tracker (YOLO or MediaPipe)
        """
        self.reachy_mini = reachy_mini
        self.head_tracker = head_tracker

        # Thread-safe frame storage
        self.latest_frame: Optional[NDArray[np.uint8]] = None
        self.frame_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Face tracking state
        self.is_head_tracking_enabled = True
        self.face_tracking_offsets: List[float] = [
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
        ]  # x, y, z, roll, pitch, yaw
        self.face_tracking_lock = threading.Lock()

        # Face tracking timing (for smooth interpolation back to neutral)
        self.last_face_detected_time: Optional[float] = None
        self.interpolation_start_time: Optional[float] = None
        self.interpolation_start_pose: Optional[NDArray[np.float32]] = None
        # Wait longer before declaring the face truly lost — short glances
        # away (looking at notes, sipping coffee) shouldn't trigger the
        # return-to-neutral + scanning sequence.
        self.face_lost_delay = 5.0
        self.interpolation_duration = 1.0  # seconds to interpolate back to neutral

        # Track state changes
        self.previous_head_tracking_state = self.is_head_tracking_enabled
        
        # Tracking scale factor (proportional gain for the camera-head servo loop).
        # Kept at 0.7: scaling the look_at_image setpoint caps how aggressive
        # the spring chases an edge-of-frame face. Without this, edge faces
        # produced 0.5+ rad targets and the head slammed at ~190°/s.
        self.tracking_scale = 0.7

        # Critically-damped 2nd-order ("spring-damper") low-pass filter.
        # Settle time ≈ 4/ω. ω=10 → ~0.4 s — the value that was running
        # smoothly before the (failed) ω=18 experiment that produced
        # destructive head whips.
        self._smooth_omega = 10.0
        self._smooth_zeta = 1.0
        # Hard safety clamps independent of the spring math. Even if a
        # stale dt or huge setpoint jump kicks the integrator, the head can
        # never command faster than these:
        #   _max_vel        — peak rad/s the smoother is allowed to output
        #   _max_step       — peak rad commanded per loop tick (~5°/tick)
        # Applied AFTER the spring update so they bound the worst case.
        self._max_vel = 1.2          # rad/s ≈ 69°/s
        self._max_step = 0.09        # rad ≈ 5° per 10 ms tick
        self._smooth_pos: List[float] = [0.0] * 6
        self._smooth_vel: List[float] = [0.0] * 6
        self._smooth_last_t: float = time.monotonic()
        # Backwards-compat alias for any code that reads _smoothed_offsets.
        self._smoothed_offsets: List[float] = self._smooth_pos
        # Target the smoother chases. Updated when a fresh face detection,
        # scanning step, or neutral-return interpolation produces new
        # offsets; smoother ticks toward this every loop iteration.
        self._target_offsets: List[float] = [0.0] * 6
        
        # --- Room scanning state ---
        # When no face is visible, the robot periodically sweeps the room.
        self._scanning = False
        self._scanning_start_time = 0.0
        # Scanning pattern: sinusoidal yaw sweep
        self._scan_yaw_amplitude = np.deg2rad(15)  # ±15° gentle peek
        self._scan_period = 8.0  # seconds for a full left-right-left cycle
        self._scan_pitch_offset = np.deg2rad(3)  # slight upward tilt while scanning
        # Start scanning immediately at boot (before any face has ever been seen)
        self._ever_seen_face = False

    def get_latest_frame(self) -> Optional[NDArray[np.uint8]]:
        """Get the latest frame (thread-safe).
        
        Returns:
            Copy of latest frame in BGR format, or None if no frame available
        """
        with self.frame_lock:
            if self.latest_frame is None:
                return None
            return self.latest_frame.copy()

    def get_face_tracking_offsets(
        self,
    ) -> Tuple[float, float, float, float, float, float]:
        """Get current face tracking offsets (thread-safe).
        
        Returns:
            Tuple of (x, y, z, roll, pitch, yaw) offsets
        """
        with self.face_tracking_lock:
            offsets = self.face_tracking_offsets
            return (offsets[0], offsets[1], offsets[2], offsets[3], offsets[4], offsets[5])

    def set_head_tracking_enabled(self, enabled: bool) -> None:
        """Enable/disable head tracking.
        
        Args:
            enabled: Whether to enable face tracking
        """
        if enabled and not self.is_head_tracking_enabled:
            # Seed spring from current commanded offsets so we don't snap
            # the head to zero on re-enable. Velocity starts at rest.
            with self.face_tracking_lock:
                self._smooth_pos = list(self.face_tracking_offsets)
            self._smooth_vel = [0.0] * 6
            self._smoothed_offsets = self._smooth_pos
            self._target_offsets = list(self._smooth_pos)
            self._smooth_last_t = time.monotonic()
            # Start scanning immediately when re-enabled
            self._start_scanning()
        self.is_head_tracking_enabled = enabled
        logger.info("Head tracking %s", "enabled" if enabled else "disabled")

    def start(self) -> None:
        """Start the camera worker loop in a thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._working_loop, daemon=True)
        self._thread.start()
        logger.info("Camera worker started")

    def stop(self) -> None:
        """Stop the camera worker loop."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        logger.info("Camera worker stopped")

    # ------------------------------------------------------------------
    # Scanning helpers
    # ------------------------------------------------------------------

    def _start_scanning(self) -> None:
        """Begin the room-scanning sweep."""
        if not self._scanning:
            self._scanning = True
            self._scanning_start_time = time.time()
            logger.debug("Started room scanning")

    def _stop_scanning(self) -> None:
        """Stop the room-scanning sweep."""
        if self._scanning:
            self._scanning = False
            logger.debug("Stopped room scanning")

    def _step_smoother(self, target: List[float]) -> List[float]:
        """Critically-damped 2nd-order step toward `target`. Returns the new
        smoothed pose offsets. Operates on the persistent _smooth_pos /
        _smooth_vel state so transitions between face-tracking, scanning,
        and neutral-return all flow through the same continuous filter.
        """
        now = time.monotonic()
        dt = now - self._smooth_last_t
        self._smooth_last_t = now
        # Clamp dt so a long pause (or first call) doesn't kick the
        # integrator into instability.
        dt = max(0.001, min(0.08, dt))
        omega = self._smooth_omega
        zeta = self._smooth_zeta
        omega_sq = omega * omega
        two_zeta_omega = 2.0 * zeta * omega
        max_vel = self._max_vel
        max_step = self._max_step
        for i in range(6):
            err = target[i] - self._smooth_pos[i]
            acc = omega_sq * err - two_zeta_omega * self._smooth_vel[i]
            v = self._smooth_vel[i] + acc * dt
            if v > max_vel: v = max_vel
            elif v < -max_vel: v = -max_vel
            self._smooth_vel[i] = v
            step = v * dt
            if step > max_step: step = max_step
            elif step < -max_step: step = -max_step
            self._smooth_pos[i] += step
        self._smoothed_offsets = self._smooth_pos
        return list(self._smooth_pos)

    def _update_scanning_offsets(self, current_time: float) -> None:
        """Compute scanning targets -- a gentle yaw peek with slight pitch up.

        Reduced amplitude (was theatrical at ±35°). Now ±15° so it reads
        like the robot is glancing around briefly, not pacing the room.
        Targets go through the spring smoother so transitions are fluid.
        """
        t = current_time - self._scanning_start_time

        yaw = float(self._scan_yaw_amplitude * np.sin(2 * np.pi * t / self._scan_period))
        pitch = float(self._scan_pitch_offset)

        # Set the spring's target; the main loop will chase it.
        self._target_offsets = [0.0, 0.0, 0.0, 0.0, pitch, yaw]

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _working_loop(self) -> None:
        """Main camera worker loop.
        
        Runs at ~25Hz, captures frames and processes face tracking.
        """
        logger.debug("Starting camera working loop")

        # Neutral pose for interpolation target
        neutral_pose = np.eye(4, dtype=np.float32)
        self.previous_head_tracking_state = self.is_head_tracking_enabled
        
        # Begin scanning right away so the robot looks for a face on startup
        if self.is_head_tracking_enabled and self.head_tracker is not None:
            self._start_scanning()

        # Detection runs at ~25 Hz (CPU-bound on aarch64), smoother runs at
        # ~100 Hz so MovementManager always sees freshly-interpolated offsets
        # between detector frames. Decoupling kills the staircase artefact
        # the user noticed on yaw turns.
        DETECTION_PERIOD = 0.04   # 25 Hz detection
        LOOP_PERIOD = 0.01        # 100 Hz smoother / write
        last_detection_t = 0.0

        while not self._stop_event.is_set():
            try:
                current_time = time.time()
                now_mono = time.monotonic()
                do_detect = (now_mono - last_detection_t) >= DETECTION_PERIOD

                if do_detect:
                    last_detection_t = now_mono
                    frame = self.reachy_mini.media.get_frame()
                    if frame is not None:
                        with self.frame_lock:
                            self.latest_frame = frame
                        if (self.previous_head_tracking_state
                                and not self.is_head_tracking_enabled):
                            self.last_face_detected_time = current_time
                            self.interpolation_start_time = None
                            self.interpolation_start_pose = None
                            self._stop_scanning()
                        self.previous_head_tracking_state = self.is_head_tracking_enabled
                        if self.is_head_tracking_enabled and self.head_tracker is not None:
                            self._process_face_tracking(frame, current_time, neutral_pose)
                        elif self.last_face_detected_time is not None:
                            self._interpolate_to_neutral(current_time, neutral_pose)

                # Always tick the spring + write offsets, regardless of
                # whether detection ran this iteration. _process_face_tracking
                # / _update_scanning_offsets / _interpolate_to_neutral now
                # only update self._target_offsets; the smoother chases it
                # at full loop rate.
                if self.is_head_tracking_enabled or self.last_face_detected_time is not None:
                    smoothed = self._step_smoother(self._target_offsets)
                    with self.face_tracking_lock:
                        self.face_tracking_offsets = smoothed

                time.sleep(LOOP_PERIOD)

            except Exception as e:
                logger.error("Camera worker error: %s", e)
                time.sleep(0.1)

        logger.debug("Camera worker thread exited")

    def _process_face_tracking(
        self, 
        frame: NDArray[np.uint8], 
        current_time: float,
        neutral_pose: NDArray[np.float32]
    ) -> None:
        """Process face tracking from frame.
        
        Args:
            frame: Current camera frame
            current_time: Current timestamp
            neutral_pose: Neutral pose matrix for interpolation
        """
        eye_center, _ = self.head_tracker.get_head_position(frame)

        if eye_center is not None:
            # Face detected!
            if not self._ever_seen_face:
                self._ever_seen_face = True
                logger.info("Face detected for the first time")
            
            # Stop scanning if we were scanning
            if self._scanning:
                self._stop_scanning()
                # Seed the spring from current head pose for a fluid hand-off.
                with self.face_tracking_lock:
                    self._smooth_pos = list(self.face_tracking_offsets)
                    # Don't carry scanning velocity into face-tracking — start
                    # the spring from rest so the transition reads as a snap-to.
                    self._smooth_vel = [0.0] * 6
                    self._smoothed_offsets = self._smooth_pos
                self._smooth_last_t = time.monotonic()

            self.last_face_detected_time = current_time
            self.interpolation_start_time = None  # Stop any interpolation

            # Convert normalized coordinates to pixel coordinates
            h, w = frame.shape[:2]
            eye_center_norm = (eye_center + 1) / 2
            eye_center_pixels = [
                eye_center_norm[0] * w,
                eye_center_norm[1] * h,
            ]

            # Get the head pose needed to look at the target
            target_pose = self.reachy_mini.look_at_image(
                eye_center_pixels[0],
                eye_center_pixels[1],
                duration=0.0,
                perform_movement=False,
            )

            # Extract translation and rotation from the target pose
            translation = target_pose[:3, 3]
            rotation = R.from_matrix(target_pose[:3, :3]).as_euler("xyz", degrees=False)

            # Scale for smoother closed-loop convergence
            translation *= self.tracking_scale
            rotation *= self.tracking_scale

            # Clamp setpoints. Even with scale=0.7, an edge-of-frame face
            # can yield rotations >0.5 rad which the spring will faithfully
            # chase at high velocity. Bound to safe head ranges.
            max_rot = 0.45      # ~26°, well within head joint limits
            max_trans = 0.04    # 4 cm body lean
            for j in range(3):
                if translation[j] > max_trans: translation[j] = max_trans
                elif translation[j] < -max_trans: translation[j] = -max_trans
                if rotation[j] > max_rot: rotation[j] = max_rot
                elif rotation[j] < -max_rot: rotation[j] = -max_rot

            # Update the SETPOINT only; the 100Hz loop ticks the spring
            # toward it, which keeps motion fluid between 25Hz detections.
            self._target_offsets = [
                translation[0], translation[1], translation[2],
                rotation[0], rotation[1], rotation[2],
            ]

        else:
            # No face detected
            if self._scanning:
                # Already scanning -- keep sweeping the room
                self._update_scanning_offsets(current_time)
            else:
                # Not scanning yet -- go through the wait/return/scan sequence
                self._interpolate_to_neutral(current_time, neutral_pose)

    def _interpolate_to_neutral(
        self, 
        current_time: float,
        neutral_pose: NDArray[np.float32]
    ) -> None:
        """Interpolate face tracking offsets back to neutral when face is lost.
        
        Once interpolation completes, automatically starts room scanning.
        
        Args:
            current_time: Current timestamp
            neutral_pose: Target neutral pose matrix
        """
        if self.last_face_detected_time is None:
            # Never seen a face -- go straight to scanning
            self._start_scanning()
            return

        time_since_face_lost = current_time - self.last_face_detected_time

        if time_since_face_lost >= self.face_lost_delay:
            # Start interpolation if not already started
            if self.interpolation_start_time is None:
                self.interpolation_start_time = current_time
                # Capture current pose as start of interpolation
                with self.face_tracking_lock:
                    current_translation = self.face_tracking_offsets[:3]
                    current_rotation_euler = self.face_tracking_offsets[3:]
                    # Convert to 4x4 pose matrix
                    pose_matrix = np.eye(4, dtype=np.float32)
                    pose_matrix[:3, 3] = current_translation
                    pose_matrix[:3, :3] = R.from_euler(
                        "xyz", current_rotation_euler
                    ).as_matrix()
                    self.interpolation_start_pose = pose_matrix

            # Calculate interpolation progress (t from 0 to 1)
            elapsed_interpolation = current_time - self.interpolation_start_time
            t = min(1.0, elapsed_interpolation / self.interpolation_duration)

            # Interpolate between current pose and neutral pose
            interpolated_pose = linear_pose_interpolation(
                self.interpolation_start_pose,
                neutral_pose,
                t,
            )

            # Extract translation and rotation from interpolated pose
            translation = interpolated_pose[:3, 3]
            rotation = R.from_matrix(interpolated_pose[:3, :3]).as_euler("xyz", degrees=False)

            # Update the spring's target; main loop chases it.
            self._target_offsets = [
                translation[0], translation[1], translation[2],
                rotation[0], rotation[1], rotation[2],
            ]

            # If interpolation is complete, start scanning the room
            if t >= 1.0:
                self.last_face_detected_time = None
                self.interpolation_start_time = None
                self.interpolation_start_pose = None
                self._smooth_pos = [0.0] * 6
                self._smooth_vel = [0.0] * 6
                self._smoothed_offsets = self._smooth_pos
                self._target_offsets = [0.0] * 6
                self._start_scanning()