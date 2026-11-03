"""Counterexamples for exact-order and native-command observation gates."""
import pytest

from stellaris_autopilot.direct_observe import DirectObserver
from stellaris_autopilot.direct_orders import Order, validate_order, verify_order


def state(tmp_path, text):
    path = tmp_path / "state.txt"
    path.write_text(text, encoding="utf-8")
    return DirectObserver(path, "0").read().state


FLEET = '''date=2200.01.01 player={country=0} country={0={}}
fleet={3={owner=0 ship_class=shipclass_military properties={mobile=yes}
combat={start_date="0.01.01"} movement_manager={coordinate={origin=92}} BODY}}
'''


@pytest.mark.parametrize("body", [
    "actions={move_to={target=92}} actions={wait={time=30}}",
    "actions={wait={time=30}} ship_class=shipclass_military",
    "actions={wait={time=30}} combat={start_date=0.01.01}",
    "actions={wait={time=30}} properties={mobile=yes}",
    "actions={wait={time=30}} owner=0",
])
def test_duplicate_action_identity_or_precondition_fields_never_verify(tmp_path, body):
    observed = state(tmp_path, FLEET.replace("BODY", body))
    assert not verify_order(observed, Order("fleet_wait", "3", days=30))


def test_duplicate_fleet_collections_cannot_hide_other_actions(tmp_path):
    observed = state(tmp_path, FLEET.replace("BODY", "actions={wait={time=30}}") + "fleet={3={owner=0}}")
    assert "fleets" in observed["metadata"]["missing_fields"]


@pytest.mark.parametrize("coordinate", [
    "coordinate={origin=9} coordinate={origin=92}",
    "movement_manager={coordinate={origin=92}}",
])
def test_duplicate_coordinate_sources_cannot_establish_orbit_system(tmp_path, coordinate):
    # The fixture already has movement_manager; a duplicate secondary manager
    # or primary coordinate must not pick a final convenient value.
    text = FLEET.replace("BODY", coordinate + " actions={wait={time=30}}")
    if coordinate.startswith("coordinate"):
        text = text.replace("movement_manager={coordinate={origin=92}}", "")
    observed = state(tmp_path, text + "planet={3={owner=0 coordinate={origin=92}}}")
    with pytest.raises(ValueError):
        validate_order(observed, Order("fleet_orbit", "3", planet_id="3"))


@pytest.mark.parametrize("suffix", ["0", "{flag_date=0}", "no"])
def test_duplicate_receipt_is_unknown_even_if_one_timestamp_is_valid(tmp_path, suffix):
    flag = "qs_direct_done_" + "a" * 32
    observed = state(tmp_path, f"date=2200.01.01 country={{0={{flags={{{flag}=0 {flag}={suffix}}}}}}}")
    assert observed["direct_bridge"]["done_flags"] == []


def test_research_choices_and_normal_progress_are_country_scoped(tmp_path):
    observed = state(tmp_path, '''date=2200.01.01 country={
      0={tech_status={alternatives={physics={tech_shields_2 tech_physics_1}}
         physics_queue={{technology=tech_shields_2 progress=12.5 date=2200.01.01}}
         auto_researching_physics=no}}
      1={tech_status={alternatives={physics={tech_secret_other}}}}
    }''')
    physics = observed["research_status"]["areas"]["physics"]
    assert physics["known"] and physics["current"] == "tech_shields_2"
    assert physics["alternatives"] == ["tech_shields_2", "tech_physics_1"]
    assert physics["queue"][0]["progress"] == 12.5
    assert "tech_secret_other" not in str(observed)
    assert observed["research_status"]["areas"]["society"]["queue"] is None


@pytest.mark.parametrize("payload", [
    "alternatives={physics={tech_a tech_a}} auto_researching_physics=no",
    "alternatives={physics={tech_a}} alternatives={physics={tech_b}} auto_researching_physics=no",
    "alternatives={physics={tech_a}} auto_researching_physics=no physics_queue={bad=1}",
    "alternatives={physics={tech_a}} auto_researching_physics=no physics_queue={{technology=tech_a progress=-1 date=2200.01.01}}",
])
def test_ambiguous_or_missing_research_never_means_idle(tmp_path, payload):
    observed = state(tmp_path, "date=2200.01.01 country={0={tech_status={" + payload + "}}}")
    assert observed["research_status"]["areas"]["physics"]["known"] is False


def test_missing_completed_list_is_unknown_and_missing_progress_is_not_zero(tmp_path):
    observed = state(tmp_path, '''date=2200.01.01 country={0={tech_status={
      alternatives={physics={tech_shields_2}} auto_researching_physics=no
      physics_queue={{technology=tech_shields_2 date=2200.01.01}}
    }}}''')["research_status"]
    assert observed["completed_known"] is False and observed["completed_technologies"] is None
    assert observed["areas"]["physics"]["current_known"] is True
    assert observed["areas"]["physics"]["queue"][0]["progress"] is None
    assert observed["areas"]["physics"]["queue"][0]["progress_known"] is False


CONSTRUCTION = '''date=2200.01.01 country={0={}}
planet={3={owner=0 colony=0 build_queue=0}}
colony={0={carrier={type=planet reference=3}}}
construction={
 queue_mgr={queues={
  0={owner=0 location={type=2 id=3} simultaneous=1 type=planet}
  1={owner=1 location={type=2 id=4} simultaneous=1 type=planet}
 }}
 item_mgr={items={
  7={queue=0 paying_country=0 progress=1 progress_needed=480 resources={minerals=500}
     buildable_district={district=district_city planet=0}}
  8={queue=1 paying_country=1 progress=2 progress_needed=480 resources={minerals=500}
     buildable_district={district=district_secret_other planet=4}}
  9=none
 }}
}'''


def test_paid_construction_joins_only_owned_queue_and_payer(tmp_path):
    observed = state(tmp_path, CONSTRUCTION)["construction"]
    assert observed["known"] and observed["queue_count"] == observed["item_count"] == 1
    assert observed["items"][0]["resources"] == {"minerals": 500}
    assert observed["items"][0]["buildable"]["planet"] == 0
    assert observed["items"][0]["target"] == {"planet_id": "3", "colony_id": "0"}
    assert "district_secret_other" not in str(observed)
    assert observed["queues"][0]["order_known"] is False


@pytest.mark.parametrize("text", [
    CONSTRUCTION.replace("paying_country=0", "paying_country=1"),
    CONSTRUCTION.replace("queue=0", "queue=99"),
    CONSTRUCTION.replace("progress=1", "progress=1 progress=2"),
    CONSTRUCTION.replace("resources={minerals=500}", "resources={minerals=500 minerals=10}", 1),
    "date=2200.01.01 country={0={}} construction={queue_mgr={queues={}}}",
])
def test_unknown_or_conflicting_construction_cannot_report_zero_items(tmp_path, text):
    observed = state(tmp_path, text)["construction"]
    assert observed["known"] is False
    assert observed["item_count"] is None


def test_empty_managers_prove_zero_but_absent_managers_do_not(tmp_path):
    empty = "date=2200.01.01 country={0={}} construction={queue_mgr={queues={}} item_mgr={items={}}}"
    assert state(tmp_path, empty)["construction"]["item_count"] == 0
    assert state(tmp_path, "date=2200.01.01 country={0={}}")["construction"]["item_count"] is None


def test_duplicate_planet_queue_reference_cannot_disappear_into_known_empty(tmp_path):
    text = '''date=2200.01.01 country={0={}}
    planet={3={owner=0 build_queue=99 build_queue=100}}
    construction={queue_mgr={queues={}} item_mgr={items={}}}'''
    observed = state(tmp_path, text)["construction"]
    assert not observed["known"] and observed["item_count"] is None


def test_construction_payload_must_match_its_planet_queue(tmp_path):
    observed = state(tmp_path, CONSTRUCTION.replace("district=district_city planet=0", "district=district_city planet=3"))["construction"]
    assert not observed["known"] and observed["item_count"] is None
    assert "items.location_conflict" in observed["unknown_fields"]
