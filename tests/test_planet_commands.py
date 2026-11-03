"""Exact native planet targets and conservative normal-order receipts."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from stellaris_autopilot.pdx import loads
from stellaris_autopilot.planet_commands import (
    native_planet_payload, planet_request, resolve_planet_command, verify_planet_command,
)
from stellaris_autopilot.saves import _planet_zones


TOKEN = "a" * 32


@pytest.fixture
def snapshot():
    planet = {
        "id": "3", "owner": 0, "controller": 0, "colony": 0, "build_queue": 0,
        "districts_known": True, "district_levels": {"district_city": 3},
        "zones_known": True, "zones_by_id": {"4": {
            "id": "4", "district_id": "10", "district_type": "district_city", "slot_index": 2,
            "type": "zone_default", "buildings_known": True, "buildings_by_id": {
                "17": {"id": "17", "type": "building_research_lab_1", "position": 0},
                "18": {"id": "18", "type": "building_holo_theatres", "position": 1},
            },
        }},
    }
    queue = {"id": "0", "owner": "0", "type": "planet", "location": {"type": 2, "id": "3"},
             "known": True, "items_known": True, "items": [], "item_count": 0}
    return {
        "country_id": "0", "date": "2200.01.10", "resources": {"minerals": 1000, "exotic_gases": 500},
        "metadata": {"campaign_id": "single_player_fixture", "game_version": "Cygnus v4.5.2 (9776)"},
        "planets": [planet], "planets_by_id": {"3": planet},
        "construction": {"known": True, "queues_by_id": {"0": queue}, "items_by_id": {}},
    }


def request(kind):
    fields = {"planet_id": "3"}
    if kind == "cancel_construction":
        fields["item_id"] = "7"
    else:
        fields["zone_id"] = "4"
        if kind == "convert_zone":
            fields["target_zone"] = "zone_research_unity"
        else:
            fields["building_id"] = "17"
            if kind == "upgrade_building":
                fields["target_building"] = "building_research_lab_2"
    return planet_request(kind, **fields)


def set_items(state, items):
    state["construction"]["items_by_id"] = {item["id"]: item for item in items}
    queue = state["construction"]["queues_by_id"]["0"]
    queue["items"] = [item for item in items if item["queue_id"] == "0"]
    queue["item_count"] = len(queue["items"])


def job(kind="upgrade_building"):
    upgrade = kind == "upgrade_building"
    return {
        "id": "7", "queue_id": "0", "paying_country": "0", "progress": 0, "progress_needed": 360,
        "resources": {"minerals": 400, "exotic_gases": 100},
        "buildable_type": "buildable_planet_upgrade_building" if upgrade else "buildable_zone",
        "buildable": {"building": "building_research_lab_2", "planet": 0, "zone": 4, "upgrade_building": 17}
        if upgrade else {"zone": "zone_research_unity", "planet": 0, "district": 10, "zone_slot": 2},
        "target": {"planet_id": "3", "colony_id": "0"},
    }


def paid_after(before, kind):
    after = deepcopy(before)
    item = job(kind)
    set_items(after, [item])
    for resource, cost in item["resources"].items():
        after["resources"][resource] -= cost
    return after


@pytest.mark.parametrize("kind", ["upgrade_building", "convert_zone"])
def test_paid_timed_exact_job_verifies_without_date_progress(snapshot, kind):
    resolved = resolve_planet_command(snapshot, request(kind))
    assert resolved.colony_id == "0" and resolved.request.planet_id == "3"
    assert resolved.district_id == "10" and resolved.slot_index == 2
    assert verify_planet_command(snapshot, paid_after(snapshot, kind), resolved)
    payload = native_planet_payload(resolved, TOKEN)
    assert payload["colony_id"] == "0" and payload["planet_id"] == "3" and payload["country_id"] == "0"
    assert payload["source_zone"] == "zone_default"


@pytest.mark.parametrize("value", [3, True, "03", "-1", "３", "4294967295", "3\neffect cash=1"])
def test_only_canonical_bounded_observed_handles_are_requests(value):
    with pytest.raises(ValueError):
        planet_request("demolish_building", planet_id=value, zone_id="4", building_id="17")


@pytest.mark.parametrize("fields", [
    {"target_building": "building_lab\nadd_resource = 1"},
    {"target_building": "building_"}, {"target_building": "building_" + "x" * 121},
    {"script": "effect remove_planet=yes"}, {"building_id": 17},
])
def test_data_keys_and_extra_fields_cannot_carry_code(fields):
    payload = request("upgrade_building").to_dict()
    payload.pop("kind")
    payload.update(fields)
    with pytest.raises(ValueError):
        planet_request("upgrade_building", **payload)


@pytest.mark.parametrize("path,value", [
    (("country_id",), "1"), (("planets_by_id", "3", "id"), "0"),
    (("planets_by_id", "3", "owner"), 1), (("planets_by_id", "3", "controller"), True),
    (("planets_by_id", "3", "colony"), "00"), (("planets_by_id", "3", "zones_known"), False),
    (("planets_by_id", "3", "zones_by_id", "4", "buildings_known"), False),
    (("planets_by_id", "3", "zones_by_id", "4", "buildings_by_id"), None),
    (("planets_by_id", "3", "zones_by_id", "4", "slot_index"), None),
    (("planets_by_id", "3", "zones_by_id", "4", "slot_index"), True),
    (("planets_by_id", "3", "zones_by_id", "4", "slot_index"), 32),
    (("construction", "queues_by_id", "0", "location"), {"type": 2, "id": "0"}),
    (("construction", "queues_by_id", "0", "items"), None),
    (("construction", "queues_by_id", "0", "owner"), "1"),
])
def test_resolve_requires_exact_owned_namespace_and_known_lists(snapshot, path, value):
    container = snapshot
    for key in path[:-1]:
        container = container[key]
    container[path[-1]] = value
    with pytest.raises(ValueError):
        resolve_planet_command(snapshot, request("upgrade_building"))


def test_building_in_another_zone_cannot_be_used_as_source(snapshot):
    del snapshot["planets_by_id"]["3"]["zones_by_id"]["4"]["buildings_by_id"]["17"]
    with pytest.raises(ValueError):
        resolve_planet_command(snapshot, request("demolish_building"))


@pytest.mark.parametrize("kind,changes", [
    ("upgrade_building", {"target_building": "building_research_lab_1"}),
    ("convert_zone", {"target_zone": "zone_default"}),
])
def test_same_type_is_not_an_upgrade_or_conversion(snapshot, kind, changes):
    with pytest.raises(ValueError):
        resolve_planet_command(snapshot, replace(request(kind), **changes))


@pytest.mark.parametrize("token", ["", "A" * 32, "a" * 31, "a" * 33, "a" * 32 + "\n", True])
def test_native_tokens_are_exact_and_canonical(snapshot, token):
    with pytest.raises(ValueError):
        native_planet_payload(resolve_planet_command(snapshot, request("upgrade_building")), token)


def test_manually_forged_resolved_extra_fields_are_rejected(snapshot):
    conversion = resolve_planet_command(snapshot, request("convert_zone"))
    with pytest.raises(ValueError):
        native_planet_payload(replace(conversion, source_building="building_capital"), TOKEN)
    set_items(snapshot, [job()])
    cancellation = resolve_planet_command(snapshot, request("cancel_construction"))
    with pytest.raises(ValueError):
        native_planet_payload(replace(cancellation, slot_index=0), TOKEN)


@pytest.mark.parametrize("kind", ["upgrade_building", "convert_zone"])
@pytest.mark.parametrize("change", [
    "free", "wrong_cost", "completed", "instant", "wrong_colony", "wrong_zone_or_slot", "wrong_source_or_district",
    "changed_source", "existing_job", "stale_queue_list", "wrong_payer", "campaign", "version", "cross_month",
])
def test_ack_wrong_target_instant_changes_and_stale_receipts_do_not_verify(snapshot, kind, change):
    resolved = resolve_planet_command(snapshot, request(kind))
    after = paid_after(snapshot, kind)
    item = after["construction"]["items_by_id"]["7"]
    if change == "free":
        after["resources"] = dict(snapshot["resources"])
    elif change == "wrong_cost":
        after["resources"]["minerals"] -= 1
    elif change == "completed":
        item["progress"] = item["progress_needed"]
    elif change == "instant":
        after["planets_by_id"]["3"]["zones_by_id"]["4"]["type"] = "zone_research_unity"
    elif change == "wrong_colony":
        item["buildable"]["planet"] = 3
    elif change == "wrong_zone_or_slot":
        item["buildable"]["zone" if kind == "upgrade_building" else "zone_slot"] = 1
    elif change == "wrong_source_or_district":
        item["buildable"]["upgrade_building" if kind == "upgrade_building" else "district"] = 18
    elif change == "changed_source":
        after["planets_by_id"]["3"]["zones_by_id"]["4"]["buildings_by_id"]["17"]["type"] = "building_capital"
    elif change == "existing_job":
        set_items(snapshot, [deepcopy(item)])
    elif change == "stale_queue_list":
        after["construction"]["queues_by_id"]["0"]["items"] = []
    elif change == "wrong_payer":
        item["paying_country"] = "1"
    elif change in ("campaign", "version"):
        after["metadata"]["campaign_id" if change == "campaign" else "game_version"] = "changed"
    else:
        after["date"] = "2200.02.01"
    assert not verify_planet_command(snapshot, after, resolved)


def test_exact_demolition_requires_only_selected_building_to_disappear(snapshot):
    resolved = resolve_planet_command(snapshot, request("demolish_building"))
    after = deepcopy(snapshot)
    buildings = after["planets_by_id"]["3"]["zones_by_id"]["4"]["buildings_by_id"]
    del buildings["17"]
    assert verify_planet_command(snapshot, after, resolved)
    del buildings["18"]
    assert not verify_planet_command(snapshot, after, resolved)


def test_deleting_other_building_or_returning_only_ack_does_not_verify(snapshot):
    resolved = resolve_planet_command(snapshot, request("demolish_building"))
    assert not verify_planet_command(snapshot, deepcopy(snapshot), resolved)
    after = deepcopy(snapshot)
    del after["planets_by_id"]["3"]["zones_by_id"]["4"]["buildings_by_id"]["18"]
    assert not verify_planet_command(snapshot, after, resolved)


def cancellation(snapshot):
    set_items(snapshot, [job()])
    resolved = resolve_planet_command(snapshot, request("cancel_construction"))
    after = deepcopy(snapshot)
    set_items(after, [])
    for resource, cost in job()["resources"].items():
        after["resources"][resource] += cost
    return resolved, after


def test_cancellation_requires_exact_unfinished_item_removal_and_full_refund(snapshot):
    resolved, after = cancellation(snapshot)
    assert verify_planet_command(snapshot, after, resolved)


@pytest.mark.parametrize("change", ["refund", "stale_queue", "instant_completion", "retained_item", "wrong_removed_item"])
def test_cancel_ack_stale_queue_or_completed_construction_does_not_verify(snapshot, change):
    resolved, after = cancellation(snapshot)
    if change == "refund":
        after["resources"]["minerals"] -= 1
    elif change == "stale_queue":
        after["construction"]["queues_by_id"]["0"]["items"] = deepcopy(snapshot["construction"]["queues_by_id"]["0"]["items"])
    elif change == "instant_completion":
        after["planets_by_id"]["3"]["zones_by_id"]["4"]["buildings_by_id"]["17"]["type"] = "building_research_lab_2"
    elif change == "retained_item":
        set_items(after, [job()])
    else:
        other = job()
        other["id"] = "8"
        set_items(snapshot, [job(), other])
        set_items(after, [job()])
    assert not verify_planet_command(snapshot, after, resolved)


@pytest.mark.parametrize("field,value", [
    ("queue_id", "1"), ("paying_country", "1"), ("progress", 360),
    ("target", {"planet_id": "0", "colony_id": "3"}),
])
def test_cancel_cannot_resolve_foreign_stale_or_finished_item(snapshot, field, value):
    item = job()
    item[field] = value
    set_items(snapshot, [item])
    with pytest.raises(ValueError):
        resolve_planet_command(snapshot, request("cancel_construction"))


def test_zone_slot_retains_inactive_array_positions_and_unknown_buildings():
    root = loads('''colony={0={carrier={type=planet reference=3} districts={10}}}
districts={10={type=district_city zones={4294967295 4294967295 4}}}
zones={4={type=zone_default}} buildings={}
''')
    result = _planet_zones(root, {"id": "3", "colony": 0})
    assert result["zones_known"] is True
    assert result["zones_by_id"]["4"]["slot_index"] == 2
    assert result["zones_by_id"]["4"]["buildings_known"] is False
    assert result["zones_by_id"]["4"]["buildings_by_id"] is None


def test_javascript_parser_is_pure_and_accepts_only_exact_typed_payloads(snapshot):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the pure JavaScript parser check")
    valid = [native_planet_payload(resolve_planet_command(snapshot, request(kind)), TOKEN)
             for kind in ("upgrade_building", "demolish_building", "convert_zone")]
    set_items(snapshot, [job()])
    valid.append(native_planet_payload(resolve_planet_command(snapshot, request("cancel_construction")), TOKEN))
    malformed = [{**valid[0], "address": "0x1234"}, {**valid[0], "country_id": "1"},
                 {**valid[0], "building_id": 17}, {**valid[0], "slot_index": True},
                 {**valid[0], "zone_id": "4294967295"}, {**valid[0], "token": "A" * 32},
                 {**valid[0], "target_building": "building_x\ninstant=yes"}]
    source = Path(__file__).parents[1] / "src" / "stellaris_autopilot" / "native_planet.js"
    script = r'''
const fs = require('fs'), vm = require('vm');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const requests = JSON.parse(fs.readFileSync(0, 'utf8'));
const results = requests.map(request => {
    context.input = request;
    try { return {value: vm.runInContext('canonicalPlanetRequest(input)', context)}; }
    catch (_) { return {error: true}; }
});
process.stdout.write(JSON.stringify(results));
'''
    result = subprocess.run([node, "-e", script, str(source)], input=json.dumps(valid + malformed + [{"kind": "other"}]),
                            capture_output=True, text=True, timeout=10, check=True)
    received = json.loads(result.stdout)
    assert [item["value"] for item in received[:len(valid)]] == valid
    assert all(item == {"error": True} for item in received[len(valid):-1])
    assert received[-1] == {"value": None}
