"""Offline natural-completion tests; no game attach or native commands."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
import threading
import time

import pytest

from stellaris_autopilot import completion_workflow as workflow
from stellaris_autopilot.task_workflow import TaskWorkflow


BODY = {"kind": "survey_body", "fleet_id": "3", "planet_id": "8", "system_id": "92"}
SYSTEM = {"kind": "survey_system", "fleet_id": "3", "system_id": "92", "planet_ids": ["9", "8"]}
ENTER = {"kind": "enter_system", "fleet_id": "3", "planet_id": "8", "system_id": "92"}
TIME = {"allow_time_advance": True, "max_game_days": 90, "max_wall_seconds": 120, "final_pause": True}


def _state():
    return {
        "date": "2200.01.01", "country_id": "0", "metadata": {"missing_fields": []},
        "fleets_by_id": {"3": {
            "id": "3", "owner": "0", "controller": 0, "ship_class": "shipclass_science_ship",
            "mobile": True, "combat": {"start_date": "0.01.01"}, "system_id": "91",
            "orders_known": True, "actions": {}, "current_order": {},
        }},
        "space": {
            "systems_by_id": {"92": {"id": "92", "visited": True, "planets_known": True,
                                        "planet_ids": ["8", "9"]}},
            "planets_by_id": {
                "8": {"id": "8", "system_id": "92", "surveyed_known": True, "surveyed": False},
                "9": {"id": "9", "system_id": "92", "surveyed_known": True, "surveyed": False},
            },
            "surveyed_planets_known": True, "surveyed_planet_ids": [],
        },
    }


class FakeConnection:
    """Advance only on observations while unpaused, preserving typed receipts."""

    def __init__(self, path):
        self.config_path = path / "direct.json"
        self.config = SimpleNamespace(country_id="0", campaign_id="campaign", session_token="a" * 32)
        self.native_bridge = SimpleNamespace(supports_wakeup=True)
        self._plan_local = SimpleNamespace(guard={})
        self.writer = threading.Lock()
        self.state = _state()
        self.paused, self.protected_pause = True, False
        self.calls, self.guards, self.observations = [], [], []
        self.runs, self.sequence, self.ticks = 0, 0, 0
        self.autocomplete = True
        self.tick_hook = None
        self.observation_hook = None
        self.dispatch_hook = None
        self.release = threading.Event()
        self.block_run = False
        self.entered_run = threading.Event()
        self.resumed = threading.Event()

    @property
    def fleet(self):
        return self.state["fleets_by_id"]["3"]

    def snapshot(self):
        self.sequence += 1
        return f"snapshot-{self.sequence}"

    def run_plan(self, callback):
        self.runs += 1
        if not self.writer.acquire(blocking=False):
            return {"status": "busy"}
        try:
            self._plan_local.guard = {}
            self.entered_run.set()
            if self.block_run:
                assert self.release.wait(3), "test did not release blocked writer"
            return callback(self, self.config)
        finally:
            self.writer.release()

    def survey(self, planet_id):
        body = self.state["space"]["planets_by_id"][planet_id]
        body["surveyed"] = True
        if planet_id not in self.state["space"]["surveyed_planet_ids"]:
            self.state["space"]["surveyed_planet_ids"].append(planet_id)

    def tick(self):
        self.ticks += 1
        if self.tick_hook is not None:
            self.tick_hook(self)
        elif self.autocomplete:
            current = self.fleet.get("current_order", {}).get("survey_planet_order")
            if current is not None:
                self.survey(str(current["deposit_holder"]["id"]))
                self.fleet["current_order"] = {}
            if self.fleet.get("actions", {}).get("move_to"):
                self.fleet["system_id"] = "92"

    def request_observation(self):
        if not self.paused:
            self.tick()
        result = {
            "status": "observed", "snapshot": self.snapshot(), "state": deepcopy(self.state),
            "native_observation": {"execution_source": "window_message",
                                   "runtime": {"paused": self.paused, "protected_pause": self.protected_pause}},
        }
        if self.observation_hook is not None:
            self.observation_hook(self, result)
        self.observations.append(deepcopy(result))
        return result

    def receipt(self, step, status):
        self.calls.append({**deepcopy(step), "while_paused": self.paused})
        self.guards.append(deepcopy(self._plan_local.guard))
        if self.dispatch_hook is not None:
            replacement = self.dispatch_hook(self, step)
            if replacement is not None:
                return replacement
        return {"status": status, "snapshot": self.snapshot(),
                "context": {"objects": {"fleet_id": deepcopy(self.fleet)}}}

    def fleet_command(self, kind, fleet_id, planet_id):
        assert kind == "survey_planet" and fleet_id == "3"
        self.fleet["current_order"] = {"survey_planet_order": {
            "order_id": 100 + len(self.calls), "can_reach": "yes", "progress": 0,
            "deposit_holder": {"type": 0, "id": int(planet_id)},
        }}
        return self.receipt({"kind": kind, "fleet_id": fleet_id, "planet_id": planet_id}, "command_verified")

    def issue_order(self, step):
        assert step["fleet_id"] == "3"
        if step["kind"] == "fleet_move":
            self.fleet["actions"] = {"move_to": {"coordinate": {"x": 0, "y": 0, "origin": 92}},
                                      "wait": {"time": 30}}
        elif step["kind"] == "fleet_stop":
            self.fleet["actions"], self.fleet["current_order"] = {}, {}
        else:
            raise AssertionError(f"unexpected fake order {step}")
        return self.receipt(step, "order_verified")

    def set_paused(self, paused):
        self.paused = paused
        if not paused:
            self.resumed.set()
        return self.receipt({"kind": "set_paused", "paused": paused}, "pause_verified")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from stellaris_autopilot import direct_mcp

    connection = FakeConnection(tmp_path)
    monkeypatch.setattr(direct_mcp, "_configuration", lambda _path: connection.config)
    task = workflow.CompletionWorkflow(connection, tmp_path / "stable" / "tasks.sqlite", poll_seconds=.001)
    yield task, connection
    connection.release.set()
    asyncio.run(task.shutdown())


def finish(task, request_id="goal"):
    result = asyncio.run(task.result(request_id, wait_seconds=3))
    assert result["status"] != "running", result
    return result


def submit(task, intent=BODY, authorization=TIME, request_id="goal", expected_snapshot=None):
    return task.submit(request_id, intent, authorization, expected_snapshot)


def kinds(connection):
    return [call["kind"] for call in connection.calls]


def test_submit_returns_durable_running_record_before_worker_finishes(setup):
    task, connection = setup
    connection.block_run = True
    started = time.monotonic()
    record = submit(task)
    assert time.monotonic() - started < .5
    assert record["status"] == "running" and record["task_kind"] == "natural_completion"
    assert connection.entered_run.wait(.5)
    assert connection.calls == []
    connection.release.set()
    assert finish(task)["status"] == "completed"


def test_single_body_runs_order_then_host_wait_and_verified_final_pause(setup):
    task, connection = setup
    submit(task)
    result = finish(task)
    assert result["status"] == "completed"
    assert result["goal_state"] == "satisfied"
    assert result["completion_scope"] == "observed_goal_and_final_pause"
    assert result["steps"][0]["status"] == "goal_satisfied"
    assert result["final_pause"]["status"] == "verified" and connection.paused is True
    assert kinds(connection) == ["survey_planet", "set_paused", "set_paused"]
    assert [call.get("paused") for call in connection.calls[1:]] == [False, True]
    assert connection.calls[0]["while_paused"] is True
    assert connection.runs == 1
    assert result["last_evidence"]["value"] is True
    assert len(connection.observations) >= 4
    for guard, call in zip(connection.guards, connection.calls):
        assert guard["expected_snapshot"].startswith("snapshot-")
        assert guard["steps"] == [{key: value for key, value in call.items() if key != "while_paused"}]


def test_system_surveys_frozen_targets_in_authorized_order_with_pause_between(setup):
    task, connection = setup
    submit(task, SYSTEM)
    result = finish(task)
    assert result["status"] == "completed"
    surveys = [call for call in connection.calls if call["kind"] == "survey_planet"]
    assert [call["planet_id"] for call in surveys] == ["9", "8"]
    assert all(call["while_paused"] for call in surveys)
    assert all(step["status"] == "goal_satisfied" for step in result["steps"])
    assert connection.state["space"]["surveyed_planet_ids"] == ["9", "8"]
    assert kinds(connection) == ["survey_planet", "set_paused", "set_paused"] * 2


def test_already_surveyed_target_is_skipped_without_dispatch_or_time(setup):
    task, connection = setup
    connection.survey("9")
    submit(task, SYSTEM)
    result = finish(task)
    assert result["status"] == "completed"
    assert [call["planet_id"] for call in connection.calls if call["kind"] == "survey_planet"] == ["8"]
    assert result["steps"][0]["status"] == "goal_satisfied"


def test_already_satisfied_goal_only_observes_and_verifies_pause(setup):
    task, connection = setup
    connection.survey("8")
    submit(task)
    result = finish(task)
    assert result["status"] == "completed" and connection.calls == []
    assert result["final_pause"]["already_paused"] is True
    assert connection.ticks == 0


def test_survey_cleanup_waits_before_dispatching_next_body(setup):
    task, connection = setup
    per_order_ticks = {}

    def tick(fake):
        order = fake.fleet["current_order"]["survey_planet_order"]
        body = str(order["deposit_holder"]["id"])
        per_order_ticks[body] = per_order_ticks.get(body, 0) + 1
        fake.survey(body)
        if per_order_ticks[body] == 2:
            fake.fleet["current_order"] = {}

    connection.tick_hook = tick
    submit(task, SYSTEM)
    result = finish(task)
    assert result["status"] == "completed"
    assert per_order_ticks["9"] == 2
    assert [call["planet_id"] for call in connection.calls if call["kind"] == "survey_planet"] == ["9", "8"]


def test_enter_system_pauses_then_stops_only_matching_move_order(setup):
    task, connection = setup
    submit(task, ENTER)
    result = finish(task)
    assert result["status"] == "completed"
    assert kinds(connection) == ["fleet_move", "set_paused", "set_paused", "fleet_stop"]
    assert connection.calls[-1]["while_paused"] is True
    assert connection.fleet["system_id"] == "92" and connection.fleet["actions"] == {}
    assert result["last_evidence"]["evidence"]["completion_scope"] == "system_entry_only"


def test_enter_system_accepts_consumed_move_action_leaving_only_original_bounded_wait(setup):
    task, connection = setup

    def consume_move(fake):
        fake.fleet.update(system_id="92", actions={"wait": {"time": 30}})

    connection.tick_hook = consume_move
    submit(task, ENTER)
    result = finish(task)
    assert result["status"] == "completed"
    assert kinds(connection) == ["fleet_move", "set_paused", "set_paused", "fleet_stop"]
    assert connection.fleet["actions"] == {} and connection.paused


@pytest.mark.parametrize("current_system,remaining_wait", [("91", 30), ("92", 31), ("92", 0)])
def test_consumed_move_with_wrong_system_or_unrecognized_wait_is_never_stopped(setup, current_system, remaining_wait):
    task, connection = setup

    def consume_move(fake):
        fake.fleet.update(system_id=current_system, actions={"wait": {"time": remaining_wait}})

    connection.tick_hook = consume_move
    submit(task, ENTER)
    result = finish(task)
    assert result["status"] == "interrupted"
    assert "fleet_stop" not in kinds(connection)


def test_external_pause_is_never_unpaused_again(setup):
    task, connection = setup
    connection.tick_hook = lambda fake: setattr(fake, "paused", True)
    submit(task)
    result = finish(task)
    assert result["status"] == "interrupted"
    assert [call["paused"] for call in connection.calls if call["kind"] == "set_paused"] == [False]
    assert result["final_pause"]["status"] == "verified"


def test_protected_pause_interrupts_without_resuming(setup):
    task, connection = setup
    connection.tick_hook = lambda fake: setattr(fake, "protected_pause", True)
    submit(task)
    result = finish(task)
    assert result["status"] == "interrupted"
    assert [call["paused"] for call in connection.calls if call["kind"] == "set_paused"] == [False, True]


def test_order_disappearance_with_unsurveyed_ledger_never_means_completion(setup):
    task, connection = setup
    connection.tick_hook = lambda fake: fake.fleet.update(current_order={})
    submit(task)
    result = finish(task)
    assert result["status"] == "interrupted"
    assert "disappeared" in result["reason"]
    assert result["goal_state"] == "pending"
    assert result["last_evidence"]["value"] is False
    assert kinds(connection).count("survey_planet") == 1
    assert result["final_pause"]["status"] == "verified"


def test_external_order_change_is_not_replaced_or_stopped(setup):
    task, connection = setup

    def replace(fake):
        fake.fleet["current_order"]["survey_planet_order"]["order_id"] += 1

    connection.tick_hook = replace
    submit(task, SYSTEM)
    result = finish(task)
    assert result["status"] == "interrupted"
    assert kinds(connection).count("survey_planet") == 1 and "fleet_stop" not in kinds(connection)
    assert result["steps"][1]["status"] == "skipped"


@pytest.mark.parametrize("deposit_holder", [{"type": False, "id": 8}, {"type": 0, "id": 8.0}])
def test_malformed_survey_target_identity_interrupts_even_if_python_equality_matches(setup, deposit_holder):
    task, connection = setup

    def replace(fake):
        fake.fleet["current_order"]["survey_planet_order"]["deposit_holder"] = deposit_holder

    connection.tick_hook = replace
    submit(task, authorization={**TIME, "max_wall_seconds": 1})
    result = finish(task)
    assert result["status"] == "interrupted"
    assert kinds(connection).count("survey_planet") == 1


@pytest.mark.parametrize("fault", ["unknown_ledger", "unknown_survey", "membership_change"])
def test_new_unknown_or_changed_frozen_evidence_stops_future_work(setup, fault):
    task, connection = setup

    def corrupt(fake):
        if fault == "unknown_ledger":
            fake.state["space"]["surveyed_planets_known"] = False
        elif fault == "unknown_survey":
            fake.state["space"]["planets_by_id"]["9"]["surveyed_known"] = False
        else:
            fake.state["space"]["systems_by_id"]["92"]["planet_ids"] = ["8"]
            fake.state["space"]["planets_by_id"].pop("9", None)

    connection.tick_hook = corrupt
    submit(task, SYSTEM)
    result = finish(task)
    assert result["status"] == "needs_observation"
    assert result["last_evidence"]["value"] is None
    assert kinds(connection).count("survey_planet") == 1
    assert result["steps"][1]["status"] == "skipped"
    assert result["final_pause"]["status"] == "verified"


def test_date_moving_backwards_interrupts_and_does_not_write_cleanup_to_uncertain_game(setup):
    task, connection = setup
    connection.tick_hook = lambda fake: fake.state.update(date="2199.12.30")
    submit(task)
    result = finish(task)
    assert result["status"] == "interrupted" and "backwards" in result["reason"]
    assert result["final_pause"]["status"] == "not_attempted"
    assert kinds(connection) == ["survey_planet", "set_paused"]


def test_game_day_deadline_stops_waiting_and_verifies_final_pause(setup):
    task, connection = setup
    connection.tick_hook = lambda fake: fake.state.update(date="2200.01.02")
    submit(task, authorization={**TIME, "max_game_days": 1})
    result = finish(task)
    assert result["status"] == "deadline" and "game-day" in result["reason"]
    assert result["final_pause"]["status"] == "verified" and connection.paused
    assert kinds(connection).count("survey_planet") == 1


def test_wall_clock_deadline_is_host_bounded_and_finally_pauses(setup):
    task, connection = setup
    connection.autocomplete = False
    submit(task, authorization={**TIME, "max_wall_seconds": 1})
    result = finish(task)
    assert result["status"] == "deadline" and "wall-clock" in result["reason"]
    assert result["final_pause"]["status"] == "verified" and connection.paused


def test_cancel_retains_current_order_pauses_and_never_replays_same_id(setup):
    task, connection = setup
    connection.autocomplete = False
    submit(task, SYSTEM)
    assert connection.resumed.wait(1)
    assert task.cancel("goal")["status"] == "cancel_requested"
    result = finish(task)
    assert result["status"] == "cancelled"
    assert kinds(connection).count("survey_planet") == 1
    assert connection.fleet["current_order"]
    assert result["steps"][1]["status"] == "skipped"
    assert result["final_pause"]["status"] == "verified"
    calls = deepcopy(connection.calls)
    assert submit(task, SYSTEM) == result and connection.calls == calls and connection.runs == 1


def test_cancel_during_verified_dispatch_finishes_receipt_without_resuming_game(setup):
    task, connection = setup
    entered, release = threading.Event(), threading.Event()

    def hold(_fake, step):
        if step["kind"] == "survey_planet":
            entered.set()
            assert release.wait(2), "test did not release dispatch"

    connection.dispatch_hook = hold
    submit(task, SYSTEM)
    assert entered.wait(1)
    try:
        assert task.cancel("goal")["status"] == "cancel_requested"
    finally:
        release.set()
    result = finish(task)
    assert result["status"] == "cancelled"
    assert [step["status"] for step in result["steps"]] == ["verified", "skipped"]
    assert kinds(connection) == ["survey_planet"]
    assert connection.fleet["current_order"] and connection.paused
    assert result["final_pause"]["status"] == "verified"


def test_cancelling_result_wait_keeps_authorized_background_runner_alive(setup):
    task, connection = setup
    connection.autocomplete = False
    submit(task)
    assert connection.resumed.wait(1)

    async def cancel_waiter():
        waiter = asyncio.create_task(task.result("goal", wait_seconds=30))
        await asyncio.sleep(.02)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        return await task.result("goal")

    record = asyncio.run(cancel_waiter())
    assert record["status"] == "running" and record["cancel_requested"] is False
    connection.autocomplete = True
    assert finish(task)["status"] == "completed"
    assert kinds(connection).count("survey_planet") == 1


def test_same_id_is_idempotent_while_running_and_changed_authorization_conflicts(setup):
    task, connection = setup
    connection.block_run = True
    first = submit(task)
    assert submit(task)["plan_hash"] == first["plan_hash"]
    assert submit(task, authorization={**TIME, "max_game_days": 89})["status"] == "conflict"
    assert submit(task, intent={**BODY, "planet_id": "9"})["status"] == "conflict"
    assert submit(task, expected_snapshot="different")["status"] == "conflict"
    assert connection.runs == 1
    connection.release.set()
    result = finish(task)
    assert submit(task) == result


def test_busy_writer_returns_without_queue_or_observation_and_same_id_cannot_replay(setup):
    task, connection = setup
    assert connection.writer.acquire(blocking=False)
    try:
        started = time.monotonic()
        submit(task)
        result = finish(task)
        assert time.monotonic() - started < 1
        assert result["status"] == "busy"
        assert connection.calls == [] and connection.observations == []
        assert result["steps"][0]["status"] == "skipped"
    finally:
        connection.writer.release()
    assert submit(task) == result and connection.runs == 1


def test_natural_completion_and_short_plans_share_one_request_identity(setup):
    task, connection = setup
    short = TaskWorkflow(connection, task.store.path)
    first = short.execute("short-id", [{"kind": "set_paused", "paused": True}])
    assert first["status"] == "completed"
    assert submit(task, request_id="short-id")["status"] == "conflict"
    assert connection.runs == 1
    submit(task)
    assert finish(task)["status"] == "completed"
    assert short.execute("goal", [{"kind": "set_paused", "paused": True}])["status"] == "conflict"
    assert connection.runs == 2


def test_shutdown_cancels_future_steps_joins_worker_and_rejects_new_work(setup):
    task, connection = setup
    connection.autocomplete = False
    submit(task, SYSTEM)
    assert connection.resumed.wait(1)
    asyncio.run(task.shutdown())
    result = asyncio.run(task.result("goal"))
    assert result["status"] == "cancelled"
    assert result["final_pause"]["status"] == "verified" and connection.paused
    assert task._active == {}
    assert kinds(connection).count("survey_planet") == 1
    assert submit(task, request_id="after-close")["status"] == "blocked"
    assert task.store.get("after-close")["status"] == "not_found"


@pytest.mark.parametrize("cancel_count", [1, 2])
def test_cancelled_shutdown_waits_for_blocked_worker_cleanup_before_propagating(setup, monkeypatch, cancel_count):
    task, connection = setup
    connection.autocomplete = False
    entered_cleanup, release_cleanup = threading.Event(), threading.Event()
    original = workflow._Run.final_pause

    def blocked_cleanup(run):
        entered_cleanup.set()
        assert release_cleanup.wait(3), "test did not release final cleanup"
        return original(run)

    monkeypatch.setattr(workflow._Run, "final_pause", blocked_cleanup)
    submit(task, SYSTEM)
    assert connection.resumed.wait(1)
    worker = task._active["goal"][0]

    async def cancel_shutdown():
        stopping = asyncio.create_task(task.shutdown())
        try:
            assert await asyncio.to_thread(entered_cleanup.wait, 1)
            for _ in range(cancel_count):
                stopping.cancel()
                await asyncio.sleep(.02)
                assert not stopping.done()
                assert worker.is_alive()
            release_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await stopping
            assert not worker.is_alive()
            assert task._active == {}
        finally:
            release_cleanup.set()

    asyncio.run(cancel_shutdown())
    result = asyncio.run(task.result("goal"))
    assert result["status"] == "cancelled"
    assert result["final_pause"]["status"] == "verified" and connection.paused
    assert kinds(connection).count("survey_planet") == 1


@pytest.mark.parametrize("key", ["actions", "current_order", "order", "orders", "queued_orders"])
def test_existing_order_containers_are_never_overwritten_on_initial_connection(setup, key):
    task, connection = setup
    connection.fleet[key] = {"fixture_existing_order": 1}
    submit(task)
    result = finish(task)
    assert result["status"] == "blocked"
    assert connection.calls == []
    assert connection.fleet[key] == {"fixture_existing_order": 1}


def test_initial_unknown_fleet_orders_prevents_dispatch_without_defaulting_to_empty(setup):
    task, connection = setup
    connection.fleet["orders_known"] = False
    submit(task)
    result = finish(task)
    assert result["status"] == "needs_observation"
    assert connection.calls == []
    assert result["last_evidence"]["value"] is None


@pytest.mark.parametrize("fault,expected", [
    ("unpaused", "blocked"), ("protected", "blocked"), ("native_missing", "blocked"),
    ("native_wrong_source", "needs_observation"), ("pause_not_boolean", "needs_observation"),
    ("bad_date", "needs_observation"), ("unknown_ledger", "needs_observation"),
    ("existing_order", "blocked"), ("stale", "stale"),
])
def test_initial_preconditions_block_before_any_precise_order(setup, fault, expected):
    task, connection = setup
    snapshot = None
    if fault == "unpaused":
        connection.paused = False
    elif fault == "protected":
        connection.protected_pause = True
    elif fault == "native_missing":
        connection.native_bridge.supports_wakeup = False
    elif fault == "native_wrong_source":
        connection.observation_hook = lambda _fake, result: result["native_observation"].update(execution_source="poll")
    elif fault == "pause_not_boolean":
        connection.observation_hook = lambda _fake, result: result["native_observation"]["runtime"].update(paused=1)
    elif fault == "bad_date":
        connection.state["date"] = "2200.01.31"
    elif fault == "unknown_ledger":
        connection.state["space"]["surveyed_planets_known"] = False
    elif fault == "existing_order":
        connection.fleet["current_order"] = {"survey_planet_order": {"order_id": 123}}
    else:
        snapshot = "old-snapshot"
    submit(task, expected_snapshot=snapshot)
    result = finish(task)
    assert result["status"] == expected
    assert connection.calls == []
    assert all(step["status"] == "skipped" for step in result["steps"])


def test_unverified_dispatch_is_recorded_once_and_future_steps_skipped(setup):
    task, connection = setup
    connection.dispatch_hook = lambda _fake, step: {"status": "submitted"} if step["kind"] == "survey_planet" else None
    submit(task, SYSTEM)
    result = finish(task)
    assert result["status"] == "unverified"
    assert [step["status"] for step in result["steps"]] == ["unverified", "skipped"]
    assert kinds(connection) == ["survey_planet"]
    assert submit(task, SYSTEM) == result and connection.runs == 1


@pytest.mark.parametrize("wait", [-1, 31, True, 1.0, "1", None])
def test_result_wait_is_strictly_bounded_without_game_access(setup, wait):
    task, connection = setup
    with pytest.raises(ValueError):
        asyncio.run(task.result("missing", wait_seconds=wait))
    assert connection.runs == 0 and connection.calls == []


def test_missing_results_and_cancel_are_offline_without_creating_store(setup):
    task, connection = setup
    assert asyncio.run(task.result("missing"))["status"] == "not_found"
    assert task.cancel("missing")["status"] == "not_found"
    assert connection.runs == 0 and not task.store.path.exists()


@pytest.mark.parametrize("authorization", [
    {**TIME, "allow_time_advance": 1}, {**TIME, "final_pause": False},
    {**TIME, "max_game_days": True}, {**TIME, "max_wall_seconds": 3601},
])
def test_schema_rejection_precedes_background_claim_or_game_access(setup, authorization):
    task, connection = setup
    with pytest.raises(ValueError):
        submit(task, authorization=authorization)
    assert connection.runs == 0 and not task.store.path.exists()
