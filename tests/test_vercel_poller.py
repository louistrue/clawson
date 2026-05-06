"""Tests for Vercel deployment-to-Event mapping."""

from datetime import datetime, timezone

from reachy_mini_openclaw.briefing.events import EventSeverity
from reachy_mini_openclaw.briefing.vercel_poller import _deployment_to_event
from reachy_mini_openclaw.mcp_clients.vercel import Deployment


def _make_dep(state: str, **kw) -> Deployment:
    return Deployment(
        uid=kw.get("uid", "dpl_1"),
        project_name=kw.get("project", "site"),
        state=state,
        created_at=kw.get("ts", datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc)),
        url=kw.get("url", "site-x.vercel.app"),
        inspector_url=kw.get("inspector_url", "https://vercel.com/me/site/dpl_1"),
        target=kw.get("target", "production"),
        raw={},
    )


def test_ready_state_maps_to_success_event():
    ev = _deployment_to_event(_make_dep("READY"))
    assert ev is not None
    assert ev.kind == "vercel_deploy_success"
    assert ev.severity == EventSeverity.INFO


def test_error_state_maps_to_fail_event():
    ev = _deployment_to_event(_make_dep("ERROR"))
    assert ev is not None
    assert ev.kind == "vercel_deploy_fail"
    assert ev.severity == EventSeverity.NORMAL


def test_intermediate_states_emit_nothing():
    for s in ("BUILDING", "QUEUED", "INITIALIZING"):
        assert _deployment_to_event(_make_dep(s)) is None


def test_canceled_state_emits_nothing():
    assert _deployment_to_event(_make_dep("CANCELED")) is None


def test_summary_includes_target_when_set():
    ev = _deployment_to_event(_make_dep("ERROR", target="production"))
    assert "(production)" in ev.summary


def test_summary_omits_target_when_none():
    ev = _deployment_to_event(_make_dep("ERROR", target=None))
    assert "(" not in ev.summary
