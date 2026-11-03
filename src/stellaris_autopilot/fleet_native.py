"""Typed normal survey, station and colony orders for one observed fleet/body.

Native factory submission is not success. Verification requires a new exact
serialized order, and station orders additionally require their actual payment.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any


FLEET_ACTIONS = frozenset({"survey_planet", "build_station", "colonize_planet"})
STATION_TYPES = frozenset({"mining", "research", "outpost"})
_ID = re.compile(r"(?:0|[1-9][0-9]{0,9})", re.ASCII)
_CLASS = {"survey_planet": "shipclass_science_ship", "build_station": "shipclass_constructor",
          "colonize_planet": "shipclass_colonizer"}
_ORDER = {"survey_planet": "survey_planet_order", "build_station": "build_orbital_station_order",
          "colonize_planet": "colonize_planet_order"}
_STATION_CLASS = {"mining": "shipclass_mining_station", "research": "shipclass_research_station",
                  "outpost": "shipclass_starbase"}


def _id(value: Any, *, observed: bool = False) -> str:
    if observed and type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not _ID.fullmatch(value) or int(value) >= 4294967295:
        raise ValueError("Object ID must be a canonical ASCII handle")
    return value


@dataclass(frozen=True)
class FleetRequest:
    kind: str
    fleet_id: str
    planet_id: str
    station_type: str | None = None

    def to_dict(self) -> dict:
        return {"fleet_id": self.fleet_id, "planet_id": self.planet_id,
                **({"station_type": self.station_type} if self.station_type is not None else {})}


@dataclass(frozen=True)
class FleetOperation(FleetRequest):
    system_id: str = ""

    def to_dict(self) -> dict:
        return {**super().to_dict(), "system_id": self.system_id}


FLEET_REQUEST_TYPES = (FleetRequest,)
FLEET_OPERATION_TYPES = (FleetOperation,)


def fleet_request(kind: str, fleet_id: str, planet_id: str, station_type: str | None = None) -> FleetRequest:
    if not isinstance(kind, str) or kind not in FLEET_ACTIONS:
        raise ValueError("Unknown fixed fleet action")
    if (kind == "build_station" and (not isinstance(station_type, str) or station_type not in STATION_TYPES)
            or kind != "build_station" and station_type is not None):
        raise ValueError("Station type is required only for a station order and must be mining/research/outpost")
    return FleetRequest(kind, _id(fleet_id), _id(planet_id), station_type)


def _fleet(state: dict, request: FleetRequest) -> dict:
    if state.get("country_id") != "0":
        raise ValueError("The pinned native fleet adapter currently requires selected country 0")
    missing = state.get("metadata", {}).get("missing_fields")
    fleet = state.get("fleets_by_id", {}).get(request.fleet_id)
    if (not isinstance(missing, list) or "fleets" in missing or not isinstance(fleet, dict)
            or fleet.get("id") != request.fleet_id or fleet.get("mobile") is not True
            or fleet.get("ship_class") != _CLASS[request.kind] or fleet.get("orders_known") is not True
            or not isinstance(fleet.get("combat"), dict) or fleet["combat"].get("start_date") != "0.01.01"):
        raise ValueError("An identified owned mobile non-combat fleet of the exact required class is necessary")
    for key in ("owner", "controller"):
        if key in fleet and _id(fleet[key], observed=True) != "0":
            raise ValueError("Fleet ownership or control is contradictory")
    # Scripted fleet actions are a separate queue that can replace a normal
    # order on a subsequent tick. Do not claim precise control over both.
    if fleet.get("actions") not in (None, {}, []):
        raise ValueError("Clear the fleet's scripted action queue before submitting a normal native order")
    return fleet


def resolve_fleet(state: dict, request: FleetRequest) -> FleetOperation:
    if not isinstance(state, dict) or not isinstance(request, FleetRequest):
        raise ValueError("A normalized state and typed fleet request are necessary")
    canonical = fleet_request(request.kind, request.fleet_id, request.planet_id, request.station_type)
    _fleet(state, canonical)
    space = state.get("space")
    planets = space.get("planets_by_id") if isinstance(space, dict) else None
    planet = planets.get(canonical.planet_id) if isinstance(planets, dict) else None
    if not isinstance(planet, dict) or planet.get("id") != canonical.planet_id:
        raise ValueError("The exact physical target planet is absent from the visited-system observations")
    system_id = _id(planet.get("system_id"))
    systems = space.get("systems_by_id")
    system = systems.get(system_id) if isinstance(systems, dict) else None
    if (not isinstance(system, dict) or system.get("id") != system_id or system.get("visited") is not True
            or system.get("planets_known") is not True or canonical.planet_id not in system.get("planet_ids", [])):
        raise ValueError("The target's exact physical-planet/system membership is unobservable")
    if planet.get("surveyed_known") is not True or type(planet.get("surveyed")) is not bool:
        raise ValueError("The target's survey status is unknown")
    if canonical.kind == "survey_planet":
        if planet["surveyed"]:
            raise ValueError("The requested physical planet is already surveyed")
    elif not planet["surveyed"]:
        raise ValueError("Survey the exact physical target before station construction or colonization")
    return FleetOperation(canonical.kind, canonical.fleet_id, canonical.planet_id, canonical.station_type, system_id)


def _current_order(state: dict, request: FleetRequest) -> dict | None:
    fleet = _fleet(state, request)
    current = fleet.get("current_order")
    key = _ORDER[request.kind]
    if not isinstance(current, dict) or set(current) != {key} or not isinstance(current[key], dict):
        return None
    return current[key]


def _number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _same_month(before: dict, after: dict) -> bool:
    dates = [state.get("date") for state in (before, after)]
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]{1,2}\.[0-9]{1,2}", value) for value in dates):
        return False
    parts = [[int(part) for part in value.split(".")] for value in dates]
    return (all(1 <= month <= 12 and 1 <= day <= 31 for _, month, day in parts)
            and parts[0][:2] == parts[1][:2] and parts[0][2] <= parts[1][2])


def _station_payment(before: dict, after: dict, order: dict) -> bool:
    costs = order.get("resources")
    if not isinstance(costs, dict) or not costs or not _same_month(before, after):
        return False
    if not isinstance(before.get("resources"), dict) or not isinstance(after.get("resources"), dict):
        return False
    paid = False
    for resource, raw in costs.items():
        cost = _number(raw)
        old, new = _number(before["resources"].get(resource)), _number(after["resources"].get(resource))
        if cost is None or cost < 0 or old is None or new is None:
            return False
        if abs(old - new - cost) > 0.001:
            return False
        paid = paid or cost > 0
    return paid


def verify_fleet(before: dict, after: dict, request: FleetOperation) -> bool:
    """Prove a new exact queued native order, never mere dispatch or completion."""
    try:
        if not isinstance(request, FleetOperation) or resolve_fleet(before, request) != request:
            return False
        if resolve_fleet(after, request) != request:
            return False
        new = _current_order(after, request)
        if not isinstance(new, dict) or new.get("can_reach") != "yes":
            return False
        order_id = new.get("order_id")
        if type(order_id) is not int or order_id < 0:
            return False
        old = _current_order(before, request)
        if isinstance(old, dict) and old.get("order_id") == order_id:
            return False
        progress = _number(new.get("progress"))
        if progress is None or progress < 0:
            return False
        if request.kind == "colonize_planet":
            return _id(new.get("planet"), observed=True) == request.planet_id
        target = new.get("deposit_holder")
        if (not isinstance(target, dict) or set(target) != {"type", "id"} or type(target["type"]) is not int
                or target["type"] != 0 or _id(target["id"], observed=True) != request.planet_id):
            return False
        if request.kind == "build_station":
            return new.get("class") == _STATION_CLASS[request.station_type] and _station_payment(before, after, new)
        return True
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
