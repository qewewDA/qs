"""Small, non-executable predicate language over normalized observations."""
from __future__ import annotations

import math
from typing import Any

MISSING = object()
OPS = {"eq", "gte", "lte", "changed", "contains"}


def lookup(state: Any, path: str) -> Any:
    value = state
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part, MISSING)
        elif isinstance(value, list) and part.isascii() and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            return MISSING
    return value


def validate_predicate(predicate: dict) -> None:
    if not isinstance(predicate, dict) or set(predicate) - {"path", "op", "value"}:
        raise ValueError("Invalid predicate fields")
    path = predicate.get("path")
    if not isinstance(path, str) or not path or len(path) > 200 or any(not p for p in path.split(".")):
        raise ValueError("Predicate requires a dotted state path")
    op = predicate.get("op")
    if not isinstance(op, str) or op not in OPS or (op != "changed" and "value" not in predicate):
        raise ValueError("Invalid predicate operator or missing value")
    if op in {"gte", "lte"}:
        target = predicate["value"]
        try:
            finite = isinstance(target, (int, float)) and not isinstance(target, bool) and math.isfinite(target)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("Numeric predicate requires a finite number")


def evaluate(predicate: dict, state: dict, before: dict | None = None) -> bool:
    value = lookup(state, predicate["path"])
    if value is MISSING:
        return False  # Unknown is never silently treated as zero or success.
    op = predicate["op"]
    if op == "changed":
        old = lookup(before, predicate["path"])
        return old is not MISSING and old != value
    target = predicate.get("value")
    if op == "eq":
        return type(value) is type(target) and value == target or (
            not isinstance(value, bool) and not isinstance(target, bool)
            and isinstance(value, (int, float)) and isinstance(target, (int, float)) and value == target
        )
    if op in {"gte", "lte"}:
        try:
            finite = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            return False
        return value >= target if op == "gte" else value <= target
    if op == "contains":
        return (isinstance(value, list) and target in value) or (
            isinstance(value, str) and isinstance(target, str) and target in value
        )
    return False
