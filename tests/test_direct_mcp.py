import asyncio
import copy
import json
import time

import pytest

pytest.importorskip("mcp")

from stellaris_autopilot import direct_mcp as direct
from stellaris_autopilot.journal import Journal
from stellaris_autopilot.observe import Observation
from mcp.server.fastmcp.exceptions import ToolError


SESSION = "a" * 32


@pytest.fixture
def config_path(tmp_path):
    user_dir = tmp_path / "game"
    user_dir.mkdir()
    path = tmp_path / "direct.json"
    path.write_text(json.dumps({
        "user_dir": str(user_dir), "state_dir": str(tmp_path / "state"), "country_id": "0",
        "campaign_id": "campaign", "verified_game_version": "4.test", "session_token": SESSION,
        "allowed_actions": ["probe_connection", "hold_military_fleets"],
        "single_player_confirmed": True, "non_ironman_confirmed": True,
        "verification_timeout_seconds": 0.02, "poll_seconds": 0.001,
    }), encoding="utf-8")
    return path


def baseline():
    fleet = {"id": "3", "ship_class": "shipclass_military", "mobile": True, "actions": {},
             "combat": {"start_date": "0.01.01"}}
    return Observation({
        "date": "2200.01.01", "country_id": "0", "fleets": [fleet], "fleets_by_id": {"3": fleet},
        "direct_bridge": {"session_flags": [f"qs_direct_session_{SESSION}"], "done_flags": [],
                          "receipt_only": True},
        "metadata": {"campaign_id": "campaign", "game_version": "4.test", "player_country_ids": ["0"],
                     "ironman": "no", "multiplayer": "unknown", "source": "C:/private/empire.txt",
                     "missing_fields": []},
    }, "before", time.time())


class SimulatedObserver:
    def __init__(self, before):
        self.before = before
        self.after = None

    def read(self):
        return copy.deepcopy(self.after or self.before)


def configure_observer(monkeypatch, before=None):
    observer = SimulatedObserver(before or baseline())
    monkeypatch.setattr(direct, "DirectObserver", lambda *_: observer)
    return observer


def publish_receipt(monkeypatch, observer, *, orders=False, digest="after", old_time=False, stop=False):
    original = direct.atomic_publish

    def publish(config, token, *, action_id):
        path = original(config, token, action_id=action_id)
        state = copy.deepcopy(observer.before.state)
        state["date"] = "2200.01.02"
        state["direct_bridge"]["done_flags"] = [f"qs_direct_done_{token}"]
        if orders:
            state["fleets_by_id"]["3"]["actions"] = {"wait": {"time": 29}}
        observer.after = Observation(state, digest, observer.before.modified_at if old_time else time.time())
        if stop:
            (config.state_dir / "STOP").touch()
        return path

    monkeypatch.setattr(direct, "atomic_publish", publish)


def invoke(server, name, arguments=None):
    return asyncio.run(server._tool_manager.call_tool(name, arguments or {}))


def records(config_path):
    config = direct._configuration(config_path)
    journal = Journal(config.state_dir / "journal.sqlite")
    try:
        return journal.records()
    finally:
        journal.close()


def test_tool_contract_separates_observation_from_writes(config_path):
    server = direct.build_direct_server(config_path)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert set(tools) == {"observe_empire", "request_observation", "probe_connection", "execute_action",
                          "issue_order", "select_research", "queue_district", "queue_building", "set_paused", "list_actions", "status", "stop_bridge",
                          "queue_ship", "upgrade_building", "demolish_building", "convert_zone", "cancel_construction",
                          "survey_planet", "build_station", "colonize_planet", "queue_colony_ship",
                          "search_game_knowledge", "get_game_definition", "get_wiki_reference", "knowledge_status"}
    assert tools["observe_empire"].annotations.readOnlyHint is True
    assert tools["probe_connection"].annotations.readOnlyHint is False
    assert tools["request_observation"].annotations.readOnlyHint is False
    assert set(tools["execute_action"].inputSchema["properties"]) == {"action_id"}
    assert set(tools["issue_order"].inputSchema["properties"]) == {"order"}
    assert not tools["probe_connection"].inputSchema.get("properties")


EXTENDED_ARGUMENTS = [
    ("queue_ship", {"starbase_id": "12", "design_id": "7"}),
    ("queue_colony_ship", {"starbase_id": "12", "design_id": "8", "species_id": "4"}),
    ("upgrade_building", {"planet_id": "3", "zone_id": "4", "building_id": "17",
                          "target_building_id": "building_research_lab_2"}),
    ("demolish_building", {"planet_id": "3", "zone_id": "4", "building_id": "17"}),
    ("convert_zone", {"planet_id": "3", "zone_id": "4", "target_zone_id": "zone_research_unity"}),
    ("cancel_construction", {"planet_id": "3", "item_id": "31"}),
    ("survey_planet", {"fleet_id": "1", "planet_id": "104"}),
    ("build_station", {"fleet_id": "2", "planet_id": "104", "station_type": "outpost"}),
    ("colonize_planet", {"fleet_id": "3", "planet_id": "105"}),
]


@pytest.mark.parametrize("name,arguments", EXTENDED_ARGUMENTS)
def test_extended_tools_expose_only_exact_typed_game_arguments(config_path, name, arguments):
    server = direct.build_direct_server(config_path)
    tool = {tool.name: tool for tool in asyncio.run(server.list_tools())}[name]
    assert set(tool.inputSchema["properties"]) == set(arguments)
    assert set(tool.inputSchema["required"]) == set(arguments)
    assert tool.annotations.readOnlyHint is False and tool.annotations.idempotentHint is False
    if name == "build_station":
        assert set(tool.inputSchema["properties"]["station_type"]["enum"]) == {"mining", "research", "outpost"}


@pytest.mark.parametrize("name,arguments", EXTENDED_ARGUMENTS)
def test_extended_tools_require_launcher_owned_backend_before_observation(config_path, monkeypatch, name, arguments):
    monkeypatch.setattr(direct, "DirectObserver", lambda *_: pytest.fail("A missing native backend read a snapshot"))
    assert invoke(direct.build_direct_server(config_path), name, arguments)["status"] == "blocked"
    assert not direct._configuration(config_path).state_dir.exists()


@pytest.mark.parametrize("name,arguments", EXTENDED_ARGUMENTS)
def test_extended_tools_require_local_action_authorization_before_observation(config_path, monkeypatch, name, arguments):
    monkeypatch.setattr(direct, "DirectObserver", lambda *_: pytest.fail("An unauthorized native command read a snapshot"))
    assert invoke(direct.build_direct_server(config_path, native_bridge=object()), name, arguments)["status"] == "blocked"
    assert not direct._configuration(config_path).state_dir.exists()


@pytest.mark.parametrize("name,arguments", EXTENDED_ARGUMENTS)
def test_extended_public_tool_identifiers_cannot_be_console_code(config_path, monkeypatch, name, arguments):
    monkeypatch.setattr(direct, "DirectObserver", lambda *_: pytest.fail("An invalid identifier read a snapshot"))
    fields = dict(arguments)
    id_field = next(key for key in fields if key.endswith("_id"))
    fields[id_field] = "3\neffect cash=1"
    with pytest.raises(ToolError):
        invoke(direct.build_direct_server(config_path, native_bridge=object()), name, fields)


@pytest.mark.parametrize("species_id", [True, 4, "04", "４", "-1", "4294967295", "4\neffect cash=1"])
def test_colony_ship_public_species_identifier_is_a_canonical_observed_handle(config_path, monkeypatch, species_id):
    monkeypatch.setattr(direct, "DirectObserver", lambda *_: pytest.fail("An invalid species identifier read a snapshot"))
    with pytest.raises(ToolError):
        invoke(direct.build_direct_server(config_path, native_bridge=object()), "queue_colony_ship",
               {"starbase_id": "12", "design_id": "8", "species_id": species_id})


def test_observe_has_no_queue_or_journal_write_and_strips_paths(config_path, monkeypatch):
    configure_observer(monkeypatch)
    config = direct._configuration(config_path)
    result = invoke(direct.build_direct_server(config_path), "observe_empire")
    assert result["snapshot"] == "before"
    assert "private" not in json.dumps(result)
    assert not config.inbox_path.exists()
    assert not config.state_dir.exists()


def test_probe_requires_fresh_state_receipt_and_clears_queue(config_path, monkeypatch):
    observer = configure_observer(monkeypatch)
    publish_receipt(monkeypatch, observer)
    result = direct.DirectConnection(config_path).probe()
    assert result["status"] == "connection_verified"
    assert result["action"] == "probe_connection"
    config = direct._configuration(config_path)
    assert "# disarmed" in config.inbox_path.read_text()
    assert records(config_path)[0]["status"] == "verified"
    assert result["token"] in records(config_path)[0]["before_json"]


@pytest.mark.parametrize("digest,old_time", [("before", False), ("after", True)])
def test_old_snapshot_or_unchanged_digest_cannot_verify(config_path, monkeypatch, digest, old_time):
    observer = configure_observer(monkeypatch)
    publish_receipt(monkeypatch, observer, digest=digest, old_time=old_time)
    connection = direct.DirectConnection(config_path)
    assert connection.probe()["status"] == "unverified"
    assert connection.probe()["status"] == "blocked"
    assert records(config_path)[0]["status"] == "unverified"


def test_receipt_alone_never_confirms_a_gameplay_order(config_path, monkeypatch):
    observer = configure_observer(monkeypatch)
    publish_receipt(monkeypatch, observer)
    result = direct.DirectConnection(config_path).execute("hold_military_fleets")
    assert result["status"] == "unverified"
    assert "context" not in result and "dispatch_state" not in result
    assert records(config_path)[0]["status"] == "unverified"


def test_hold_verifies_real_fleet_wait_orders(config_path, monkeypatch):
    observer = configure_observer(monkeypatch)
    observer_read = observer.read
    reads = []

    def counted_read():
        reads.append(True)
        return observer_read()

    monkeypatch.setattr(observer, "read", counted_read)
    publish_receipt(monkeypatch, observer, orders=True)
    result = direct.DirectConnection(config_path).execute("hold_military_fleets")
    assert result["status"] == "action_verified"
    assert result["action"] == "hold_military_fleets"
    assert result["dispatch_state"] == "verified" and result["goal_state"] == "pending"
    assert result["context"]["snapshot"] == result["snapshot"] == "after"
    assert len(reads) == 2  # Existing before/after proof, no receipt-only reread.
    assert records(config_path)[0]["status"] == "verified"


def test_mixed_queue_does_not_verify_a_hold():
    state = {"fleets_by_id": {"3": {"actions": {"wait": {"time": 30}, "move_to": {"target": 4}}}}}
    assert not direct.DirectConnection._orders_verified(state, ["3"])


def test_unobservable_or_active_combat_blocks_before_dispatch(config_path, monkeypatch):
    observation = baseline()
    observation.state["fleets"][0]["combat"]["start_date"] = "2200.01.01"
    configure_observer(monkeypatch, observation)
    with pytest.raises(ValueError, match="combat"):
        direct.DirectConnection(config_path).execute("hold_military_fleets")
    assert not direct._configuration(config_path).inbox_path.exists()


def test_stop_does_not_misreport_a_foreign_inbox_as_an_active_controller(config_path):
    config = direct._configuration(config_path)
    config.inbox_path.write_text("foreign commands")
    with pytest.raises(ToolError, match="ownership"):
        invoke(direct.build_direct_server(config_path), "stop_bridge")
    assert (config.state_dir / "STOP").exists()
    assert config.inbox_path.read_text() == "foreign commands"


@pytest.mark.parametrize("unknown", ["fleet_collection", "ship_class"])
def test_unknown_fleet_data_cannot_be_treated_as_a_complete_target_set(config_path, monkeypatch, unknown):
    observation = baseline()
    if unknown == "fleet_collection":
        observation.state["metadata"]["missing_fields"] = ["fleets"]
    else:
        observation.state["fleets"][0].pop("ship_class")
    configure_observer(monkeypatch, observation)
    with pytest.raises(ValueError, match="unobservable"):
        direct.DirectConnection(config_path).execute("hold_military_fleets")
    assert not direct._configuration(config_path).inbox_path.exists()


@pytest.mark.parametrize("field,value", [("country_id", "1"), ("campaign_id", "other"),
                                         ("game_version", "other"), ("player_country_ids", ["0", "1"])])
def test_identity_mismatch_cannot_publish(config_path, monkeypatch, field, value):
    observation = baseline()
    if field == "country_id":
        observation.state[field] = value
    else:
        observation.state["metadata"][field] = value
    configure_observer(monkeypatch, observation)
    with pytest.raises(ValueError):
        direct.DirectConnection(config_path).probe()
    config = direct._configuration(config_path)
    assert not config.inbox_path.exists()
    assert not config.state_dir.exists()


def test_missing_session_or_stale_observation_cannot_publish(config_path, monkeypatch):
    observation = baseline()
    observation.state["direct_bridge"]["session_flags"] = []
    configure_observer(monkeypatch, observation)
    with pytest.raises(ValueError, match="session"):
        direct.DirectConnection(config_path).probe()
    assert not direct._configuration(config_path).inbox_path.exists()


def test_unknown_and_revoked_actions_do_not_read_or_publish(config_path, monkeypatch):
    monkeypatch.setattr(direct, "DirectObserver", lambda *_: pytest.fail("Unauthorized action read a snapshot"))
    connection = direct.DirectConnection(config_path)
    assert connection.execute("run arbitrary code")["status"] == "blocked"
    raw = json.loads(config_path.read_text())
    raw["allowed_actions"] = []
    config_path.write_text(json.dumps(raw))
    assert connection.probe()["status"] == "blocked"


def test_stop_during_dispatch_retains_unresolved_and_disarms(config_path, monkeypatch):
    observer = configure_observer(monkeypatch)
    publish_receipt(monkeypatch, observer, stop=True)
    assert direct.DirectConnection(config_path).probe()["status"] == "unverified"
    assert records(config_path)[0]["status"] == "unverified"
    assert "# disarmed" in direct._configuration(config_path).inbox_path.read_text()


def test_cross_controller_lock_blocks_publication(config_path, monkeypatch):
    configure_observer(monkeypatch)
    config = direct._configuration(config_path)
    lock = config.user_dir / ".qs_direct_controller.lock"
    lock.write_text("interrupted controller")
    with pytest.raises(ValueError, match="lock"):
        direct.DirectConnection(config_path).probe()
    assert lock.read_text() == "interrupted controller"
    assert not config.inbox_path.exists()


def test_error_after_atomic_publication_stays_unresolved_and_disarms(config_path, monkeypatch):
    configure_observer(monkeypatch)
    original = direct.atomic_publish

    def fail_after_publish(config, token, *, action_id):
        original(config, token, action_id=action_id)
        raise OSError("simulated failure after publication")

    monkeypatch.setattr(direct, "atomic_publish", fail_after_publish)
    with pytest.raises(OSError):
        direct.DirectConnection(config_path).probe()
    assert records(config_path)[0]["status"] == "unverified"
    assert "# disarmed" in direct._configuration(config_path).inbox_path.read_text()


def test_disarm_failure_cannot_mark_an_operation_verified(config_path, monkeypatch):
    observer = configure_observer(monkeypatch)
    publish_receipt(monkeypatch, observer)

    def fail_disarm(_):
        raise OSError("simulated inbox disarm failure")

    monkeypatch.setattr(direct, "disarm_inbox", fail_disarm)
    with pytest.raises(OSError, match="disarm"):
        direct.DirectConnection(config_path).probe()
    assert records(config_path)[0]["status"] == "unverified"
    assert direct.DirectConnection(config_path).probe()["status"] == "blocked"


def precise_observer(config_path, monkeypatch, *, ready=True):
    raw = json.loads(config_path.read_text())
    raw["allowed_orders"] = ["fleet_wait", "fleet_orbit"]
    config_path.write_text(json.dumps(raw))
    before = baseline()
    before.state["fleets"][0]["system_id"] = "92"
    planet = {"id": "3", "owner": 0, "system_id": "92"}
    before.state["planets"] = [planet]
    before.state["planets_by_id"] = {"3": planet}
    before.state["direct_bridge"]["target_flags"] = [f"qs_direct_targets_{SESSION}"] if ready else []
    return configure_observer(monkeypatch, before)


def test_precise_order_permissions_checked_before_observing(config_path, monkeypatch):
    monkeypatch.setattr(direct, "DirectObserver", lambda *_: pytest.fail("Unauthorized order read a snapshot"))
    result = direct.DirectConnection(config_path).issue_order({"kind": "fleet_wait", "fleet_id": "3", "days": 30})
    assert result["status"] == "blocked"
    assert not direct._configuration(config_path).inbox_path.exists()


def test_precise_orders_require_this_session_target_mod(config_path, monkeypatch):
    precise_observer(config_path, monkeypatch, ready=False)
    result = direct.DirectConnection(config_path).issue_order({"kind": "fleet_wait", "fleet_id": "3", "days": 30})
    assert result["status"] == "blocked"
    assert "bootstrapped" in result["reason"]
    assert not direct._configuration(config_path).state_dir.exists()


@pytest.mark.parametrize("payload", [
    {"kind": "research", "technology": "tech_physics_1"},
    {"kind": "build", "planet_id": "3", "building": "building_research_lab_1"},
    {"kind": "fleet_wait", "fleet_id": "3\neffect cash=1", "days": 30},
    {"kind": "fleet_wait", "fleet_id": "3", "days": True},
    {"kind": "fleet_wait", "fleet_id": "3", "days": 30, "console": "human_ai"},
])
def test_unsupported_or_malformed_orders_fail_before_observing(config_path, monkeypatch, payload):
    monkeypatch.setattr(direct, "DirectObserver", lambda *_: pytest.fail("Malformed order read a snapshot"))
    with pytest.raises(ValueError):
        direct.DirectConnection(config_path).issue_order(payload)


@pytest.mark.parametrize("kind", ["fleet_wait", "fleet_orbit"])
@pytest.mark.parametrize("correct_queue", [False, True])
def test_precise_order_receipt_requires_exact_target_queue(config_path, monkeypatch, kind, correct_queue):
    observer = precise_observer(config_path, monkeypatch)
    original = direct.publish_order
    request = {"kind": kind, "fleet_id": "3", **({"days": 40} if kind == "fleet_wait" else {"planet_id": "3"})}

    def publish(config, order, token):
        path = original(config, order, token)
        after = copy.deepcopy(observer.before)
        after.state["date"] = "2200.01.02"
        after.state["direct_bridge"]["done_flags"] = [f"qs_direct_done_{token}"]
        if correct_queue:
            actions = {"wait": {"time": 40}} if kind == "fleet_wait" else {
                "orbit_planet": {"orbitable": {"planet": 3}}, "wait": {"time": 30},
            }
            after.state["fleets_by_id"]["3"]["actions"] = actions
        observer.after = Observation(after.state, "after", time.time())
        return path

    monkeypatch.setattr(direct, "publish_order", publish)
    connection = direct.DirectConnection(config_path)
    result = connection.issue_order(request)
    assert result["status"] == ("order_verified" if correct_queue else "unverified")
    assert records(config_path)[0]["status"] == ("verified" if correct_queue else "unverified")
    assert json.loads(records(config_path)[0]["before_json"])["_direct_order"] == request
    assert "# disarmed" in direct._configuration(config_path).inbox_path.read_text()
    if not correct_queue:
        assert connection.issue_order(request)["status"] == "blocked"


@pytest.mark.parametrize("remaining", [None, "scripted", "native", "unknown"])
def test_fleet_stop_requires_proof_that_both_colony_fleet_order_queues_are_empty(config_path, monkeypatch, remaining):
    observer = precise_observer(config_path, monkeypatch)
    raw = json.loads(config_path.read_text())
    raw["allowed_orders"] = ["fleet_stop"]
    config_path.write_text(json.dumps(raw))
    fleet = observer.before.state["fleets_by_id"]["3"]
    fleet.update(ship_class="shipclass_colonizer", orders_known=True, actions={"wait": {"time": 30}},
                 current_order={"colonize_planet_order": {"planet": 104}})
    original = direct.publish_order

    def publish(config, order, token):
        path = original(config, order, token)
        after = copy.deepcopy(observer.before.state)
        after["direct_bridge"]["done_flags"] = [f"qs_direct_done_{token}"]
        stopped = after["fleets_by_id"]["3"]
        stopped.update(actions={}, current_order={}, order={}, orders=[], queued_orders=[])
        if remaining == "scripted":
            stopped["actions"] = {"wait": {"time": 30}}
        elif remaining == "native":
            stopped["current_order"] = {"colonize_planet_order": {"planet": 104}}
        elif remaining == "unknown":
            stopped["orders_known"] = False
        observer.after = Observation(after, "stop_after", time.time())
        return path

    monkeypatch.setattr(direct, "publish_order", publish)
    result = invoke(direct.build_direct_server(config_path), "issue_order", {"order": {"kind": "fleet_stop", "fleet_id": "3"}})
    assert result["status"] == ("order_verified" if remaining is None else "unverified")
    assert records(config_path)[0]["status"] == ("verified" if remaining is None else "unverified")


def native_observer(config_path, monkeypatch):
    raw = json.loads(config_path.read_text())
    raw["allowed_native_actions"] = ["select_research"]
    config_path.write_text(json.dumps(raw))
    before = baseline()
    before.state["research_status"] = {
        "completed_known": True, "completed_technologies": ["tech_shields_1"],
        "areas": {"physics": {"alternatives_known": True, "alternatives": ["tech_shields_2"],
                              "auto_researching": False, "current_known": False, "queue_known": False,
                              "current": None, "queue": None}},
    }
    return configure_observer(monkeypatch, before)


class SimulatedNative:
    def __init__(self, observer, *, correct=True, completed=False, status="submitted"):
        self.observer, self.correct, self.completed, self.native_status = observer, correct, completed, status
        self.submissions = []
        self.cancellations = []

    def submit(self, selection, token):
        self.submissions.append((selection, token))
        after = copy.deepcopy(self.observer.before.state)
        if self.correct:
            after["research_status"]["areas"]["physics"].update({
                "current_known": True, "queue_known": True, "current": selection.technology,
                "queue": [{"technology": selection.technology, "date": "2200.01.02",
                           "progress": None, "progress_known": False}],
            })
        if self.completed:
            after["research_status"]["completed_technologies"].append(selection.technology)
        after["date"] = "2200.01.02"
        self.observer.after = Observation(after, "native_after", time.time())
        return {"status": "pending"}

    def result(self, token):
        return {"status": self.native_status, "technology": "tech_shields_2", "token": token,
                "kind": "select_research", "area": "physics", "country_id": "0"}

    def cancel(self, token):
        self.cancellations.append(token)
        return {"status": self.native_status}

    def request_dump(self, token):
        return {"status": "pending", "token": token}


def test_native_tool_needs_a_launcher_owned_backend(config_path, monkeypatch):
    monkeypatch.setattr(direct, "DirectObserver", lambda *_: pytest.fail("Missing native backend read game state"))
    assert direct.DirectConnection(config_path).select_research("physics", "tech_shields_2")["status"] == "blocked"


@pytest.mark.parametrize("area,technology", [
    ("physics\nhuman_ai", "tech_shields_2"), ("physics", "tech_shields_2\nresearch_all_technologies"),
    ("all", "tech_shields_2"), ("physics", True),
])
def test_native_identifiers_cannot_be_code_or_unbounded_fields(config_path, monkeypatch, area, technology):
    monkeypatch.setattr(direct, "DirectObserver", lambda *_: pytest.fail("Malformed request read game state"))
    with pytest.raises(ValueError):
        direct.DirectConnection(config_path).select_research(area, technology)


@pytest.mark.parametrize("change", ["not_visible", "auto_enabled", "completed", "unknown_completed"])
def test_native_selection_preconditions_block_before_submission(config_path, monkeypatch, change):
    observer = native_observer(config_path, monkeypatch)
    research = observer.before.state["research_status"]
    if change == "not_visible":
        research["areas"]["physics"]["alternatives"] = []
    elif change == "auto_enabled":
        research["areas"]["physics"]["auto_researching"] = True
    elif change == "completed":
        research["completed_technologies"].append("tech_shields_2")
    else:
        research["completed_known"] = False
    backend = SimulatedNative(observer)
    with pytest.raises(ValueError):
        direct.DirectConnection(config_path, native_bridge=backend).select_research("physics", "tech_shields_2")
    assert not backend.submissions and not direct._configuration(config_path).state_dir.exists()


@pytest.mark.parametrize("correct,completed,status", [
    (True, False, "submitted"), (False, False, "submitted"),
    (True, True, "submitted"), (True, False, "ambiguous"),
])
def test_native_submission_requires_fresh_unfinished_exact_research(config_path, monkeypatch, correct, completed, status):
    observer = native_observer(config_path, monkeypatch)
    backend = SimulatedNative(observer, correct=correct, completed=completed, status=status)
    result = direct.DirectConnection(config_path, native_bridge=backend).select_research("physics", "tech_shields_2")
    verified = correct and not completed and status == "submitted"
    assert result["status"] == ("research_verified" if verified else "unverified")
    assert len(backend.submissions) == 1
    assert records(config_path)[0]["status"] == ("verified" if verified else "unverified")
    assert (backend.submissions[0][1] in backend.cancellations) is not verified
    assert "# disarmed" in direct._configuration(config_path).inbox_path.read_text()


def ship_observer(config_path, monkeypatch):
    import test_ship_commands as ships
    raw = json.loads(config_path.read_text())
    raw["allowed_native_actions"] = ["queue_ship"]
    config_path.write_text(json.dumps(raw))
    before = baseline()
    metadata = before.state["metadata"]
    before.state.update(ships.state.__wrapped__())
    before.state["metadata"] = metadata
    return configure_observer(monkeypatch, before)


class SimulatedShipNative:
    supports_wakeup = True

    def __init__(self, observer, *, outcome="paid", paused=True):
        self.observer, self.outcome = observer, outcome
        self.last_runtime = {"paused": paused, "protected_pause": False}
        self.submissions, self.queries, self.cancellations = {}, {}, []

    def submit(self, command, token):
        import test_ship_commands as ships
        self.submissions[token] = command
        after = ships.paid_after(self.observer.before.state)
        if self.outcome == "ack_only":
            after = copy.deepcopy(self.observer.before.state)
            after["direct_bridge"]["done_flags"] = [f"qs_direct_done_{token}"]
        elif self.outcome == "wrong_payment":
            after["resources"]["alloys"] -= 1
        self.observer.after = Observation(after, "ship_after", time.time())
        return {"status": "pending", "token": token}

    def result(self, token):
        if token in self.queries:
            return {"status": "dump_requested", "kind": "request_observation", "country_id": "0", "token": token}
        command = self.submissions[token]
        result = {"status": "submitted", "kind": command.kind, "country_id": "0", "token": token,
                  **command.to_dict()}
        if self.outcome == "rejected":
            result.update(status="blocked", submitted=False)
        elif self.outcome == "wrong_correlation":
            result["station_id"] = "12"
        return result

    def request_dump(self, token):
        self.queries[token] = True
        return {"status": "pending", "token": token}

    def cancel(self, token):
        self.cancellations.append(token)
        return {"status": "cancelled", "token": token}


@pytest.mark.parametrize("runtime,wakeup", [(None, True), ({"paused": False}, True),
                                           ({"paused": 1}, True), ({"paused": True}, False)])
def test_extended_commands_require_strict_fresh_paused_native_runtime(config_path, monkeypatch, runtime, wakeup):
    observer = ship_observer(config_path, monkeypatch)
    backend = SimulatedShipNative(observer)
    backend.last_runtime, backend.supports_wakeup = runtime, wakeup
    result = direct.DirectConnection(config_path, native_bridge=backend).queue_ship("12", "7")
    assert result["status"] == "blocked" and "paused" in result["reason"]
    assert not backend.submissions and not direct._configuration(config_path).state_dir.exists()


@pytest.mark.parametrize("outcome", ["paid", "ack_only", "wrong_payment", "wrong_correlation", "rejected"])
def test_ship_mcp_requires_paid_postconditions_and_correlated_native_dispatch(config_path, monkeypatch, outcome):
    observer = ship_observer(config_path, monkeypatch)
    backend = SimulatedShipNative(observer, outcome=outcome)
    connection = direct.DirectConnection(config_path, native_bridge=backend)
    result = connection.queue_ship("12", "7")
    expected = "command_verified" if outcome == "paid" else "blocked" if outcome == "rejected" else "unverified"
    assert result["status"] == expected
    assert len(backend.submissions) == 1
    if outcome == "paid":
        assert result["dispatch_state"] == "verified" and result["goal_state"] == "pending"
        assert result["context"]["snapshot"] == result["snapshot"] == "ship_after"
        assert result["context"]["target_refs"]["queue_id"] == "8"
        assert result["context"]["queue"]["id"] == "8"
        assert len(backend.queries) == 0
    else:
        assert "context" not in result and "dispatch_state" not in result
    record = records(config_path)[0]
    assert record["status"] == ("verified" if outcome == "paid" else "reconciled" if outcome == "rejected" else "unverified")
    recorded = json.loads(record["before_json"])["_native_operation"]
    assert {key: recorded[key] for key in ("starbase_id", "station_id", "design_id", "queue_id")} == {
        "starbase_id": "12", "station_id": "100", "design_id": "7", "queue_id": "8"}
    assert "# disarmed" in direct._configuration(config_path).inbox_path.read_text()
    if outcome not in {"paid", "rejected"}:
        assert connection.queue_ship("12", "7")["status"] == "blocked"
