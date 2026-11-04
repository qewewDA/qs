"""Offline integration through the real MCP and the unchanged paid-action executor."""
import asyncio
import copy
import json
import threading
import time

import pytest

pytest.importorskip("mcp")

from stellaris_autopilot import direct_mcp as direct
from stellaris_autopilot.existing_session import ExistingGameSession
from stellaris_autopilot.observe import Observation
from stellaris_autopilot.task_workflow import TaskWorkflow
from test_direct_mcp import config_path as _config_fixture
from test_direct_mcp import SimulatedShipNative, invoke, records, ship_observer
from test_ship_commands import job, set_items


STEPS = [{"kind": "queue_ship", "starbase_id": "12", "design_id": "7"}] * 3


@pytest.fixture
def config_path(tmp_path):
    return _config_fixture.__wrapped__(tmp_path)


class SerialNative(SimulatedShipNative):
    def submit(self, command, token):
        self.submissions[token] = command
        before = self.observer.after or self.observer.before
        state = copy.deepcopy(before.state)
        item = job()
        item["id"] = str(30 + len(self.submissions))
        items = list(state["construction"]["items_by_id"].values())
        set_items(state, [*items, item])
        state["resources"]["alloys"] -= 100 if self.outcome != "wrong_payment" else 99
        self.observer.after = Observation(state, f"after-{len(self.submissions)}", time.time())


def setup(config_path, monkeypatch, *, outcome="paid"):
    observer = ship_observer(config_path, monkeypatch)
    backend = SerialNative(observer, outcome=outcome)
    connection = direct.DirectConnection(config_path, native_bridge=backend)
    return observer, backend, connection


@pytest.mark.parametrize("batch", [False, True])
def test_three_paid_orders_keep_identical_verification_without_extra_reads(config_path, monkeypatch, batch):
    observer, backend, connection = setup(config_path, monkeypatch)
    reads = []
    original_read = observer.read

    def counted():
        reads.append(True)
        return original_read()

    observer.read = counted
    server = direct.build_direct_server(config_path, native_bridge=backend, connection=connection)
    initial = observer.before.state["resources"]["alloys"]
    if batch:
        result = invoke(server, "execute_plan", {"request_id": "three-ships", "steps": STEPS,
                                                 "expected_snapshot": "before"})
        assert result["status"] == "completed"
        receipts = [step["result"] for step in result["steps"]]
    else:
        receipts = [invoke(server, "queue_ship", {"starbase_id": "12", "design_id": "7"}) for _ in STEPS]
    assert all(receipt["status"] == "command_verified" and receipt["goal_state"] == "pending"
               for receipt in receipts)
    assert observer.after.state["resources"]["alloys"] == initial - 300
    assert len(observer.after.state["construction"]["items_by_id"]) == 3
    assert [record["status"] for record in records(config_path)] == ["verified"] * 3
    assert len(reads) == 6 and len(backend.submissions) == 3 and not backend.queries
    assert not (direct._configuration(config_path).user_dir / ".qs_direct_controller.lock").exists()


@pytest.mark.parametrize("outcome,expected", [("wrong_payment", "unverified"), ("rejected", "blocked")])
def test_batch_does_not_bypass_payment_or_native_rejection(config_path, monkeypatch, outcome, expected):
    _, backend, connection = setup(config_path, monkeypatch, outcome=outcome)
    workflow = TaskWorkflow(connection, config_path.parent / "tasks.sqlite")
    result = workflow.execute("bad-step", STEPS)
    assert result["status"] == expected
    assert [item["status"] for item in result["steps"]] == [expected, "skipped", "skipped"]
    assert len(backend.submissions) == 1
    assert workflow.execute("bad-step", STEPS) == result
    assert len(backend.submissions) == 1


def test_result_retry_and_cancel_survive_deleted_live_configuration(config_path, monkeypatch):
    _, backend, connection = setup(config_path, monkeypatch)
    server = direct.build_direct_server(config_path, native_bridge=backend, connection=connection)
    args = {"request_id": "saved-result", "steps": STEPS}
    original = invoke(server, "execute_plan", args)
    config_path.unlink()
    monkeypatch.setattr(direct, "DirectObserver", lambda *_: pytest.fail("Offline result read the game"))
    for name, data in (("execute_plan", args), ("task_result", {"request_id": "saved-result"}),
                       ("cancel_task", {"request_id": "saved-result"})):
        assert invoke(server, name, data) == original
    assert len(backend.submissions) == 3
    assert invoke(server, "execute_plan", {**args, "steps": STEPS[:1]})["status"] == "conflict"


def test_expected_snapshot_returns_current_relevant_state_without_dispatch(config_path, monkeypatch):
    _, backend, connection = setup(config_path, monkeypatch)
    workflow = TaskWorkflow(connection, config_path.parent / "tasks.sqlite")
    result = workflow.execute("old-context", STEPS, "outdated")
    assert result["status"] == "stale"
    receipt = result["steps"][0]["result"]
    assert receipt["snapshot"] == receipt["context"]["snapshot"] == "before"
    assert "resources" in receipt["context"] and "dispatch_state" not in receipt
    assert not backend.submissions and records(config_path) == []


def test_configuration_change_stops_remaining_steps(config_path, monkeypatch):
    _, backend, connection = setup(config_path, monkeypatch)
    original = connection.queue_ship

    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        raw = json.loads(config_path.read_text())
        raw["allowed_native_actions"] = []
        config_path.write_text(json.dumps(raw))
        return result

    connection.queue_ship = revoke
    result = TaskWorkflow(connection, config_path.parent / "tasks.sqlite").execute("revoke", STEPS)
    assert result["status"] == "partial"
    assert [step["status"] for step in result["steps"]] == ["verified", "blocked", "skipped"]
    assert len(backend.submissions) == 1


def test_unrelated_snapshot_change_stops_conservative_plan(config_path, monkeypatch):
    observer, backend, connection = setup(config_path, monkeypatch)
    original = connection.queue_ship

    def user_changed(*args, **kwargs):
        result = original(*args, **kwargs)
        observer.after = Observation(observer.after.state, "manual-change", time.time())
        return result

    connection.queue_ship = user_changed
    result = TaskWorkflow(connection, config_path.parent / "tasks.sqlite").execute("user-change", STEPS)
    assert result["status"] == "partial"
    assert [step["status"] for step in result["steps"]] == ["verified", "stale", "skipped"]
    assert len(backend.submissions) == 1


def test_plan_mailbox_lease_blocks_a_different_connection_immediately(config_path, monkeypatch):
    _, backend, connection = setup(config_path, monkeypatch)
    second = direct.DirectConnection(config_path, native_bridge=backend)
    workflow = TaskWorkflow(connection, config_path.parent / "tasks.sqlite")
    entered, release = threading.Event(), threading.Event()
    original = connection.queue_ship
    result = []

    def hold(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    connection.queue_ship = hold
    thread = threading.Thread(target=lambda: result.append(workflow.execute("held", STEPS)))
    thread.start()
    try:
        assert entered.wait(2)
        with pytest.raises(direct.DirectLockBusy):
            second.queue_ship("12", "7")
        server = direct.build_direct_server(config_path, native_bridge=backend, connection=second)
        assert invoke(server, "queue_ship", {"starbase_id": "12", "design_id": "7"})["status"] == "busy"
        assert workflow.store.get("held")["steps"][0]["status"] == "executing"
        assert workflow.store.cancel("held")["status"] == "cancel_requested"
        assert workflow.execute("held", STEPS)["status"] == "running"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert result[0]["status"] == "cancelled"
    assert [step["status"] for step in result[0]["steps"]] == ["verified", "skipped", "skipped"]
    assert len(backend.submissions) == 1


def test_attached_lifecycle_returns_busy_and_offline_results_bypass_it(tmp_path):
    session = ExistingGameSession(tmp_path / "game", tmp_path / "state")
    workflow = TaskWorkflow(session, tmp_path / "state" / "tasks.sqlite")
    entered, release = threading.Event(), threading.Event()

    def hold():
        with session._operation:
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert entered.wait(2)
        assert session.queue_ship("12", "7")["status"] == "busy"
        assert workflow.execute("busy", STEPS)["status"] == "busy"
        assert workflow.store.get("busy")["status"] == "busy"
    finally:
        release.set()
        thread.join(5)


def test_mcp_schema_exposes_typed_short_plan_and_offline_annotations(config_path):
    tools = {tool.name: tool for tool in asyncio.run(direct.build_direct_server(config_path).list_tools())}
    schema = tools["execute_plan"].inputSchema
    assert schema["properties"]["steps"]["maxItems"] == 20
    assert schema["properties"]["steps"]["items"]["discriminator"]["propertyName"] == "kind"
    assert set(schema["properties"]) == {"request_id", "steps", "expected_snapshot"}
    assert tools["execute_plan"].annotations.idempotentHint is True
    assert tools["task_result"].annotations.readOnlyHint is True
    assert tools["cancel_task"].annotations.idempotentHint is True


def test_new_session_uses_explicit_stable_store_before_any_live_access(config_path, monkeypatch, tmp_path):
    _, backend, connection = setup(config_path, monkeypatch)
    path = tmp_path / "campaign" / "tasks.sqlite"
    first = direct.build_direct_server(config_path, native_bridge=backend, connection=connection, task_store_path=path)
    args = {"request_id": "session-independent", "steps": STEPS}
    result = invoke(first, "execute_plan", args)
    assert result["status"] == "completed"
    next_server = direct.build_direct_server(tmp_path / "new-session" / "missing.json",
                                            connection=object(), task_store_path=path)
    assert invoke(next_server, "task_result", {"request_id": args["request_id"]}) == result
    assert invoke(next_server, "execute_plan", args) == result
    assert len(backend.submissions) == 3


def test_writer_cleanup_failure_after_verified_steps_cannot_claim_no_dispatch(config_path, monkeypatch):
    _, backend, connection = setup(config_path, monkeypatch)
    original = connection.run_plan

    def fail_after(callback):
        original(callback)
        raise OSError("writer cleanup failed")

    connection.run_plan = fail_after
    workflow = TaskWorkflow(connection, config_path.parent / "tasks.sqlite")
    result = workflow.execute("cleanup-failed", STEPS)
    assert result["status"] == "partial"
    assert all(step["status"] == "verified" for step in result["steps"])
    assert "no plan step was submitted" not in result["reason"]
    assert workflow.execute("cleanup-failed", STEPS) == result
    assert len(backend.submissions) == 3
