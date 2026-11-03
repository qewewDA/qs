"""Normal paid ship construction from an observed design at an owned shipyard."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re

from .pdx import PDXNode


ORDINARY_SHIP_SIZES = frozenset({"science", "constructor", "corvette", "frigate", "destroyer", "cruiser",
                                "battleship", "titan", "juggernaut", "colossus"})


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,9}", value, re.ASCII) or int(value) >= 4294967295:
        raise ValueError("Expected a canonical observed object handle")
    return value


def _one(node, key):
    values = node.get_all(key) if isinstance(node, PDXNode) else []
    return values[0] if len(values) == 1 else None


def _handle(value):
    try:
        return _id(str(value) if type(value) is int else value)
    except ValueError:
        return None


def observe_shipyards(root: PDXNode, country_id: str, fleets: list[dict]) -> dict:
    """Join current country designs and station→fleet→shipyard queue identities."""
    from .saves import _structure
    result = {"designs_known": False, "designs_by_id": {}, "shipyards_known": False, "shipyards_by_id": {},
              "species_known": False, "species_by_id": {}}
    country = _one(_one(root, "country"), country_id)
    owned_species = _one(country, "owned_species_refs")
    species_db = _one(root, "species_db")
    if isinstance(owned_species, PDXNode) and isinstance(species_db, PDXNode):
        identifiers = [_handle(value) for value in owned_species.values()]
        known_species = (all(key is None for key, _ in owned_species.entries)
                         and None not in identifiers and len(identifiers) == len(set(identifiers)))
        for species_id in identifiers:
            definition = _one(species_db, species_id)
            if not isinstance(definition, PDXNode):
                known_species = False
                continue
            rights = _one(_one(country, "modules"), "standard_species_rights_module")
            primary = _one(rights, "primary")
            control = _one(primary, "colonization_control")
            rights_known = (_one(rights, "enabled") == "yes"
                            and _handle(_one(primary, "species_index")) == species_id
                            and isinstance(control, str)
                            and control in {"colonization_control_no", "colonization_control_yes"})
            result["species_by_id"][species_id] = {
                "id": species_id, "definition": _structure(definition),
                "colonization_allowed_known": rights_known,
                "colonization_allowed": control == "colonization_control_no" if rights_known else None,
                "colonization_rights_source": "primary" if rights_known else None,
            }
        result["species_known"] = known_species
    collection = _one(country, "ship_design_collection")
    designs = _one(root, "ship_design")
    if isinstance(collection, PDXNode) and isinstance(designs, PDXNode):
        references = _one(collection, "ship_design")
        ids = [_handle(value) for value in references.values()] if isinstance(references, PDXNode) else []
        known = (isinstance(references, PDXNode) and all(key is None for key, _ in references.entries)
                 and None not in ids and len(ids) == len(set(ids)))
        for design_id in ids:
            definition = _one(designs, design_id)
            if not isinstance(definition, PDXNode):
                known = False
                continue
            stages = _one(definition, "growth_stages")
            stages = list(stages.values()) if isinstance(stages, PDXNode) else []
            if not stages or not isinstance(stages[0], PDXNode) or not isinstance(_one(stages[0], "ship_size"), str):
                known = False
                continue
            result["designs_by_id"][design_id] = {"id": design_id, "ship_size": _one(stages[0], "ship_size"),
                                                    "definition": _structure(definition)}
        result["designs_known"] = known
    stations = _one(_one(root, "starbase_mgr"), "starbases")
    ships = _one(root, "ships")
    own_fleets = {fleet["id"] for fleet in fleets}
    construction = _one(root, "construction")
    queues = _one(_one(construction, "queue_mgr"), "queues")
    if not all(isinstance(value, PDXNode) for value in (stations, ships, queues)):
        return result
    known = True
    for starbase_id, starbase in stations.entries:
        if not isinstance(starbase, PDXNode):
            continue
        station_id = _handle(_one(starbase, "station"))
        station = _one(ships, station_id)
        if not isinstance(station, PDXNode):
            known = False
            continue
        fleet_id = _handle(_one(station, "fleet"))
        if fleet_id not in own_fleets:
            continue
        queue_id = _handle(_one(starbase, "shipyard_build_queue"))
        if queue_id is None:  # The engine's unset sentinel means no shipyard.
            if _one(starbase, "shipyard_build_queue") != 4294967295:
                known = False
            continue
        queue = _one(queues, queue_id)
        location = _one(queue, "location")
        if (_handle(starbase_id) is None or len(stations.get_all(starbase_id)) != 1
                or _handle(_one(queue, "owner")) != country_id
                or _one(queue, "type") not in {"ships", "combined"}
                or _one(location, "type") != 0 or _handle(_one(location, "id")) != station_id):
            known = False
            continue
        result["shipyards_by_id"][starbase_id] = {
            "id": starbase_id, "station_id": station_id, "fleet_id": fleet_id,
            "queue_id": queue_id, "owner": country_id, "level": _one(starbase, "level"),
        }
    result["shipyards_known"] = known
    return result


@dataclass(frozen=True)
class ShipRequest:
    starbase_id: str
    design_id: str

    @property
    def kind(self):
        return "queue_ship"

    def to_dict(self):
        return {"starbase_id": self.starbase_id, "design_id": self.design_id}


@dataclass(frozen=True)
class ShipConstruction(ShipRequest):
    station_id: str
    queue_id: str

    def to_dict(self):
        return {**super().to_dict(), "station_id": self.station_id, "queue_id": self.queue_id}


def ship_request(starbase_id, design_id):
    return ShipRequest(_id(starbase_id), _id(design_id))


def resolve_ship(state, request, *, allowed_sizes=ORDINARY_SHIP_SIZES):
    request = ship_request(request.starbase_id, request.design_id)
    status = state.get("shipbuilding", {})
    if state.get("country_id") != "0" or status.get("shipyards_known") is not True or status.get("designs_known") is not True:
        raise ValueError("Complete owned shipyard and current design observations are required")
    yard = status.get("shipyards_by_id", {}).get(request.starbase_id)
    design = status.get("designs_by_id", {}).get(request.design_id)
    if (not isinstance(yard, dict) or yard.get("owner") != "0" or yard.get("id") != request.starbase_id
            or not isinstance(design, dict) or design.get("id") != request.design_id):
        raise ValueError("Select an observed owned shipyard and current country ship design")
    # Colony ships, defense platforms and biological designs have distinct
    # native buildable payloads. Never silently reuse the ordinary factory.
    if design.get("ship_size") not in allowed_sizes:
        raise ValueError("This design requires a specialized construction adapter")
    construction = state.get("construction", {})
    queue = construction.get("queues_by_id", {}).get(yard["queue_id"])
    if (construction.get("known") is not True or not isinstance(queue, dict)
            or queue.get("id") != yard["queue_id"] or queue.get("known") is not True or queue.get("items_known") is not True
            or queue.get("owner") != "0" or queue.get("type") not in {"ships", "combined"}
            or queue.get("location") != {"type": 0, "id": yard["station_id"]}):
        raise ValueError("The precise owned shipyard queue is unobservable")
    listed = queue.get("items")
    global_items = construction.get("items_by_id")
    if not isinstance(listed, list) or not isinstance(global_items, dict):
        raise ValueError("Shipyard queue item membership is unobservable")
    ids = [_id(item.get("id")) for item in listed if isinstance(item, dict)]
    expected = {key: value for key, value in global_items.items() if value.get("queue_id") == yard["queue_id"]}
    if (len(ids) != len(listed) or len(ids) != len(set(ids)) or type(queue.get("item_count")) is not int
            or queue["item_count"] != len(ids) or {item["id"]: item for item in listed} != expected):
        raise ValueError("Shipyard queue item membership conflicts with global items")
    return ShipConstruction(request.starbase_id, request.design_id, _id(yard["station_id"]), _id(yard["queue_id"]))


def paid_job(before, after, queue_id):
    """Return the unique new unfinished job only after exact same-month payment."""
    dates = [state.get("date") for state in (before, after)]
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}", value) for value in dates):
        return None
    old_date, new_date = (tuple(map(int, value.split("."))) for value in dates)
    if (old_date[:2] != new_date[:2] or old_date > new_date
            or not all(1 <= month <= 12 and 1 <= day <= 30 for _, month, day in (old_date, new_date))
            or before.get("country_id") != after.get("country_id")
            or not isinstance(before.get("metadata", {}).get("campaign_id"), str)
            or not before["metadata"]["campaign_id"]
            or before["metadata"]["campaign_id"] != after.get("metadata", {}).get("campaign_id")
            or not isinstance(before.get("metadata", {}).get("game_version"), str)
            or not before["metadata"]["game_version"]
            or before["metadata"]["game_version"] != after.get("metadata", {}).get("game_version")):
        return None
    old = before["construction"]["items_by_id"]
    new = [value for key, value in after["construction"]["items_by_id"].items()
           if key not in old and value.get("queue_id") == queue_id]
    if len(new) != 1 or new[0].get("paying_country") != "0":
        return None
    all_after = after["construction"]["items_by_id"]
    if set(all_after) - set(old) != {new[0].get("id")} or any(all_after.get(key) != value for key, value in old.items()):
        return None
    item = new[0]
    numbers = (item.get("progress"), item.get("progress_needed"))
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in numbers) or not 0 <= numbers[0] < numbers[1]:
        return None
    costs = item.get("resources")
    if not isinstance(costs, dict) or not costs:
        return None
    positive = False
    for resource, cost in costs.items():
        if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
            return None
        if cost:
            positive = True
            old_amount, new_amount = before["resources"].get(resource), after["resources"].get(resource)
            if (any(type(x) not in (int, float) or not math.isfinite(x) for x in (old_amount, new_amount))
                    or not math.isclose(old_amount - new_amount, cost, abs_tol=0.0001, rel_tol=1e-8)):
                return None
    return item if positive else None


def verify_ship(before, after, request):
    try:
        if resolve_ship(before, request) != request or resolve_ship(after, request) != request:
            return False
        if before["shipbuilding"] != after["shipbuilding"] or before["fleets_by_id"] != after["fleets_by_id"]:
            return False
        item = paid_job(before, after, request.queue_id)
        return (item is not None and item.get("buildable_type") == "buildable_ship"
                and item.get("buildable") == {"ship_design_implementation": {
                    "design": int(request.design_id), "upgrade": 4294967295, "growth_stage": 0},
                    "orbitable": {"starbase": int(request.starbase_id)}})
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return False
