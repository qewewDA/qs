"""Bounded local connection evidence, without attaching or changing a game."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .campaign import CampaignError
from .native_builds import LEGACY_SHA256, build_for_hash, supported_versions


PINNED_GAME_VERSION = "Cygnus v4.5.2"
_MAX_LAUNCHER_BYTES = 64 * 1024
_MAX_PUBLIC_OPERATIONS = 10
_ACTION_KINDS = frozenset({
    "probe_connection", "hold_military_fleets", "issue_order", "select_research", "queue_district",
    "queue_building", "set_paused", "queue_ship", "queue_colony_ship", "upgrade_building",
    "demolish_building", "convert_zone", "cancel_construction", "survey_planet", "build_station",
    "colonize_planet", "fleet_move", "fleet_wait", "fleet_orbit", "fleet_stop",
})


class ConnectionPreflightError(CampaignError):
    """A failed connection with enough evidence to report without further probes."""

    def __init__(self, code: str, reason: str, diagnostics: dict, next_step: str):
        super().__init__(reason)
        self.code, self.diagnostics, self.next_step = code, diagnostics, next_step
        self.phase = ("verify_running_process" if code in {"unsupported_game_build", "executable_unreadable"}
                      else "acquire_controller" if code == "controller_lock_present"
                      else "bind_game_window" if code in {"game_not_paused", "pause_unverified"}
                      else "check_unresolved_operations")


def _action_kind(action) -> str:
    if not isinstance(action, str):
        return "unknown"
    # Older journals use native:<kind>:<payload>; current ones use <kind>:<JSON>.
    head = action.removeprefix("native:").split(":", 1)[0]
    return head if head in _ACTION_KINDS else "unknown"


def executable_evidence(executable: Path) -> dict:
    """The hash is authoritative; the adjacent launcher version is only a label."""
    result = {
        "supported": None,
        "sha256": None,
        "installed_version_hint": None,
        "supported_game_version": PINNED_GAME_VERSION,
        "supported_sha256": LEGACY_SHA256,
        "supported_game_versions": supported_versions(),
        "requires_native_adapter": None,
        "knowledge_rebuild_enables_native_calls": False,
    }
    try:
        with Path(executable).open("rb") as stream:
            result["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        result["read_error"] = "executable_unreadable"
    else:
        build = build_for_hash(result["sha256"])
        result["supported"] = build is not None
        result["requires_native_adapter"] = not result["supported"]
        if build is not None:
            result.update(supported_game_version=build.game_version, supported_sha256=build.sha256,
                          control_scope=("observation_pause_checkpoint_only" if build.observation_only
                                         else "locally_authorized_actions"))
    try:
        path = Path(executable).parent / "launcher-settings.json"
        with path.open("rb") as stream:
            raw = stream.read(_MAX_LAUNCHER_BYTES + 1)
        if len(raw) <= _MAX_LAUNCHER_BYTES:
            data = json.loads(raw.decode("utf-8-sig"))
            version = data.get("version") if isinstance(data, dict) else None
            if isinstance(version, str) and re.fullmatch(r"[A-Za-z0-9 .()_-]{1,100}", version, re.ASCII):
                result["installed_version_hint"] = version
    except (OSError, ValueError, UnicodeError):
        pass
    return result


def unresolved_evidence(state_root: Path, preview_journal) -> dict:
    """Inspect every journal; bound only the public list, never the blocking scan."""
    total, records = 0, []
    for path in sorted(Path(state_root).glob("sessions/*/journal.sqlite")):
        items = preview_journal(path).unresolved()
        total += len(items)
        for item in items:
            if len(records) >= _MAX_PUBLIC_OPERATIONS:
                break
            # The session and integer ID locate the exact record without exposing paths,
            # command text, credentials or any unbounded journal detail.
            record = {"session": path.parent.name, "operation_id": item.get("id"),
                      "status": item.get("status"), "action": _action_kind(item.get("action")),
                      "outcome": "unknown", "replayed": False}
            if not re.fullmatch(r"[0-9a-f]{32}", record["session"], re.ASCII):
                record["session"] = None
            if type(record["operation_id"]) is not int:
                record["operation_id"] = None
            if record["status"] not in {"pending", "dispatched", "unverified"}:
                record["status"] = "unknown"
            records.append(record)
    return {"blocked": bool(total), "count_at_least": total, "records": records,
            "records_truncated": total > len(records), "journal_modified": False,
            "policy": "Preserve unknown outcomes. Never replay or edit SQLite to bypass a blocker."}


def controller_lock_evidence(user_dir: Path | None) -> dict:
    if user_dir is None:
        return {"present": None, "inspected": False}
    try:
        with (Path(user_dir) / ".qs_existing_controller.lock").open("rb") as stream:
            raw = stream.read(4097)
    except FileNotFoundError:
        return {"present": False, "inspected": True}
    except OSError:
        return {"present": None, "inspected": True, "read_error": "controller_lock_unreadable"}
    result = {"present": True, "inspected": True, "owner_liveness": "unknown", "modified": False}
    try:
        if len(raw) > 4096:
            raise ValueError("oversized")
        record = json.loads(raw.decode("utf-8"))
        if not isinstance(record, dict):
            raise ValueError("invalid")
        pid, session = record.get("controller_pid"), record.get("session")
        if type(pid) is int and 0 < pid < 2**32:
            result["controller_pid"] = pid
        if isinstance(session, str) and re.fullmatch(r"[0-9a-f]{32}", session, re.ASCII):
            result["session"] = session
    except (ValueError, UnicodeError):
        result["read_error"] = "controller_lock_record_invalid"
    return result


def check_connection_preflight(executable: Path, state_root: Path, preview_journal,
                               *, user_dir: Path | None = None) -> None:
    """Report independent file/build and journal blockers in a single failed attempt."""
    build = executable_evidence(executable)
    try:
        operations = unresolved_evidence(state_root, preview_journal)
    except (ValueError, OSError):
        operations = {"blocked": True, "read_error": "journal_unreadable", "journal_modified": False}
    lock = controller_lock_evidence(user_dir)
    diagnostics = {"executable": build, "unresolved_operations": operations, "controller_lock": lock,
                   "game_attached": False, "game_commands_sent": 0}
    if operations.get("read_error"):
        raise ConnectionPreflightError(
            "journal_unreadable", "Earlier operation outcomes could not be inspected", diagnostics,
            "Report the journal read error and other available diagnostics. Preserve the journals; "
            "repair local readability before reconnecting. Unknown records are not proof of pending "
            "operations or an empty journal.")
    if operations.get("blocked"):
        reason = "An earlier attached operation is unresolved; inspect it before reconnecting"
        if build.get("sha256") is not None and not build["supported"]:
            reason += "; the game executable is also unsupported"
        raise ConnectionPreflightError(
            "unresolved_operations", reason, diagnostics,
            "Report all blockers and the listed session/operation IDs. Preserve unknown outcomes; "
            "do not edit the journal, replay commands, or retry until the blockers are corrected.")
    if build.get("read_error"):
        raise ConnectionPreflightError(
            "executable_unreadable", "The selected game executable could not be read", diagnostics,
            "Check the configured game directory and permission to read stellaris.exe. Build support "
            "is unknown until its hash can be read; do not change the native adapter or retry in a loop.")
    if not build["supported"]:
        raise ConnectionPreflightError(
            "unsupported_game_build", "Unsupported game executable", diagnostics,
            "This executable needs a verified native adapter for its exact build. Rebuilding the knowledge "
            "bundle or changing the hash allowlist is insufficient. Keep the current game and save; "
            "report the installed and supported versions without further probes or automatic retries.")
    if lock.get("present") is True or lock.get("read_error"):
        raise ConnectionPreflightError(
            "controller_lock_present", "The existing controller lock requires an ownership review", diagnostics,
            "Report the lock's session and controller PID when known. Its owner liveness is unknown; "
            "do not delete it or start another controller. Review ownership locally before reconnecting.")
