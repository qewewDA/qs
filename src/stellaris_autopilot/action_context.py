"""Small action receipts built only from observations the executor already verified."""
from __future__ import annotations

import copy
import math

from .observe import Observation


_OBJECT_FIELDS = (
    "id", "name", "owner", "controller", "system_id", "ship_class", "mobile",
    "orders_known", "orders", "actions", "build_queue", "colony",
    "districts_known", "district_levels", "zones_known", "zones_by_id",
)
_IMMEDIATE = frozenset({
    "set_paused", "demolish_building", "cancel_construction", "fleet_stop", "probe_connection",
})


def current_plan_context(observation: Observation, steps: list[dict]) -> dict:
    """Return the already-read current targets after a stale guard; this is not a write receipt."""
    state = observation.state
    targets, queues = {}, set()
    shipbuilding = state.get("shipbuilding")
    selected_ships = {}
    if isinstance(shipbuilding, dict):
        selected_ships = {key: copy.deepcopy(shipbuilding[key]) for key in
                          ("known", "designs_known", "shipyards_known", "species_known") if key in shipbuilding}
    research = state.get("research_status")
    selected_research = {}
    for step in steps:
        for field, collection in (("planet_id", "planets_by_id"), ("fleet_id", "fleets_by_id")):
            identifier = step.get(field)
            index = state.get(collection)
            value = index.get(identifier) if isinstance(index, dict) and isinstance(identifier, str) else None
            if value is None and field == "planet_id":
                space = state.get("space")
                index = space.get(collection) if isinstance(space, dict) else None
                value = index.get(identifier) if isinstance(index, dict) else None
            if isinstance(identifier, str):
                targets[f"{field}:{identifier}"] = copy.deepcopy(value) if isinstance(value, dict) else None
            if isinstance(value, dict):
                if isinstance(value.get("build_queue"), str):
                    queues.add(value["build_queue"])
        if isinstance(shipbuilding, dict):
            for field, collection in (("starbase_id", "shipyards_by_id"), ("design_id", "designs_by_id"),
                                      ("species_id", "species_by_id")):
                identifier = step.get(field)
                index = shipbuilding.get(collection)
                value = index.get(identifier) if isinstance(index, dict) and isinstance(identifier, str) else None
                if isinstance(value, dict):
                    selected_ships.setdefault(collection, {})[identifier] = copy.deepcopy(value)
                    if field == "starbase_id" and isinstance(value.get("queue_id"), str):
                        queues.add(value["queue_id"])
        if step["kind"] == "select_research" and isinstance(research, dict):
            for key in ("known", "completed_known", "completed_technologies"):
                if key in research:
                    selected_research[key] = copy.deepcopy(research[key])
            areas = research.get("areas")
            if isinstance(areas, dict) and step["area"] in areas:
                selected_research.setdefault("areas", {})[step["area"]] = copy.deepcopy(areas[step["area"]])
    construction = state.get("construction")
    index = construction.get("queues_by_id") if isinstance(construction, dict) else None
    return {"snapshot": observation.digest, "date": state.get("date"),
            "observed_at": observation.modified_at, "country_id": state.get("country_id"),
            "resources": copy.deepcopy(state.get("resources")),
            "targets": targets, "shipbuilding": selected_ships or None,
            "research_status": selected_research or None,
            "construction_known": construction.get("known") if isinstance(construction, dict) else None,
            "queues_by_id": {key: copy.deepcopy(index[key]) for key in queues if key in index}
                if isinstance(index, dict) else None,
            "missing_fields": copy.deepcopy(state.get("metadata", {}).get("missing_fields")),
            "proof": "Fresh current observation only; this step was not dispatched. No balance change was inferred."}


def _number(value) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


def verified_context(before: Observation, after: Observation, kind: str,
                     payload: dict | None = None, runtime: dict | None = None) -> dict:
    """Format existing proof, without reading the game, copying all indexes, or guessing missing data."""
    payload = payload or {}
    state = after.state
    old_resources, resources = before.state.get("resources"), state.get("resources")
    delta = {}
    unknown_changes = []
    if isinstance(old_resources, dict) and isinstance(resources, dict):
        for name in resources.keys() | old_resources.keys():
            if name not in resources or name not in old_resources:
                unknown_changes.append(name)
                continue
            old, new = old_resources[name], resources[name]
            if not _number(old) or not _number(new):
                unknown_changes.append(name)
                continue
            try:
                change = new - old
            except OverflowError:
                unknown_changes.append(name)
                continue
            if not _number(change):
                unknown_changes.append(name)
            elif change:
                delta[name] = change
    objects = {}
    for field, collection in (("planet_id", "planets_by_id"), ("fleet_id", "fleets_by_id")):
        identifier = payload.get(field)
        index = state.get(collection)
        obj = index.get(identifier) if isinstance(index, dict) and isinstance(identifier, str) else None
        if obj is None and field == "planet_id":
            space = state.get("space")
            index = space.get("planets_by_id") if isinstance(space, dict) else None
            obj = index.get(identifier) if isinstance(index, dict) and isinstance(identifier, str) else None
        if isinstance(obj, dict):
            selected = {key: copy.deepcopy(obj[key]) for key in _OBJECT_FIELDS if key in obj}
            zone_id = payload.get("zone_id")
            zones = selected.get("zones_by_id")
            if isinstance(zone_id, str) and isinstance(zones, dict):
                selected["zones_by_id"] = {zone_id: zones[zone_id]} if zone_id in zones else {}
            objects[field] = selected
    queue_id = payload.get("queue_id")
    construction = state.get("construction")
    queues = construction.get("queues_by_id") if isinstance(construction, dict) else None
    queue = queues.get(queue_id) if isinstance(queues, dict) and isinstance(queue_id, str) else None
    pause = runtime.get("paused") if isinstance(runtime, dict) else None
    pause = pause if type(pause) is bool else None
    return {
        "dispatch_state": "verified",
        "goal_state": "completed" if kind in _IMMEDIATE else "pending",
        "context": {
            "snapshot": after.digest, "date": state.get("date"), "observed_at": after.modified_at,
            "country_id": state.get("country_id"),
            "resources": copy.deepcopy(resources) if isinstance(resources, dict) else None,
            "resource_change": delta,
            "resource_change_known": (isinstance(old_resources, dict) and isinstance(resources, dict)
                                      and not unknown_changes),
            "resource_change_unknown": unknown_changes,
            "resource_change_note": "Observed balance difference; not an effective-price quote.",
            "paused": pause, "pause_known": pause is not None,
            "target_refs": copy.deepcopy(payload), "objects": objects,
            "queue": copy.deepcopy(queue) if isinstance(queue, dict) else None,
            "proof": "Existing verified before/after; no additional observation was requested.",
        },
    }
