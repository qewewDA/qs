from copy import deepcopy
from dataclasses import replace
import math

import pytest

from stellaris_autopilot.fleet_native import FleetOperation, FleetRequest, fleet_request, resolve_fleet, verify_fleet


@pytest.fixture
def state():
    return {
        "country_id": "0", "date": "2200.01.02", "metadata": {"missing_fields": []},
        "resources": {"minerals": 300, "alloys": 200, "influence": 150},
        "fleets_by_id": {"1": {"id": "1", "ship_class": "shipclass_science_ship", "mobile": True, "orders_known": True,
                               "combat": {"start_date": "0.01.01"}}},
        "space": {"planets_by_id": {"104": {"id": "104", "system_id": "247",
                                            "surveyed_known": True, "surveyed": False}},
                  "systems_by_id": {"247": {"id": "247", "visited": True, "planets_known": True,
                                              "planet_ids": ["104", "105"]}}},
    }


def _prepared(state, kind, station_type=None):
    request = fleet_request(kind, "1", "104", station_type)
    state["fleets_by_id"]["1"]["ship_class"] = {
        "survey_planet": "shipclass_science_ship", "build_station": "shipclass_constructor",
        "colonize_planet": "shipclass_colonizer",
    }[kind]
    state["space"]["planets_by_id"]["104"]["surveyed"] = kind != "survey_planet"
    return resolve_fleet(state, request)


def _ordered(state, request):
    after = deepcopy(state)
    common = {"progress": 0, "order_id": 5, "can_reach": "yes"}
    if request.kind == "colonize_planet":
        order = {"colonize_planet_order": {"planet": 104, **common}}
    else:
        value = {"deposit_holder": {"type": 0, "id": 104}, **common}
        if request.kind == "build_station":
            value.update({"class": {"mining": "shipclass_mining_station", "research": "shipclass_research_station",
                                     "outpost": "shipclass_starbase"}[request.station_type],
                          "resources": {"minerals": 100}})
            after["resources"]["minerals"] = 200
        order = {{"survey_planet": "survey_planet_order", "build_station": "build_orbital_station_order"}[request.kind]: value}
    after["fleets_by_id"]["1"]["current_order"] = order
    return after


@pytest.mark.parametrize("value", [True, 1, "01", "-1", "1.0", "１", "4294967295", "1\neffect cash = 1"])
def test_fleet_requests_reject_noncanonical_or_injectable_object_ids(value):
    with pytest.raises(ValueError):
        fleet_request("survey_planet", value, "104")
    with pytest.raises(ValueError):
        fleet_request("survey_planet", "1", value)


@pytest.mark.parametrize("known", [False, None, 1, "yes"])
def test_ambiguous_or_unobserved_script_queue_cannot_allow_native_orders(state, known):
    state["fleets_by_id"]["1"]["orders_known"] = known
    with pytest.raises(ValueError):
        resolve_fleet(state, fleet_request("survey_planet", "1", "104"))


@pytest.mark.parametrize("kind,station_type", [("survey", None), ("survey_planet", "mining"),
                                             ("build_station", None), ("build_station", "observation"),
                                             ("colonize_planet", "outpost"), ([], None)])
def test_request_fields_cannot_select_an_unbounded_command_or_station_class(kind, station_type):
    with pytest.raises(ValueError):
        fleet_request(kind, "1", "104", station_type)


@pytest.mark.parametrize("kind,station_type", [("survey_planet", None), ("build_station", "mining"),
                                             ("build_station", "research"), ("build_station", "outpost"),
                                             ("colonize_planet", None)])
def test_typed_fleet_requests_bind_physical_planet_to_observed_system_and_verify_exact_new_order(state, kind, station_type):
    request = _prepared(state, kind, station_type)
    assert isinstance(request, FleetOperation)
    assert request.system_id == "247" and request.to_dict()["planet_id"] == "104"
    assert verify_fleet(state, _ordered(state, request), request)


@pytest.mark.parametrize("key,value", [("id", "2"), ("mobile", False), ("ship_class", "shipclass_constructor"),
                                     ("combat", {}), ("combat", {"start_date": "2200.01.01"}),
                                     ("owner", 1), ("controller", True), ("actions", {"wait": {"time": 30}})])
def test_wrong_class_unowned_combat_or_competing_scripted_queue_blocks_survey(state, key, value):
    state["fleets_by_id"]["1"][key] = value
    with pytest.raises(ValueError):
        resolve_fleet(state, fleet_request("survey_planet", "1", "104"))


def test_ambiguous_target_membership_and_survey_status_do_not_become_defaults(state):
    for edit in (lambda s: s["metadata"].update(missing_fields=["fleets"]),
                 lambda s: s["space"]["planets_by_id"]["104"].update(system_id="248"),
                 lambda s: s["space"]["systems_by_id"]["247"].update(visited=False),
                 lambda s: s["space"]["systems_by_id"]["247"].update(planet_ids=["105"]),
                 lambda s: s["space"]["planets_by_id"]["104"].update(surveyed_known=False),
                 lambda s: s["space"]["planets_by_id"]["104"].update(surveyed=None)):
        changed = deepcopy(state)
        edit(changed)
        with pytest.raises(ValueError):
            resolve_fleet(changed, fleet_request("survey_planet", "1", "104"))


def test_receipts_stale_orders_and_other_target_cannot_prove_survey(state):
    request = _prepared(state, "survey_planet")
    after = _ordered(state, request)
    stale = deepcopy(after)
    assert not verify_fleet(stale, after, request)
    order = after["fleets_by_id"]["1"]["current_order"]["survey_planet_order"]
    order["deposit_holder"]["id"] = 105
    after["direct_bridge"] = {"done_flags": ["receipt"]}
    assert not verify_fleet(state, after, request)
    order["deposit_holder"].update(type=1, id=104)
    assert not verify_fleet(state, after, request)
    order["deposit_holder"].update(type=False)
    assert not verify_fleet(state, after, request)


@pytest.mark.parametrize("key,value", [("can_reach", "no"), ("can_reach", True), ("order_id", True),
                                     ("order_id", -1), ("progress", math.inf), ("progress", -1)])
def test_native_survey_requires_reachable_finite_active_exact_order(state, key, value):
    request = _prepared(state, "survey_planet")
    after = _ordered(state, request)
    after["fleets_by_id"]["1"]["current_order"]["survey_planet_order"][key] = value
    assert not verify_fleet(state, after, request)


def test_station_requires_actual_payment_and_exact_selected_class(state):
    request = _prepared(state, "build_station", "mining")
    after = _ordered(state, request)
    order = after["fleets_by_id"]["1"]["current_order"]["build_orbital_station_order"]
    assert verify_fleet(state, after, request)
    after["resources"]["minerals"] = 300
    assert not verify_fleet(state, after, request)
    after["resources"]["minerals"] = 200
    order["class"] = "shipclass_research_station"
    assert not verify_fleet(state, after, request)
    order["class"] = "shipclass_mining_station"
    for resources in ({}, {"minerals": 0}, {"minerals": -100}, {"minerals": True}, {"minerals": math.inf}):
        order["resources"] = resources
        assert not verify_fleet(state, after, request)


def test_monthly_income_cannot_masquerade_as_station_payment(state):
    request = _prepared(state, "build_station", "outpost")
    after = _ordered(state, request)
    after["date"] = "2200.02.01"
    assert not verify_fleet(state, after, request)


def test_colony_handle_equal_to_another_physical_planet_never_proves_colonization_target(state):
    request = _prepared(state, "colonize_planet")
    after = _ordered(state, request)
    after["fleets_by_id"]["1"]["current_order"]["colonize_planet_order"]["planet"] = 0
    assert not verify_fleet(state, after, request)
    assert not verify_fleet(state, _ordered(state, request), replace(request, system_id="92"))
    assert not verify_fleet(state, _ordered(state, request), FleetRequest("colonize_planet", "1", "104"))
