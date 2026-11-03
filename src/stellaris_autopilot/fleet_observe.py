"""Observe bounded navigation targets from the player's visited-system ledger.

Physical planet IDs, colony handles and system IDs stay in separate namespaces.
Unvisited galaxy bodies are deliberately excluded from the normalized targets.
"""

from __future__ import annotations

import re
from typing import Any

from .pdx import PDXNode


_ID = re.compile(r"(?:0|[1-9][0-9]{0,9})", re.ASCII)


def _one(node: PDXNode | None, key: str) -> Any:
    values = node.get_all(key) if isinstance(node, PDXNode) else []
    return values[0] if len(values) == 1 else None


def _node(node: PDXNode | None, key: str) -> PDXNode | None:
    value = _one(node, key)
    return value if isinstance(value, PDXNode) else None


def _id(value: Any) -> str | None:
    if type(value) is int:
        value = str(value)
    return (value if isinstance(value, str) and _ID.fullmatch(value)
            and int(value) < 4294967295 else None)


def _ids(node: Any) -> list[str] | None:
    if not isinstance(node, PDXNode):
        return None
    result: list[str] = []
    for key, value in node.entries:
        identifier = _id(value)
        if key is not None or identifier is None or identifier in result:
            return None
        result.append(identifier)
    return result


def _name(node: PDXNode) -> str | None:
    value = _one(node, "name")
    if isinstance(value, PDXNode):
        value = _one(value, "key")
    return value if isinstance(value, str) else None


def _reference_value(value: Any) -> tuple[bool, str | None]:
    """Keep an explicit unset handle distinct from a missing/malformed value."""
    if (identifier := _id(value)) is not None:
        return True, identifier
    if type(value) is int and value == 4294967295 or value == "4294967295":
        return True, None
    return False, None


def _system_reference(system: PDXNode, key: str) -> tuple[bool, str | None]:
    values = system.get_all(key)
    return _reference_value(values[0]) if len(values) == 1 else (False, None)


def _system_starbase(system: PDXNode) -> tuple[bool, str | None]:
    singular, plural = system.get_all("starbase"), system.get_all("starbases")
    if singular:
        return _system_reference(system, "starbase") if not plural else (False, None)
    if len(plural) != 1 or not isinstance(plural[0], PDXNode):
        return False, None
    references: list[str] = []
    for key, value in plural[0].entries:
        known, identifier = _reference_value(value)
        if key is not None or not known or identifier in references:
            return False, None
        if identifier is not None:
            references.append(identifier)
    # The observed current format has one slot. Multiple positive starbases
    # cannot be collapsed into one target; explicit unset slots are empty.
    return (True, references[0] if references else None) if len(references) <= 1 else (False, None)


def _surveyed(country: PDXNode | None) -> set[str] | None:
    values = _node(country, "surveyed_deposit_holders")
    if values is None:
        return None
    result: set[str] = set()
    seen: set[tuple[int, str]] = set()
    for key, value in values.entries:
        kind = _one(value, "type") if isinstance(value, PDXNode) else None
        identifier = _id(_one(value, "id")) if isinstance(value, PDXNode) else None
        if key is not None or type(kind) is not int or kind not in (0, 1, 2) or identifier is None:
            return None
        if (kind, identifier) in seen:
            return None
        seen.add((kind, identifier))
        if kind == 0:
            result.add(identifier)
    return result


def observe_space(root: PDXNode, country_id: str) -> dict:
    """Return only structurally established visited systems and their bodies.

    ``surveyed`` is known only from a complete, well-shaped deposit-holder
    ledger. Missing ownership/station fields remain unknown. These observations
    do not replace native pathfinding, surveying or construction legality.
    """
    country = _node(_node(root, "country"), country_id)
    visited = _ids(_one(country, "visited_objects"))
    surveyed = _surveyed(country)
    systems = _node(root, "galactic_object")
    planets = _node(root, "planet")
    if not root.get_all("planet"):
        planets = _node(_node(root, "planets"), "planet")
    result: dict[str, Any] = {
        "visited_systems_known": visited is not None,
        "visited_system_ids": visited,
        "surveyed_planets_known": surveyed is not None,
        "surveyed_planet_ids": sorted(surveyed, key=int) if surveyed is not None else None,
        "systems_by_id": {}, "planets_by_id": {}, "unknown_fields": [],
    }
    if visited is None:
        result["unknown_fields"].append("visited_system_ids")
        return result
    if surveyed is None:
        result["unknown_fields"].append("surveyed_planet_ids")
    for system_id in visited:
        system = _node(systems, system_id)
        if system is None:
            result["unknown_fields"].append(f"system:{system_id}")
            continue
        references = system.get_all("planet")
        body_ids = [_id(value) for value in references]
        known = bool(references) and all(value is not None for value in body_ids) and len(set(body_ids)) == len(body_ids)
        entry = {"id": system_id, "name": _name(system), "visited": True,
                 "planets_known": known, "planet_ids": body_ids if known else None,
                 "hyperlanes_known": False, "hyperlane_system_ids": None}
        for key in ("owner", "controller", "starbase"):
            reference_known, identifier = (_system_starbase(system) if key == "starbase"
                                           else _system_reference(system, key))
            entry[f"{key}_known"] = reference_known
            if reference_known:
                entry[key] = identifier
        lanes = system.get_all("hyperlane")
        if (len(lanes) == 1 and isinstance(lanes[0], PDXNode) and lanes[0].entries
                and all(key is None for key, _ in lanes[0].entries)):
            lanes = list(lanes[0].values())
        destinations = [_id(_one(lane, "to")) if isinstance(lane, PDXNode) else None for lane in lanes]
        if lanes and all(value is not None for value in destinations) and len(set(destinations)) == len(destinations):
            entry.update(hyperlanes_known=True, hyperlane_system_ids=destinations)
        result["systems_by_id"][system_id] = entry
        if not known:
            result["unknown_fields"].append(f"system:{system_id}.planet_ids")
            continue
        for planet_id in body_ids:
            body = _node(planets, planet_id)
            origin = _id(_one(_node(body, "coordinate"), "origin"))
            if body is None or origin != system_id:
                result["unknown_fields"].append(f"planet:{planet_id}.system_membership")
                continue
            item = {"id": planet_id, "system_id": system_id, "name": _name(body),
                    "surveyed_known": surveyed is not None,
                    "surveyed": planet_id in surveyed if surveyed is not None else None}
            for key in ("planet_class", "owner", "controller", "colony", "build_queue",
                        "shipclass_orbital_station", "orbital_defence"):
                value = _one(body, key)
                if key == "planet_class":
                    if isinstance(value, str):
                        item[key] = value
                elif (identifier := _id(value)) is not None:
                    item[key] = identifier
            result["planets_by_id"][planet_id] = item
    return result
