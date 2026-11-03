"""Exact zone ownership, normal paid building jobs and unknown-state regressions."""
from copy import deepcopy

import pytest

from stellaris_autopilot.native import building_request, resolve_building, verify_building
from stellaris_autopilot.pdx import loads
from stellaris_autopilot.saves import _planet_zones
import test_native_construction as construction


@pytest.fixture
def building_states():
    before, _ = construction.snapshots.__wrapped__()
    planet = before["planets_by_id"]["3"]
    planet.update({"zones_known": True, "zones_by_id": {"0": {
        "id": "0", "district_id": "0", "district_type": "district_city", "type": "zone_default",
        "buildings_known": True, "buildings_by_id": {"0": {"id": "0", "type": "building_capital", "position": 0}},
    }}})
    after = deepcopy(before)
    after["resources"]["minerals"] -= 400
    construction.install_item(after, {"id": "8", "queue_id": "0", "paying_country": "0", "progress": 0,
                         "progress_needed": 360, "resources": {"minerals": 400},
                         "buildable_type": "buildable_planet_building", "buildable": {
                             "planet": 0, "zone": 0, "building": "building_research_lab_1"},
                         "target": {"planet_id": "3", "colony_id": "0"}})
    return before, after


def test_exact_paid_building_verifies_without_date_progress(building_states):
    before, after = building_states
    request = resolve_building(before, building_request("3", "0", "building_research_lab_1"))
    assert request.queue_id == "0" and request.zone_id == "0"
    assert verify_building(before, after, request)


@pytest.mark.parametrize("planet,zone,building", [
    (3, "0", "building_a"), ("3", "00", "building_a"), ("3", "4294967295", "building_a"),
    ("3", True, "building_a"), ("3", "0", "building_a\neffect cash=1"), ("3", "0", "building_"),
])
def test_building_identifiers_cannot_be_code_or_sentinels(planet, zone, building):
    with pytest.raises(ValueError):
        building_request(planet, zone, building)


@pytest.mark.parametrize("field,value", [
    ("zone", 1), ("planet", 4), ("building", "building_stronghold"),
])
def test_wrong_zone_planet_or_building_cannot_verify(building_states, field, value):
    before, after = building_states
    request = resolve_building(before, building_request("3", "0", "building_research_lab_1"))
    after["construction"]["items_by_id"]["8"]["buildable"][field] = value
    assert not verify_building(before, after, request)


@pytest.mark.parametrize("change", ["no_payment", "finished", "instant", "unknown_before", "unknown_after", "wrong_queue", "old_job", "cross_month"])
def test_building_ack_or_wrong_payment_cannot_pass(building_states, change):
    before, after = building_states
    request = resolve_building(before, building_request("3", "0", "building_research_lab_1"))
    if change == "no_payment":
        after["resources"] = dict(before["resources"])
    elif change == "finished":
        after["construction"]["items_by_id"]["8"]["progress"] = 360
    elif change == "instant":
        after["planets_by_id"]["3"]["zones_by_id"]["0"]["buildings_by_id"]["9"] = {"type": "building_research_lab_1"}
    elif change.startswith("unknown_"):
        (before if change.endswith("before") else after)["planets_by_id"]["3"]["zones_known"] = False
    elif change == "wrong_queue":
        after["construction"]["items_by_id"]["8"]["queue_id"] = "9"
    elif change == "old_job":
        before["construction"]["items_by_id"]["8"] = deepcopy(after["construction"]["items_by_id"]["8"])
    else:
        after["date"] = "2200.02.01"
    assert not verify_building(before, after, request)


BASE = '''colony={0={carrier={type=planet reference=3} districts={0 1}}}
districts={0={type=district_city zones={0 1}} 1={type=district_mining zones={4294967295}}}
zones={0={type=zone_default buildings={0}} 1={type=zone_research_unity}}
buildings={0={type=building_capital position=0}}
'''


def test_zone_join_preserves_unknown_empty_and_skips_inactive_slot():
    result = _planet_zones(loads(BASE), {"id": "3", "colony": 0})
    assert result["zones_known"] is True and set(result["zones_by_id"]) == {"0", "1"}
    assert result["zones_by_id"]["0"]["buildings_known"] is True
    assert result["zones_by_id"]["1"]["buildings_known"] is False
    assert result["zones_by_id"]["1"]["buildings_by_id"] is None


@pytest.mark.parametrize("old,new", [
    ("reference=3", "reference=4"), ("districts={0 1}", "districts={0 0}"),
    ("zones={0 1}", "zones={0 0}"), ("zones={4294967295}", "zones={0}"),
    ("type=zone_default", "type=zone_default type=zone_urban"),
])
def test_ambiguous_zone_parent_or_identity_is_unknown(old, new):
    result = _planet_zones(loads(BASE.replace(old, new)), {"id": "3", "colony": 0})
    assert result["zones_known"] is False


@pytest.mark.parametrize("old,new", [
    ("buildings={0}", "buildings={0 0}"), ("position=0", "position=0 position=1"),
    ("type=building_capital", "type=building_capital type=building_other"),
])
def test_ambiguous_building_list_stays_unknown(old, new):
    result = _planet_zones(loads(BASE.replace(old, new)), {"id": "3", "colony": 0})
    assert result["zones_by_id"]["0"]["buildings_known"] is False
