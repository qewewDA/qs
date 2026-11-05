"""Offline completion integration through the actual direct executor and writer lease.

The native responses and normalized observations are simulated.  No subprocess,
game, Windows input, or live service is created by these tests.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import threading
import time

import pytest

pytest.importorskip("mcp")

from stellaris_autopilot import direct_mcp as direct
from stellaris_autopilot.completion_workflow import CompletionWorkflow
from stellaris_autopilot.fleet_native import FleetOperation
from stellaris_autopilot.native import PauseRequest
from stellaris_autopilot.observe import Observation
from test_direct_mcp import baseline, configure_observer, invoke, records
from test_direct_mcp import config_path as _config_fixture


INTENT = {"kind": "survey_body", "fleet_id": "1", "planet_id": "104", "system_id": "247"}
AUTHORIZATION = {"allow_time_advance": True, "max_game_days": 10,
                 "max_wall_seconds": 10, "final_pause": True}


@pytest.fixture
def config_path(tmp_path):
    path = _config_fixture.__wrapped__(tmp_path)
    raw = json.loads(path.read_text())
    raw["allowed_native_actions"] = ["survey_planet", "set_paused"]
    raw["verification_timeout_seconds"] = 0.05
    path.write_text(json.dumps(raw))
    return path


class SurveyAndPauseNative:
    """One fused fake for real survey dispatch, pause proof and correlated dumps."""

    supports_wakeup = True

    def __init__(self, config, observer, *, block_running=False, bad_correlation=None):
        self.config, self.observer = config, observer
        self.last_runtime = {"paused": True, "protected_pause": False}
        self.submissions, self.queries, self.cancellations = {}, {}, []
        self.revision = 0
        self.pending_pause = None
        self.block_running, self.bad_correlation = block_running, bad_correlation
        self.running_dump_entered, self.release_running_dump = threading.Event(), threading.Event()

    def _publish(self, state=None):
        self.revision += 1
        observation = self.observer.after or self.observer.before
        self.observer.after = Observation(deepcopy(state or observation.state),
                                          f"integration-{self.revision}", time.time())

    def _response(self, kind, token, status):
        return {"kind": kind, "token": token, "country_id": "0", "status": status,
                "execution_source": "window_message", "runtime": dict(self.last_runtime)}

    def submit(self, selection, token):
        script = self.config.inbox_path.read_text(encoding="ascii")
        self.submissions[token] = selection
        if isinstance(selection, FleetOperation):
            assert selection.kind == "survey_planet"
            assert selection.fleet_id == "1" and selection.planet_id == "104"
            assert selection.system_id == "247"
            assert self.last_runtime["paused"] is True
            state = deepcopy((self.observer.after or self.observer.before).state)
            state["fleets_by_id"]["1"]["current_order"] = {"survey_planet_order": {
                "deposit_holder": {"type": 0, "id": 104}, "order_id": 5,
                "progress": 0, "can_reach": "yes",
            }}
            self._publish(state)
        else:
            assert isinstance(selection, PauseRequest)
            assert f"qs_direct_done_{token}" in script
            assert f"is_paused = {'yes' if selection.paused else 'no'}" in script
            self.pending_pause = token, selection.paused
            self.last_runtime = {"paused": selection.paused, "protected_pause": False}
        return {"status": "pending", "token": token}

    def run_inbox(self, token):
        assert self.pending_pause is not None
        operation_token, paused = self.pending_pause
        state = deepcopy((self.observer.after or self.observer.before).state)
        state["direct_bridge"]["done_flags"].append(f"qs_direct_done_{operation_token}")
        self.last_runtime = {"paused": paused, "protected_pause": False}
        self._publish(state)
        self.queries[token] = self._response("run_inbox", token, "inbox_executed")
        self.pending_pause = None
        return {"status": "pending", "token": token}

    def request_dump(self, token):
        state = deepcopy((self.observer.after or self.observer.before).state)
        if self.last_runtime["paused"] is False:
            if self.block_running and not self.running_dump_entered.is_set():
                self.running_dump_entered.set()
                assert self.release_running_dump.wait(5), "test did not release running observation"
            state["date"] = "2200.01.02"
            state["space"]["surveyed_planet_ids"] = ["104"]
            state["space"]["planets_by_id"]["104"]["surveyed"] = True
            state["fleets_by_id"]["1"].pop("current_order", None)
        self._publish(state)
        response = self._response("request_observation", token, "dump_requested")
        if self.bad_correlation == "token":
            response["token"] = "f" * 32
        elif self.bad_correlation == "kind":
            response["kind"] = "run_inbox"
        self.queries[token] = response
        return {"status": "pending", "token": token}

    def result(self, token):
        if token in self.queries:
            return deepcopy(self.queries[token])
        selection = self.submissions[token]
        return {"status": "submitted", "kind": selection.kind, "country_id": "0",
                "token": token, **selection.to_dict()}

    def cancel(self, token):
        self.cancellations.append(token)
        return {"status": "cancelled", "token": token}


def setup(config_path, monkeypatch, **options):
    before = baseline()
    fleet = {"id": "1", "ship_class": "shipclass_science_ship", "mobile": True,
             "owner": 0, "controller": 0, "orders_known": True, "system_id": "247",
             "combat": {"start_date": "0.01.01"}, "actions": {}}
    before.state.update(fleets=[fleet], fleets_by_id={"1": fleet}, space={
        "visited_systems_known": True, "visited_system_ids": ["247"],
        "surveyed_planets_known": True, "surveyed_planet_ids": [],
        "systems_by_id": {"247": {"id": "247", "visited": True,
                                     "planets_known": True, "planet_ids": ["104"]}},
        "planets_by_id": {"104": {"id": "104", "system_id": "247",
                                     "surveyed_known": True, "surveyed": False}},
    })
    observer = configure_observer(monkeypatch, before)
    backend = SurveyAndPauseNative(direct._configuration(config_path), observer, **options)
    connection = direct.DirectConnection(config_path, native_bridge=backend)
    workflow = CompletionWorkflow(connection, config_path.parent / "tasks.sqlite", poll_seconds=0.001)
    return observer, backend, connection, workflow


def wait_finished(workflow, request_id):
    result = asyncio.run(workflow.result(request_id, wait_seconds=5))
    assert result["status"] != "running", result
    return result


def test_real_executor_surveys_one_body_with_verified_time_commands_and_journals(config_path, monkeypatch):
    observer, native, _connection, workflow = setup(config_path, monkeypatch)
    try:
        workflow.submit("survey-integrated", INTENT, AUTHORIZATION)
        result = wait_finished(workflow, "survey-integrated")
    finally:
        asyncio.run(workflow.shutdown())

    assert result["status"] == "completed", result
    assert result["completion_scope"] == "observed_goal_and_final_pause"
    assert result["goal_state"] == "satisfied"
    assert result["final_pause"]["status"] == "verified"
    assert result["steps"][0]["status"] == "goal_satisfied"
    assert result["steps"][0]["result"]["status"] == "command_verified"
    assert result["steps"][0]["result"]["goal_state"] == "pending"
    selections = list(native.submissions.values())
    assert [selection.kind for selection in selections] == ["survey_planet", "set_paused", "set_paused"]
    assert [selection.paused for selection in selections if isinstance(selection, PauseRequest)] == [False, True]
    assert native.last_runtime == {"paused": True, "protected_pause": False}
    assert observer.after.state["space"]["surveyed_planet_ids"] == ["104"]
    journal = records(config_path)
    assert len(journal) == 3 and all(item["status"] == "verified" for item in journal)
    assert len({item["id"] for item in journal}) == 3
    assert {query["kind"] for query in native.queries.values()} == {"request_observation", "run_inbox"}
    assert all(query["execution_source"] == "window_message" for query in native.queries.values())
    config = direct._configuration(config_path)
    assert "# disarmed" in config.inbox_path.read_text()
    assert not (config.user_dir / ".qs_direct_controller.lock").exists()


def test_whole_runner_writer_lease_blocks_competing_write_and_results_remain_offline(config_path, monkeypatch):
    _observer, native, connection, workflow = setup(config_path, monkeypatch, block_running=True)
    second = direct.DirectConnection(config_path, native_bridge=native)
    server = direct.build_direct_server(config_path, connection=second, native_bridge=native)
    try:
        workflow.submit("held-survey", INTENT, AUTHORIZATION)
        assert native.running_dump_entered.wait(3)
        assert (native.config.user_dir / ".qs_direct_controller.lock").exists()
        assert invoke(server, "set_paused", {"paused": True})["status"] == "busy"
        assert invoke(server, "task_result", {"request_id": "held-survey"})["status"] == "running"
        assert invoke(server, "cancel_task", {"request_id": "held-survey"})["status"] == "cancel_requested"
        native.release_running_dump.set()
        result = wait_finished(workflow, "held-survey")
    finally:
        native.release_running_dump.set()
        asyncio.run(workflow.shutdown())

    assert result["status"] == "cancelled", result
    assert result["final_pause"]["status"] == "verified"
    assert all(item["status"] == "verified" for item in records(config_path))
    assert not (native.config.user_dir / ".qs_direct_controller.lock").exists()
    submitted = len(native.submissions)
    config_path.unlink()
    monkeypatch.setattr(direct, "DirectObserver", lambda *_: pytest.fail("offline result accessed the game"))
    assert invoke(server, "task_result", {"request_id": "held-survey", "wait_seconds": 1}) == result
    assert invoke(server, "cancel_task", {"request_id": "held-survey"}) == result
    retry_host = CompletionWorkflow(connection, workflow.store.path)
    assert retry_host.submit("held-survey", INTENT, AUTHORIZATION) == result
    assert len(native.submissions) == submitted


@pytest.mark.parametrize("bad_correlation", ["token", "kind"])
def test_fresh_dump_without_matching_native_query_cannot_start_a_completion_task(
        config_path, monkeypatch, bad_correlation):
    _observer, native, _connection, workflow = setup(config_path, monkeypatch,
                                                   bad_correlation=bad_correlation)
    try:
        workflow.submit("bad-native-proof", INTENT, AUTHORIZATION)
        result = wait_finished(workflow, "bad-native-proof")
    finally:
        asyncio.run(workflow.shutdown())
    assert result["status"] == "unverified", result
    assert result["final_pause"]["status"] == "not_attempted"
    assert not native.submissions and native.queries
    assert records(config_path) == []
    assert not (native.config.user_dir / ".qs_direct_controller.lock").exists()


def test_mcp_completion_schema_requires_typed_intent_and_explicit_time_authorization(config_path):
    tools = {tool.name: tool for tool in asyncio.run(direct.build_direct_server(config_path).list_tools())}
    submit = tools["submit_completion"]
    schema = submit.inputSchema
    assert set(schema["required"]) == {"request_id", "intent", "time_authorization"}
    assert schema["properties"]["intent"]["discriminator"]["propertyName"] == "kind"
    assert len(schema["properties"]["intent"]["oneOf"]) == 3
    authorization_ref = schema["properties"]["time_authorization"]["$ref"].split("/")[-1]
    authorization = schema["$defs"][authorization_ref]
    assert set(authorization["required"]) == {
        "allow_time_advance", "max_game_days", "max_wall_seconds", "final_pause",
    }
    assert authorization["properties"]["allow_time_advance"]["const"] is True
    assert authorization["properties"]["final_pause"]["const"] is True
    assert submit.annotations.idempotentHint is True and submit.annotations.readOnlyHint is False
    result = tools["task_result"]
    assert result.annotations.readOnlyHint is True
    assert result.inputSchema["properties"]["wait_seconds"]["minimum"] == 0
    assert result.inputSchema["properties"]["wait_seconds"]["maximum"] == 30
