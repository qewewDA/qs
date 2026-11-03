"""Experimental file transport for two fixed actions, not a general executor.

``probe_connection`` writes a receipt flag and a log line. The separately
authorized ``hold_military_fleets`` clears the current orders of eligible owned
fleets and queues a normal 30-day wait. Both request a dump. This component
cannot build, research, grant resources or run model text. Publishing a file
does not prove that the game consumed it or that the requested orders applied.

The effect runs in the console's current player-country scope. Exact country
binding depends on bootstrap setting the unpredictable session flag ONLY on the
locally configured country, and the runtime verifying that binding in a fresh
snapshot. This module never bootstraps that flag. Numeric ``country:ID`` effect
scopes were rejected by the tested game build and must not be used here.

The caller must hold the user-directory transport lock and retain an unresolved
journal operation until a fresh state verifies its receipt. Atomic replacement
does not coordinate unrelated programs writing to the same directory.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
import re
import stat
import tempfile
from typing import Any


INBOX_NAME = "qs_direct_inbox.txt"
DUMP_NAME = "dump_qs_direct.sav.txt"
OWNERSHIP_HEADER = "# SAA_DIRECT_INBOX_V1"
PROBE_ACTION = "probe_connection"
HOLD_ACTION = "hold_military_fleets"
_ACTIONS = frozenset({PROBE_ACTION, HOLD_ACTION})
ORDER_KINDS = frozenset({"fleet_wait", "fleet_orbit", "fleet_move", "fleet_stop"})
NATIVE_ACTIONS = frozenset({"select_research", "queue_district", "queue_building", "set_paused", "queue_ship",
                            "upgrade_building", "demolish_building", "convert_zone", "cancel_construction",
                            "survey_planet", "build_station", "colonize_planet", "queue_colony_ship"})
_TOKEN = re.compile(r"[0-9a-f]{32}", re.ASCII)
_COUNTRY = re.compile(r"[0-9]{1,10}", re.ASCII)

# Published for local tooling; load_direct_config enforces this without adding
# a JSON-schema runtime dependency to the base package.
DIRECT_CONFIG_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["user_dir", "state_dir", "country_id", "campaign_id", "verified_game_version", "session_token"],
    "properties": {
        "user_dir": {"type": "string", "minLength": 1},
        "state_dir": {"type": "string", "minLength": 1},
        "country_id": {"type": "string", "pattern": r"^[0-9]{1,10}$"},
        "campaign_id": {"type": "string", "minLength": 1, "maxLength": 512},
        "verified_game_version": {"type": "string", "minLength": 1, "maxLength": 256},
        "session_token": {"type": "string", "pattern": r"^[0-9a-f]{32}$"},
        "allowed_actions": {"type": "array", "uniqueItems": True,
                            "items": {"enum": [PROBE_ACTION, HOLD_ACTION]}},
        "allowed_orders": {"type": "array", "uniqueItems": True,
                           "items": {"enum": sorted(ORDER_KINDS)}},
        "allowed_native_actions": {"type": "array", "uniqueItems": True,
                                   "items": {"enum": sorted(NATIVE_ACTIONS)}},
        "single_player_confirmed": {"type": "boolean", "default": False},
        "non_ironman_confirmed": {"type": "boolean", "default": False},
        "max_snapshot_age_seconds": {"type": "number", "exclusiveMinimum": 0, "default": 180},
        "verification_timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 600, "default": 60},
        "poll_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 30, "default": 1},
    },
}


@dataclass(frozen=True)
class DirectConfig:
    user_dir: Path
    state_dir: Path
    country_id: str
    campaign_id: str
    verified_game_version: str
    session_token: str
    allowed_actions: tuple[str, ...] = ()
    allowed_orders: tuple[str, ...] = ()
    allowed_native_actions: tuple[str, ...] = ()
    single_player_confirmed: bool = False
    non_ironman_confirmed: bool = False
    max_snapshot_age_seconds: float = 180
    verification_timeout_seconds: float = 60
    poll_seconds: float = 1

    @property
    def inbox_path(self) -> Path:
        return self.user_dir / INBOX_NAME

    @property
    def dump_path(self) -> Path:
        return self.user_dir / "save games" / DUMP_NAME


def _token(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"{name} must contain exactly 32 lowercase ASCII hex characters")
    return value


def validate_direct_config(config: DirectConfig) -> None:
    """Validate the independent direct policy, without touching the game."""
    if not isinstance(config, DirectConfig):
        raise ValueError("Expected a DirectConfig")
    for name in ("user_dir", "state_dir"):
        value = getattr(config, name)
        if not isinstance(value, Path) or not value.is_absolute() or any(c in str(value) for c in "\0\r\n"):
            raise ValueError(f"{name} must be an absolute local path without control characters")
    if not isinstance(config.country_id, str) or not _COUNTRY.fullmatch(config.country_id):
        raise ValueError("country_id must contain 1–10 ASCII decimal digits")
    for name, limit in (("campaign_id", 512), ("verified_game_version", 256)):
        value = getattr(config, name)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{name} must be nonempty text of at most {limit} characters")
    if config.verified_game_version.strip().casefold() in {"unknown", "none"}:
        raise ValueError("verified_game_version must identify a known game version")
    _token(config.session_token, "session_token")
    actions = config.allowed_actions
    if (not isinstance(actions, (tuple, list))
            or any(not isinstance(action, str) or action not in _ACTIONS for action in actions)
            or len(actions) != len(set(actions))):
        raise ValueError("allowed_actions may contain only fixed direct actions, without duplicates")
    orders = config.allowed_orders
    if (not isinstance(orders, (tuple, list))
            or any(not isinstance(order, str) or order not in ORDER_KINDS for order in orders)
            or len(orders) != len(set(orders))):
        raise ValueError("allowed_orders may contain only fixed direct order kinds, without duplicates")
    native = config.allowed_native_actions
    if (not isinstance(native, (tuple, list))
            or any(not isinstance(action, str) or action not in NATIVE_ACTIONS for action in native)
            or len(native) != len(set(native))):
        raise ValueError("allowed_native_actions may contain only fixed native actions, without duplicates")
    for name in ("single_player_confirmed", "non_ironman_confirmed"):
        if type(getattr(config, name)) is not bool:
            raise ValueError(f"{name} must be boolean")
    for name in ("max_snapshot_age_seconds", "verification_timeout_seconds", "poll_seconds"):
        value = getattr(config, name)
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value > 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(f"{name} must be a positive finite number")
    if config.verification_timeout_seconds > 600 or config.poll_seconds > 30:
        raise ValueError("Verification timeout <=600 seconds and poll <=30 seconds required")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate direct config field: {key}")
        result[key] = value
    return result


def load_direct_config(path: str | Path) -> DirectConfig:
    """Load only the direct schema; relative paths resolve beside this file."""
    path = Path(path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
    if not isinstance(raw, dict):
        raise ValueError("Direct configuration must be a JSON object")
    unknown = set(raw) - set(DirectConfig.__dataclass_fields__)
    if unknown:
        raise ValueError(f"Unknown direct config fields: {sorted(unknown)}")
    missing = set(DIRECT_CONFIG_SCHEMA["required"]) - set(raw)
    if missing:
        raise ValueError(f"Missing direct config fields: {sorted(missing)}")
    for name in ("user_dir", "state_dir"):
        value = raw[name]
        if not isinstance(value, str) or not value.strip() or any(c in value for c in "\0\r\n"):
            raise ValueError(f"{name} must be a nonempty local path string")
        directory = Path(value).expanduser()
        raw[name] = (directory if directory.is_absolute() else path.parent / directory).resolve()
    actions = raw.get("allowed_actions", [])
    if not isinstance(actions, list):
        raise ValueError("allowed_actions must be a list")
    raw["allowed_actions"] = tuple(actions)
    orders = raw.get("allowed_orders", [])
    if not isinstance(orders, list):
        raise ValueError("allowed_orders must be a list")
    raw["allowed_orders"] = tuple(orders)
    native = raw.get("allowed_native_actions", [])
    if not isinstance(native, list):
        raise ValueError("allowed_native_actions must be a list")
    raw["allowed_native_actions"] = tuple(native)
    config = DirectConfig(**raw)
    validate_direct_config(config)
    return config


def ownership_header(config: DirectConfig) -> str:
    """Bind project ownership to the locally selected country and session."""
    validate_direct_config(config)
    return f"{OWNERSHIP_HEADER} session={config.session_token} country={config.country_id}\n"


def render_action(config: DirectConfig, action_id: str, token: str) -> str:
    """Compile a fixed locally authorized action, never model text or paths."""
    validate_direct_config(config)
    _token(token, "operation token")
    if not isinstance(action_id, str) or action_id not in _ACTIONS:
        raise ValueError("Unknown direct action ID")
    if action_id not in config.allowed_actions:
        raise ValueError(f"{action_id} is not locally authorized")
    _confirm_mode(config)
    session = f"qs_direct_session_{config.session_token}"
    done = f"qs_direct_done_{token}"
    orders = ""
    if action_id == HOLD_ACTION:
        orders = (
            "every_owned_fleet = { limit = { is_mobile = yes "
            "is_ship_class = shipclass_military is_in_combat = no } "
            "clear_fleet_actions = this clear_orders = yes queue_actions = { wait = 30 } } "
        )
    return (
        ownership_header(config)
        + "effect if = { limit = { is_ai = no is_ironman = no is_multiplayer = no "
        + f"has_country_flag = {session} NOT = {{ has_country_flag = {done} }} "
        + "} " + orders
        + f'set_country_flag = {done} log = "SAA_DIRECT|{action_id}|'
        + f'session={config.session_token}|token={token}" }}\n'
        + "debug_dumpgamestate qs_direct\n"
    )


def render_probe(config: DirectConfig, token: str) -> str:
    """Compile only the connection probe for callers using the initial API."""
    return render_action(config, PROBE_ACTION, token)


def _confirm_mode(config: DirectConfig) -> None:
    if not config.single_player_confirmed or not config.non_ironman_confirmed:
        raise ValueError("Direct publication requires confirmed single-player non-Ironman mode")


def _signature(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _owned_signature(path: Path, header: bytes) -> tuple | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise ValueError("Direct inbox must be a regular file, not a link or reparse point")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        # Windows directory entries can retain the former creation time after
        # replacement. Compare path/handle identity without ctime, then use the
        # opened handle's full signature for stable comparisons.
        if _signature(opened)[:4] != _signature(info)[:4]:
            raise ValueError("Direct inbox changed during ownership check")
        if stream.readline(len(header) + 1) != header:
            raise ValueError("Refusing to overwrite an inbox not owned by this direct session")
        if _signature(os.fstat(stream.fileno())) != _signature(opened):
            raise ValueError("Direct inbox changed during ownership check")
    return _signature(opened)


def _check_stop(config: DirectConfig) -> None:
    if os.path.lexists(config.state_dir / "STOP"):
        raise ValueError("STOP file exists; refusing direct publication")


def _publish(config: DirectConfig, content: str, *, honor_stop: bool) -> Path:
    validate_direct_config(config)
    if honor_stop:
        _check_stop(config)
    info = config.user_dir.lstat()
    if (not stat.S_ISDIR(info.st_mode) or config.user_dir.is_symlink()
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise ValueError("user_dir must be an existing directory, not a link or reparse point")
    destination = config.inbox_path
    header = ownership_header(config).encode("ascii")
    before = _owned_signature(destination, header)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".qs_direct_inbox-", suffix=".tmp", dir=config.user_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if honor_stop:
            _check_stop(config)
        if _owned_signature(destination, header) != before:
            raise ValueError("Direct inbox changed before publication; inspect before retrying")
        os.replace(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def atomic_publish(config: DirectConfig, token: str, action_id: str = PROBE_ACTION) -> Path:
    """Publish a complete fixed action under the caller's transport lock; honor STOP."""
    return _publish(config, render_action(config, action_id, token), honor_stop=True)


def publish_order(config: DirectConfig, order, token: str) -> Path:
    """Publish a validated precise order through the same owned, atomic mailbox."""
    from .direct_orders import render_order

    return _publish(config, render_order(config, order, token), honor_stop=True)


def publish_pause_verification(config: DirectConfig, paused: bool, token: str) -> Path:
    """Record a pause postcondition through the real game trigger; never set it."""
    validate_direct_config(config)
    _confirm_mode(config)
    _token(token, "operation token")
    if type(paused) is not bool or "set_paused" not in config.allowed_native_actions:
        raise ValueError("Pause verification requires a boolean and local authorization")
    body = (
        ownership_header(config)
        + "effect if = { limit = { is_ai = no is_ironman = no is_multiplayer = no "
        + f"has_country_flag = qs_direct_session_{config.session_token} "
        + f"is_paused = {'yes' if paused else 'no'} NOT = {{ has_country_flag = qs_direct_done_{token} }} "
        + f'}} set_country_flag = qs_direct_done_{token} log = "SAA_DIRECT|pause_postcondition|token={token}" }}\n'
        + "debug_dumpgamestate qs_direct\n"
    )
    return _publish(config, body, honor_stop=True)


def publish_observation(config: DirectConfig) -> Path:
    """Explicitly request only a new dump; observation readers never call this."""
    validate_direct_config(config)
    _confirm_mode(config)
    return _publish(config, ownership_header(config) + "debug_dumpgamestate qs_direct\n", honor_stop=True)


def disarm_inbox(config: DirectConfig) -> Path:
    """Replace an owned inbox with comments even after STOP or permission revocation."""
    return _publish(config, ownership_header(config) + "# disarmed\n", honor_stop=False)
