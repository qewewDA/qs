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
