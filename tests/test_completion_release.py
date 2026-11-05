"""Terminal success must follow writer cleanup, including its failure window."""
import asyncio
import threading

import pytest

from stellaris_autopilot.completion_workflow import CompletionWorkflow
from stellaris_autopilot.task_owner import current_owner
from stellaris_autopilot.task_store import TaskStore
from test_completion_workflow import BODY, TIME, FakeConnection


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_terminal_state_is_not_published_until_writer_cleanup_exits(tmp_path, monkeypatch, cleanup_fails):
    from stellaris_autopilot import direct_mcp

    connection = FakeConnection(tmp_path)
    monkeypatch.setattr(direct_mcp, "_configuration", lambda _: connection.config)
    host = CompletionWorkflow(connection, tmp_path / "tasks.sqlite", poll_seconds=.001)
    entered, release = threading.Event(), threading.Event()
    original = connection.run_plan

    def cleanup(callback):
        result = original(callback)
        entered.set()
        assert release.wait(3)
        if cleanup_fails:
            raise OSError("writer cleanup failed")
        return result

    connection.run_plan = cleanup
    try:
        host.submit("cleanup", BODY, TIME)
        assert entered.wait(2)
        assert host.store.get("cleanup")["status"] == "running"
        release.set()
        result = asyncio.run(host.result("cleanup", 3))
        assert result["status"] == ("unverified" if cleanup_fails else "completed")
        before = len(connection.calls), len(connection.observations)
        assert host.submit("cleanup", BODY, TIME) == result
        assert (len(connection.calls), len(connection.observations)) == before
    finally:
        release.set()
        asyncio.run(host.shutdown())


def test_dead_completion_owner_does_not_claim_its_old_pause_is_current(tmp_path, monkeypatch):
    from stellaris_autopilot import task_owner

    store = TaskStore(tmp_path / "tasks.sqlite")
    store.claim("dead", "hash", [{"kind": "survey_planet"}], None, current_owner(), task_metadata={
        "task_kind": "natural_completion", "goal_state": "satisfied",
        "final_pause": {"status": "verified"}, "completion_scope": "observed_goal_and_final_pause",
    })
    monkeypatch.setattr(task_owner, "owner_alive", lambda _: False)
    result = store.get("dead")
    assert result["status"] == "interrupted" and result["goal_state"] == "unknown"
    assert result["final_pause"]["status"] == "unverified"
    assert result["steps"][0]["status"] == "skipped"
