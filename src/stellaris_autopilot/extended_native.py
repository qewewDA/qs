"""Typed extension registry for normal planetary, fleet and shipyard commands."""
from __future__ import annotations

import re

from .ship_commands import ShipRequest, ShipConstruction, ship_request, resolve_ship, verify_ship, _id
from .planet_commands import (PlanetRequest, ResolvedPlanetCommand, resolve_planet_command,
                              verify_planet_command, native_planet_payload)
from .fleet_native import (FleetRequest, FleetOperation, fleet_request, resolve_fleet, verify_fleet)
from .colony_ships import (ColonyShipRequest, ColonyShipConstruction, colony_ship_request,
                           resolve_colony_ship, verify_colony_ship, colony_ship_payload)


REQUEST_TYPES = (ShipRequest, PlanetRequest, FleetRequest, ColonyShipRequest)
RESOLVED_TYPES = (ShipConstruction, ResolvedPlanetCommand, FleetOperation, ColonyShipConstruction)


def resolve_extended(state, request):
    if isinstance(request, ColonyShipRequest):
        return resolve_colony_ship(state, request)
    if isinstance(request, ShipRequest):
        return resolve_ship(state, request)
    if isinstance(request, PlanetRequest):
        return resolve_planet_command(state, request)
    if isinstance(request, FleetRequest):
        return resolve_fleet(state, request)
    raise ValueError("Unsupported extended native request")


def verify_extended(before, after, command):
    if isinstance(command, ColonyShipConstruction):
        return verify_colony_ship(before, after, command)
    if isinstance(command, ShipConstruction):
        return verify_ship(before, after, command)
    if isinstance(command, ResolvedPlanetCommand):
        return verify_planet_command(before, after, command)
    if isinstance(command, FleetOperation):
        return verify_fleet(before, after, command)
    return False


def extended_payload(command, token):
    if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{32}", token, re.ASCII) is None:
        raise ValueError("Invalid native operation token")
    if isinstance(command, ColonyShipConstruction):
        colony_ship_request(command.starbase_id, command.design_id, command.species_id)
        return colony_ship_payload(command, token)
    if isinstance(command, ShipConstruction):
        ship_request(command.starbase_id, command.design_id)
        _id(command.station_id)
        _id(command.queue_id)
        return {"kind": command.kind, "country_id": "0", "token": token, **command.to_dict()}
    if isinstance(command, ResolvedPlanetCommand):
        return native_planet_payload(command, token)
    if isinstance(command, FleetOperation):
        fleet_request(command.kind, command.fleet_id, command.planet_id, command.station_type)
        _id(command.system_id)
        return {"kind": command.kind, "country_id": "0", "token": token, **command.to_dict()}
    raise ValueError("Unsupported resolved native command")
