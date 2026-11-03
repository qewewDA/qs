"""Colony ships must carry explicit colonization data and normal payment proof."""
from copy import deepcopy
from dataclasses import replace
import math

import pytest

from stellaris_autopilot.colony_ships import (
    ColonyShipConstruction, ColonyShipRequest, colony_ship_payload, colony_ship_request,
    resolve_colony_ship, verify_colony_ship,
)
from stellaris_autopilot.ship_commands import ShipConstruction, ship_request


TOKEN = "a" * 32


@pytest.fixture
def state():
    return {
        "country_id": "0", "date": "2200.01.10",
        "resources": {"alloys": 500, "food": 500, "consumer_goods": 500},
        "metadata": {"campaign_id": "campaign", "game_version": "Cygnus v4.5.2"},
        "fleets_by_id": {"10": {"id": "10", "ship_class": "shipclass_starbase", "mobile": False}},
        "shipbuilding": {
            "designs_known": True, "shipyards_known": True, "species_known": True,
            "designs_by_id": {"7": {"id": "7", "ship_size": "colonizer", "definition": {"name": "Colony Ship"}}},
            "species_by_id": {"0": {"id": "0", "definition": {"name": "Human", "class": "HUM"},
                                     "colonization_allowed_known": True, "colonization_allowed": True}},
            "shipyards_by_id": {"12": {
                "id": "12", "owner": "0", "station_id": "100", "fleet_id": "10", "queue_id": "8",
            }},
        },
        "construction": {"known": True, "items_by_id": {}, "queues_by_id": {"8": {
            "id": "8", "owner": "0", "type": "ships", "location": {"type": 0, "id": "100"},
            "known": True, "items_known": True, "items": [], "item_count": 0,
        }}},
    }


def job():
    return {
        "id": "31", "queue_id": "8", "paying_country": "0", "progress": 0, "progress_needed": 360,
        "resources": {"alloys": 200, "food": 200, "consumer_goods": 200},
        "buildable_type": "buildable_colony_ship",
        "buildable": {"ship_design_implementation": {"design": 7, "upgrade": 4294967295, "growth_stage": 0},
                      "orbitable": {"starbase": 12}, "colonization_data": {"species": 0}},
    }


def set_items(state, items):
    state["construction"]["items_by_id"] = {item["id"]: item for item in items}
    queue = state["construction"]["queues_by_id"]["8"]
    queue["items"] = [item for item in items if item["queue_id"] == "8"]
    queue["item_count"] = len(queue["items"])


def paid_after(state):
    after = deepcopy(state)
    item = job()
    set_items(after, [item])
    for resource, amount in item["resources"].items():
        after["resources"][resource] -= amount
    return after


def test_colony_request_keeps_species_separate_from_design_and_shipyard(state):
    request = colony_ship_request("12", "7", "0")
    assert request == ColonyShipRequest("12", "7", "0")
    assert not isinstance(request, type(ship_request("12", "7")))
    command = resolve_colony_ship(state, request)
    assert command == ColonyShipConstruction("12", "7", "0", "100", "8")
    assert not isinstance(command, ShipConstruction)
    assert colony_ship_payload(command, TOKEN) == {
        "kind": "queue_colony_ship", "country_id": "0", "token": TOKEN,
        "starbase_id": "12", "design_id": "7", "species_id": "0", "station_id": "100", "queue_id": "8",
    }
    assert verify_colony_ship(state, paid_after(state), command)


def test_species_generation_bits_are_preserved_in_request_and_paid_job(state):
    species_id = "2348810241"
    species = state["shipbuilding"]["species_by_id"].pop("0")
    species["id"] = species_id
    state["shipbuilding"]["species_by_id"][species_id] = species
    command = resolve_colony_ship(state, colony_ship_request("12", "7", species_id))
    assert colony_ship_payload(command, TOKEN)["species_id"] == species_id
    after = paid_after(state)
    data = after["construction"]["items_by_id"]["31"]["buildable"]["colonization_data"]
    data["species"] = int(species_id)
    assert verify_colony_ship(state, after, command)
    data["species"] = 1  # Same slot index is not the full live species handle.
    assert not verify_colony_ship(state, after, command)


@pytest.mark.parametrize("value", [True, False, 0, None, [], "00", "-1", "０", "4294967295", "1\neffect cash=1"])
def test_colony_parameters_are_exact_observed_handles(value):
    for args in ((value, "7", "0"), ("12", value, "0"), ("12", "7", value)):
        with pytest.raises(ValueError):
            colony_ship_request(*args)


@pytest.mark.parametrize("field,value", [
    ("species_known", False), ("species_known", None), ("species_known", "yes"),
    ("species_by_id", {}), ("species_by_id", None), ("species_by_id", {"0": {"id": "1"}}),
    ("species_by_id", {"0": None}), ("species_by_id", {"0": {"id": 0}}),
])
def test_colony_ship_requires_complete_owned_species_identity(state, field, value):
    state["shipbuilding"][field] = value
    with pytest.raises(ValueError, match="species"):
        resolve_colony_ship(state, colony_ship_request("12", "7", "0"))


def test_species_knowledge_cannot_substitute_for_selected_species_membership(state):
    with pytest.raises(ValueError, match="species"):
        resolve_colony_ship(state, colony_ship_request("12", "7", "1"))
    state["shipbuilding"].pop("species_known")
    with pytest.raises(ValueError, match="species"):
        resolve_colony_ship(state, colony_ship_request("12", "7", "0"))


@pytest.mark.parametrize("field,value", [
    ("colonization_allowed_known", False), ("colonization_allowed_known", None),
    ("colonization_allowed_known", 1), ("colonization_allowed_known", "yes"),
    ("colonization_allowed", False), ("colonization_allowed", None),
    ("colonization_allowed", 1), ("colonization_allowed", "yes"),
])
def test_known_owned_species_does_not_imply_permission_to_colonize(state, field, value):
    state["shipbuilding"]["species_by_id"]["0"][field] = value
    with pytest.raises(ValueError, match="permission"):
        resolve_colony_ship(state, colony_ship_request("12", "7", "0"))


@pytest.mark.parametrize("field", ["colonization_allowed_known", "colonization_allowed"])
def test_missing_species_rights_remain_unknown_instead_of_allowed(state, field):
    state["shipbuilding"]["species_by_id"]["0"].pop(field)
    with pytest.raises(ValueError, match="permission"):
        resolve_colony_ship(state, colony_ship_request("12", "7", "0"))


@pytest.mark.parametrize("size", ["corvette", "science", "constructor", "mining_station", "military_station_small", None])
def test_colony_factory_is_never_used_for_another_ship_class(state, size):
    state["shipbuilding"]["designs_by_id"]["7"]["ship_size"] = size
    with pytest.raises(ValueError):
        resolve_colony_ship(state, colony_ship_request("12", "7", "0"))


@pytest.mark.parametrize("field,value", [
    ("owner", "1"), ("id", "13"), ("queue_id", "9"), ("station_id", "12"),
])
def test_colony_ship_reuses_exact_owned_shipyard_and_station_queue_join(state, field, value):
    state["shipbuilding"]["shipyards_by_id"]["12"][field] = value
    with pytest.raises(ValueError):
        resolve_colony_ship(state, colony_ship_request("12", "7", "0"))


@pytest.mark.parametrize("token", [None, True, "a" * 31, "A" * 32, "a" * 32 + "\n", "z" * 32])
def test_colony_native_payload_has_a_canonical_operation_token(state, token):
    command = resolve_colony_ship(state, colony_ship_request("12", "7", "0"))
    with pytest.raises(ValueError):
        colony_ship_payload(command, token)


def test_payload_revalidates_programmatically_constructed_commands(state):
    command = resolve_colony_ship(state, colony_ship_request("12", "7", "0"))
    for field, value in (("species_id", False), ("design_id", 7), ("starbase_id", "012"),
                         ("station_id", "4294967295"), ("queue_id", "8\nrun x")):
        with pytest.raises(ValueError):
            colony_ship_payload(replace(command, **{field: value}), TOKEN)
    with pytest.raises(ValueError):
        colony_ship_payload(colony_ship_request("12", "7", "0"), TOKEN)


@pytest.mark.parametrize("field,value", [
    ("buildable_type", "buildable_ship"), ("paying_country", "1"), ("queue_id", "9"),
    ("progress", -1), ("progress", True), ("progress", 360), ("progress_needed", 0),
    ("progress_needed", math.inf), ("progress", math.nan), ("resources", {}),
    ("resources", {"alloys": 0}), ("resources", {"alloys": -200}),
    ("resources", {"alloys": True}), ("resources", {"alloys": math.inf}),
])
def test_colony_ship_needs_normal_new_unfinished_paid_subtype_job(state, field, value):
    command = resolve_colony_ship(state, colony_ship_request("12", "7", "0"))
    after = paid_after(state)
    after["construction"]["items_by_id"]["31"][field] = value
    assert not verify_colony_ship(state, after, command)


@pytest.mark.parametrize("path,value", [
    (("colonization_data", "species"), 1), (("colonization_data", "species"), False),
    (("colonization_data", "species"), 0.0), (("colonization_data", "species"), "0"),
    (("colonization_data",), {}), (("colonization_data",), {"species": 0, "automation": 1}),
    (("colonization_data",), {"species": 0, "designation": "HUM"}),
    (("ship_design_implementation", "design"), 8), (("ship_design_implementation", "growth_stage"), False),
    (("ship_design_implementation", "growth_stage"), 1), (("ship_design_implementation", "upgrade"), 0),
    (("orbitable", "starbase"), 100), (("orbitable", "starbase"), "12"),
])
def test_exact_colonization_species_and_native_namespace_are_required(state, path, value):
    command = resolve_colony_ship(state, colony_ship_request("12", "7", "0"))
    after = paid_after(state)
    node = after["construction"]["items_by_id"]["31"]["buildable"]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert not verify_colony_ship(state, after, command)


@pytest.mark.parametrize("change", [
    "generic_without_species", "missing_colonization", "wrong_species_field", "free", "partial_payment",
    "missing_stock", "cross_month", "backwards_date", "old_job", "two_new_jobs", "changed_species",
    "changed_fleet", "species_unknown", "rights_unknown", "rights_revoked", "wrong_campaign", "wrong_queue", "forged_command",
])
def test_receipts_or_partial_matches_do_not_prove_a_colony_ship(state, change):
    command = resolve_colony_ship(state, colony_ship_request("12", "7", "0"))
    after = paid_after(state)
    item = after["construction"]["items_by_id"]["31"]
    after["direct_bridge"] = {"done_flags": [f"qs_direct_done_{TOKEN}"]}
    if change == "generic_without_species":
        item["buildable_type"] = "buildable_ship"
        item["buildable"].pop("colonization_data")
    elif change == "missing_colonization":
        item["buildable"].pop("colonization_data")
    elif change == "wrong_species_field":
        item["buildable"]["species"] = item["buildable"].pop("colonization_data")
    elif change == "free":
        after["resources"] = deepcopy(state["resources"])
    elif change == "partial_payment":
        after["resources"]["food"] += 1
    elif change == "missing_stock":
        after["resources"].pop("consumer_goods")
    elif change == "cross_month":
        after["date"] = "2200.02.01"
    elif change == "backwards_date":
        after["date"] = "2200.01.09"
    elif change == "old_job":
        set_items(state, [job()])
    elif change == "two_new_jobs":
        second = job()
        second["id"] = "32"
        set_items(after, [item, second])
    elif change == "changed_species":
        after["shipbuilding"]["species_by_id"]["0"]["definition"]["class"] = "MAM"
    elif change == "changed_fleet":
        after["fleets_by_id"]["10"]["mobile"] = True
    elif change == "species_unknown":
        after["shipbuilding"]["species_known"] = False
    elif change == "rights_unknown":
        after["shipbuilding"]["species_by_id"]["0"]["colonization_allowed_known"] = False
    elif change == "rights_revoked":
        after["shipbuilding"]["species_by_id"]["0"]["colonization_allowed"] = False
    elif change == "wrong_campaign":
        after["metadata"]["campaign_id"] = "other"
    elif change == "wrong_queue":
        after["construction"]["queues_by_id"]["8"]["location"]["id"] = "12"
    else:
        command = replace(command, species_id="1")
    assert not verify_colony_ship(state, after, command)
