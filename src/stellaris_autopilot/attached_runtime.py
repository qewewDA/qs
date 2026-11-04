"""Connect to the currently loaded game; disconnect never owns or closes it."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
import uuid

from .attached_process import attach_game
from .campaign import CampaignError, _atomic, package_dump
from .connection_cancel import check_cancelled
from .direct import (NATIVE_ACTIONS, ORDER_KINDS, OWNERSHIP_HEADER, DirectConfig,
                     disarm_inbox, load_direct_config, ownership_header)
from .direct_mcp import DirectConnection, _controller_lock, validate_connection
from .direct_observe import DirectObserver
from .direct_orders import render_target_events
from .mcp_server import _PreviewJournal
from .native import NativeResearchBridge


def resolve_user_dir(game_dir: Path, explicit: Path | None = None) -> Path:
    if explicit is not None:
        result = Path(explicit).resolve(strict=True)
    elif (Path(game_dir) / "userdir.txt").is_file():
        value = (Path(game_dir) / "userdir.txt").read_text(encoding="utf-8-sig").strip()
        if not value or any(c in value for c in "\x00\r\n"):
            raise CampaignError("The game user directory redirect is invalid")
        result = Path(value)
        if not result.is_absolute():
            result = Path(game_dir) / result
        result = result.resolve(strict=True)
    else:
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) != 0:
            raise CampaignError("Cannot locate Documents; pass --user-dir locally")
        result = (Path(buffer.value) / "Paradox Interactive" / "Stellaris").resolve(strict=True)
    if not result.is_dir():
        raise CampaignError("The selected game user directory does not exist")
    return result


class AttachedRuntime:
    def __init__(self, process, native, user_dir: Path, state_dir: Path, token: str, *, extended=False,
                 deadline=None, progress=None, cancel_event=None):
        self.process, self.native = process, native
        self.user_dir, self.state_dir, self.token = Path(user_dir), Path(state_dir), token
        self.config_path = self.state_dir / "direct.json"
        self.connection = None
        self.closed = False
        self.mutex = threading.Lock()
        self.latest_checkpoint = None
        self._lock_file = self.user_dir / ".qs_existing_controller.lock"
        self._lock_descriptor = None
        self._old_inbox = None
        self._inbox_owned = False
        self._events = None
        self._extended = extended
        self._deadline, self._progress = deadline, progress
        self._cancel_event = cancel_event
        self.initial_summary = None

    def _phase(self, phase: str):
        check_cancelled(self._cancel_event, phase)
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise CampaignError("Connection deadline exceeded at " + phase)
        if self._progress is not None:
            self._progress(phase)

    def _until(self, seconds: float) -> float:
        deadline = time.monotonic() + seconds
        return min(deadline, self._deadline) if self._deadline is not None else deadline

    def _wait(self, token: str, kind: str, expected: str, *, timeout=120) -> dict:
        deadline = self._until(timeout)
        while time.monotonic() < deadline:
            check_cancelled(self._cancel_event, "wait_native_operation")
            if self.process.poll() is not None:
                raise CampaignError("The attached game exited")
            result = self.native.result(token)
            if result.get("status") not in ("pending", "processing", "unknown"):
                if (result.get("status") != expected or result.get("kind") != kind
                        or result.get("token") != token or result.get("country_id") != "0"
                        or result.get("execution_source") != "window_message"):
                    raise CampaignError("The attached native operation did not verify; inspect local logs")
                return result
            time.sleep(0.1)
        raise CampaignError("Attached game operation timed out; do not replay it")

    def _fresh(self):
        token, started = uuid.uuid4().hex, time.time()
        self.native.request_dump(token)
        result = self._wait(token, "request_observation", "dump_requested")
        deadline = self._until(120)
        observer = DirectObserver(self.user_dir / "save games" / "dump_qs_direct.sav.txt", "0")
        while time.monotonic() < deadline:
            check_cancelled(self._cancel_event, "read_current_save")
            if self.process.poll() is not None:
                raise CampaignError("The attached game exited")
            try:
                observed = observer.read()
            except ValueError:
                pass
            else:
                check_cancelled(self._cancel_event, "read_current_save")
                if observed.modified_at >= started:
                    return observed, result
            time.sleep(0.2)
        raise CampaignError("No fresh matching dump in the selected user directory")

    def _write_inbox(self, config: DirectConfig, text: str):
        _atomic(config.inbox_path, (ownership_header(config) + text).encode("utf-8"))
        self._inbox_owned = True

    def initialize(self):
        self._phase("acquire_controller")
        self._lock_descriptor = os.open(self._lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(self._lock_descriptor, json.dumps({"controller_pid": os.getpid(), "session": self.token}).encode())
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._phase("bind_game_window")
        self.native.bind_existing_window(uuid.uuid4().hex)
        self._phase("read_current_save")
        observed, receipt = self._fresh()
        meta = observed.state.get("metadata", {})
        if (observed.state.get("country_id") != "0" or meta.get("player_country_ids") != ["0"]
                or meta.get("ironman") not in (False, "no") or meta.get("multiplayer") in (True, "yes")
                or meta.get("game_version") != "Cygnus v4.5.2" or not meta.get("campaign_id")):
            raise CampaignError("Load the supported single-player non-Ironman country 0 before connecting")
        if receipt.get("runtime", {}).get("paused") is not True:
            raise CampaignError("Pause the loaded game before connecting")
        # Preserve the live state before loading any session-specific bridge definitions.
        self._phase("backup_current_save")
        package_dump(self.user_dir / "save games" / "dump_qs_direct.sav.txt",
                     self.state_dir / "before-attachment.sav", "0", expected_digest=observed.digest)
        self._phase("prepare_session")
        config = DirectConfig(
            user_dir=self.user_dir, state_dir=self.state_dir, country_id="0", campaign_id=meta["campaign_id"],
            verified_game_version=meta["game_version"], session_token=self.token,
            allowed_actions=("probe_connection",),
            allowed_orders=tuple(sorted(ORDER_KINDS)) if self._extended else ("fleet_wait", "fleet_orbit"),
            allowed_native_actions=tuple(sorted(NATIVE_ACTIONS)) if self._extended else
            ("select_research", "queue_district", "queue_building", "set_paused"),
            single_player_confirmed=True, non_ironman_confirmed=True,
            verification_timeout_seconds=120, poll_seconds=0.25,
            event_namespace="qs_live_" + self.token)
        raw = {key: str(value) if isinstance(value, Path) else value for key, value in vars(config).items()}
        _atomic(self.config_path, json.dumps(raw, indent=2).encode("utf-8"))
        if config.inbox_path.exists():
            old = config.inbox_path.read_bytes()
            lines = old.decode("utf-8-sig").splitlines()
            if (len(lines) != 2 or not lines[0].startswith(OWNERSHIP_HEADER + " ")
                    or lines[1] not in ("# disarmed", "debug_dumpgamestate qs_direct")):
                raise CampaignError("The existing command inbox is active or not owned by this project")
            self._old_inbox = old
        with _controller_lock(config):
            self._phase("load_session_events")
            events = render_target_events(self.token, namespace=config.event_namespace)
            events += (
                f"\ncountry_event = {{ id = {config.event_namespace}.99 hide_window = yes is_triggered_only = yes\n"
                " immediate = { if = { limit = { is_ai = no is_multiplayer = no is_ironman = no }\n"
                f" set_country_flag = qs_direct_session_{self.token}\n }} }} }}\n")
            path = self.user_dir / ("qs_attach_events_" + self.token + ".txt")
            with path.open("xb") as stream:
                stream.write(events.encode("utf-8"))
            self._events = (path, events.encode("utf-8"))
            self.native.load_attached_events(self.token)
            self._wait(self.token, "load_attached_events", "events_load_returned")
            self._phase("bind_current_session")
            self._write_inbox(config, f"event {config.event_namespace}.99 0\nevent {config.event_namespace}.3 0\n")
            token = uuid.uuid4().hex
            self.native.run_inbox(token)
            self._wait(token, "run_inbox", "inbox_executed")
            self._phase("verify_current_session")
            observed, receipt = self._fresh()
            validate_connection(config, observed)
            if (f"qs_direct_targets_{self.token}" not in observed.state["direct_bridge"]["target_flags"]
                    or receipt.get("runtime", {}).get("paused") is not True):
                raise CampaignError("Runtime event targets or pause were not verified")
            disarm_inbox(config)
        self.connection = DirectConnection(self.config_path, native_bridge=self.native)
        self.initial_summary = {
            "country_id": observed.state["country_id"], "date": observed.state["date"],
            "campaign_id": config.campaign_id, "game_version": config.verified_game_version,
            "snapshot": observed.digest, "paused": True,
            "planet_count": len(observed.state.get("planets_by_id", {})),
            "fleet_count": len(observed.state.get("fleets_by_id", {})),
            "allowed_orders": list(config.allowed_orders),
            "allowed_native_actions": list(config.allowed_native_actions),
        }
        _atomic(self.state_dir / "attached.json", json.dumps({
            "session": self.token, "pid": self.process.pid, "campaign_id": config.campaign_id,
            "date": observed.state["date"], "game_started": False, "connection_mode": "existing_game",
            "engine_single_player_guard_verified": True}, indent=2).encode())
        self._phase("connected")
        if self._deadline is not None or self._cancel_event is not None:
            self.native.finish_initialization()
        self._deadline, self._progress, self._cancel_event = None, None, None
        return self

    def status(self) -> dict:
        return {"connection_mode": "existing_game", "connected": not self.closed,
                "game_still_running": None if self.closed else self.process.poll() is None,
                "owns_game_process": False, "closes_game_on_disconnect": False,
                "latest_checkpoint": self.latest_checkpoint,
                "unresolved": _PreviewJournal(self.state_dir / "journal.sqlite").unresolved()}

    def checkpoint_game(self) -> dict:
        with self.mutex:
            if self.closed or self.connection is None:
                raise CampaignError("The existing-game connection is closed")
            if self.connection.request_observation().get("status") != "observed":
                raise CampaignError("Cannot refresh before saving the attached game")
            if self.connection.set_paused(True).get("status") != "pause_verified":
                raise CampaignError("Cannot verify pause before saving")
            config = load_direct_config(self.config_path)
            with _controller_lock(config):
                if _PreviewJournal(self.state_dir / "journal.sqlite").unresolved():
                    raise CampaignError("Unresolved operations block the checkpoint; inspect them first")
                observed, receipt = self._fresh()
                validate_connection(config, observed)
                if receipt.get("runtime", {}).get("paused") is not True:
                    raise CampaignError("Pause was not verified")
                destination = self.user_dir / "save games" / "qs_mcp" / (
                    observed.state["date"] + "-" + uuid.uuid4().hex[:12] + ".sav")
                saved = package_dump(config.dump_path, destination, config.country_id,
                                     expected_digest=observed.digest)
                self.latest_checkpoint = {**saved, "path": str(destination)}
                _atomic(self.state_dir / "last-checkpoint.json", json.dumps(self.latest_checkpoint, indent=2).encode())
                return {"status": "checkpoint_saved", "checkpoint": self.latest_checkpoint,
                        "game_still_running": self.process.poll() is None, "game_closed": False}

    def disconnect_game(self, *, save: bool = True) -> dict:
        if type(save) is not bool:
            raise ValueError("save must be boolean")
        checkpoint = self.checkpoint_game() if save else None
        running = self.process.poll() is None
        self.close()
        return {"status": "disconnected", "checkpoint": checkpoint,
                "game_closed": False, "game_still_running": running, "saved": checkpoint is not None}

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self._inbox_owned:
                config = load_direct_config(self.config_path)
                with _controller_lock(config):
                    if config.inbox_path.read_text(encoding="utf-8").startswith(ownership_header(config)):
                        (self.state_dir / "STOP").touch()
                        disarm_inbox(config)
                        if self._old_inbox is not None:
                            _atomic(config.inbox_path, self._old_inbox)
            if self._events is not None:
                path, content = self._events
                if path.is_file() and path.read_bytes() == content:
                    path.unlink()
        finally:
            try:
                self.native.close()
            finally:
                self.process.close()
                if self._lock_descriptor is not None:
                    os.close(self._lock_descriptor)
                    self._lock_descriptor = None
                    if self._lock_file.exists():
                        record = json.loads(self._lock_file.read_text(encoding="utf-8"))
                        if record.get("session") == self.token:
                            self._lock_file.unlink()


def connect_existing(executable: Path, state_root: Path, *, user_dir: Path, pid: int | None = None,
                     extended: bool = False, timeout_seconds: float | None = None,
                     progress=None, cancel_event: threading.Event | None = None) -> AttachedRuntime:
    if timeout_seconds is not None and (type(timeout_seconds) not in (int, float)
                                       or not 5 <= timeout_seconds <= 120):
        raise ValueError("Connection timeout must be between 5 and 120 seconds")
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None

    def phase(name):
        check_cancelled(cancel_event, name)
        if deadline is not None and time.monotonic() >= deadline:
            raise CampaignError("Connection deadline exceeded at " + name)
        if progress is not None:
            progress(name)

    phase("check_unresolved_operations")
    state_root = Path(state_root).resolve()
    for path in state_root.glob("sessions/*/journal.sqlite"):
        if _PreviewJournal(path).unresolved():
            raise CampaignError("An earlier attached operation is unresolved; inspect it before reconnecting")
    phase("verify_running_process")
    process_options = {"deadline": deadline} if deadline is not None else {}
    if cancel_event is not None:
        process_options["cancel_event"] = cancel_event
    process = attach_game(executable, pid=pid, **process_options)
    native = runtime = None
    try:
        token = uuid.uuid4().hex
        state_dir = state_root / "sessions" / token
        phase("start_native_worker")
        native_options = {"initialization_deadline": deadline} if deadline is not None else {}
        if cancel_event is not None:
            native_options["cancel_event"] = cancel_event
        native = NativeResearchBridge(process, executable, state_dir, **native_options)
        options = {"extended": extended}
        if deadline is not None or progress is not None:
            options.update(deadline=deadline, progress=progress)
        if cancel_event is not None:
            options["cancel_event"] = cancel_event
        runtime = AttachedRuntime(process, native, user_dir, state_dir, token, **options)
        return runtime.initialize()
    except BaseException:
        if runtime is not None:
            runtime.close()
        else:
            if native is not None:
                native.close()
            process.close()
        raise
