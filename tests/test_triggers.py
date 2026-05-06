"""Voice + face-detect standup triggers."""

import asyncio
import time as _time
from datetime import time

import pytest

from reachy_mini_openclaw.briefing.triggers import (
    FaceDetectStandupTrigger,
    make_voice_trigger,
)
from reachy_mini_openclaw.clawson_config import FocusSettings


class _FakeStandup:
    def __init__(self):
        self.calls = 0

    async def run_now(self):
        self.calls += 1


# -------------------- voice trigger --------------------


@pytest.mark.asyncio
async def test_voice_trigger_fires_on_keyword():
    runner = _FakeStandup()
    cb = make_voice_trigger(runner)
    await cb("Clawson, do the standup now please")
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_voice_trigger_ignores_unrelated_speech():
    runner = _FakeStandup()
    cb = make_voice_trigger(runner)
    await cb("hey, what's the weather like")
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_voice_trigger_matches_rollup_alias():
    runner = _FakeStandup()
    cb = make_voice_trigger(runner)
    await cb("give me the rollup")
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_voice_trigger_case_insensitive():
    runner = _FakeStandup()
    cb = make_voice_trigger(runner)
    await cb("STANDUP")
    assert runner.calls == 1


# -------------------- face-detect trigger --------------------


class _FakeCamera:
    def __init__(self, last_seen: float = -1e9):
        self.last_face_detected_time = last_seen


def _focus_settings_weekday() -> FocusSettings:
    # Make the standup_days include "today's weekday" so the trigger considers it.
    from datetime import datetime
    today_weekday = datetime.now().weekday()
    return FocusSettings(
        timezone="UTC",
        active_start=time(0, 0),
        active_end=time(23, 59),
        standup_time=time(7, 30),
        standup_days=(today_weekday,),
    )


@pytest.mark.asyncio
async def test_face_detect_trigger_skipped_without_camera():
    runner = _FakeStandup()
    f = _focus_settings_weekday()
    trig = FaceDetectStandupTrigger(camera_worker=None, standup_runner=runner, focus_settings=f)
    # run() should return immediately without calling run_now.
    stopped = {"v": False}

    def should_stop():
        return stopped["v"]

    task = asyncio.create_task(trig.run(should_stop))
    await asyncio.sleep(0.05)
    stopped["v"] = True
    await asyncio.wait_for(task, timeout=2.0)
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_face_detect_arms_only_once_per_day(monkeypatch):
    """If the trigger fires, set _fired_for_date so a follow-up tick doesn't refire."""
    runner = _FakeStandup()
    f = _focus_settings_weekday()
    cam = _FakeCamera(last_seen=_time.monotonic())  # face right now
    trig = FaceDetectStandupTrigger(
        camera_worker=cam,
        standup_runner=runner,
        focus_settings=f,
        window_start=time(0, 0),  # window always open for the test
    )
    await trig._tick()
    await trig._tick()
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_face_detect_ignores_stale_face_timestamp():
    runner = _FakeStandup()
    f = _focus_settings_weekday()
    cam = _FakeCamera(last_seen=_time.monotonic() - 1000.0)  # face seen 1000s ago
    trig = FaceDetectStandupTrigger(
        camera_worker=cam, standup_runner=runner, focus_settings=f,
        window_start=time(0, 0),
    )
    await trig._tick()
    assert runner.calls == 0
