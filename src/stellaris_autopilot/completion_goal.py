"""Pure, conservative evidence for explicitly bounded natural completion.

The public intent dictionaries are JSON compatible.  ``survey_system`` freezes
its ordered body IDs at authorization time.  Evaluations never send an order,
advance time, or infer completion from a disappearing fleet order.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, StrictInt, StrictStr, TypeAdapter,
    field_validator,
)

from .direct_orders import Order, validate_order


_ID = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z", re.ASCII)
_SNAPSHOT = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z", re.ASCII)
_DATE = re.compile(r"([0-9]+)\.([0-9]{1,2})\.([0-9]{1,2})\Z", re.ASCII)


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or int(value) >= 4294967295:
        raise ValueError("Object IDs must be canonical ASCII decimal strings below the unset-ID sentinel")
    return value


CanonicalId = Annotated[StrictStr, AfterValidator(_identifier)]


class _Intent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, revalidate_instances="always")


class SurveyBodyIntent(_Intent):
    kind: Literal["survey_body"]
    fleet_id: CanonicalId
    planet_id: CanonicalId
    system_id: CanonicalId


class SurveySystemIntent(_Intent):
    kind: Literal["survey_system"]
    fleet_id: CanonicalId
    system_id: CanonicalId
    planet_ids: Annotated[tuple[CanonicalId, ...], Field(min_length=1, max_length=100)]

    @field_validator("planet_ids", mode="before")
    @classmethod
    def _freeze_bodies(cls, value: Any) -> tuple:
        if not isinstance(value, (list, tuple)):
            raise ValueError("planet_ids must be an ordered list or tuple of exact body IDs")
        return tuple(value)

    @field_validator("planet_ids")
    @classmethod
    def _unique_bodies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("planet_ids must contain unique exact body IDs")
        return value


class EnterSystemIntent(_Intent):
    kind: Literal["enter_system"]
    fleet_id: CanonicalId
    planet_id: CanonicalId
    system_id: CanonicalId


CompletionIntent = Annotated[
    SurveyBodyIntent | SurveySystemIntent | EnterSystemIntent,
    Field(discriminator="kind"),
]
_INTENT = TypeAdapter(CompletionIntent)


class TimeAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, revalidate_instances="always")

    allow_time_advance: Literal[True]
    max_game_days: Annotated[StrictInt, Field(ge=1, le=3600)]
    max_wall_seconds: Annotated[StrictInt, Field(ge=1, le=3600)]
    final_pause: Literal[True]

    @field_validator("allow_time_advance", "final_pause", mode="before")
    @classmethod
    def _explicit_true(cls, value: Any) -> bool:
        # Literal[True] alone also accepts 1 in Pydantic's literal comparison.
        if type(value) is not bool or value is not True:
            raise ValueError("Time advancement and final pause require explicit boolean true")
        return value


def normalize_completion(
    intent: CompletionIntent | dict,
    time_authorization: TimeAuthorization | dict,
    expected_snapshot: str | None = None,
) -> tuple[dict, dict, str]:
    """Validate before connection acquisition and hash the entire authorization."""
    if expected_snapshot is not None and (
        not isinstance(expected_snapshot, str) or not _SNAPSHOT.fullmatch(expected_snapshot)
    ):
        raise ValueError("expected_snapshot must be a bounded snapshot identifier")
    normalized = _INTENT.validate_python(intent).model_dump(mode="json")
    authorization = TimeAuthorization.model_validate(time_authorization).model_dump(mode="json")
    canonical = json.dumps(
        {"type": "natural_completion_v1", "intent": normalized,
         "time_authorization": authorization, "expected_snapshot": expected_snapshot},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return normalized, authorization, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def game_day(date: str) -> int:
    """Return a zero-based absolute day in Stellaris' strict 360-day calendar."""
    match = _DATE.fullmatch(date) if isinstance(date, str) else None
    if match is None:
        raise ValueError("Game date must be an ASCII year.month.day string")
    year, month, day = (int(part) for part in match.groups())
    if not 1 <= month <= 12 or not 1 <= day <= 30:
        raise ValueError("Game date must use months 1..12 and days 1..30")
    return year * 360 + (month - 1) * 30 + day - 1


def _unknown(reason: str, fields: list[str], evidence: dict) -> dict:
    return {"value": None, "reason": reason, "unknown_fields": fields, "evidence": evidence}


def _known_ids(value: Any, *, nonempty: bool = False) -> list[str] | None:
    if not isinstance(value, list) or nonempty and not value:
        return None
    try:
        normalized = [_identifier(identifier) for identifier in value]
    except (ValueError, TypeError):
        return None
    return normalized if len(set(normalized)) == len(normalized) else None


def evaluate_goal(state: dict, intent_dict: CompletionIntent | dict) -> dict:
    """Prove True, disprove False, or retain None for missing/conflicting evidence.

    All goals require an owned mobile fleet with known noncombat and order
    state.  Entering a system proves only system membership, not arrival at
    its center, stopping, or the completion of other queued work.
    """
    intent = _INTENT.validate_python(intent_dict).model_dump(mode="json")
    kind, fleet_id, system_id = intent["kind"], intent["fleet_id"], intent["system_id"]
    evidence: dict[str, Any] = {"kind": kind, "fleet_id": fleet_id, "system_id": system_id}
    try:
        validate_order(state, Order("fleet_stop", fleet_id))
    except (ValueError, TypeError, KeyError, AttributeError):
        return _unknown("fleet_validation_failed", [f"fleets_by_id.{fleet_id}"], evidence)
    fleet = state["fleets_by_id"][fleet_id]
    if fleet.get("orders_known") is not True:
        return _unknown("fleet_orders_unobservable", [f"fleets_by_id.{fleet_id}.orders_known"], evidence)
    try:
        current_system = _identifier(fleet.get("system_id"))
    except (ValueError, TypeError):
        return _unknown("fleet_system_unobservable", [f"fleets_by_id.{fleet_id}.system_id"], evidence)
    evidence["fleet_system_id"] = current_system
    if kind.startswith("survey_") and fleet.get("ship_class") != "shipclass_science_ship":
        return _unknown("science_fleet_required", [f"fleets_by_id.{fleet_id}.ship_class"], evidence)

    space = state.get("space")
    systems = space.get("systems_by_id") if isinstance(space, dict) else None
    bodies = space.get("planets_by_id") if isinstance(space, dict) else None
    system = systems.get(system_id) if isinstance(systems, dict) else None
    field = f"space.systems_by_id.{system_id}"
    if not isinstance(system, dict) or system.get("id") != system_id or system.get("visited") is not True:
        return _unknown("target_system_unobservable", [field], evidence)
    body_ids = _known_ids(system.get("planet_ids"), nonempty=True)
    if system.get("planets_known") is not True or body_ids is None or not isinstance(bodies, dict):
        return _unknown("system_membership_unobservable", [f"{field}.planet_ids"], evidence)
    for body_id in body_ids:
        body = bodies.get(body_id)
        if not isinstance(body, dict) or body.get("id") != body_id or body.get("system_id") != system_id:
            return _unknown("system_membership_contradiction",
                            [f"space.planets_by_id.{body_id}.system_id"], evidence)
    # A complete system body list cannot exclude an observed body claiming it.
    for body_id, body in bodies.items():
        if isinstance(body, dict) and body.get("system_id") == system_id and body_id not in body_ids:
            return _unknown("system_membership_contradiction", [f"{field}.planet_ids"], evidence)

    if kind == "survey_system":
        frozen_ids = intent["planet_ids"]
        if set(body_ids) != set(frozen_ids):
            return _unknown("frozen_system_membership_changed", [f"{field}.planet_ids"], evidence)
        targets = frozen_ids
    else:
        target = intent["planet_id"]
        evidence["planet_id"] = target
        if target not in body_ids:
            return _unknown("target_system_membership_contradiction",
                            [f"space.planets_by_id.{target}.system_id", f"{field}.planet_ids"], evidence)
        targets = [target]

    if kind == "enter_system":
        evidence["completion_scope"] = "system_entry_only"
        return {"value": current_system == system_id, "evidence": evidence}

    ledger = _known_ids(space.get("surveyed_planet_ids"))
    if space.get("surveyed_planets_known") is not True or ledger is None:
        return _unknown("survey_ledger_unobservable", ["space.surveyed_planet_ids"], evidence)
    surveyed_ids = set(ledger)
    surveyed: list[str] = []
    unsurveyed: list[str] = []
    for body_id in targets:
        body = bodies[body_id]
        field = f"space.planets_by_id.{body_id}"
        if body.get("surveyed_known") is not True or type(body.get("surveyed")) is not bool:
            return _unknown("survey_status_unobservable", [f"{field}.surveyed"], evidence)
        if body["surveyed"] != (body_id in surveyed_ids):
            return _unknown("survey_ledger_contradiction", [f"{field}.surveyed", "space.surveyed_planet_ids"], evidence)
        (surveyed if body["surveyed"] else unsurveyed).append(body_id)
    evidence.update(surveyed_planet_ids=surveyed, unsurveyed_planet_ids=unsurveyed)
    return {"value": not unsurveyed, "evidence": evidence}
