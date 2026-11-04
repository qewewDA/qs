"""Task identity and persisted evidence work independently of any live game."""
from concurrent.futures import ThreadPoolExecutor
import copy
import threading

import pytest

from stellaris_autopilot.task_owner import current_owner
from stellaris_autopilot.task_plan import normalize_plan
from stellaris_autopilot.task_store import TaskStore


STEPS = [{"kind": "queue_ship", "starbase_id": "1", "design_id": str(index)} for index in (2, 3, 4)]


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "stable" / "tasks.sqlite")


def claim(store, request_id="test-1", steps=None, expected_snapshot=None):
    normalized, digest = normalize_plan(copy.deepcopy(STEPS if steps is None else steps), expected_snapshot)
    return store.claim(request_id, digest, normalized, expected_snapshot, current_owner())


def test_missing_result_and_cancel_do_not_create_storage(store):
    assert store.get("missing") == {"status": "not_found", "request_id": "missing"}
    assert store.cancel("missing") == {"status": "not_found", "request_id": "missing"}
    assert not store.path.exists()
    assert not store.path.parent.exists()


@pytest.mark.parametrize("method", ["get", "cancel"])
@pytest.mark.parametrize("request_id", ["../task", "", True, "a" * 97, "任务"])
def test_offline_lookups_validate_id_before_touching_storage(store, method, request_id):
    with pytest.raises(ValueError):
        getattr(store, method)(request_id)
    assert not store.path.exists()


def test_same_id_same_plan_returns_original_without_reclaiming(store):
    original, claimed = claim(store)
    duplicate, reclaimed = claim(store)
    assert claimed is True and reclaimed is False
    assert duplicate == original
    assert duplicate["owner"] == current_owner()
    assert duplicate["expected_snapshot"] is None


def test_same_id_different_plan_conflicts_without_replacing_original(store):
    original, _ = claim(store)
    conflict, claimed = claim(store, steps=[{**STEPS[0], "design_id": "10"}])
    assert conflict["status"] == "conflict" and claimed is False
    assert store.get("test-1") == original


def test_independent_ids_and_returned_data_are_isolated(store):
    first, _ = claim(store, "first")
    second, _ = claim(store, "second", steps=[{**STEPS[0], "design_id": "99"}])
    first["steps"][0]["request"]["design_id"] = "999"
    fetched = store.get("first")
    fetched["steps"][0]["status"] = "forged"
    assert store.get("first")["steps"][0]["request"]["design_id"] == "2"
    assert store.get("first")["steps"][0]["status"] == "pending"
    assert store.get("second") == second


def test_concurrent_claims_have_exactly_one_winner(store):
    # Establish the shared schema first; the race here exercises atomic claims.
    claim(store, "schema")
    store.finish("schema", "completed")
    barrier = threading.Barrier(8)

    def compete(_index):
        independent = TaskStore(store.path)
        barrier.wait(timeout=5)
        return claim(independent, "race")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(compete, range(8)))
    assert sum(claimed for _record, claimed in results) == 1
    assert all(record["request_id"] == "race" for record, _claimed in results)
    assert len({record["created_at"] for record, _claimed in results}) == 1


def test_concurrent_different_plans_with_same_id_have_one_winner_one_conflict(store):
    claim(store, "schema")
    store.finish("schema", "completed")
    barrier = threading.Barrier(2)

    def compete(design):
        barrier.wait(timeout=5)
        return claim(TaskStore(store.path), "intent-race", steps=[{**STEPS[0], "design_id": design}])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, ["11", "12"]))
    assert sum(claimed for _record, claimed in results) == 1
    assert sorted(record["status"] for record, _claimed in results) == ["conflict", "running"]
    winner = next(record for record, claimed in results if claimed)
    assert store.get("intent-race") == winner


def test_result_read_does_not_start_a_writer_transaction(store, monkeypatch):
    original, _ = claim(store)

    def forbidden_transaction():
        pytest.fail("Result lookup must not acquire a task writer transaction")

    monkeypatch.setattr(store, "_transaction", forbidden_transaction)
    assert store.get("test-1") == original


def test_step_claim_is_durable_and_cannot_be_submitted_twice(store):
    claim(store)
    assert store.start_step("test-1", 0) is True
    assert TaskStore(store.path).get("test-1")["steps"][0]["status"] == "executing"
    with pytest.raises(ValueError, match="twice"):
        store.start_step("test-1", 0)


def test_cancel_before_step_stops_future_work_and_preserves_pending_evidence(store):
    claim(store)
    cancelled = store.cancel("test-1")
    assert cancelled["status"] == "cancel_requested"
    assert cancelled["cancel_requested"] is True
    assert store.start_step("test-1", 0) is False
    finished = store.finish("test-1", "cancelled")
    assert [step["status"] for step in finished["steps"]] == ["skipped"] * 3
    assert store.cancel("test-1") == finished


def test_cancellation_cannot_erase_active_step_verification_or_payment(store):
    claim(store)
    assert store.start_step("test-1", 0)
    store.cancel("test-1")
    receipt = {"status": "construction_verified", "snapshot": "after", "resource_change": {"alloys": -100}}
    store.step_result("test-1", 0, "verified", receipt)
    assert store.start_step("test-1", 1) is False
    finished = store.finish("test-1", "cancelled")
    assert finished["steps"][0]["status"] == "verified"
    assert finished["steps"][0]["result"] == receipt
    assert [step["status"] for step in finished["steps"][1:]] == ["skipped", "skipped"]


def test_interrupted_active_step_remains_unknown_and_pending_steps_are_skipped(store):
    claim(store)
    store.start_step("test-1", 0)
    result = store.finish("test-1", "unverified", reason="lost reply")
    assert result["status"] == "unverified"
    assert [step["status"] for step in result["steps"]] == ["unverified", "skipped", "skipped"]
    assert claim(store)[1] is False


def test_result_requires_an_active_step_claim(store):
    claim(store)
    with pytest.raises(ValueError, match="active durable claim"):
        store.step_result("test-1", 0, "verified", {"snapshot": "forged"})


@pytest.mark.parametrize("state", ["completed", "partial", "blocked", "unverified", "cancelled", "busy"])
def test_terminal_cancel_and_duplicate_claim_preserve_original_result(store, state):
    claim(store)
    final = store.finish("test-1", state)
    assert store.cancel("test-1") == final
    duplicate, claimed = claim(store)
    assert claimed is False and duplicate == final


def test_dead_owner_is_interrupted_without_reclaiming_or_losing_verified_prefix(store, monkeypatch):
    from stellaris_autopilot import task_owner

    claim(store)
    store.start_step("test-1", 0)
    store.step_result("test-1", 0, "verified", {"snapshot": "after-first"})
    store.start_step("test-1", 1)
    monkeypatch.setattr(task_owner, "owner_alive", lambda _owner: False)
    interrupted = TaskStore(store.path).get("test-1")
    assert interrupted["status"] == "interrupted"
    assert [step["status"] for step in interrupted["steps"]] == ["verified", "unverified", "skipped"]
    duplicate, claimed = claim(TaskStore(store.path))
    assert claimed is False and duplicate["status"] == "interrupted"
    assert duplicate["steps"][0]["result"] == {"snapshot": "after-first"}


@pytest.mark.parametrize("alive", [True, None])
def test_live_or_unknown_owner_does_not_authorize_recovery(store, monkeypatch, alive):
    from stellaris_autopilot import task_owner

    claim(store)
    store.start_step("test-1", 0)
    monkeypatch.setattr(task_owner, "owner_alive", lambda _owner: alive)
    record, claimed = claim(store)
    assert record["status"] == "running" and claimed is False
    assert record["steps"][0]["status"] == "executing"
