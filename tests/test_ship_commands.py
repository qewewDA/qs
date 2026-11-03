"""Ship designs, exact owned shipyard queues and normal paid construction proof."""
from copy import deepcopy
from dataclasses import replace
import math

import pytest

from stellaris_autopilot.pdx import loads
from stellaris_autopilot.ship_commands import (
    ShipConstruction, observe_shipyards, resolve_ship, ship_request, verify_ship,
)


SHIPYARD_SAVE = '''
country={0={ship_design_collection={ship_design={7}}}}
ship_design={7={name="Corvette" growth_stages={{ship_size=corvette section={slot=mid template=corvette_1}}}}}
starbase_mgr={starbases={12={station=100 shipyard_build_queue=8 level=starbase_starport}}}
ships={100={fleet=10}}
construction={queue_mgr={queues={8={owner=0 type=ships location={type=0 id=100}}}}}
'''


@pytest.fixture
def state():
    fleet = {"id": "10", "ship_class": "shipclass_starbase", "mobile": False}
    return {
        "country_id": "0", "date": "2200.01.10", "resources": {"alloys": 500, "minerals": 200},
        "metadata": {"campaign_id": "campaign", "game_version": "Cygnus v4.5.2"},
        "fleets_by_id": {"10": fleet},
        "shipbuilding": observe_shipyards(loads(SHIPYARD_SAVE), "0", [fleet]),
        "construction": {"known": True, "items_by_id": {}, "queues_by_id": {"8": {
            "id": "8", "owner": "0", "type": "ships", "location": {"type": 0, "id": "100"},
            "known": True, "items_known": True, "items": [], "item_count": 0,
        }}},
    }


def job():
    return {
        "id": "31", "queue_id": "8", "paying_country": "0", "progress": 0, "progress_needed": 60,
        "resources": {"alloys": 100}, "buildable_type": "buildable_ship",
        "buildable": {"ship_design_implementation": {"design": 7, "upgrade": 4294967295, "growth_stage": 0},
                      "orbitable": {"starbase": 12}},
    }


def set_items(state, items):
    state["construction"]["items_by_id"] = {item["id"]: item for item in items}
    queue = state["construction"]["queues_by_id"]["8"]
    queue["items"] = [item for item in items if item["queue_id"] == "8"]
    queue["item_count"] = len(queue["items"])


def paid_after(state):
    after = deepcopy(state)
    set_items(after, [job()])
    after["resources"]["alloys"] -= 100
    return after


def test_owned_station_fleet_queue_and_country_design_are_distinct_observed_handles(state):
    resolved = resolve_ship(state, ship_request("12", "7"))
    assert resolved == ShipConstruction("12", "7", "100", "8")
    assert resolved.to_dict() == {"starbase_id": "12", "design_id": "7", "station_id": "100", "queue_id": "8"}
    assert verify_ship(state, paid_after(state), resolved)


@pytest.mark.parametrize("value", [True, 7, "07", "-1", "７", "4294967295", "7\neffect alloys=1"])
def test_ship_request_accepts_only_canonical_bounded_observed_handles(value):
    with pytest.raises(ValueError):
        ship_request(value, "7")
    with pytest.raises(ValueError):
        ship_request("12", value)


@pytest.mark.parametrize("old,new,key", [
    ("ship_design={7}", "ship_design={7 7}", "designs_known"),
    ("ship_design={7}", "ship_design={7 8}", "designs_known"),
    ("ship_design={7}", "ship_design={bad=7}", "designs_known"),
    ("growth_stages={{ship_size=corvette", "growth_stages={{size=corvette", "designs_known"),
    ("station=100", "station=101", "shipyards_known"),
    ("shipyard_build_queue=8", "shipyard_build_queue=9", "shipyards_known"),
    ("owner=0 type=ships", "owner=1 type=ships", "shipyards_known"),
    ("location={type=0 id=100}", "location={type=2 id=100}", "shipyards_known"),
    ("location={type=0 id=100}", "location={type=0 id=12}", "shipyards_known"),
])
def test_bad_references_duplicates_and_wrong_location_never_become_known_empty(old, new, key):
    observed = observe_shipyards(loads(SHIPYARD_SAVE.replace(old, new)), "0", [{"id": "10"}])
    assert observed[key] is False


def test_foreign_station_is_excluded_and_unset_shipyard_is_known_absence():
    foreign = observe_shipyards(loads(SHIPYARD_SAVE), "0", [{"id": "11"}])
    assert foreign["shipyards_known"] is True and foreign["shipyards_by_id"] == {}
    unset = observe_shipyards(loads(SHIPYARD_SAVE.replace("shipyard_build_queue=8", "shipyard_build_queue=4294967295")),
                             "0", [{"id": "10"}])
    assert unset["shipyards_known"] is True and unset["shipyards_by_id"] == {}


@pytest.mark.parametrize("path,value", [
    (("country_id",), "1"),
    (("shipbuilding", "designs_known"), False),
    (("shipbuilding", "shipyards_known"), False),
    (("shipbuilding", "designs_by_id", "7"), None),
    (("shipbuilding", "shipyards_by_id", "12", "owner"), "1"),
    (("construction", "queues_by_id", "8", "owner"), "1"),
    (("construction", "queues_by_id", "8", "known"), False),
    (("construction", "queues_by_id", "8", "items_known"), False),
    (("construction", "queues_by_id", "8", "location"), {"type": 0, "id": "12"}),
    (("construction", "queues_by_id", "8", "type"), "planet"),
])
def test_owned_shipyard_design_and_queue_must_be_known_and_join_exactly(state, path, value):
    node = state
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValueError):
        resolve_ship(state, ship_request("12", "7"))


@pytest.mark.parametrize("size", ["mining_station", "research_station", "observation_station", "colonizer",
                                  "military_station_small", "starbase_outpost", "mauler_stage_1", "transport"])
def test_orbital_station_design_cannot_be_built_at_a_shipyard(state, size):
    state["shipbuilding"]["designs_by_id"]["7"]["ship_size"] = size
    with pytest.raises(ValueError, match="specialized construction adapter"):
        resolve_ship(state, ship_request("12", "7"))


@pytest.mark.parametrize("field,value", [
    ("queue_id", "9"), ("paying_country", "1"), ("progress", True),
    ("progress", -1), ("progress", math.nan), ("progress_needed", math.inf),
    ("progress", 60), ("progress_needed", 0), ("resources", {}),
    ("resources", {"alloys": 0}), ("resources", {"alloys": -100}),
    ("resources", {"alloys": True}), ("resources", {"alloys": math.inf}),
    ("buildable_type", "buildable_planet_building"),
    ("buildable", {"ship_design_implementation": {"design": 8, "upgrade": 4294967295, "growth_stage": 0},
                   "orbitable": {"starbase": 12}}),
    ("buildable", {"ship_design_implementation": {"design": 7, "upgrade": 4294967295, "growth_stage": 1},
                   "orbitable": {"starbase": 12}}),
    ("buildable", {"ship_design_implementation": {"design": 7, "upgrade": 4294967295, "growth_stage": 0},
                   "orbitable": {"starbase": 100}}),
])
def test_receipt_wrong_payment_target_or_completed_job_never_verifies(state, field, value):
    command = resolve_ship(state, ship_request("12", "7"))
    after = paid_after(state)
    after["construction"]["items_by_id"]["31"][field] = value
    assert not verify_ship(state, after, command)


@pytest.mark.parametrize("change", ["free", "wrong_cost", "missing_stock", "boolean_stock", "cross_month",
                                    "old_job", "two_new_jobs", "changed_fleet", "changed_design", "forged_command"])
def test_ship_job_needs_unique_new_exact_payment_and_unchanged_current_design(state, change):
    command = resolve_ship(state, ship_request("12", "7"))
    after = paid_after(state)
    if change == "free":
        after["resources"] = deepcopy(state["resources"])
    elif change == "wrong_cost":
        after["resources"]["alloys"] -= 1
    elif change == "missing_stock":
        del state["resources"]["alloys"]
    elif change == "boolean_stock":
        after["resources"]["alloys"] = True
    elif change == "cross_month":
        after["date"] = "2200.02.01"
    elif change == "old_job":
        set_items(state, [job()])
    elif change == "two_new_jobs":
        second = job()
        second["id"] = "32"
        set_items(after, [job(), second])
    elif change == "changed_fleet":
        after["fleets_by_id"]["10"]["mobile"] = True
    elif change == "changed_design":
        after["shipbuilding"]["designs_by_id"]["7"]["ship_size"] = "destroyer"
    else:
        command = replace(command, station_id="12")
    assert not verify_ship(state, after, command)


@pytest.mark.parametrize("change", ["construction_unknown", "stale_queue", "stale_count", "queue_id",
                                    "design_id", "yard_id", "backwards_date", "campaign", "version"])
def test_unknown_or_inconsistent_join_cannot_prove_a_paid_ship_even_with_matching_global_item(state, change):
    command = resolve_ship(state, ship_request("12", "7"))
    after = paid_after(state)
    if change == "construction_unknown":
        after["construction"]["known"] = False
    elif change == "stale_queue":
        after["construction"]["queues_by_id"]["8"]["items"] = []
    elif change == "stale_count":
        after["construction"]["queues_by_id"]["8"]["item_count"] = 0
    elif change == "queue_id":
        after["construction"]["queues_by_id"]["8"]["id"] = "9"
    elif change == "design_id":
        state["shipbuilding"]["designs_by_id"]["7"]["id"] = "8"
        after["shipbuilding"]["designs_by_id"]["7"]["id"] = "8"
    elif change == "yard_id":
        state["shipbuilding"]["shipyards_by_id"]["12"]["id"] = "13"
        after["shipbuilding"]["shipyards_by_id"]["12"]["id"] = "13"
    elif change == "backwards_date":
        after["date"] = "2200.01.09"
    else:
        after["metadata"]["campaign_id" if change == "campaign" else "game_version"] = "different"
    assert not verify_ship(state, after, command)
