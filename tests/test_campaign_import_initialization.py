"""Acceptance tests for the measured imported-save first-load queue cache."""

import copy
import time

import pytest

from stellaris_autopilot.campaign import (
    CampaignError,
    CampaignStore,
    imported_queue_initialization,
)
from stellaris_autopilot.direct_observe import DirectObserver


def _queue(queue_id, planet_id, **updates):
    queue = {
        "id": queue_id,
        "owner": "0",
        "type": "planet",
        "location": {"type": 2, "id": planet_id},
        "simultaneous": 1,
        "disabled": None,
        "known": True,
        "items_known": True,
        "item_count": 0,
        "items": [],
        "order_known": True,
    }
    queue.update(updates)
    return queue


def _body(planet_id, **updates):
    names = {"10": "Earth", "11": "Mars", "12": "Station", "13": "Outpost"}
    stations = {"10": "70", "11": "71", "12": "77", "13": "78"}
    body = {
        "id": planet_id,
        "system_id": "1",
        "name": names[planet_id],
        "surveyed_known": True,
        "surveyed": False,
        "controller": "0",
        "shipclass_orbital_station": stations[planet_id],
    }
    body.update(updates)
    return body


def _base_state():
    """Small normalized fixture shaped like the real date-one failure sample.

    The real sample had 56 added body references and five selected empty
    queues; this keeps two added references (one plain body and one orbital
    station) without copying the large dump into the repository.
    """
    q5 = _queue("5", "10")
    planet10 = {
        "id": "10",
        "name": "Earth",
        "owner": "0",
        "controller": "0",
        "colony": "20",
        "build_queue": "5",
    }
    return {
        "date": "2200.01.01",
        "country_id": "0",
        "resources": {"minerals": 617.55104},
        "metadata": {"source": "fixture://original.sav", "game_version": "Cygnus v4.5.2"},
        "planets": [planet10],
        "planets_by_id": {"10": planet10},
        "space": {
            "visited_systems_known": True,
            "visited_system_ids": ["1"],
            "surveyed_planets_known": True,
            "surveyed_planet_ids": [],
            "systems_by_id": {
                "1": {
                    "id": "1",
                    "name": "Sol",
                    "visited": True,
                    "planets_known": True,
                    "planet_ids": ["10", "11", "12"],
                }
            },
            "planets_by_id": {
                "10": _body("10", owner="0", colony="20", build_queue="5"),
                "11": _body("11"),
                "12": _body("12"),
            },
            "unknown_fields": [],
        },
        "construction": {
            "known": True,
            "queues_known": True,
            "items_known": True,
            "queue_count": 1,
            "item_count": 0,
            "queues": [q5],
            "queues_by_id": {"5": q5},
            "items": [],
            "items_by_id": {},
            "unknown_fields": [],
        },
    }


def _initialized_state():
    state = copy.deepcopy(_base_state())
    state["space"]["planets_by_id"]["11"]["build_queue"] = "6"
    state["space"]["planets_by_id"]["12"]["build_queue"] = "7"
    q6 = _queue("6", "11")
    q7 = _queue("7", "12")
    state["construction"]["queues"].extend([q6, q7])
    state["construction"]["queues_by_id"].update({"6": q6, "7": q7})
    state["construction"]["queue_count"] = 3
    return state


def _snapshots(expected, observed):
    return copy.deepcopy(expected), copy.deepcopy(observed)


def _assert_unchanged(expected, observed, before_expected, before_observed):
    assert expected == before_expected
    assert observed == before_observed


def test_accepts_new_body_references_and_empty_orbital_station_queues():
    expected, observed = _base_state(), _initialized_state()
    before_expected, before_observed = _snapshots(expected, observed)

    result = imported_queue_initialization(expected, observed)

    assert result == {
        "uncolonized_body_queues": [
            {"planet_id": "11", "queue_id": "6"},
            {"planet_id": "12", "queue_id": "7"},
        ],
        "selected_empty_queue_ids": ["6", "7"],
    }
    _assert_unchanged(expected, observed, before_expected, before_observed)


def test_unchanged_state_reports_no_initialization_and_preserves_inputs():
    expected, observed = _base_state(), _base_state()
    before_expected, before_observed = _snapshots(expected, observed)

    assert imported_queue_initialization(expected, observed) == {
        "uncolonized_body_queues": [],
        "selected_empty_queue_ids": [],
    }
    _assert_unchanged(expected, observed, before_expected, before_observed)


def _change_existing_queue(expected, observed):
    observed["construction"]["queues_by_id"]["5"]["simultaneous"] = 2


def _remove_existing_queue(expected, observed):
    expected["construction"]["queues_by_id"].pop("5")
    expected["construction"]["queues"].clear()
    expected["construction"]["queue_count"] = 0


def _add_job_to_existing_queue(expected, observed):
    queue = observed["construction"]["queues_by_id"]["5"]
    queue["items"].append({"id": "job-1", "queue_id": "5"})
    queue["item_count"] = 1
    queue["order_known"] = False


def _change_resources(expected, observed):
    observed["resources"]["minerals"] = 42


def _mark_body_as_owned_planet(expected, observed):
    planet = {
        "id": "11",
        "name": "Mars",
        "owner": "0",
        "controller": "0",
        "colony": "21",
        "build_queue": "6",
    }
    expected["planets"].append(planet)
    expected["planets_by_id"]["11"] = planet


def _change_unrelated_owned_planet(expected, observed):
    observed["planets_by_id"]["10"]["name"] = "Terra"
    for planet in observed["planets"]:
        if planet["id"] == "10":
            planet["name"] = "Terra"


def _add_old_reference_that_aliases_new_queue(expected, observed):
    extra = _body("13", build_queue="6")
    expected["space"]["planets_by_id"]["13"] = extra
    observed["space"]["planets_by_id"]["13"] = copy.deepcopy(extra)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_change_existing_queue, id="old-queue-changed"),
        pytest.param(_remove_existing_queue, id="old-queue-missing"),
        pytest.param(_add_job_to_existing_queue, id="existing-queue-job"),
        pytest.param(_change_resources, id="resource-changed"),
        pytest.param(_mark_body_as_owned_planet, id="owned-planet"),
        pytest.param(_change_unrelated_owned_planet, id="owned-planet-field-changed"),
        pytest.param(_add_old_reference_that_aliases_new_queue, id="old-reference-alias"),
    ],
)
def test_rejects_changes_to_existing_gameplay_state(mutate):
    expected, observed = _base_state(), _initialized_state()
    mutate(expected, observed)
    before_expected, before_observed = _snapshots(expected, observed)

    assert imported_queue_initialization(expected, observed) is None
    _assert_unchanged(expected, observed, before_expected, before_observed)


def _reuse_new_queue_for_another_body(expected, observed):
    observed["space"]["planets_by_id"]["12"]["build_queue"] = "6"
    queue = observed["construction"]["queues_by_id"].pop("7")
    observed["construction"]["queues"].remove(queue)
    observed["construction"]["queue_count"] = 2


def _change_queue_owner(expected, observed):
    observed["construction"]["queues_by_id"]["6"]["owner"] = "1"


def _change_body_controller(expected, observed):
    observed["space"]["planets_by_id"]["11"]["controller"] = "1"


def _remove_orbital_station(expected, observed):
    del expected["space"]["planets_by_id"]["11"]["shipclass_orbital_station"]
    del observed["space"]["planets_by_id"]["11"]["shipclass_orbital_station"]


def _change_queue_target(expected, observed):
    observed["construction"]["queues_by_id"]["6"]["location"]["id"] = "12"


def _make_new_queue_nonempty(expected, observed):
    queue = observed["construction"]["queues_by_id"]["6"]
    queue["items"].append({"id": "job-1", "queue_id": "6"})
    queue["item_count"] = 1
    queue["order_known"] = False


def _change_queue_disabled(expected, observed):
    observed["construction"]["queues_by_id"]["6"]["disabled"] = True


def _change_queue_known_to_int(expected, observed):
    observed["construction"]["queues_by_id"]["6"]["known"] = 1


def _change_queue_simultaneous_to_bool(expected, observed):
    observed["construction"]["queues_by_id"]["6"]["simultaneous"] = True


def _change_queue_item_count_to_bool(expected, observed):
    observed["construction"]["queues_by_id"]["6"]["item_count"] = True


def _change_location_type_to_bool(expected, observed):
    observed["construction"]["queues_by_id"]["6"]["location"]["type"] = True


def _change_queue_items_known_to_false(expected, observed):
    observed["construction"]["queues_by_id"]["6"]["items_known"] = False


def _change_queue_order_known_to_false(expected, observed):
    observed["construction"]["queues_by_id"]["6"]["order_known"] = False


def _change_queue_disabled_to_false(expected, observed):
    observed["construction"]["queues_by_id"]["6"]["disabled"] = False


def _change_queue_type(expected, observed):
    observed["construction"]["queues_by_id"]["6"]["type"] = "pop"


def _change_station_to_int(expected, observed):
    observed["space"]["planets_by_id"]["11"]["shipclass_orbital_station"] = 71


def _change_location_type_to_one(expected, observed):
    observed["construction"]["queues_by_id"]["6"]["location"]["type"] = 1


def _add_extra_queue_field(expected, observed):
    observed["construction"]["queues_by_id"]["6"]["unexpected"] = 1


def _use_leading_zero_queue_id(expected, observed):
    observed["space"]["planets_by_id"]["11"]["build_queue"] = "06"
    queue = observed["construction"]["queues_by_id"].pop("6")
    queue["id"] = "06"
    observed["construction"]["queues_by_id"]["06"] = queue


def _change_body_name(expected, observed):
    observed["space"]["planets_by_id"]["11"]["name"] = "Venus"


def _add_owner_to_new_body(expected, observed):
    observed["space"]["planets_by_id"]["11"]["owner"] = "0"


def _change_body_surveyed(expected, observed):
    observed["space"]["planets_by_id"]["11"]["surveyed"] = True


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_reuse_new_queue_for_another_body, id="duplicate-new-reference"),
        pytest.param(_change_queue_owner, id="queue-owner"),
        pytest.param(_change_body_controller, id="body-controller"),
        pytest.param(_remove_orbital_station, id="missing-orbital-station"),
        pytest.param(_change_queue_target, id="queue-target"),
        pytest.param(_make_new_queue_nonempty, id="nonempty-new-queue"),
        pytest.param(_change_queue_disabled, id="queue-disabled-not-none"),
        pytest.param(_change_queue_known_to_int, id="queue-known-not-true"),
        pytest.param(_change_queue_items_known_to_false, id="queue-items-known-not-true"),
        pytest.param(_change_queue_order_known_to_false, id="queue-order-known-not-true"),
        pytest.param(_change_queue_simultaneous_to_bool, id="queue-simultaneous-not-int"),
        pytest.param(_change_queue_item_count_to_bool, id="queue-item-count-not-int"),
        pytest.param(_change_location_type_to_bool, id="queue-location-type-not-int"),
        pytest.param(_change_location_type_to_one, id="queue-location-type-not-two"),
        pytest.param(_change_queue_disabled_to_false, id="queue-disabled-not-none-false"),
        pytest.param(_change_queue_type, id="queue-type-not-planet"),
        pytest.param(_change_station_to_int, id="orbital-station-not-str"),
        pytest.param(_add_extra_queue_field, id="extra-queue-field"),
        pytest.param(_use_leading_zero_queue_id, id="noncanonical-queue-id"),
        pytest.param(_change_body_name, id="body-name-changed"),
        pytest.param(_add_owner_to_new_body, id="new-body-owner"),
        pytest.param(_change_body_surveyed, id="new-body-surveyed-changed"),
    ],
)
def test_rejects_reference_identity_and_new_queue_shape_changes(mutate):
    expected, observed = _base_state(), _initialized_state()
    mutate(expected, observed)
    before_expected, before_observed = _snapshots(expected, observed)

    assert imported_queue_initialization(expected, observed) is None
    _assert_unchanged(expected, observed, before_expected, before_observed)


def _duplicate_queue_id(expected, observed):
    observed["construction"]["queues"].append(copy.deepcopy(observed["construction"]["queues_by_id"]["6"]))


def _remove_queue_from_list(expected, observed):
    observed["construction"]["queues"].remove(observed["construction"]["queues_by_id"]["6"])


def _remove_queue_from_map(expected, observed):
    observed["construction"]["queues_by_id"].pop("6")


def _change_queue_count(expected, observed):
    observed["construction"]["queue_count"] = 4


def _change_queue_count_to_bool(expected, observed):
    observed["construction"]["queue_count"] = True


def _replace_queue_list_with_mapping(expected, observed):
    observed["construction"]["queues"] = {}


def _replace_queue_map_with_list(expected, observed):
    observed["construction"]["queues_by_id"] = []


def _change_construction_known_to_false(expected, observed):
    observed["construction"]["known"] = False


def _change_construction_queues_known_to_false(expected, observed):
    observed["construction"]["queues_known"] = False


def _change_construction_items_known_to_false(expected, observed):
    observed["construction"]["items_known"] = False


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_duplicate_queue_id, id="duplicate-queue-id"),
        pytest.param(_remove_queue_from_list, id="queue-list-mismatch"),
        pytest.param(_remove_queue_from_map, id="queue-map-mismatch"),
        pytest.param(_change_queue_count, id="queue-count-mismatch"),
        pytest.param(_change_queue_count_to_bool, id="queue-count-not-int"),
        pytest.param(_replace_queue_list_with_mapping, id="queue-list-not-list"),
        pytest.param(_replace_queue_map_with_list, id="queue-map-not-dict"),
        pytest.param(_change_construction_known_to_false, id="construction-known-not-true"),
        pytest.param(_change_construction_queues_known_to_false, id="construction-queues-known-not-true"),
        pytest.param(_change_construction_items_known_to_false, id="construction-items-known-not-true"),
    ],
)
def test_rejects_queue_list_map_and_count_inconsistency(mutate):
    expected, observed = _base_state(), _initialized_state()
    mutate(expected, observed)
    before_expected, before_observed = _snapshots(expected, observed)

    assert imported_queue_initialization(expected, observed) is None
    _assert_unchanged(expected, observed, before_expected, before_observed)


def _save_text(version):
    return (
        f'version="{version}" date="2200.01.01" galaxy={{name="import-init"}} '
        "ironman=no multiplayer=no player={country=0}\n"
        'species_db={1={portrait="human"}}\n'
        'country={0={name="Earth" founder_species_ref=1 flag={icon={file="x.dds"}} '
        "resources={minerals=1} visited_objects={1} surveyed_deposit_holders={}}}\n"
        'galactic_object={1={name="Sol" planet=10 planet=11 planet=12}}\n'
        'planet={10={name="Earth" coordinate={origin=1} owner=0 controller=0 colony=20 '
        "build_queue=5 shipclass_orbital_station=70} "
        '11={name="Mars" coordinate={origin=1} controller=0 shipclass_orbital_station=71} '
        '12={name="Station" coordinate={origin=1} controller=0 shipclass_orbital_station=77}}\n'
        "construction={queue_mgr={queues={5={owner=0 type=planet location={type=2 id=10} "
        "simultaneous=1}}} item_mgr={items={}}}\n"
    )


def _observed_text(token, version):
    text = _save_text(version).replace(
        '0={name="Earth"', "0={flags={qs_direct_session_" + token + '=1} name="Earth"'
    )
    text = text.replace(
        '11={name="Mars" coordinate={origin=1} controller=0 shipclass_orbital_station=71}',
        '11={name="Mars" coordinate={origin=1} controller=0 shipclass_orbital_station=71 build_queue=6}',
    )
    text = text.replace(
        '12={name="Station" coordinate={origin=1} controller=0 shipclass_orbital_station=77}',
        '12={name="Station" coordinate={origin=1} controller=0 shipclass_orbital_station=77 build_queue=7}',
    )
    text = text.replace(
        "queues={5={owner=0 type=planet location={type=2 id=10} simultaneous=1}}",
        "queues={5={owner=0 type=planet location={type=2 id=10} simultaneous=1} "
        "6={owner=0 type=planet location={type=2 id=11} simultaneous=1} "
        "7={owner=0 type=planet location={type=2 id=12} simultaneous=1}}",
    )
    return text


def _observation(tmp_path, token, version="Cygnus v4.5.2"):
    time.sleep(0.02)
    path = tmp_path / "observed.sav"
    path.write_text(_observed_text(token, version), encoding="utf-8")
    return DirectObserver(path, "0").read()


def _imported_session(tmp_path, version="Cygnus v4.5.2"):
    source = tmp_path / "source.sav"
    source.write_text(_save_text(version), encoding="utf-8")
    store = CampaignStore(tmp_path / "campaign")
    with store.locked():
        store.initialize(source, country_id="0", single_player_confirmed=True, non_ironman_confirmed=True)
        first = store.begin_session()
        store.update_session(first["token"], status="connected")
        store.update_session(first["token"], status="interrupted")
        second = store.begin_session(recover_last_checkpoint=True)
        store.update_session(second["token"], status="connected")
    return store, second


def test_verify_reload_accepts_imported_first_load_queue_initialization(tmp_path):
    store, session = _imported_session(tmp_path)
    observation = _observation(tmp_path, session["token"])

    with store.locked():
        checkpoint = store.verify_reload(store.load()["latest_checkpoint"], observation)

    assert checkpoint["kind"] == "imported_save"
    assert checkpoint["reload_verified"] is True
    assert checkpoint["reload_initialization"] == {
        "uncolonized_body_queues": [
            {"planet_id": "11", "queue_id": "6"},
            {"planet_id": "12", "queue_id": "7"},
        ],
        "selected_empty_queue_ids": ["6", "7"],
    }
    assert len(checkpoint["reload_gameplay_sha256"]) == 64
    assert checkpoint["reload_session_token"] == session["token"]
    with store.locked():
        persisted = store.load()["checkpoints"][-1]
    assert persisted["reload_initialization"] == checkpoint["reload_initialization"]


def test_verify_reload_rejects_packaged_dump_queue_initialization(tmp_path):
    source = tmp_path / "source.sav"
    source.write_text(_save_text("Cygnus v4.5.2"), encoding="utf-8")
    store = CampaignStore(tmp_path / "campaign")
    with store.locked():
        store.initialize(source, country_id="0", single_player_confirmed=True, non_ironman_confirmed=True)
        first = store.begin_session()
        store.update_session(first["token"], status="connected")
        source_observation = DirectObserver(source, "0").read()
        packaged = store.checkpoint_dump(
            source,
            session_token=first["token"],
            expected_digest=source_observation.digest,
            expected_date=source_observation.state["date"],
        )
        store.finish_session(first["token"])
        second = store.begin_session()
        store.update_session(second["token"], status="connected")

    observation = _observation(tmp_path, second["token"])
    with store.locked(), pytest.raises(CampaignError, match="differs from the current checkpoint"):
        store.verify_reload(packaged["id"], observation)

    with store.locked():
        assert store.load()["checkpoints"][-1]["reload_verified"] is False


def test_verify_reload_rejects_non_target_game_version_initialization(tmp_path):
    store, session = _imported_session(tmp_path, "Cygnus v4.5.3")
    observation = _observation(tmp_path, session["token"], "Cygnus v4.5.3")

    with store.locked(), pytest.raises(CampaignError, match="differs from the current checkpoint"):
        store.verify_reload(store.load()["latest_checkpoint"], observation)

    with store.locked():
        assert store.load()["checkpoints"][-1]["reload_verified"] is False
