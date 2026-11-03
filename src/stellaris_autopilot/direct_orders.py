"""Bounded orders for exact observed fleets; no general script interface.

Target events must be installed and bootstrapped for this session. Console
``event <id> fleet/planet <ID>`` explicitly binds the requested object type;
bare numeric event targets are countries, even for fleet/planet events.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any

from .direct import ORDER_KINDS, DirectConfig, _confirm_mode, _token, ownership_header, validate_direct_config


_ID = re.compile(r"(?:0|[1-9][0-9]{0,9})", re.ASCII)
_CLASSES = frozenset({"shipclass_military", "shipclass_science_ship", "shipclass_constructor", "shipclass_colonizer"})


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or int(value) >= 4294967295:
        raise ValueError("Object IDs must be canonical ASCII decimal strings below the unset-ID sentinel")
    return value


def _state_identifier(value: Any) -> str:
    if type(value) is int and value >= 0:
        value = str(value)
    return _identifier(value)


@dataclass(frozen=True)
class Order:
    kind: str
    fleet_id: str
    planet_id: str | None = None
    days: int | None = None

    def to_dict(self) -> dict:
        result = {"kind": self.kind, "fleet_id": self.fleet_id}
        if self.planet_id is not None:
            result["planet_id"] = self.planet_id
        if self.days is not None:
            result["days"] = self.days
        return result


def parse_order(raw: dict) -> Order:
    """Accept exactly the fields for a known order; reject coerced IDs and booleans."""
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError("Order must be an object with string field names")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in ORDER_KINDS:
        raise ValueError("Unknown direct order kind")
    expected = {"kind", "fleet_id"}
    if kind != "fleet_stop":
        expected.add("days" if kind == "fleet_wait" else "planet_id")
    if set(raw) != expected:
        raise ValueError("Order has missing or unexpected fields")
    fleet_id = _identifier(raw["fleet_id"])
    if kind == "fleet_wait":
        days = raw["days"]
        if type(days) is not int or not 1 <= days <= 365:
            raise ValueError("Wait duration must be an integer in 1..365 game days")
        return Order(kind, fleet_id, days=days)
    if kind == "fleet_stop":
        return Order(kind, fleet_id)
    return Order(kind, fleet_id, planet_id=_identifier(raw["planet_id"]))


def _typed(order: Order) -> Order:
    if not isinstance(order, Order):
        raise ValueError("Expected a parsed Order")
    return parse_order(order.to_dict())


def validate_order(state: dict, order: Order) -> None:
    """Require a complete, owned, mobile and explicitly non-combat fleet snapshot."""
    order = _typed(order)
    if not isinstance(state, dict):
        raise ValueError("A normalized state is required")
    country = _identifier(state.get("country_id"))
    metadata = state.get("metadata")
    missing = metadata.get("missing_fields") if isinstance(metadata, dict) else None
    if not isinstance(missing, list) or any(not isinstance(name, str) for name in missing) or "fleets" in missing:
        raise ValueError("Complete fleet ownership is not established by this snapshot")
    fleets = state.get("fleets_by_id")
    fleet = fleets.get(order.fleet_id) if isinstance(fleets, dict) else None
    if not isinstance(fleet, dict) or fleet.get("id") != order.fleet_id:
        raise ValueError("Requested fleet is not an identified owned fleet")
    # The normalizer can establish ownership from a complete country fleet
    # manager when an object omits owner. Explicit contradictory claims fail.
    for key in ("owner", "controller"):
        if key in fleet and _state_identifier(fleet[key]) != country:
            raise ValueError("Requested fleet is not owned and controlled by the selected country")
    ship_class = fleet.get("ship_class")
    if not isinstance(ship_class, str) or ship_class not in _CLASSES or fleet.get("mobile") is not True:
        raise ValueError("Requested fleet must be a mobile military, science or construction fleet")
    combat = fleet.get("combat")
    if not isinstance(combat, dict) or combat.get("start_date") != "0.01.01":
        raise ValueError("Requested fleet has active or unobservable combat state")
    if order.kind == "fleet_orbit":
        if "planets" in missing:
            raise ValueError("Planet ownership is unobservable")
        planets = state.get("planets_by_id")
        planet = planets.get(order.planet_id) if isinstance(planets, dict) else None
        if (not isinstance(planet, dict) or planet.get("id") != order.planet_id
                or _state_identifier(planet.get("owner")) != country):
            raise ValueError("Orbit target must be an explicitly owned planet")
        if "controller" in planet and _state_identifier(planet["controller"]) != country:
            raise ValueError("Orbit target is not controlled by the selected country")
        if _identifier(fleet.get("system_id")) != _identifier(planet.get("system_id")):
            raise ValueError("Orbit target must be in the fleet's observed current system")
    elif order.kind == "fleet_move":
        _move_target(state, order)


def _move_target(state: dict, order: Order) -> dict:
    """A visited physical body is an explicit anchor for the destination system."""
    space = state.get("space")
    planets = space.get("planets_by_id") if isinstance(space, dict) else None
    target = planets.get(order.planet_id) if isinstance(planets, dict) else None
    systems = space.get("systems_by_id") if isinstance(space, dict) else None
    if not isinstance(target, dict) or target.get("id") != order.planet_id:
        raise ValueError("Move target must be an observed physical body in a visited system")
    system_id = _identifier(target.get("system_id"))
    system = systems.get(system_id) if isinstance(systems, dict) else None
    if (not isinstance(system, dict) or system.get("id") != system_id or system.get("visited") is not True
            or system.get("planets_known") is not True or order.planet_id not in system.get("planet_ids", [])):
        raise ValueError("Move destination system membership is unobservable or contradictory")
    return target


def render_target_events(session: str) -> str:
    """Render the fixed typed-binding Mod events for one unpredictable session."""
    _token(session, "session token")
    guard = f"has_country_flag = qs_direct_session_{session} is_ai = no is_multiplayer = no is_ironman = no"
    result = "namespace = qs_direct_targets\n\n"
    for event_type, number, target in (("planet_event", 1, "planet"), ("fleet_event", 2, "fleet")):
        result += (
            f"{event_type} = {{\n id = qs_direct_targets.{number}\n hide_window = yes\n"
            " is_triggered_only = yes\n immediate = {\n"
            f"  if = {{ limit = {{ owner = {{ {guard} }} }}\n"
            f"   save_global_event_target_as = qs_direct_{target}_{session}\n"
            "  }\n }\n}\n\n"
        )
    result += (
        "country_event = {\n id = qs_direct_targets.3\n hide_window = yes\n is_triggered_only = yes\n"
        f" immediate = {{ if = {{ limit = {{ {guard} }}\n"
        f"  set_country_flag = qs_direct_targets_{session}\n"
        " } }\n}\n"
    )
    # A move anchor may be unowned. Binding it changes only a private named
    # target; the actual fleet action still requires the selected human owner,
    # the session/bootstrap flags and the engine's full pathfinding predicate.
    result += (
        "\nplanet_event = {\n id = qs_direct_targets.4\n hide_window = yes\n is_triggered_only = yes\n"
        f" immediate = {{ if = {{ limit = {{ any_country = {{ {guard} }} }}\n"
        f"  save_global_event_target_as = qs_direct_navigation_{session}\n"
        " } }\n}\n"
    )
    return result


def render_order(config: DirectConfig, order: Order, token: str) -> str:
    """Compile only validated typed parameters into session-bound fixed templates."""
    validate_direct_config(config)
    _confirm_mode(config)
    _token(token, "operation token")
    order = _typed(order)
    if order.kind not in config.allowed_orders:
        raise ValueError("Direct order kind is not locally authorized")
    session = config.session_token
    fleet = f"qs_direct_fleet_{session}"
    planet = f"qs_direct_planet_{session}"
    navigation = f"qs_direct_navigation_{session}"
    owner = f"owner = {{ has_country_flag = qs_direct_session_{session} is_ai = no }}"
    cleanup_targets = ((fleet,) if order.kind == "fleet_stop" else
                       (fleet, navigation) if order.kind == "fleet_move" else (fleet, planet))
    cleanup = "".join(
        f"effect if = {{ limit = {{ exists = event_target:{target} }} clear_global_event_target = {target} }}\n"
        for target in cleanup_targets
    )
    binding = f"event qs_direct_targets.2 fleet {order.fleet_id}\n"
    fleet_guard = (
        f"event_target:{fleet} = {{ {owner} is_mobile = yes is_in_combat = no "
        "OR = { is_ship_class = shipclass_military is_ship_class = shipclass_science_ship "
        "is_ship_class = shipclass_constructor is_ship_class = shipclass_colonizer } "
    )
    target_guard = ""
    if order.kind == "fleet_orbit":
        binding = f"event qs_direct_targets.1 planet {order.planet_id}\n" + binding
        target_guard = f"exists = event_target:{planet} event_target:{planet} = {{ {owner} }} "
        fleet_guard += f"solar_system = {{ is_same_value = event_target:{planet}.solar_system }} "
        queue = f"orbit_planet = event_target:{planet} wait = 30"
    elif order.kind == "fleet_move":
        binding = f"event qs_direct_targets.4 planet {order.planet_id}\n" + binding
        target_guard = f"exists = event_target:{navigation} "
        fleet_guard += f"can_access_system = event_target:{navigation}.solar_system "
        queue = f"move_to = event_target:{navigation}.solar_system wait = 30"
    elif order.kind == "fleet_wait":
        queue = f"wait = {order.days}"
    else:
        queue = ""
    fleet_guard += "} "
    execution = "clear_fleet_actions = this clear_orders = yes"
    if queue:
        execution += f" queue_actions = {{ {queue} }}"
    return (
        ownership_header(config) + cleanup + binding
        + "effect if = { limit = { is_ai = no is_multiplayer = no is_ironman = no "
        + f"has_country_flag = qs_direct_session_{session} has_country_flag = qs_direct_targets_{session} "
        + f"NOT = {{ has_country_flag = qs_direct_done_{token} }} exists = event_target:{fleet} "
        + target_guard + fleet_guard + "} "
        + f"event_target:{fleet} = {{ {execution} }} "
        + f'set_country_flag = qs_direct_done_{token} log = "SAA_DIRECT|{order.kind}|token={token}" }}\n'
        + cleanup + "debug_dumpgamestate qs_direct\n"
    )


def _wait_matches(value: Any, maximum: int) -> bool:
    if not isinstance(value, dict) or set(value) != {"time"}:
        return False
    duration = value["time"]
    try:
        return type(duration) in (int, float) and math.isfinite(duration) and 0 < duration <= maximum
    except OverflowError:
        return False


def verify_order(state: dict, order: Order) -> bool:
    """Verify exact known queue layouts; receipts and unproven orbit fields do not suffice."""
    try:
        validate_order(state, order)
        fleet = state["fleets_by_id"][order.fleet_id]
        if order.kind == "fleet_stop":
            # The source normalizer establishes this only when the complete
            # fleet object has unambiguous action/current/queued-order fields.
            # An omitted field can then mean empty; an explicitly unknown or
            # malformed field still cannot establish that orders were cleared.
            if fleet.get("orders_known") is not True:
                return False
            return all(key not in fleet or isinstance(fleet[key], (dict, list)) and not fleet[key]
                       for key in ("actions", "current_order", "order", "orders", "queued_orders"))
        actions = fleet.get("actions")
        if not isinstance(actions, dict):
            return False
        if order.kind == "fleet_wait":
            return set(actions) == {"wait"} and _wait_matches(actions["wait"], order.days)
        if order.kind == "fleet_move":
            target = _move_target(state, order)
            if set(actions) != {"move_to", "wait"} or not _wait_matches(actions["wait"], 30):
                return False
            move = actions["move_to"]
            # Cygnus 4.5.2 serializes the fixed solar_system action as the
            # system center coordinate. Only this measured layout proves the
            # requested destination; arbitrary points and assumed target
            # references cannot establish that the script queued this action.
            if not isinstance(move, dict) or set(move) != {"coordinate"}:
                return False
            coordinate = move["coordinate"]
            return (isinstance(coordinate, dict) and set(coordinate) == {"x", "y", "origin"}
                    and all(type(coordinate[axis]) in (int, float) and coordinate[axis] == 0
                            for axis in ("x", "y"))
                    and _state_identifier(coordinate["origin"]) == target["system_id"])
        if set(actions) != {"orbit_planet", "wait"} or not _wait_matches(actions["wait"], 30):
            return False
        orbit = actions["orbit_planet"]
        if not isinstance(orbit, dict) or set(orbit) != {"orbitable"}:
            return False
        orbitable = orbit["orbitable"]
        return (isinstance(orbitable, dict) and set(orbitable) == {"planet"}
                and _state_identifier(orbitable["planet"]) == order.planet_id)
    except (ValueError, TypeError, KeyError):
        return False
