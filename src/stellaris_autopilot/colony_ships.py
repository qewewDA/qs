"""Paid colony-ship construction with an explicit observed colonization species.

The ordinary ship buildable has no colonization data. This adapter requires the
specialized engine buildable and proves its species payload in the fresh queue.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .ship_commands import _id, paid_job, resolve_ship, ship_request


@dataclass(frozen=True)
class ColonyShipRequest:
    starbase_id: str
    design_id: str
    species_id: str

    @property
    def kind(self) -> str:
        return "queue_colony_ship"

    def to_dict(self) -> dict:
        return {"starbase_id": self.starbase_id, "design_id": self.design_id, "species_id": self.species_id}


@dataclass(frozen=True)
class ColonyShipConstruction(ColonyShipRequest):
    station_id: str
    queue_id: str

    def to_dict(self) -> dict:
        return {**super().to_dict(), "station_id": self.station_id, "queue_id": self.queue_id}


def colony_ship_request(starbase_id, design_id, species_id) -> ColonyShipRequest:
    return ColonyShipRequest(_id(starbase_id), _id(design_id), _id(species_id))


def resolve_colony_ship(state: dict, request: ColonyShipRequest) -> ColonyShipConstruction:
    if not isinstance(state, dict) or type(request) not in (ColonyShipRequest, ColonyShipConstruction):
        raise ValueError("A normalized state and typed colony ship request are required")
    request = colony_ship_request(request.starbase_id, request.design_id, request.species_id)
    # The shared resolver binds current country design, physical station,
    # starbase, owned construction queue and complete queue membership.
    ship = resolve_ship(state, ship_request(request.starbase_id, request.design_id), allowed_sizes={"colonizer"})
    status = state.get("shipbuilding")
    species = status.get("species_by_id") if isinstance(status, dict) else None
    species = species.get(request.species_id) if isinstance(species, dict) else None
    if (status.get("species_known") is not True or not isinstance(species, dict)
            or species.get("id") != request.species_id):
        raise ValueError("Select a species from the complete observed country-owned species references")
    if species.get("colonization_allowed_known") is not True or species.get("colonization_allowed") is not True:
        raise ValueError("The selected species must have explicitly observable permission to colonize")
    return ColonyShipConstruction(request.starbase_id, request.design_id, request.species_id,
                                  ship.station_id, ship.queue_id)


def colony_ship_payload(command: ColonyShipConstruction, token: str) -> dict:
    if (type(command) is not ColonyShipConstruction or not isinstance(token, str)
            or re.fullmatch(r"[0-9a-f]{32}", token, re.ASCII) is None):
        raise ValueError("A resolved colony ship command and canonical operation token are required")
    colony_ship_request(command.starbase_id, command.design_id, command.species_id)
    _id(command.station_id)
    _id(command.queue_id)
    return {"kind": command.kind, "country_id": "0", "token": token, **command.to_dict()}


def _exact_colony_buildable(value, command: ColonyShipConstruction) -> bool:
    if not isinstance(value, dict) or set(value) != {"ship_design_implementation", "orbitable", "colonization_data"}:
        return False
    expected = {
        "ship_design_implementation": {"design": int(command.design_id), "upgrade": 4294967295, "growth_stage": 0},
        "orbitable": {"starbase": int(command.starbase_id)},
        "colonization_data": {"species": int(command.species_id)},
    }
    for field, entries in expected.items():
        observed = value[field]
        if (not isinstance(observed, dict) or set(observed) != set(entries)
                or any(type(observed[key]) is not int or observed[key] != number for key, number in entries.items())):
            return False
    return True


def verify_colony_ship(before: dict, after: dict, command: ColonyShipConstruction) -> bool:
    """Require the new normal paid subtype job, exact species, and stable identities."""
    try:
        if (type(command) is not ColonyShipConstruction or resolve_colony_ship(before, command) != command
                or resolve_colony_ship(after, command) != command):
            return False
        if before["shipbuilding"] != after["shipbuilding"] or before["fleets_by_id"] != after["fleets_by_id"]:
            return False
        item = paid_job(before, after, command.queue_id)
        return (item is not None and item.get("buildable_type") == "buildable_colony_ship"
                and _exact_colony_buildable(item.get("buildable"), command))
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return False
