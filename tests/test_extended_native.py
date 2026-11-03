"""Typed extension dispatch boundaries without attaching a native game process."""
from dataclasses import replace

import pytest

from stellaris_autopilot.extended_native import extended_payload, resolve_extended, verify_extended
from stellaris_autopilot.fleet_native import FleetOperation, fleet_request
from stellaris_autopilot.native import NativeResearchBridge
from stellaris_autopilot.planet_commands import ResolvedPlanetCommand, planet_request
from stellaris_autopilot.ship_commands import ShipConstruction, ship_request


TOKEN = "a" * 32


def commands():
    return [
        ShipConstruction("12", "7", "100", "8"),
        ResolvedPlanetCommand(planet_request("demolish_building", planet_id="3", zone_id="4", building_id="17"),
                              "0", "0", "10", 2, "zone_default", "building_research_lab_1"),
        FleetOperation("survey_planet", "1", "104", None, "247"),
        FleetOperation("build_station", "2", "104", "outpost", "247"),
        FleetOperation("colonize_planet", "3", "105", None, "247"),
    ]


@pytest.mark.parametrize("command", commands())
def test_native_submit_sends_the_full_resolved_typed_payload_bound_to_one_token(command):
    bridge = object.__new__(NativeResearchBridge)
    calls = []

    def call(method, payload):
        calls.append((method, payload))
        return {"status": "pending", "token": payload["token"]}

    bridge._call = call
    assert bridge.submit(command, TOKEN) == {"status": "pending", "token": TOKEN}
    assert calls == [("enqueue", {"kind": command.kind, **command.to_dict(), "country_id": "0", "token": TOKEN})]
    assert not ({"script", "path", "address", "coordinates", "process"} & calls[0][1].keys())


@pytest.mark.parametrize("token", [True, 1, "", "A" * 32, "a" * 31, "a" * 33, "a" * 32 + "\n"])
@pytest.mark.parametrize("command", commands())
def test_native_submit_rejects_noncanonical_token_before_rpc(command, token):
    bridge = object.__new__(NativeResearchBridge)
    bridge._call = lambda *_: pytest.fail("A malformed token reached the worker")
    with pytest.raises(ValueError):
        bridge.submit(command, token)


@pytest.mark.parametrize("command", [ship_request("12", "7"), fleet_request("survey_planet", "1", "104"),
                                     planet_request("cancel_construction", planet_id="3", item_id="31"),
                                     {"kind": "queue_ship", "starbase_id": "12", "design_id": "7"},
                                     "effect cash=1", None])
def test_unresolved_requests_or_raw_objects_never_reach_native_enqueue(command):
    bridge = object.__new__(NativeResearchBridge)
    bridge._call = lambda *_: pytest.fail("A request without observation-bound identities reached the worker")
    with pytest.raises(ValueError):
        bridge.submit(command, TOKEN)
    with pytest.raises(ValueError):
        extended_payload(command, TOKEN)
    assert verify_extended({}, {}, command) is False


@pytest.mark.parametrize("value", [None, {}, {"kind": "survey_planet", "fleet_id": "1", "planet_id": "104"},
                                  "effect cash=1", object()])
def test_extension_resolver_cannot_accept_arbitrary_payload_or_code(value):
    with pytest.raises(ValueError):
        resolve_extended({}, value)


@pytest.mark.parametrize("command", [
    replace(commands()[0], station_id="100\nscript"), replace(commands()[0], queue_id="4294967295"),
    replace(commands()[2], system_id="0247"), replace(commands()[2], station_type="outpost"),
    replace(commands()[3], station_type="observation"),
    replace(commands()[1], colony_id="3\neffect cash=1"), replace(commands()[1], slot_index=True),
])
def test_forged_resolved_id_fields_are_revalidated_before_native_rpc(command):
    bridge = object.__new__(NativeResearchBridge)
    bridge._call = lambda *_: pytest.fail("A forged resolved command reached the worker")
    with pytest.raises(ValueError):
        bridge.submit(command, TOKEN)
