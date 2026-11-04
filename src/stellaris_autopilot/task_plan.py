"""Bounded precise plans: normalize intent before acquiring any live connection."""
from __future__ import annotations

from typing import Annotated, Literal
import hashlib
import json
import re

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, TypeAdapter

from .direct_orders import parse_order
from .native import building_request, district_request, pause_request, research_selection
from .ship_commands import ship_request
from .colony_ships import colony_ship_request
from .planet_commands import planet_request
from .fleet_native import fleet_request


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ResearchStep(_Step):
    kind: Literal["select_research"]
    area: Literal["physics", "society", "engineering"]
    technology_id: StrictStr


class DistrictStep(_Step):
    kind: Literal["queue_district"]
    planet_id: StrictStr
    district_id: StrictStr


class BuildingStep(_Step):
    kind: Literal["queue_building"]
    planet_id: StrictStr
    zone_id: StrictStr
    building_id: StrictStr


class ShipStep(_Step):
    kind: Literal["queue_ship"]
    starbase_id: StrictStr
    design_id: StrictStr


class ColonyShipStep(_Step):
    kind: Literal["queue_colony_ship"]
    starbase_id: StrictStr
    design_id: StrictStr
    species_id: StrictStr


class UpgradeStep(_Step):
    kind: Literal["upgrade_building"]
    planet_id: StrictStr
    zone_id: StrictStr
    building_id: StrictStr
    target_building_id: StrictStr


class DemolishStep(_Step):
    kind: Literal["demolish_building"]
    planet_id: StrictStr
    zone_id: StrictStr
    building_id: StrictStr


class ConvertStep(_Step):
    kind: Literal["convert_zone"]
    planet_id: StrictStr
    zone_id: StrictStr
    target_zone_id: StrictStr


class CancelConstructionStep(_Step):
    kind: Literal["cancel_construction"]
    planet_id: StrictStr
    item_id: StrictStr


class FleetTargetStep(_Step):
    kind: Literal["survey_planet", "colonize_planet"]
    fleet_id: StrictStr
    planet_id: StrictStr


class StationStep(_Step):
    kind: Literal["build_station"]
    fleet_id: StrictStr
    planet_id: StrictStr
    station_type: Literal["mining", "research", "outpost"]


class FleetMoveStep(_Step):
    kind: Literal["fleet_move", "fleet_orbit"]
    fleet_id: StrictStr
    planet_id: StrictStr


class FleetWaitStep(_Step):
    kind: Literal["fleet_wait"]
    fleet_id: StrictStr
    days: StrictInt


class FleetStopStep(_Step):
    kind: Literal["fleet_stop"]
    fleet_id: StrictStr


class PauseStep(_Step):
    kind: Literal["set_paused"]
    paused: StrictBool


PlanStep = Annotated[
    ResearchStep | DistrictStep | BuildingStep | ShipStep | ColonyShipStep | UpgradeStep
    | DemolishStep | ConvertStep | CancelConstructionStep | FleetTargetStep | StationStep
    | FleetMoveStep | FleetWaitStep | FleetStopStep | PauseStep,
    Field(discriminator="kind"),
]
PlanSteps = Annotated[list[PlanStep], Field(min_length=1, max_length=20)]
_PLAN = TypeAdapter(PlanSteps)
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z", re.ASCII)


def request_identifier(value: str) -> str:
    if not isinstance(value, str) or not _REQUEST_ID.fullmatch(value):
        raise ValueError("request_id must be 1..96 ASCII letters, digits, dot, underscore, colon or hyphen")
    return value


def dispatch_step(connection, step: dict):
    """Call the existing typed API; also usable with a validation-only target."""
    kind = step["kind"]
    args = {key: value for key, value in step.items() if key != "kind"}
    if kind.startswith("fleet_"):
        return connection.issue_order(step)
    if kind in {"survey_planet", "colonize_planet", "build_station"}:
        return connection.fleet_command(kind, **args)
    if kind in {"upgrade_building", "demolish_building", "convert_zone", "cancel_construction"}:
        if "target_building_id" in args:
            args["target_building"] = args.pop("target_building_id")
        if "target_zone_id" in args:
            args["target_zone"] = args.pop("target_zone_id")
        return connection.planet_command(kind, **args)
    if kind == "select_research":
        return connection.select_research(args["area"], args["technology_id"])
    if kind == "queue_district":
        return connection.queue_district(args["planet_id"], args["district_id"])
    if kind == "queue_building":
        return connection.queue_building(args["planet_id"], args["zone_id"], args["building_id"])
    if kind == "set_paused":
        return connection.set_paused(args["paused"])
    return getattr(connection, kind)(**args)


class _Validate:
    issue_order = staticmethod(parse_order)
    fleet_command = staticmethod(fleet_request)
    planet_command = staticmethod(planet_request)
    select_research = staticmethod(research_selection)
    queue_district = staticmethod(district_request)
    queue_building = staticmethod(building_request)
    queue_ship = staticmethod(ship_request)
    queue_colony_ship = staticmethod(colony_ship_request)
    set_paused = staticmethod(pause_request)


def normalize_plan(steps, expected_snapshot: str | None = None) -> tuple[list[dict], str]:
    if (expected_snapshot is not None and (not isinstance(expected_snapshot, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", expected_snapshot, re.ASCII))):
        raise ValueError("expected_snapshot must be a bounded snapshot identifier")
    normalized = [step.model_dump() for step in _PLAN.validate_python(steps)]
    fleets = set()
    for index, step in enumerate(normalized):
        dispatch_step(_Validate(), step)
        if step["kind"] == "set_paused" and (index != 0 or step["paused"] is not True):
            raise ValueError("A plan may explicitly pause only as its first step; time advancement is separate")
        fleet = step.get("fleet_id")
        if fleet in fleets:
            raise ValueError("This fleet already has an unfinished plan order; use separate natural-completion tasks")
        if fleet is not None and step["kind"] != "fleet_stop":
            fleets.add(fleet)
    canonical = json.dumps({"steps": normalized, "expected_snapshot": expected_snapshot},
                           sort_keys=True, separators=(",", ":"), allow_nan=False)
    return normalized, hashlib.sha256(canonical.encode("utf-8")).hexdigest()
