"""Offline workflow tests never attach, parse a game snapshot, or submit native commands."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy
from types import SimpleNamespace
import threading

import pytest

from stellaris_autopilot import task_workflow as workflow
from stellaris_autopilot.task_owner import current_owner
from stellaris_autopilot.task_plan import normalize_plan


STEPS = [{"kind": "queue_ship", "starbase_id": "1", "design_id": str(index)} for index in (2, 3, 4)]


class FakeConnection:
    def __init__(self):
        self._plan_local = SimpleNamespace(guard={})
        self.runs = 0
        self.available = True
        self.before = None

    def run_plan(self, callback):
        self.runs += 1
        if not self.available:
            raise ValueError("No game is connected")
        self._plan_local.guard = {}
        if self.before is not None:
            result = self.before()
            if result is not None:
                return result
        return callback(self, SimpleNamespace())


@pytest.fixture
def setup(tmp_path, monkeypatch):
    connection = FakeConnection()
    task = workflow.TaskWorkflow(connection, tmp_path / "stable" / "tasks.sqlite")
    calls = []

    def dispatch(_connection, step):
        calls.append(copy.deepcopy(step))
        return {"status": "command_verified", "snapshot": f"after-{len(calls)}", "goal_state": "pending"}

    monkeypatch.setattr(workflow, "dispatch_step", dispatch)
    return task, connection, calls


def test_known_single_action_is_one_run_without_preparation_or_extra_result_call(setup):
    task, connection, calls = setup
    result = task.execute("single", STEPS[:1])
    assert result["status"] == "completed"
    assert connection.runs == 1 and calls == STEPS[:1]
    assert result["steps"][0]["status"] == "verified"
    assert task.store.get("single") == result


def test_same_id_retry_works_offline_and_different_plan_conflicts_without_a_live_read(setup):
    task, connection, calls = setup
    result = task.execute("retry", STEPS)
    connection.available = False
    assert task.execute("retry", STEPS) == result
    assert task.execute("retry", [{**STEPS[0], "design_id": "99"}])["status"] == "conflict"
    assert connection.runs == 1 and calls == STEPS
    assert task.store.get("retry") == result
    assert task.store.cancel("retry") == result


def test_fresh_workflow_can_read_and_retry_original_result_with_unavailable_new_connection(setup):
    task, _connection, calls = setup
    result = task.execute("saved", STEPS[:1])
    replacement = FakeConnection()
    replacement.available = False
    new_task = workflow.TaskWorkflow(replacement, task.store.path)
    assert new_task.store.get("saved") == result
    assert new_task.execute("saved", STEPS[:1]) == result
    assert replacement.runs == 0 and len(calls) == 1


def test_hash_ignores_dictionary_key_order_when_retrying(setup):
    task, connection, calls = setup
    original = task.execute("ordered", STEPS)
    reordered = [dict(reversed(list(step.items()))) for step in STEPS]
    assert task.execute("ordered", reordered) == original
    assert connection.runs == 1 and calls == STEPS


def test_changed_snapshot_requirement_conflicts_for_an_existing_request_id(setup):
    task, connection, calls = setup
    original = task.execute("snapshot-bound", STEPS[:1], "before")
    conflict = task.execute("snapshot-bound", STEPS[:1], "different-before")
    assert conflict["status"] == "conflict"
    assert task.store.get("snapshot-bound") == original
    assert connection.runs == 1 and len(calls) == 1


@pytest.mark.parametrize("bad_steps", [STEPS + [{"kind": "no_such_action"}],
                                            [STEPS[0], {**STEPS[1], "design_id": True}],
                                            [{"kind": "set_paused", "paused": False}]])
def test_entire_plan_is_validated_before_any_connection_or_task_claim(setup, bad_steps):
    task, connection, calls = setup
    with pytest.raises(ValueError):
        task.execute("invalid", bad_steps)
    assert calls == [] and connection.runs == 0
    assert not task.store.path.exists()


def test_expected_snapshot_guard_follows_verified_receipts(setup, monkeypatch):
    task, connection, _calls = setup
    seen = []

    def dispatch(actual, _step):
        seen.append(copy.deepcopy(actual._plan_local.guard))
        return {"status": "command_verified", "snapshot": f"after-{len(seen)}"}

    monkeypatch.setattr(workflow, "dispatch_step", dispatch)
    assert task.execute("guard", STEPS, "before")["status"] == "completed"
    assert [guard["expected_snapshot"] for guard in seen] == ["before", "after-1", "after-2"]
    assert [guard["steps"] for guard in seen] == [STEPS[index:] for index in range(len(STEPS))]
    assert connection._plan_local.guard["expected_snapshot"] == "after-3"


def test_already_satisfied_research_uses_current_guard_snapshot(setup, monkeypatch):
    task, _connection, _calls = setup
    seen = []

    def dispatch(connection, step):
        seen.append((step["kind"], connection._plan_local.guard.get("expected_snapshot")))
        if step["kind"] == "select_research":
            connection._plan_local.guard["last_snapshot"] = "already-current"
            return {"status": "already_selected", "research": {"technology": "tech_power_plant_1"}}
        return {"status": "command_verified", "snapshot": "after-build"}

    monkeypatch.setattr(workflow, "dispatch_step", dispatch)
    steps = [{"kind": "select_research", "area": "physics", "technology_id": "tech_power_plant_1"}, STEPS[0]]
    result = task.execute("already", steps, "before")
    assert result["status"] == "completed"
    assert [step["status"] for step in result["steps"]] == ["already_satisfied", "verified"]
    assert seen == [("select_research", "before"), ("queue_ship", "already-current")]


@pytest.mark.parametrize("status", ["blocked", "stopped", "stale", "busy", "unverified", "submitted", "other"])
def test_failure_or_unknown_stops_future_steps_and_keeps_verified_prefix(setup, monkeypatch, status):
    task, _connection, _calls = setup
    calls = []

    def dispatch(_connection, step):
        calls.append(step)
        if len(calls) == 2:
            return {"status": status, "reason": "fixture outcome"}
        return {"status": "command_verified", "snapshot": "after-first", "goal_state": "pending"}

    monkeypatch.setattr(workflow, "dispatch_step", dispatch)
    result = task.execute("failure", STEPS)
    assert len(calls) == 2
    assert result["steps"][0]["status"] == "verified"
    assert result["steps"][2]["status"] == "skipped"
    unknown = status not in {"blocked", "stopped", "stale", "busy"}
    assert result["status"] == ("unverified" if unknown else "partial")
    assert result["steps"][1]["status"] == ("unverified" if unknown else status)
    assert task.execute("failure", STEPS) == result
    assert len(calls) == 2


@pytest.mark.parametrize("value", [None, [], "submitted", 42])
def test_non_object_executor_reply_is_unverified_and_never_replayed(setup, monkeypatch, value):
    task, connection, _calls = setup
    monkeypatch.setattr(workflow, "dispatch_step", lambda *_args: value)
    result = task.execute("malformed", STEPS)
    assert result["status"] == "unverified"
    assert [step["status"] for step in result["steps"]] == ["unverified", "skipped", "skipped"]
    assert task.execute("malformed", STEPS) == result and connection.runs == 1


@pytest.mark.parametrize("error", [ValueError("bad live state"), OSError("worker disconnected"),
                                        RuntimeError("interrupted execution")])
def test_exception_after_step_claim_is_unknown_without_replay(setup, monkeypatch, error):
    task, connection, calls = setup

    def dispatch(_connection, step):
        calls.append(step)
        raise error

    monkeypatch.setattr(workflow, "dispatch_step", dispatch)
    result = task.execute("unknown", STEPS)
    assert result["status"] == "unverified"
    assert [step["status"] for step in result["steps"]] == ["unverified", "skipped", "skipped"]
    assert task.execute("unknown", STEPS) == result
    assert len(calls) == 1 and connection.runs == 1


def test_unexpected_exception_is_reraised_but_still_persists_non_replayable_unknown(setup, monkeypatch):
    task, connection, calls = setup

    def dispatch(_connection, step):
        calls.append(step)
        raise KeyError("broken executor implementation")

    monkeypatch.setattr(workflow, "dispatch_step", dispatch)
    with pytest.raises(KeyError):
        task.execute("unexpected", STEPS)
    record = task.store.get("unexpected")
    assert record["status"] == "unverified"
    assert [step["status"] for step in record["steps"]] == ["unverified", "skipped", "skipped"]
    assert task.execute("unexpected", STEPS) == record
    assert len(calls) == 1 and connection.runs == 1


def test_receipt_persist_failure_stops_without_repaying_verified_game_effect(setup, monkeypatch):
    task, connection, calls = setup

    def broken_save(*_args):
        raise OSError("task receipt disk failure after primitive success")

    monkeypatch.setattr(task.store, "step_result", broken_save)
    result = task.execute("persist-gap", STEPS)
    assert result["status"] == "unverified"
    assert [step["status"] for step in result["steps"]] == ["unverified", "skipped", "skipped"]
    assert len(calls) == 1
    assert task.execute("persist-gap", STEPS) == result and connection.runs == 1


def test_unavailable_connection_does_not_submit_any_step_or_allow_same_id_replay(setup):
    task, connection, calls = setup
    connection.available = False
    result = task.execute("offline", STEPS)
    assert result["status"] == "blocked"
    assert all(step["status"] == "skipped" for step in result["steps"])
    connection.available = True
    assert task.execute("offline", STEPS) == result
    assert calls == [] and connection.runs == 1


def test_lifecycle_busy_does_not_queue_or_enter_any_step(setup):
    task, connection, calls = setup
    connection.before = lambda: {"status": "busy"}
    result = task.execute("busy", STEPS)
    assert result["status"] == "busy"
    assert all(step["status"] == "skipped" for step in result["steps"])
    assert calls == []


def test_cancel_before_first_step_prevents_all_dispatches(setup):
    task, connection, calls = setup
    connection.before = lambda: task.store.cancel("pre-cancel") and None
    result = task.execute("pre-cancel", STEPS)
    assert result["status"] == "cancelled"
    assert all(step["status"] == "skipped" for step in result["steps"])
    assert calls == []


def test_cancel_during_step_preserves_verified_result_and_stops_future_steps(setup, monkeypatch):
    task, _connection, calls = setup
    entered, release = threading.Event(), threading.Event()

    def dispatch(_connection, step):
        calls.append(step)
        entered.set()
        assert release.wait(5)
        return {"status": "command_verified", "snapshot": "after-first", "goal_state": "pending",
                "context": {"resource_changes": {"alloys": -100}}}

    monkeypatch.setattr(workflow, "dispatch_step", dispatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(task.execute, "cancel-active", STEPS)
        try:
            assert entered.wait(5)
            # Both APIs must complete while the primitive remains blocked.
            result = pool.submit(task.store.get, "cancel-active").result(timeout=2)
            assert result["steps"][0]["status"] == "executing"
            cancelled = pool.submit(task.store.cancel, "cancel-active").result(timeout=2)
            assert cancelled["status"] == "cancel_requested" and not worker.done()
        finally:
            release.set()
        final = worker.result(timeout=5)
    assert final["status"] == "cancelled"
    assert [step["status"] for step in final["steps"]] == ["verified", "skipped", "skipped"]
    assert final["steps"][0]["result"]["context"]["resource_changes"]["alloys"] == -100
    assert len(calls) == 1


def test_cancel_does_not_hide_an_unverified_active_step(setup, monkeypatch):
    task, _connection, calls = setup

    def dispatch(_connection, step):
        calls.append(step)
        task.store.cancel("cancel-unknown")
        return {"status": "unverified", "reason": "submitted but no matching postcondition"}

    monkeypatch.setattr(workflow, "dispatch_step", dispatch)
    result = task.execute("cancel-unknown", STEPS)
    assert result["status"] == "unverified" and result["cancel_requested"] is True
    assert [step["status"] for step in result["steps"]] == ["unverified", "skipped", "skipped"]
    assert len(calls) == 1


def test_concurrent_same_request_never_starts_a_second_run(setup, monkeypatch):
    task, connection, calls = setup
    entered, release = threading.Event(), threading.Event()

    def dispatch(_connection, step):
        calls.append(step)
        entered.set()
        assert release.wait(5)
        return {"status": "command_verified", "snapshot": "after"}

    monkeypatch.setattr(workflow, "dispatch_step", dispatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(task.execute, "shared", STEPS[:1])
        try:
            assert entered.wait(5)
            duplicate = pool.submit(task.execute, "shared", STEPS[:1]).result(timeout=2)
            assert duplicate["status"] == "running"
            assert duplicate["steps"][0]["status"] == "executing"
            assert connection.runs == 1 and len(calls) == 1
        finally:
            release.set()
        assert first.result(timeout=5)["status"] == "completed"


def test_restarted_executor_never_takes_over_a_dead_owner_step(setup, monkeypatch):
    from stellaris_autopilot import task_owner

    task, connection, calls = setup
    normalized, digest = normalize_plan(STEPS)
    task.store.claim("dead", digest, normalized, None, current_owner())
    task.store.start_step("dead", 0)
    monkeypatch.setattr(task_owner, "owner_alive", lambda _owner: False)
    result = task.execute("dead", STEPS)
    assert result["status"] == "interrupted"
    assert [step["status"] for step in result["steps"]] == ["unverified", "skipped", "skipped"]
    assert connection.runs == 0 and calls == []


def test_dispatch_completion_does_not_claim_natural_goal_completion_or_change_pause(setup):
    task, _connection, calls = setup
    result = task.execute("dispatch-only", STEPS)
    assert result["status"] == "completed"
    assert all(step["result"]["goal_state"] == "pending" for step in result["steps"])
    assert "natural completion" in result["note"]
    assert [step["kind"] for step in calls] == ["queue_ship"] * 3


def test_async_cancel_waits_for_current_step_cleanup_then_stops_future_steps(setup, monkeypatch):
    task, _connection, calls = setup
    entered, release = threading.Event(), threading.Event()

    def dispatch(_connection, step):
        calls.append(step)
        entered.set()
        assert release.wait(5)
        return {"status": "command_verified", "snapshot": "after-active"}

    monkeypatch.setattr(workflow, "dispatch_step", dispatch)

    async def scenario():
        runner = asyncio.create_task(task.execute_async("async-cancel", STEPS))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            runner.cancel()
            # Let execute_async observe cancellation without releasing the primitive.
            for _ in range(10):
                await asyncio.sleep(0)
            assert not runner.done()
            assert task.store.get("async-cancel")["steps"][0]["status"] == "executing"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await runner

    asyncio.run(scenario())
    result = task.store.get("async-cancel")
    assert result["status"] == "cancelled"
    assert [step["status"] for step in result["steps"]] == ["verified", "skipped", "skipped"]
    assert len(calls) == 1


def test_already_set_cancel_event_stops_before_first_dispatch(setup):
    task, _connection, calls = setup
    cancelled = threading.Event()
    cancelled.set()
    result = task.execute("early-sync-cancel", STEPS, _cancel_event=cancelled)
    assert result["status"] == "cancelled"
    assert all(step["status"] == "skipped" for step in result["steps"])
    assert calls == []


def test_async_cancel_before_durable_claim_prevents_later_dispatch(setup, monkeypatch):
    task, _connection, calls = setup
    before_claim, release_claim = threading.Event(), threading.Event()
    original_claim = task.store.claim

    def delayed_claim(*args, **kwargs):
        before_claim.set()
        assert release_claim.wait(5)
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(task.store, "claim", delayed_claim)

    async def scenario():
        runner = asyncio.create_task(task.execute_async("early-async-cancel", STEPS))
        try:
            assert await asyncio.to_thread(before_claim.wait, 5)
            runner.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
            assert not runner.done()
            assert task.store.get("early-async-cancel")["status"] == "not_found"
        finally:
            release_claim.set()
        with pytest.raises(asyncio.CancelledError):
            await runner

    asyncio.run(scenario())
    result = task.store.get("early-async-cancel")
    assert result["status"] == "cancelled"
    assert all(step["status"] == "skipped" for step in result["steps"])
    assert calls == []
