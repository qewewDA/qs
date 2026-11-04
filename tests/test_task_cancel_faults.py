"""Cancellation fault regressions use only temporary SQLite files and fake actions."""
import asyncio
import copy
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from stellaris_autopilot import task_owner, task_store, task_workflow
from stellaris_autopilot.task_plan import normalize_plan


STEPS = [
    {"kind": "queue_ship", "starbase_id": "1", "design_id": "2"},
    {"kind": "queue_ship", "starbase_id": "1", "design_id": "3"},
]


class CleanupConnection:
    """Keep the whole executor lease active until its explicit cleanup finishes."""

    def __init__(self):
        self._plan_local = SimpleNamespace(guard={})
        self.entered = threading.Event()
        self.release_step = threading.Event()
        self.cleanup_entered = threading.Event()
        self.release_cleanup = threading.Event()
        self.cleaned = threading.Event()
        self.calls = []

    def run_plan(self, callback):
        try:
            return callback(self, SimpleNamespace())
        finally:
            self.cleanup_entered.set()
            assert self.release_cleanup.wait(5)
            self.cleaned.set()

    def dispatch(self, _connection, step):
        self.calls.append(copy.deepcopy(step))
        self.entered.set()
        assert self.release_step.wait(5)
        return {"status": "command_verified", "snapshot": "after-first"}


async def exercise_cancel(task, connection, request_id, attempted, *, begin_fault, end_fault):
    runner = asyncio.create_task(task.execute_async(request_id, STEPS))
    outcome = None
    try:
        assert await asyncio.to_thread(connection.entered.wait, 5)
        begin_fault()
        runner.cancel()
        assert await asyncio.to_thread(attempted.wait, 5)
        for _ in range(5):
            await asyncio.sleep(0)
        assert not runner.done(), "A failed cancel write must not abandon the active worker"
        assert not connection.cleaned.is_set()

        end_fault()
        connection.release_step.set()
        assert await asyncio.to_thread(connection.cleanup_entered.wait, 5)
        assert connection.calls == STEPS[:1]
        for _ in range(3):
            runner.cancel()
            await asyncio.sleep(0)
            assert not runner.done(), "Repeated cancellation must still join executor cleanup"
        connection.release_cleanup.set()
        outcome = (await asyncio.wait_for(asyncio.gather(runner, return_exceptions=True), 5))[0]
        assert isinstance(outcome, asyncio.CancelledError)
        assert connection.cleaned.is_set()
    finally:
        end_fault()
        connection.release_step.set()
        connection.release_cleanup.set()
        if not runner.done():
            await asyncio.wait_for(asyncio.gather(runner, return_exceptions=True), 5)
        elif outcome is None:
            # Retrieve a failed coroutine even when an earlier assertion exposes a regression.
            await asyncio.gather(runner, return_exceptions=True)

    record = task.store.get(request_id)
    assert record["status"] == "cancelled"
    assert record["cancel_requested"] is True
    assert [step["status"] for step in record["steps"]] == ["verified", "skipped"]
    assert connection.calls == STEPS[:1]


@pytest.mark.parametrize("failure", [OSError, sqlite3.OperationalError])
def test_cancel_database_errors_and_repeated_cancellation_still_join_cleanup(tmp_path, monkeypatch, failure):
    connection = CleanupConnection()
    task = task_workflow.TaskWorkflow(connection, tmp_path / "tasks.sqlite")
    monkeypatch.setattr(task_workflow, "dispatch_step", connection.dispatch)
    original_cancel = task.store.cancel
    attempted = threading.Event()
    loop_thread = threading.get_ident()
    failed_calls = []

    def faulty_cancel(request_id):
        if threading.get_ident() == loop_thread:
            failed_calls.append(request_id)
            attempted.set()
            raise failure("transient cancel persistence failure")
        return original_cancel(request_id)

    monkeypatch.setattr(task.store, "cancel", faulty_cancel)
    asyncio.run(exercise_cancel(task, connection, "cancel-fault", attempted,
                                begin_fault=lambda: None, end_fault=lambda: None))
    # Both the initial cancellation write and the post-join write may fail.
    assert failed_calls == ["cancel-fault", "cancel-fault"]


def test_real_sqlite_writer_lock_does_not_abandon_active_submission(tmp_path, monkeypatch):
    connection = CleanupConnection()
    task = task_workflow.TaskWorkflow(connection, tmp_path / "tasks.sqlite")
    monkeypatch.setattr(task_workflow, "dispatch_step", connection.dispatch)
    original_cancel = task.store.cancel
    original_connect = sqlite3.connect
    attempted = threading.Event()
    loop_thread = threading.get_ident()
    blockers = []
    failures = []

    def short_connect(*args, **kwargs):
        kwargs["timeout"] = 0
        return original_connect(*args, **kwargs)

    def observed_cancel(request_id):
        try:
            return original_cancel(request_id)
        except sqlite3.OperationalError as error:
            failures.append(str(error))
            raise
        finally:
            if threading.get_ident() == loop_thread:
                attempted.set()

    def begin_fault():
        blocker = original_connect(task.store.path)
        blocker.execute("BEGIN IMMEDIATE")
        blockers.append(blocker)

    def end_fault():
        while blockers:
            blocker = blockers.pop()
            blocker.rollback()
            blocker.close()

    monkeypatch.setattr(task_store.sqlite3, "connect", short_connect)
    monkeypatch.setattr(task.store, "cancel", observed_cancel)
    asyncio.run(exercise_cancel(task, connection, "cancel-locked", attempted,
                                begin_fault=begin_fault, end_fault=end_fault))
    assert any("locked" in message for message in failures)


def test_cancel_dead_owner_reports_interruption_and_preserves_unverified_step(tmp_path, monkeypatch):
    store = task_store.TaskStore(tmp_path / "tasks.sqlite")
    steps, plan_hash = normalize_plan(STEPS)
    store.claim("dead-owner", plan_hash, steps, None, task_owner.current_owner())
    assert store.start_step("dead-owner", 0)
    monkeypatch.setattr(task_owner, "owner_alive", lambda _owner: False)

    result = store.cancel("dead-owner")
    assert result["status"] == "interrupted"
    assert result["cancel_requested"] is False
    assert [step["status"] for step in result["steps"]] == ["unverified", "skipped"]
    assert store.cancel("dead-owner") == result
    assert store.get("dead-owner") == result
    replay, claimed = store.claim("dead-owner", plan_hash, steps, None, task_owner.current_owner())
    assert claimed is False
    assert replay == result
