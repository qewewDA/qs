"""Pause requests and trigger-only postcondition publication, without a game."""
from copy import deepcopy
from dataclasses import asdict, replace
import asyncio
import json
import time

import pytest

from stellaris_autopilot.direct import DirectConfig, ownership_header, publish_pause_verification
from stellaris_autopilot.native import NativeResearchBridge, PauseRequest, pause_request
from stellaris_autopilot.observe import Observation
from stellaris_autopilot.pdx import PDXNode, loads


SESSION = "a" * 32
TOKEN = "b" * 32
OTHER_TOKEN = "c" * 32
NON_BOOLEANS = [0, 1, -1, 0.0, 1.0, "yes", "no", "true", "false", "", None, [], {}, b"true"]


@pytest.fixture
def pause_config(tmp_path):
    user_dir = tmp_path / "test game directory"
    user_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return DirectConfig(
        user_dir=user_dir, state_dir=state_dir, country_id="0", campaign_id="pause-test",
        verified_game_version="4.5.2-test", session_token=SESSION,
        allowed_native_actions=("set_paused",),
        single_player_confirmed=True, non_ironman_confirmed=True,
    )


def nodes(node):
    yield node
    for _, value in node.entries:
        if isinstance(value, PDXNode):
            yield from nodes(value)


@pytest.mark.parametrize("paused", [True, False])
def test_pause_request_preserves_exact_boolean_and_fixed_kind(paused):
    request = pause_request(paused)
    assert isinstance(request, PauseRequest)
    assert request.paused is paused
    assert request.kind == "set_paused"
    assert request.to_dict() == {"paused": paused}


@pytest.mark.parametrize("paused", NON_BOOLEANS)
def test_pause_request_does_not_coerce_truthy_or_falsey_values(paused):
    with pytest.raises(ValueError, match="boolean"):
        pause_request(paused)


@pytest.mark.parametrize("paused", [True, False])
def test_native_submit_sends_only_typed_pause_request_without_attaching_or_starting_processes(paused):
    bridge = object.__new__(NativeResearchBridge)
    calls = []

    def capture(method, *arguments):
        calls.append((method, arguments))
        return {"status": "submitted", "token": TOKEN}

    bridge._call = capture
    assert bridge.submit(pause_request(paused), TOKEN) == {"status": "submitted", "token": TOKEN}
    assert calls == [("enqueue", ({"kind": "set_paused", "token": TOKEN, "country_id": "0",
                                   "paused": paused},))]


@pytest.mark.parametrize("paused", NON_BOOLEANS)
def test_native_submit_revalidates_programmatically_constructed_pause_requests(paused):
    bridge = object.__new__(NativeResearchBridge)
    bridge._call = lambda *_: pytest.fail("invalid pause input reached native dispatch")
    with pytest.raises(ValueError, match="boolean"):
        bridge.submit(PauseRequest(paused), TOKEN)


@pytest.mark.parametrize("token", ["", "b" * 31, "b" * 33, "B" * 32, "g" * 32, None,
                                   TOKEN + "\neffect game_paused = no", TOKEN + "\0"])
def test_native_pause_rejects_invalid_operation_tokens_before_dispatch(token):
    bridge = object.__new__(NativeResearchBridge)
    bridge._call = lambda *_: pytest.fail("invalid token reached native dispatch")
    with pytest.raises(ValueError):
        bridge.submit(pause_request(True), token)


@pytest.mark.parametrize("paused,expected", [(True, "yes"), (False, "no")])
def test_publisher_checks_pause_trigger_and_writes_one_guarded_receipt_without_setting_pause(
        pause_config, paused, expected):
    path = publish_pause_verification(pause_config, paused, TOKEN)
    assert path == pause_config.inbox_path
    header, command, dump = path.read_text(encoding="ascii").splitlines()
    assert header + "\n" == ownership_header(pause_config)
    assert command.startswith("effect ")
    assert dump == "debug_dumpgamestate qs_direct"
    root = loads(command.removeprefix("effect "))
    assert [key for key, _ in root.items()] == ["if"]
    body = root.get("if")
    limit = body.get("limit")
    assert limit.get("is_paused") == expected
    assert limit.get("has_country_flag") == f"qs_direct_session_{SESSION}"
    assert limit.get("NOT").get("has_country_flag") == f"qs_direct_done_{TOKEN}"
    assert {key: limit.get(key) for key in ("is_ai", "is_ironman", "is_multiplayer")} == {
        "is_ai": "no", "is_ironman": "no", "is_multiplayer": "no",
    }
    assert set(key for key, _ in body.items()) == {"limit", "set_country_flag", "log"}
    assert body.get_all("set_country_flag") == [f"qs_direct_done_{TOKEN}"]
    keys = [key for node in nodes(root) for key, _ in node.items()]
    assert keys.count("is_paused") == 1
    assert keys.count("set_country_flag") == 1
    assert not {"set_paused", "game_paused", "pause", "every_country", "every_owned_fleet"} & set(keys)
    assert "game_paused" not in command and "country:" not in command


def test_distinct_verification_requests_never_reuse_a_prior_operation_receipt(pause_config):
    publish_pause_verification(pause_config, True, TOKEN)
    publish_pause_verification(pause_config, False, OTHER_TOKEN)
    script = pause_config.inbox_path.read_text(encoding="ascii")
    assert f"qs_direct_done_{TOKEN}" not in script
    assert f"qs_direct_done_{OTHER_TOKEN}" in script
    assert "is_paused = no" in script
    assert script.count("set_country_flag") == 1


@pytest.mark.parametrize("paused", NON_BOOLEANS)
def test_invalid_boolean_cannot_publish_or_modify_an_existing_owned_inbox(pause_config, paused):
    original = ownership_header(pause_config) + "# disarmed\n"
    pause_config.inbox_path.write_text(original, encoding="ascii")
    with pytest.raises(ValueError):
        publish_pause_verification(pause_config, paused, TOKEN)
    assert pause_config.inbox_path.read_text(encoding="ascii") == original


@pytest.mark.parametrize("token", ["", "b" * 31, "B" * 32, "g" * 32, None,
                                   TOKEN + "\neffect game_paused = no", TOKEN + '" }', TOKEN + "\0"])
def test_pause_verification_token_cannot_add_a_command(pause_config, token):
    with pytest.raises(ValueError):
        publish_pause_verification(pause_config, True, token)
    assert not pause_config.inbox_path.exists()


@pytest.mark.parametrize("field,value", [
    ("allowed_native_actions", ()), ("allowed_native_actions", ("select_research",)),
    ("single_player_confirmed", False), ("non_ironman_confirmed", False),
    ("session_token", SESSION + "\neffect game_paused = no"),
    ("country_id", "0\neffect game_paused = no"),
])
def test_unconfirmed_unauthorized_or_injectable_configuration_cannot_publish(pause_config, field, value):
    with pytest.raises(ValueError):
        publish_pause_verification(replace(pause_config, **{field: value}), True, TOKEN)
    assert not pause_config.inbox_path.exists()


def test_pause_verification_honors_stop_without_clobbering_the_owned_inbox(pause_config):
    original = ownership_header(pause_config) + "# disarmed\n"
    pause_config.inbox_path.write_text(original, encoding="ascii")
    (pause_config.state_dir / "STOP").touch()
    with pytest.raises(ValueError, match="STOP"):
        publish_pause_verification(pause_config, True, TOKEN)
    assert pause_config.inbox_path.read_text(encoding="ascii") == original


def test_pause_verification_refuses_a_foreign_inbox(pause_config):
    pause_config.inbox_path.write_text("foreign commands\n", encoding="ascii")
    with pytest.raises(ValueError, match="owned|ownership"):
        publish_pause_verification(pause_config, True, TOKEN)
    assert pause_config.inbox_path.read_text(encoding="ascii") == "foreign commands\n"


class PauseObserver:
    def __init__(self):
        self.before = Observation({
            "date": "2200.01.15", "country_id": "0",
            "direct_bridge": {"session_flags": [f"qs_direct_session_{SESSION}"], "done_flags": [],
                              "receipt_only": True},
            "metadata": {"campaign_id": "pause-test", "game_version": "4.5.2-test",
                         "player_country_ids": ["0"], "ironman": "no", "multiplayer": "no",
                         "missing_fields": []},
        }, "pause_before", time.time())
        self.after = None

    def read(self):
        return deepcopy(self.after or self.before)


class SimulatedPauseBackend:
    """Correlated local responses; it never creates a process or sends input."""

    def __init__(self, config, observer, trace, *, receipt="correct", ack_changes=None, query_changes=None,
                 stale=False, same_digest=False, stop_on_query=False):
        self.config, self.observer, self.trace = config, observer, trace
        self.receipt, self.ack_changes, self.query_changes = receipt, ack_changes or {}, query_changes or {}
        self.stale, self.same_digest, self.stop_on_query = stale, same_digest, stop_on_query
        self.selection = None
        self.token = None
        self.submissions, self.queries, self.cancellations = [], {}, []

    def submit(self, selection, token):
        # The trigger-only verification file must be armed before dispatch.
        script = self.config.inbox_path.read_text(encoding="ascii")
        assert f"qs_direct_done_{token}" in script
        assert f"is_paused = {'yes' if selection.paused else 'no'}" in script
        self.selection, self.token = selection, token
        self.submissions.append((selection, token))
        self.trace.append("submit")
        return {"status": "pending", "token": token}

    def run_inbox(self, token):
        assert self.selection is not None
        self.trace.append("run_inbox")
        self.queries[token] = {
            "status": "inbox_executed", "token": token, "kind": "run_inbox", "country_id": "0",
            "runtime": {"paused": self.selection.paused, "protected_pause": False}, **self.query_changes,
        }
        state = deepcopy(self.observer.before.state)
        state["direct_bridge"]["done_flags"] = (
            [f"qs_direct_done_{self.token}"] if self.receipt == "correct"
            else [f"qs_direct_done_{OTHER_TOKEN}"] if self.receipt == "wrong" else []
        )
        modified = self.observer.before.modified_at if self.stale else time.time()
        digest = self.observer.before.digest if self.same_digest else "pause_after"
        self.observer.after = Observation(state, digest, modified)
        if self.stop_on_query:
            (self.config.state_dir / "STOP").touch()
        return {"status": "pending", "token": token}

    def result(self, token):
        if token == self.token:
            return {"status": "submitted", "token": token, "kind": "set_paused", "country_id": "0",
                    "paused": self.selection.paused, **self.ack_changes}
        assert token in self.queries, "The controller queried an unsubmitted operation"
        return deepcopy(self.queries[token])

    def cancel(self, token):
        self.cancellations.append(token)
        return {"status": "cancelled", "token": token}


@pytest.fixture
def pause_connection(pause_config, monkeypatch):
    pytest.importorskip("mcp")
    from stellaris_autopilot import direct_mcp
    from stellaris_autopilot.journal import Journal

    raw = asdict(pause_config)
    raw["user_dir"], raw["state_dir"] = str(pause_config.user_dir), str(pause_config.state_dir)
    raw["verification_timeout_seconds"], raw["poll_seconds"] = 0.05, 0.001
    config_path = pause_config.state_dir.parent / "pause_config.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    observer, trace = PauseObserver(), []
    monkeypatch.setattr(direct_mcp, "DirectObserver", lambda *_: observer)
    publish = direct_mcp.publish_pause_verification

    def tracked_publish(*args):
        path = publish(*args)
        trace.append("publish_verification")
        return path

    monkeypatch.setattr(direct_mcp, "publish_pause_verification", tracked_publish)

    def records():
        journal = Journal(pause_config.state_dir / "journal.sqlite")
        try:
            return journal.records()
        finally:
            journal.close()

    def configure(**kwargs):
        backend = SimulatedPauseBackend(pause_config, observer, trace, **kwargs)
        connection = direct_mcp.DirectConnection(config_path, native_bridge=backend)
        return connection, observer, backend, trace, records

    return configure


@pytest.mark.parametrize("paused", [True, False])
def test_connection_requires_trigger_receipt_and_correlated_live_pause_without_advancing_date(
        pause_connection, pause_config, paused):
    connection, observer, backend, trace, records = pause_connection()
    result = connection.set_paused(paused)
    assert result["status"] == "pause_verified"
    assert result["pause"] == {"paused": paused}
    assert result["date"] == observer.before.state["date"] == observer.after.state["date"]
    assert result["native_verification"]["kind"] == "run_inbox"
    assert result["native_verification"]["status"] == "inbox_executed"
    assert result["native_verification"]["runtime"]["paused"] is paused
    assert result["native_verification"]["runtime"]["protected_pause"] is False
    assert result["native_verification"]["token"] != result["token"]
    assert trace[:3] == ["publish_verification", "submit", "run_inbox"]
    assert len(backend.submissions) == 1
    assert records()[0]["status"] == "verified"
    assert "# disarmed" in pause_config.inbox_path.read_text(encoding="ascii")


@pytest.mark.parametrize("receipt", ["missing", "wrong"])
def test_native_pause_ack_and_live_state_without_unique_game_receipt_stay_unverified(pause_connection, receipt):
    connection, _, backend, _, records = pause_connection(receipt=receipt)
    result = connection.set_paused(True)
    assert result["status"] == "unverified"
    assert records()[0]["status"] == "unverified"
    assert len(backend.submissions) == 1
    assert backend.submissions[0][1] in backend.cancellations


@pytest.mark.parametrize("runtime", [
    {"paused": False, "protected_pause": False}, {"paused": None, "protected_pause": False},
    {"paused": 1, "protected_pause": False}, {"paused": "yes", "protected_pause": False},
    {"protected_pause": False}, None,
])
def test_game_receipt_with_wrong_or_unknown_live_pause_state_stays_unverified(pause_connection, runtime):
    connection, _, _, _, records = pause_connection(query_changes={"runtime": runtime})
    assert connection.set_paused(True)["status"] == "unverified"
    assert records()[0]["status"] == "unverified"


@pytest.mark.parametrize("runtime", [
    {"paused": True}, {"paused": True, "protected_pause": None},
    {"paused": True, "protected_pause": 0}, {"paused": True, "protected_pause": 1},
    {"paused": True, "protected_pause": "no"}, {"paused": True, "protected_pause": []},
])
def test_missing_or_nonboolean_protected_pause_state_cannot_verify(pause_connection, runtime):
    connection, _, _, _, records = pause_connection(query_changes={"runtime": runtime})
    assert connection.set_paused(True)["status"] == "unverified"
    assert records()[0]["status"] == "unverified"


@pytest.mark.parametrize("change", [
    {"token": OTHER_TOKEN}, {"country_id": "1"}, {"kind": "request_observation"},
    {"status": "dump_requested"}, {"status": "ambiguous"}, {"status": "pending"},
])
def test_pause_query_must_match_token_kind_country_and_exact_execution_status(pause_connection, change):
    connection, _, _, _, records = pause_connection(query_changes=change)
    assert connection.set_paused(True)["status"] == "unverified"
    assert records()[0]["status"] == "unverified"


@pytest.mark.parametrize("change", [
    {"token": OTHER_TOKEN}, {"country_id": "1"}, {"kind": "queue_district"}, {"paused": 1},
])
def test_native_pause_ack_must_match_the_typed_request_before_verification_query(pause_connection, change):
    connection, _, backend, _, records = pause_connection(ack_changes=change)
    assert connection.set_paused(True)["status"] == "unverified"
    assert backend.queries == {}
    assert records()[0]["status"] == "unverified"


@pytest.mark.parametrize("options", [{"stale": True}, {"same_digest": True}])
def test_pause_receipt_and_live_state_need_a_fresh_changed_snapshot(pause_connection, options):
    connection, _, _, _, records = pause_connection(**options)
    assert connection.set_paused(True)["status"] == "unverified"
    assert records()[0]["status"] == "unverified"


def test_stop_while_pause_query_is_pending_cancels_operation_and_query_and_disarms(
        pause_connection, pause_config):
    connection, _, backend, _, records = pause_connection(
        query_changes={"status": "pending"}, stop_on_query=True)
    result = connection.set_paused(True)
    assert result["status"] == "unverified"
    assert "Stopped" in result["reason"]
    assert backend.token in backend.cancellations
    assert set(backend.queries) <= set(backend.cancellations)
    assert records()[0]["status"] == "unverified"
    assert "# disarmed" in pause_config.inbox_path.read_text(encoding="ascii")


def test_pause_rejected_before_native_transfer_is_reconciled_without_a_query(pause_connection, pause_config):
    connection, _, backend, _, records = pause_connection(ack_changes={"status": "blocked", "submitted": False})
    assert connection.set_paused(False)["status"] == "blocked"
    assert backend.queries == {}
    assert records()[0]["status"] == "reconciled"
    assert "# disarmed" in pause_config.inbox_path.read_text(encoding="ascii")


@pytest.mark.parametrize("paused", [0, 1, "false", "yes"])
def test_mcp_pause_argument_is_strict_before_the_connection_is_called(pause_connection, monkeypatch, paused):
    from mcp.server.fastmcp.exceptions import ToolError
    from stellaris_autopilot import direct_mcp

    connection, _, backend, _, _ = pause_connection()
    calls = []

    def capture(self, value):
        calls.append(value)
        return {"status": "captured"}

    monkeypatch.setattr(direct_mcp.DirectConnection, "set_paused", capture)
    server = direct_mcp.build_direct_server(connection.config_path, native_bridge=backend)
    with pytest.raises(ToolError):
        asyncio.run(server._tool_manager.call_tool("set_paused", {"paused": paused}))
    assert calls == []


@pytest.mark.parametrize("paused", [True, False])
def test_mcp_pause_schema_and_valid_booleans_have_no_extra_command_parameters(pause_connection, monkeypatch, paused):
    from stellaris_autopilot import direct_mcp

    connection, _, backend, _, _ = pause_connection()
    calls = []

    def capture(self, value):
        calls.append(value)
        return {"status": "captured", "paused": value}

    monkeypatch.setattr(direct_mcp.DirectConnection, "set_paused", capture)
    server = direct_mcp.build_direct_server(connection.config_path, native_bridge=backend)
    tool = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == "set_paused")
    assert set(tool.inputSchema["properties"]) == {"paused"}
    assert tool.inputSchema["properties"]["paused"]["type"] == "boolean"
    assert asyncio.run(server._tool_manager.call_tool("set_paused", {"paused": paused})) == {
        "status": "captured", "paused": paused,
    }
    assert len(calls) == 1 and calls[0] is paused
