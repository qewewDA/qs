"""Local permission policy: only the user configuration can authorize tools."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from .predicates import validate_predicate

EVENTS = {"defend_capital": "saa.20"}


@dataclass(frozen=True)
class Action:
    id: str
    description: str
    category: str
    steps: list[dict] = field(default_factory=list)
    event: str | None = None
    preconditions: list[dict] = field(default_factory=list)
    expected: list[dict] = field(default_factory=list)


@dataclass
class Config:
    save_path: Path
    country_id: str | None = None
    campaign_id: str = ""
    state_dir: Path = Path(".autopilot")
    actions: dict[str, Action] = field(default_factory=dict)
    allowed_actions: list[str] = field(default_factory=list)
    verified_game_version: str = ""
    single_player_confirmed: bool = False
    non_ironman_confirmed: bool = False
    max_save_age_seconds: float = 180
    verification_timeout_seconds: float = 60
    poll_seconds: float = 2
    console_key: str = "`"
    resolution: tuple[int, int] | None = None
    log_path: Path | None = None
    save_steps: list[dict] = field(default_factory=list)


def load_config(path: str | Path) -> Config:
    from .windows import validate_steps

    path = Path(path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a JSON object")
    known = set(Config.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Unknown config fields: {sorted(unknown)}")
    if "save_path" not in raw:
        raise ValueError("save_path is required")
    actions = {}
    entries = raw.pop("actions", [])
    if not isinstance(entries, list):
        raise ValueError("actions must be a list")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("actions must contain objects")
        try:
            action = Action(**entry)
        except TypeError:
            raise ValueError("Invalid or missing action fields") from None
        if not isinstance(action.preconditions, list) or not isinstance(action.expected, list):
            raise ValueError("Action predicates must be lists")
        if (not isinstance(action.id, str) or not 1 <= len(action.id) <= 80
                or not action.id.isascii() or not action.id.replace("_", "").isalnum()
                or action.id == "wait" or action.id in actions):
            raise ValueError("Action IDs must be unique ASCII identifiers (1–80 characters); wait is reserved")
        if not isinstance(action.description, str) or not action.description.strip():
            raise ValueError("Action description must be nonempty text")
        if not isinstance(action.category, str) or action.category not in {"research", "economy", "defense", "diplomacy"}:
            raise ValueError("Invalid action category")
        if not isinstance(action.steps, list) or (action.event is not None and not isinstance(action.event, str)):
            raise ValueError("Action steps must be a list and event must be text or null")
        if bool(action.steps) == bool(action.event):
            raise ValueError("Each action requires exactly one of steps or event")
        if action.event and action.event != EVENTS.get(action.id):
            raise ValueError("Only the fixed defend_capital event is exposed")
        if action.steps:
            validate_steps(action.steps)
        if not action.expected:
            raise ValueError("Every action needs observable expected predicates")
        for predicate in action.preconditions + action.expected:
            validate_predicate(predicate)
        actions[action.id] = action
    for key in ("save_path", "state_dir", "log_path"):
        if key in raw and not (key == "log_path" and raw[key] is None):
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise ValueError(f"{key} must be a nonempty path string")
        if raw.get(key) is not None:
            value = Path(raw[key]).expanduser()
            raw[key] = value if value.is_absolute() else path.parent / value
    raw.setdefault("state_dir", path.parent / ".autopilot")
    cfg = Config(**raw, actions=actions)
    if not isinstance(cfg.verified_game_version, str):
        raise ValueError("verified_game_version must be a string")
    if not isinstance(cfg.campaign_id, str):
        raise ValueError("campaign_id must be a string")
    if not isinstance(cfg.console_key, str) or cfg.console_key not in {"`", "f2", "f12", "\\"}:
        raise ValueError("Unsupported console_key")
    if not isinstance(cfg.allowed_actions, list) or any(not isinstance(a, str) or a not in actions
                                                      for a in cfg.allowed_actions):
        raise ValueError("allowed_actions must reference configured actions")
    if cfg.country_id is not None:
        cfg.country_id = str(cfg.country_id)
        if not cfg.country_id.isascii() or not cfg.country_id.isdecimal():
            raise ValueError("country_id must be a nonnegative decimal ID")
    for name in ("single_player_confirmed", "non_ironman_confirmed"):
        if type(getattr(cfg, name)) is not bool:
            raise ValueError(f"{name} must be boolean")
    for name in ("max_save_age_seconds", "verification_timeout_seconds", "poll_seconds"):
        value = getattr(cfg, name)
        try:
            finite = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite or value <= 0:
            raise ValueError(f"{name} must be a positive finite number")
    if cfg.verification_timeout_seconds > 600 or cfg.poll_seconds > 30:
        raise ValueError("Verification timeout <=600 seconds and poll <=30 seconds required")
    if cfg.resolution is not None:
        if (not isinstance(cfg.resolution, list) or len(cfg.resolution) != 2
                or any(type(n) is not int or n < 100 for n in cfg.resolution)):
            raise ValueError("resolution must be [client_width, client_height]")
        cfg.resolution = tuple(cfg.resolution)
    if not isinstance(cfg.save_steps, list):
        raise ValueError("save_steps must be a list")
    if cfg.save_steps:
        validate_steps(cfg.save_steps)
    # Save macros are also local trusted configuration, never model generated.
    return cfg
