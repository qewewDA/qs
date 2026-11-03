"""Independent normal-payment, pending-job and district-observability gates."""
from copy import deepcopy

import pytest

from stellaris_autopilot.direct_observe import DirectObserver
from stellaris_autopilot.native import (
    DistrictConstruction, DistrictRequest, district_request, resolve_district, verify_district,
)


@pytest.fixture
def snapshots():
    planet = {
        "id": "3", "owner": 0, "controller": 0, "colony": 0, "build_queue": 0,
        "districts_known": True, "district_levels": {"district_city": 3},
    }
    queue = {
        "id": "0", "owner": "0", "type": "planet", "location": {"type": 2, "id": "3"},
        "simultaneous": 1, "disabled": False, "known": True, "items_known": True,
        "item_count": 0, "items": [], "order_known": True,
    }
    before = {
        "country_id": "0", "date": "2200.01.10", "resources": {"minerals": 1000},
        "planets": [planet], "planets_by_id": {"3": planet},
        "construction": {
            "known": True, "queues_known": True, "items_known": True,
            "queue_count": 1, "item_count": 0, "queues": [queue], "queues_by_id": {"0": queue},
            "items": [], "items_by_id": {}, "unknown_fields": [],
        },
    }
    after = deepcopy(before)
    after["date"] = "2200.01.11"
    after["resources"]["minerals"] = 500
    item = {
        "id": "7", "queue_id": "0", "paying_country": "0", "progress": 1,
        "progress_needed": 480, "resources": {"minerals": 500},
        "buildable_type": "buildable_district",
        "buildable": {"district": "district_city", "planet": 0},
        "target": {"planet_id": "3", "colony_id": "0"},
    }
    install_item(after, item)
    return before, after


def install_item(state, item):
    construction = state["construction"]
    construction["items_by_id"][item["id"]] = item
    construction["items"] = list(construction["items_by_id"].values())
    construction["item_count"] = len(construction["items"])
    queue = construction["queues_by_id"].get(item["queue_id"])
    if queue is not None:
        queue["items"] = [value for value in construction["items"] if value["queue_id"] == queue["id"]]
        queue["item_count"] = len(queue["items"])
        queue["order_known"] = not queue["items"]


def set_field(state, path, value):
    container = state
    for key in path[:-1]:
        container = container[key]
    container[path[-1]] = value


def test_exact_pending_job_with_same_month_payment_and_unchanged_levels_verifies(snapshots):
    before, after = snapshots
    request = district_request("3", "district_city")
    resolved = resolve_district(before, request)
    assert request == DistrictRequest("3", "district_city")
    assert resolved == DistrictConstruction("3", "district_city", "0", "0")
    assert resolved.kind == "queue_district"
    assert resolved.to_dict() == {"planet_id": "3", "district": "district_city", "queue_id": "0", "colony_id": "0"}
    assert verify_district(before, after, resolved)


@pytest.mark.parametrize("planet_id,district", [
    (3, "district_city"), (True, "district_city"), ("03", "district_city"),
    ("-1", "district_city"), ("4294967295", "district_city"), ("３", "district_city"),
    ("3\neffect cash = 1", "district_city"), ("3", "district_"), ("3", "city"),
    ("3", "district_city\neffect cash = 1"), ("3", "district_" + "a" * 121), ("3", None),
])
def test_district_request_only_accepts_bounded_canonical_ids(planet_id, district):
    with pytest.raises(ValueError):
        district_request(planet_id, district)


@pytest.mark.parametrize("path,value", [
    (("country_id",), "1"),
    (("planets_by_id",), {}),
    (("planets_by_id", "3", "owner"), 1),
    (("planets_by_id", "3", "controller"), 1),
    (("planets_by_id", "3", "owner"), None),
    (("planets_by_id", "3", "controller"), True),
    (("planets_by_id", "3", "districts_known"), False),
    (("planets_by_id", "3", "district_levels"), None),
    (("planets_by_id", "3", "build_queue"), "999"),
    (("planets_by_id", "3", "build_queue"), "00"),
    (("construction", "known"), False),
    (("construction", "queues_by_id", "0", "known"), False),
    (("construction", "queues_by_id", "0", "items_known"), False),
    (("construction", "queues_by_id", "0", "owner"), "1"),
    (("construction", "queues_by_id", "0", "type"), "ships"),
    (("construction", "queues_by_id", "0", "location"), {"type": 2, "id": "4"}),
    (("construction", "queues_by_id", "0", "location"), {"type": 0, "id": "3"}),
])
def test_resolve_rejects_unobservable_foreign_or_mismatched_queue(snapshots, path, value):
    before, _ = snapshots
    set_field(before, path, value)
    with pytest.raises(ValueError):
        resolve_district(before, district_request("3", "district_city"))


@pytest.mark.parametrize("field,value", [
    ("paying_country", "1"), ("queue_id", "1"), ("buildable_type", "buildable_ship"),
    ("buildable", {"district": "district_city", "planet": 4}),
    ("buildable", {"district": "district_mining", "planet": 3}),
    ("buildable", {"district": "district_city", "planet": "3"}),
    ("progress", -1), ("progress", 480), ("progress", 481), ("progress", None),
    ("progress", True), ("progress", float("nan")),
    ("progress_needed", 0), ("progress_needed", float("inf")),
    ("resources", {}), ("resources", {"minerals": 0}),
    ("resources", {"minerals": -500}), ("resources", {"minerals": True}),
    ("resources", {"minerals": float("nan")}),
])
def test_receipt_like_pending_item_with_wrong_target_cost_or_completed_progress_does_not_verify(
        snapshots, field, value):
    before, after = snapshots
    after["construction"]["items_by_id"]["7"][field] = value
    assert not verify_district(before, after, DistrictConstruction("3", "district_city", "0", "0"))


def test_existing_item_cannot_prove_a_new_pending_job(snapshots):
    before, after = snapshots
    install_item(before, deepcopy(after["construction"]["items_by_id"]["7"]))
    assert not verify_district(before, after, DistrictConstruction("3", "district_city", "0", "0"))


def test_two_new_items_in_target_queue_cannot_prove_one_new_job(snapshots):
    before, after = snapshots
    second = deepcopy(after["construction"]["items_by_id"]["7"])
    second["id"] = "8"
    install_item(after, second)
    assert not verify_district(before, after, DistrictConstruction("3", "district_city", "0", "0"))


@pytest.mark.parametrize("side,path,value", [
    ("after", ("date",), "2200.02.01"),
    ("after", ("resources", "minerals"), 1000),
    ("after", ("resources", "minerals"), 499),
    ("after", ("resources", "minerals"), 501),
    ("after", ("resources", "minerals"), None),
    ("before", ("resources", "minerals"), None),
    ("after", ("planets_by_id", "3", "district_levels", "district_city"), 4),
    ("after", ("construction", "known"), False),
    ("before", ("construction", "known"), False),
    ("after", ("planets_by_id", "3", "districts_known"), False),
    ("before", ("planets_by_id", "3", "districts_known"), False),
])
def test_unknown_data_wrong_payment_cross_month_or_instant_level_gain_never_verifies(snapshots, side, path, value):
    before, after = snapshots
    set_field(before if side == "before" else after, path, value)
    assert not verify_district(before, after, DistrictConstruction("3", "district_city", "0", "0"))


DISTRICT_SAVE = '''date=2200.01.10 player={country=0} country={0={resources={minerals=1000}}}
planets={planet={3={owner=0 controller=0 colony=0 build_queue=0}}}
colony={
 0={carrier={type=planet reference=3} districts={10 11}}
 1={carrier={type=planet reference=4} districts={99}}
}
districts={
 10={type=district_city level=3}
 11={type=district_industrial level=1}
 99={type=district_foreign_secret level=7}
}
construction={queue_mgr={queues={
 0={owner=0 location={type=2 id=3} simultaneous=1 type=planet}
}} item_mgr={items={}}}
'''


def observe_districts(tmp_path, text):
    path = tmp_path / "district_snapshot.sav.txt"
    path.write_text(text, encoding="utf-8")
    return DirectObserver(path, "0").read().state


def test_actual_colony_carrier_and_district_handles_normalize_only_selected_planet(tmp_path):
    state = observe_districts(tmp_path, DISTRICT_SAVE)
    planet = state["planets_by_id"]["3"]
    assert planet["districts_known"] is True
    assert planet["district_levels"] == {"district_city": 3, "district_industrial": 1}
    assert "district_foreign_secret" not in str(state["planets"])
    assert resolve_district(state, district_request("3", "district_city")) == DistrictConstruction(
        "3", "district_city", "0", "0")


@pytest.mark.parametrize("old,new", [
    ("colony=0", "missing_colony=0"),
    ("colony=0", "colony=0 colony=1"),
    ("carrier={type=planet reference=3}", "carrier={type=ship reference=3}"),
    ("carrier={type=planet reference=3}", "carrier={type=planet reference=4}"),
    ("carrier={type=planet reference=3}", "carrier={type=planet}"),
    ("carrier={type=planet reference=3}", "carrier={type=planet reference=3} carrier={type=planet reference=3}"),
    ("districts={10 11}", "missing_districts={10 11}"),
    ("districts={10 11}", "districts={10 11 10}"),
    ("districts={10 11}", "districts={10 500}"),
    ("type=district_city level=3", "type=district_city"),
    ("type=district_city level=3", "type=district_city level=3 level=4"),
    ("type=district_city level=3", 'type=district_city level="3"'),
    ("type=district_city level=3", "type=district_city level=-1"),
    ("type=district_city level=3", "type=district_city level=1.5"),
    ("11={type=district_industrial level=1}", "11={type=district_city level=1}"),
    ("10={type=district_city level=3}", "10={type=district_city level=3} 10={type=district_city level=4}"),
])
def test_duplicate_missing_or_bad_district_reference_stays_unknown_instead_of_zero(tmp_path, old, new):
    state = observe_districts(tmp_path, DISTRICT_SAVE.replace(old, new, 1))
    planet = state["planets_by_id"]["3"]
    assert planet["districts_known"] is False
    assert planet["district_levels"] is None
    with pytest.raises(ValueError):
        resolve_district(state, district_request("3", "district_city"))


def test_explicit_complete_empty_district_list_is_distinct_from_missing_list(tmp_path):
    state = observe_districts(tmp_path, DISTRICT_SAVE.replace("districts={10 11}", "districts={}", 1))
    planet = state["planets_by_id"]["3"]
    assert planet["districts_known"] is True
    assert planet["district_levels"] == {}
    assert "district_city" not in planet["district_levels"]
