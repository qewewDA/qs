from copy import deepcopy
from dataclasses import replace
import math

import pytest

from stellaris_autopilot.direct import DirectConfig, ownership_header
from stellaris_autopilot.direct_orders import (
    Order, parse_order, render_order, render_target_events, validate_order, verify_order,
)
from stellaris_autopilot.pdx import loads


SESSION = "a" * 32
TOKEN = "b" * 32


@pytest.fixture
def config(tmp_path):
    return DirectConfig(
        user_dir=tmp_path / "game", state_dir=tmp_path / "state", country_id="0",
        campaign_id="campaign-a", verified_game_version="4.test", session_token=SESSION,
        allowed_orders=("fleet_wait", "fleet_orbit"),
        single_player_confirmed=True, non_ironman_confirmed=True,
    )


@pytest.fixture
def state():
    return {
        "country_id": "0", "metadata": {"missing_fields": []},
        "fleets_by_id": {"3": {
            "id": "3", "ship_class": "shipclass_military", "mobile": True,
            "combat": {"start_date": "0.01.01"}, "system_id": "92", "actions": {},
        }},
        "planets_by_id": {"3": {"id": "3", "owner": 0, "controller": 0, "system_id": "92"}},
    }


def test_colony_fleet_can_be_stopped_before_replacing_a_colonization_target(state, config):
    fleet = state["fleets_by_id"]["3"]
    fleet["ship_class"] = "shipclass_colonizer"
    fleet["orders_known"] = True
    order = parse_order({"kind": "fleet_stop", "fleet_id": "3"})
    validate_order(state, order)
    assert verify_order(state, order)
    script = render_order(replace(config, allowed_orders=("fleet_stop",)), order, TOKEN)
    assert "is_ship_class = shipclass_colonizer" in script
    assert "queue_actions" not in script


@pytest.mark.parametrize("raw", [
    {"kind": "fleet_wait", "fleet_id": "3", "days": 1},
    {"kind": "fleet_wait", "fleet_id": "3", "days": 365},
    {"kind": "fleet_orbit", "fleet_id": "3", "planet_id": "3"},
    {"kind": "fleet_stop", "fleet_id": "3"},
])
def test_parse_order_round_trips_canonical_parameters(raw):
    assert parse_order(raw).to_dict() == raw


@pytest.mark.parametrize("raw", [
    [], None, {}, {"kind": []}, {"kind": "cash"},
    {"kind": "fleet_wait", "fleet_id": "3", "days": 30, "script": "cash 1"},
    {"kind": "fleet_wait", "fleet_id": "3", "days": 30, "planet_id": None},
    {"kind": "fleet_orbit", "fleet_id": "3", "planet_id": "3", "days": 30},
    {"kind": "fleet_wait", "fleet_id": "3"},
    {"kind": "fleet_orbit", "fleet_id": "3"},
    {"kind": "fleet_stop"},
    {"kind": "fleet_stop", "fleet_id": "3", "days": 30},
    {"kind": "fleet_stop", "fleet_id": "3", "planet_id": "3"},
])
def test_parse_order_rejects_unknown_or_extra_fields(raw):
    with pytest.raises(ValueError):
        parse_order(raw)


@pytest.mark.parametrize("identifier", [
    3, True, None, [], "", "03", " 3", "+3", "-1", "3.0", "３", "3\neffect cash = 1",
    "4294967295", "9999999999", "1" * 11,
])
def test_ids_cannot_be_coerced_or_inject_commands(identifier):
    for raw in (
        {"kind": "fleet_wait", "fleet_id": identifier, "days": 30},
        {"kind": "fleet_orbit", "fleet_id": "3", "planet_id": identifier},
        {"kind": "fleet_stop", "fleet_id": identifier},
    ):
        with pytest.raises(ValueError, match="IDs"):
            parse_order(raw)


@pytest.mark.parametrize("days", [True, False, 0, -1, 366, 30.0, "30", None, math.inf, math.nan])
def test_wait_duration_is_bounded_integer(days):
    with pytest.raises(ValueError, match="duration"):
        parse_order({"kind": "fleet_wait", "fleet_id": "3", "days": days})


@pytest.mark.parametrize("ship_class", ["shipclass_military", "shipclass_science_ship", "shipclass_constructor"])
def test_known_owned_fleet_classes_accept_orders(state, ship_class):
    state["fleets_by_id"]["3"]["ship_class"] = ship_class
    validate_order(state, Order("fleet_wait", "3", days=30))
    validate_order(state, Order("fleet_orbit", "3", planet_id="3"))
    validate_order(state, Order("fleet_stop", "3"))


@pytest.mark.parametrize("key,value", [
    ("id", "4"), ("owner", 1), ("owner", True), ("controller", 1),
    ("ship_class", "shipclass_starbase"), ("ship_class", "shipclass_transport"), ("ship_class", []),
    ("mobile", False), ("mobile", "yes"),
    ("combat", None), ("combat", {}), ("combat", {"start_date": "2200.01.01"}),
])
def test_unsafe_or_unknown_fleet_state_blocks_orders(state, key, value):
    state["fleets_by_id"]["3"][key] = value
    with pytest.raises(ValueError):
        validate_order(state, Order("fleet_wait", "3", days=30))


def test_incomplete_ownership_and_missing_target_never_become_defaults(state):
    for modified in ({}, {**state, "metadata": {}}, {**state, "metadata": {"missing_fields": ["fleets"]}},
                     {**state, "fleets_by_id": {}}):
        with pytest.raises(ValueError):
            validate_order(modified, Order("fleet_wait", "3", days=30))


@pytest.mark.parametrize("key,value", [
    ("id", "4"), ("owner", 1), ("owner", None), ("controller", 1),
    ("system_id", "93"), ("system_id", None),
])
def test_orbit_requires_explicit_owned_controlled_planet_in_same_system(state, key, value):
    state["planets_by_id"]["3"][key] = value
    with pytest.raises(ValueError):
        validate_order(state, Order("fleet_orbit", "3", planet_id="3"))


def test_missing_fleet_system_and_missing_planet_ownership_block_orbit(state):
    for kind in ("fleet system", "planet owner"):
        changed = deepcopy(state)
        if kind == "fleet system":
            del changed["fleets_by_id"]["3"]["system_id"]
        else:
            del changed["planets_by_id"]["3"]["owner"]
        with pytest.raises(ValueError):
            validate_order(changed, Order("fleet_orbit", "3", planet_id="3"))


def test_target_events_bind_only_session_owned_objects_and_mark_bootstrap():
    root = loads(render_target_events(SESSION))
    assert root.get("namespace") == "qs_direct_targets"
    for event_type, number, kind in (("planet_event", 1, "planet"), ("fleet_event", 2, "fleet")):
        event = next(event for event in root.get_all(event_type) if event.get("id") == f"qs_direct_targets.{number}")
        assert event.get("id") == f"qs_direct_targets.{number}"
        branch = event.get("immediate").get("if")
        owner = branch.get("limit").get("owner")
        assert owner.get("has_country_flag") == f"qs_direct_session_{SESSION}"
        assert owner.get("is_ai") == owner.get("is_multiplayer") == owner.get("is_ironman") == "no"
        assert branch.get("save_global_event_target_as") == f"qs_direct_{kind}_{SESSION}"
    bootstrap = root.get("country_event").get("immediate").get("if")
    assert bootstrap.get("limit").get("has_country_flag") == f"qs_direct_session_{SESSION}"
    assert bootstrap.get("set_country_flag") == f"qs_direct_targets_{SESSION}"


@pytest.mark.parametrize("session", [None, "a" * 31, "a" * 31 + "\n", "z" * 32])
def test_target_events_reject_session_injection(session):
    with pytest.raises(ValueError):
        render_target_events(session)


@pytest.mark.parametrize("order", [Order("fleet_wait", "3", days=17), Order("fleet_orbit", "3", planet_id="3")])
def test_order_script_uses_typed_ids_guards_and_receipt_only_inside_execution(config, order):
    script = render_order(config, order, TOKEN)
    assert script.startswith(ownership_header(config))
    commands = script.splitlines()[1:]
    assert "event qs_direct_targets.2 fleet 3" in commands
    assert ("event qs_direct_targets.1 planet 3" in commands) == (order.kind == "fleet_orbit")
    cleanup = [line for line in commands if "clear_global_event_target =" in line]
    assert len(cleanup) == 4
    for line in cleanup:
        branch = loads(line.removeprefix("effect ")).get("if")
        target = branch.get("clear_global_event_target")
        assert target in {f"qs_direct_fleet_{SESSION}", f"qs_direct_planet_{SESSION}"}
        assert branch.get("limit").get("exists") == f"event_target:{target}"
    body = loads(next(line[7:] for line in commands if line.startswith("effect if = { limit = { is_ai"))).get("if")
    guard = body.get("limit")
    assert guard.get_all("has_country_flag") == [f"qs_direct_session_{SESSION}", f"qs_direct_targets_{SESSION}"]
    assert guard.get("NOT").get("has_country_flag") == f"qs_direct_done_{TOKEN}"
    assert guard.get("is_ai") == guard.get("is_multiplayer") == guard.get("is_ironman") == "no"
    fleet_guard = guard.get(f"event_target:qs_direct_fleet_{SESSION}")
    assert fleet_guard.get("owner").get("has_country_flag") == f"qs_direct_session_{SESSION}"
    assert fleet_guard.get("is_mobile") == "yes" and fleet_guard.get("is_in_combat") == "no"
    execution = body.get(f"event_target:qs_direct_fleet_{SESSION}")
    assert execution.get("clear_fleet_actions") == "this"
    assert execution.get("clear_orders") == "yes"
    assert script.index("clear_fleet_actions = this") < script.index("clear_orders = yes") < script.index("queue_actions")
    queue = execution.get("queue_actions")
    assert queue.get("wait") == (17 if order.kind == "fleet_wait" else 30)
    assert body.get("set_country_flag") == f"qs_direct_done_{TOKEN}"
    if order.kind == "fleet_orbit":
        assert queue.get("orbit_planet") == f"event_target:qs_direct_planet_{SESSION}"
        assert fleet_guard.get("solar_system").get("is_same_value") == (
            f"event_target:qs_direct_planet_{SESSION}.solar_system"
        )
    assert script.count("set_country_flag") == 1 and commands[-1] == "debug_dumpgamestate qs_direct"
    assert "clear_global_event_targets = yes" not in script


def test_render_revalidates_programmatic_orders_and_authorization(config):
    for order in (Order("fleet_wait", "3", days=True), Order("fleet_orbit", "3", planet_id="3", days=30),
                  Order("fleet_wait", "3\ncash 1", days=30)):
        with pytest.raises(ValueError):
            render_order(config, order, TOKEN)
    with pytest.raises(ValueError, match="authorized"):
        render_order(replace(config, allowed_orders=()), Order("fleet_wait", "3", days=30), TOKEN)
    with pytest.raises(ValueError, match="token"):
        render_order(config, Order("fleet_wait", "3", days=30), TOKEN + "\neffect cash = 1")


@pytest.mark.parametrize("duration,expected", [(30, True), (1, True), (0.5, True), (0, False),
                                               (-1, False), (31, False), (True, False),
                                               (math.nan, False), (math.inf, False), (10 ** 1000, False)])
def test_verification_checks_finite_remaining_wait_only_on_requested_fleet(state, duration, expected):
    state["fleets_by_id"]["3"]["actions"] = {"wait": {"time": duration}}
    assert verify_order(state, Order("fleet_wait", "3", days=30)) is expected


def test_receipt_or_other_fleets_matching_orders_do_not_prove_requested_order(state):
    state["direct_bridge"] = {"done_flags": [f"qs_direct_done_{TOKEN}"]}
    state["fleets_by_id"]["4"] = {**deepcopy(state["fleets_by_id"]["3"]), "id": "4",
                                   "actions": {"wait": {"time": 30}}}
    assert not verify_order(state, Order("fleet_wait", "3", days=30))
    state["fleets_by_id"]["3"]["actions"] = {"wait": {"time": 30}, "move_to": {"target": 92}}
    assert not verify_order(state, Order("fleet_wait", "3", days=30))


@pytest.mark.parametrize("planet_id,expected", [(3, True), ("3", True), (4, False), (True, False), (3.0, False)])
def test_orbit_verifies_exact_observed_queue_layout(state, planet_id, expected):
    state["fleets_by_id"]["3"]["actions"] = {
        "orbit_planet": {"orbitable": {"planet": planet_id}}, "wait": {"time": 30},
    }
    assert verify_order(state, Order("fleet_orbit", "3", planet_id="3")) is expected


def test_unproven_current_orbit_field_and_incomplete_queue_do_not_verify(state):
    fleet = state["fleets_by_id"]["3"]
    fleet["orbit"] = {"planet": 3}
    fleet["actions"] = {"wait": {"time": 20}}
    assert not verify_order(state, Order("fleet_orbit", "3", planet_id="3"))
    fleet["actions"] = {"orbit_planet": {"orbitable": {"planet": 3}}, "wait": {"time": 31}}
    assert not verify_order(state, Order("fleet_orbit", "3", planet_id="3"))


def test_navigation_binding_does_not_require_target_ownership():
    root = loads(render_target_events(SESSION))
    event = next(event for event in root.get_all("planet_event") if event.get("id") == "qs_direct_targets.4")
    branch = event.get("immediate").get("if")
    guard = branch.get("limit").get("any_country")
    assert guard.get("has_country_flag") == f"qs_direct_session_{SESSION}"
    assert guard.get("is_ai") == guard.get("is_multiplayer") == guard.get("is_ironman") == "no"
    assert branch.get("save_global_event_target_as") == f"qs_direct_navigation_{SESSION}"


def _navigation_state(state):
    state["space"] = {
        "systems_by_id": {"247": {"id": "247", "visited": True, "planets_known": True, "planet_ids": ["2124"]}},
        "planets_by_id": {"2124": {"id": "2124", "system_id": "247"}},
    }
    return state


def test_move_is_an_exact_visited_system_with_native_pathfinding_guard(config, state):
    state = _navigation_state(state)
    order = parse_order({"kind": "fleet_move", "fleet_id": "3", "planet_id": "2124"})
    validate_order(state, order)
    script = render_order(replace(config, allowed_orders=("fleet_move",)), order, TOKEN)
    assert "event qs_direct_targets.4 planet 2124" in script
    assert f"can_access_system = event_target:qs_direct_navigation_{SESSION}.solar_system" in script
    assert f"move_to = event_target:qs_direct_navigation_{SESSION}.solar_system wait = 30" in script
    assert "set_location" not in script and "set_owner" not in script


@pytest.mark.parametrize("field,value", [("visited", False), ("visited", None), ("planets_known", False),
                                        ("planet_ids", ["2125"]), ("id", "92")])
def test_move_rejects_unknown_or_contradictory_system_membership(state, field, value):
    state = _navigation_state(state)
    state["space"]["systems_by_id"]["247"][field] = value
    with pytest.raises(ValueError):
        validate_order(state, Order("fleet_move", "3", planet_id="2124"))


@pytest.fixture
def measured_move_actions():
    # Minimal fleet 3 excerpt from the real Cygnus 4.5.2 (9776) dump at
    # direct-bridge-research/extended_aebd8928/campaign/userdir/save games/
    # dump_qs_direct.sav.txt:827417-827432. The order was issued while paused;
    # the observed physical anchor 104 is in destination system 247.
    return {"move_to": {"coordinate": {"x": 0, "y": 0, "origin": 247}}, "wait": {"time": 30}}


@pytest.mark.parametrize("origin", [247, "247"])
def test_move_verifies_measured_system_center_destination(state, measured_move_actions, origin):
    state = _navigation_state(state)
    measured_move_actions["move_to"]["coordinate"]["origin"] = origin
    state["fleets_by_id"]["3"]["actions"] = measured_move_actions
    assert verify_order(state, Order("fleet_move", "3", planet_id="2124"))


@pytest.mark.parametrize("axis,value", [
    ("x", 0.000001), ("y", -0.000001), ("x", 1), ("y", 10 ** 1000),
    ("x", True), ("y", False), ("x", "0"), ("y", None),
    ("x", math.nan), ("y", math.inf), ("x", -math.inf), ("y", [0, 0]),
])
def test_move_rejects_noncenter_or_unknown_coordinates(state, measured_move_actions, axis, value):
    state = _navigation_state(state)
    measured_move_actions["move_to"]["coordinate"][axis] = value
    state["fleets_by_id"]["3"]["actions"] = measured_move_actions
    assert not verify_order(state, Order("fleet_move", "3", planet_id="2124"))


@pytest.mark.parametrize("origin", [248, 2124, True, 247.0, "0247", " 247", None, [],
                                     -1, "4294967295"])
def test_move_rejects_wrong_or_noncanonical_system_origin(state, measured_move_actions, origin):
    state = _navigation_state(state)
    measured_move_actions["move_to"]["coordinate"]["origin"] = origin
    state["fleets_by_id"]["3"]["actions"] = measured_move_actions
    assert not verify_order(state, Order("fleet_move", "3", planet_id="2124"))


@pytest.mark.parametrize("move", [
    {"target": {"galactic_object": 247}}, {"target": 247},
    {"coordinate": {"x": 0, "origin": 247}}, {"coordinate": {"y": 0, "origin": 247}},
    {"coordinate": {"x": 0, "y": 0}},
    {"coordinate": {"x": 0, "y": 0, "origin": 247, "z": 0}},
    {"coordinate": {"x": 0, "y": 0, "origin": 247}, "target": {"galactic_object": 247}},
    {"coordinate": [{"x": 0, "y": 0, "origin": 247}]},
    {"coordinate": None}, [], None,
])
def test_move_rejects_assumed_or_ambiguous_destination_layout(state, measured_move_actions, move):
    state = _navigation_state(state)
    measured_move_actions["move_to"] = move
    state["fleets_by_id"]["3"]["actions"] = measured_move_actions
    assert not verify_order(state, Order("fleet_move", "3", planet_id="2124"))


def test_move_receipt_cannot_substitute_for_exact_serialized_destination(state, measured_move_actions):
    state = _navigation_state(state)
    order = Order("fleet_move", "3", planet_id="2124")
    fleet = state["fleets_by_id"]["3"]
    state["direct_bridge"] = {"done_flags": [f"qs_direct_done_{TOKEN}"]}
    fleet["actions"] = measured_move_actions
    assert verify_order(state, order)
    fleet["actions"]["move_to"]["coordinate"]["origin"] = 248
    assert not verify_order(state, order)
    fleet["system_id"] = "247"
    fleet["actions"] = {"wait": {"time": 30}}
    assert not verify_order(state, order)


@pytest.mark.parametrize("actions", [
    {"move_to": {"coordinate": {"x": 0, "y": 0, "origin": 247}}},
    {"move_to": {"coordinate": {"x": 0, "y": 0, "origin": 247}}, "wait": {"time": 31}},
    {"move_to": {"coordinate": {"x": 0, "y": 0, "origin": 247}}, "wait": {"time": 0}},
    {"move_to": {"coordinate": {"x": 0, "y": 0, "origin": 247}}, "wait": {"time": True}},
    {"move_to": {"coordinate": {"x": 0, "y": 0, "origin": 247}}, "wait": {"time": 30},
     "orbit_planet": {"orbitable": {"planet": 2124}}},
])
def test_move_requires_only_measured_move_and_remaining_bounded_wait(state, actions):
    state = _navigation_state(state)
    state["fleets_by_id"]["3"]["actions"] = actions
    assert not verify_order(state, Order("fleet_move", "3", planet_id="2124"))


def test_stop_clears_both_queues_with_session_guard_and_no_followup_wait(config):
    order = Order("fleet_stop", "3")
    script = render_order(replace(config, allowed_orders=("fleet_stop",)), order, TOKEN)
    assert script.startswith(ownership_header(config))
    assert "event qs_direct_targets.2 fleet 3" in script.splitlines()
    assert "event qs_direct_targets.1" not in script and "event qs_direct_targets.4" not in script
    assert "queue_actions" not in script and "wait =" not in script
    assert "clear_fleet_actions = this clear_orders = yes" in script
    commands = script.splitlines()[1:]
    body = loads(next(line[7:] for line in commands if line.startswith("effect if = { limit = { is_ai"))).get("if")
    guard = body.get("limit")
    assert guard.get_all("has_country_flag") == [f"qs_direct_session_{SESSION}", f"qs_direct_targets_{SESSION}"]
    assert guard.get("NOT").get("has_country_flag") == f"qs_direct_done_{TOKEN}"
    assert guard.get("is_ai") == guard.get("is_multiplayer") == guard.get("is_ironman") == "no"
    fleet = f"event_target:qs_direct_fleet_{SESSION}"
    assert guard.get("exists") == fleet
    fleet_guard = guard.get(fleet)
    assert fleet_guard.get("owner").get("has_country_flag") == f"qs_direct_session_{SESSION}"
    assert fleet_guard.get("is_mobile") == "yes" and fleet_guard.get("is_in_combat") == "no"
    assert body.get(fleet).get("clear_fleet_actions") == "this"
    assert body.get(fleet).get("clear_orders") == "yes"
    assert body.get("set_country_flag") == f"qs_direct_done_{TOKEN}"
    assert sum("clear_global_event_target =" in line for line in commands) == 2
    assert commands[-1] == "debug_dumpgamestate qs_direct"


@pytest.mark.parametrize("key,value", [
    ("actions", {"wait": {"time": 1}}),
    ("current_order", {"survey_planet_order": {"order_id": 2}}),
    ("order", {"move_to_system_point_order": {"order_id": 3}}),
    ("order", [{"survey_planet_order": {"order_id": 4}}]),
    ("orders", {"unexpected": {}}), ("queued_orders", [1]),
    ("actions", None), ("current_order", None), ("order", "none"),
    ("actions", False), ("order", 0),
])
def test_stop_rejects_remaining_or_unobservable_order_fields(state, key, value):
    fleet = state["fleets_by_id"]["3"]
    fleet["orders_known"] = True
    fleet[key] = value
    assert not verify_order(state, Order("fleet_stop", "3"))


def test_stop_verifies_all_empty_fields_only_with_complete_order_observation(state):
    fleet = state["fleets_by_id"]["3"]
    order = Order("fleet_stop", "3")
    assert not verify_order(state, order)
    for unknown in (False, None, "yes", 1):
        fleet["orders_known"] = unknown
        assert not verify_order(state, order)
    fleet["orders_known"] = True
    assert verify_order(state, order)
    fleet.update(actions=[], current_order={}, order=[])
    assert verify_order(state, order)
    for key in ("actions", "current_order", "order"):
        fleet.pop(key)
    assert verify_order(state, order)
    state["metadata"]["missing_fields"] = ["fleets"]
    assert not verify_order(state, order)


def test_stop_receipt_and_other_empty_fleet_cannot_hide_remaining_order(state):
    state["direct_bridge"] = {"done_flags": [f"qs_direct_done_{TOKEN}"]}
    fleet = state["fleets_by_id"]["3"]
    fleet["orders_known"] = True
    state["fleets_by_id"]["4"] = {**deepcopy(fleet), "id": "4"}
    fleet["current_order"] = {"survey_planet_order": {"order_id": 1}}
    assert not verify_order(state, Order("fleet_stop", "3"))
