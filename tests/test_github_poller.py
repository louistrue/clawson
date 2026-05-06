"""Tests for the GitHub-event-to-Event mapping."""

from datetime import datetime, timezone

from reachy_mini_openclaw.briefing.events import EventSeverity
from reachy_mini_openclaw.briefing.poller import (
    _api_to_web,
    _notification_to_event,
    _workflow_run_to_event,
)
from reachy_mini_openclaw.mcp_clients.github import Notification, WorkflowRun


def _now() -> datetime:
    return datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc)


def test_api_to_web_rewrites_pulls():
    api = "https://api.github.com/repos/o/r/pulls/42"
    assert _api_to_web(api) == "https://github.com/o/r/pull/42"


def test_api_to_web_passthrough_for_issues():
    api = "https://api.github.com/repos/o/r/issues/7"
    assert _api_to_web(api) == "https://github.com/o/r/issues/7"


def test_review_request_notification_maps_to_event():
    n = Notification(
        id="123",
        reason="review_requested",
        type="PullRequest",
        title="Add caching",
        repo="o/r",
        url="https://api.github.com/repos/o/r/pulls/9",
        updated_at=_now(),
        raw={},
    )
    ev = _notification_to_event(n)
    assert ev is not None
    assert ev.kind == "review_requested"
    assert ev.severity == EventSeverity.NORMAL
    assert "github.com/o/r/pull/9" in ev.link


def test_subscribed_notification_returns_none():
    n = Notification(
        id="1", reason="subscribed", type="Issue", title="x",
        repo="o/r", url="", updated_at=_now(), raw={},
    )
    assert _notification_to_event(n) is None


def test_workflow_run_failure_to_ci_fail_event():
    run = WorkflowRun(
        id=1, repo="o/r", branch="feat/x",
        status="completed", conclusion="failure",
        actor_login="me", html_url="http://x",
        name="CI", created_at=_now(), updated_at=_now(),
    )
    ev = _workflow_run_to_event(run, previous_conclusion=None)
    assert ev is not None
    assert ev.kind == "ci_fail"
    assert ev.raw["branch"] == "feat/x"


def test_workflow_run_success_after_fail_to_recovery_event():
    run = WorkflowRun(
        id=2, repo="o/r", branch="feat/x",
        status="completed", conclusion="success",
        actor_login="me", html_url="http://x",
        name="CI", created_at=_now(), updated_at=_now(),
    )
    ev = _workflow_run_to_event(run, previous_conclusion="failure")
    assert ev is not None
    assert ev.kind == "ci_pass_after_fail"


def test_workflow_run_plain_success_to_ci_pass():
    run = WorkflowRun(
        id=3, repo="o/r", branch="main",
        status="completed", conclusion="success",
        actor_login="me", html_url="http://x",
        name="CI", created_at=_now(), updated_at=_now(),
    )
    ev = _workflow_run_to_event(run, previous_conclusion="success")
    assert ev is not None
    assert ev.kind == "ci_pass"  # filter will suppress; classifier still emits


def test_workflow_run_in_progress_returns_none():
    run = WorkflowRun(
        id=4, repo="o/r", branch="main",
        status="in_progress", conclusion=None,
        actor_login="me", html_url="http://x",
        name="CI", created_at=_now(), updated_at=_now(),
    )
    assert _workflow_run_to_event(run, previous_conclusion=None) is None


def test_workflow_run_cancelled_returns_none():
    run = WorkflowRun(
        id=5, repo="o/r", branch="main",
        status="completed", conclusion="cancelled",
        actor_login="me", html_url="http://x",
        name="CI", created_at=_now(), updated_at=_now(),
    )
    assert _workflow_run_to_event(run, previous_conclusion="failure") is None
