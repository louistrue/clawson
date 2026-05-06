"""Action mode: ConfirmationSystem + ActionRegistry."""

import asyncio

import pytest

from reachy_mini_openclaw.actions import (
    Action,
    ActionRegistry,
    ConfirmationSystem,
)


# -------------------- ConfirmationSystem --------------------


@pytest.mark.asyncio
async def test_confirm_returns_true_on_confirm():
    sys = ConfirmationSystem()
    task = asyncio.create_task(sys.request("Add task: buy milk", timeout_s=2.0))
    await asyncio.sleep(0.05)  # let request register the pending future
    assert sys.has_pending
    assert sys.confirm() is True
    assert (await task) is True


@pytest.mark.asyncio
async def test_confirm_returns_false_on_deny():
    sys = ConfirmationSystem()
    task = asyncio.create_task(sys.request("send Slack DM", timeout_s=2.0))
    await asyncio.sleep(0.05)
    assert sys.deny() is True
    assert (await task) is False


@pytest.mark.asyncio
async def test_confirm_returns_false_on_timeout():
    sys = ConfirmationSystem()
    result = await sys.request("nope", timeout_s=0.05)
    assert result is False


@pytest.mark.asyncio
async def test_new_request_supersedes_prior():
    sys = ConfirmationSystem()
    first = asyncio.create_task(sys.request("first", timeout_s=2.0))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(sys.request("second", timeout_s=2.0))
    await asyncio.sleep(0.05)
    # First should already have resolved to False (preempted).
    assert (await first) is False
    sys.confirm()
    assert (await second) is True


@pytest.mark.asyncio
async def test_announce_callback_runs_with_prompt():
    sys = ConfirmationSystem()
    captured: list[str] = []

    async def announce(msg: str) -> None:
        captured.append(msg)

    task = asyncio.create_task(
        sys.request("Add task: write tests", timeout_s=2.0, on_announce=announce)
    )
    await asyncio.sleep(0.05)
    sys.confirm()
    await task
    assert captured
    assert "write tests" in captured[0]
    assert "Right" in captured[0] and "left" in captured[0].lower()


def test_confirm_with_no_pending_returns_false():
    sys = ConfirmationSystem()
    assert sys.confirm() is False
    assert sys.deny() is False
    assert sys.has_pending is False


# -------------------- ActionRegistry --------------------


def _noop_executor():
    async def _exec(args):
        return {"ok": True, "args": args}
    return _exec


def test_register_and_get():
    reg = ActionRegistry()
    a = Action(
        name="todoist_add",
        tool_spec={"type": "function", "name": "todoist_add"},
        executor=_noop_executor(),
        requires_confirmation=True,
        preview=lambda args: f"Add: {args.get('title', '?')}",
    )
    reg.register(a)
    fetched = reg.get("todoist_add")
    assert fetched is a
    assert "todoist_add" in reg.names()


def test_get_returns_none_for_unknown():
    reg = ActionRegistry()
    assert reg.get("does_not_exist") is None


def test_tool_specs_returns_all():
    reg = ActionRegistry()
    reg.register(Action(
        name="a", tool_spec={"name": "a"}, executor=_noop_executor(),
        requires_confirmation=False,
    ))
    reg.register(Action(
        name="b", tool_spec={"name": "b"}, executor=_noop_executor(),
        requires_confirmation=True, preview=lambda _: "do b",
    ))
    specs = reg.tool_specs()
    names = {s["name"] for s in specs}
    assert names == {"a", "b"}
