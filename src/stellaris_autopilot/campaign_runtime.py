"""Paused checkpoints and reload gates for a launcher-owned campaign game.

The caller owns the campaign lock for this runtime's entire lifetime. Model
tools share one connection so the first write cannot bypass reload validation.
"""
from __future__ import annotations

from pathlib import Path
import json
import threading
import time
import uuid

from .campaign import CampaignError, CampaignStore
from .direct import disarm_inbox, load_direct_config
from .direct_mcp import DirectConnection, _controller_lock, validate_connection
from .direct_observe import DirectObserver
from .native import PINNED_EXE_SHA256


def validate_format_proof(path: Path | None, manifest: dict) -> bool:
    """Accept only a bounded, correlated local report from two actual reloads."""
    if path is None:
        return False
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return False
        proof = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(proof, dict):
        return False
    packaged, cycles, cleanup = proof.get("packaged"), proof.get("cycles"), proof.get("cleanup")
    if (proof.get("verified") is not True or proof.get("exe_sha256") != PINNED_EXE_SHA256
            or proof.get("keyboard_mouse_input") is not False
            or proof.get("game_version") != manifest["game_version"]
            or proof.get("checkpoint_format") != "zip-meta-gamestate-from-debug-dump"
            or not isinstance(packaged, dict) or not isinstance(cleanup, dict)
            or packaged.get("campaign_id") != manifest["campaign_id"]
            or packaged.get("country_id") != manifest["country_id"]
            or packaged.get("game_version") != manifest["game_version"]
            or cleanup.get("owned_game_closed") is not True or cleanup.get("redirect_removed") is not True
            or cleanup.get("unresolved") != []
            or not isinstance(cycles, list) or len(cycles) < 2):
        return False
    loaded = set()
    previous = None
    for item in cycles:
        if not isinstance(item, dict):
            return False
        saved = item.get("save_and_close")
        if (item.get("reload_verified") is not True or not isinstance(saved, dict)
                or saved.get("status") != "saved_and_closed" or saved.get("owned_process_closed") is not True
                or not isinstance(saved.get("checkpoint"), dict)
                or saved["checkpoint"].get("parse_verified") is not True
                or item.get("campaign_id") != manifest["campaign_id"]
                or item.get("country_id") != manifest["country_id"]
                or not isinstance(item.get("expected_date"), str)
                or item.get("observed_date") != item["expected_date"]
                or not isinstance(item.get("loaded_checkpoint"), str)
                or item["loaded_checkpoint"] in loaded
                or not isinstance(item.get("expected_state_sha256"), str)
                or len(item["expected_state_sha256"]) != 64
                or item.get("observed_state_sha256") != item["expected_state_sha256"]):
            return False
        if previous is not None and (item["loaded_checkpoint"] != previous.get("id")
                                     or item.get("loaded_sha256") != previous.get("sha256")):
            return False
        loaded.add(item["loaded_checkpoint"])
        previous = saved["checkpoint"]
    return True


def remove_owned_redirect(store: CampaignStore, game_dir: Path):
    redirect = game_dir / "userdir.txt"
    expected = (store.root / "userdir").as_posix() + "/"
    if redirect.exists() and redirect.read_text(encoding="utf-8") == expected:
        redirect.unlink()


class CampaignConnection(DirectConnection):
    """Confirm this session loaded its exact checkpoint before any gameplay write."""

    def __init__(self, config_path, *, store, session, native_bridge=None):
        super().__init__(config_path, native_bridge=native_bridge)
        self.store, self.session = store, session
        self._reload_confirmed = False
        self._reload_mutex = threading.Lock()

    def _confirm_reload(self):
        with self._reload_mutex:
            if self._reload_confirmed:
                return
            config = load_direct_config(self.config_path)
            observation = self._read(DirectObserver(config.dump_path, config.country_id))
            validate_connection(config, observation)
            self.store.verify_reload(self.session["loaded_checkpoint"], observation)
            self._reload_confirmed = True

    def request_observation(self) -> dict:
        result = super().request_observation()
        if result.get("status") == "observed":
            self._confirm_reload()
        return result

    def _execute(self, *args, **kwargs) -> dict:
        # Keep initialize fast while the game loads. The first fresh observation
        # or gameplay write performs the gate instead of blocking the handshake.
        self._confirm_reload()
        return super()._execute(*args, **kwargs)


class CampaignRuntime:
    def __init__(self, store, session, config_path, process, native, *, format_verified=False,
                 game_dir: Path):
        self.store, self.session, self.config_path = store, session, config_path
        self.process, self.native = process, native
        self.connection = CampaignConnection(config_path, store=store, session=session, native_bridge=native)
        self.format_verified = format_verified
        self.game_dir = game_dir
        self.mutex = threading.Lock()

    def checkpoint(self, *, close=False, trial_only=False):
        """Pause, lock out gameplay writers, request exact fresh bytes, then save."""
        with self.mutex:
            if self.process.poll() is not None:
                raise CampaignError("The owned game exited before a verified save")
            refreshed = self.connection.request_observation()
            if refreshed.get("status") != "observed":
                raise CampaignError("Cannot refresh the current campaign before saving")
            pause = self.connection.set_paused(True)
            if pause.get("status") != "pause_verified":
                raise CampaignError("Could not verify pause before saving")
            config = load_direct_config(self.config_path)
            with _controller_lock(config):
                if self.store.unresolved():
                    raise CampaignError("Gameplay commands remain unresolved; save/exit is blocked")
                token, published_at = uuid.uuid4().hex, time.time()
                query_finished = False
                try:
                    self.native.request_dump(token)
                    deadline = time.monotonic() + config.verification_timeout_seconds
                    observer = DirectObserver(config.dump_path, config.country_id)
                    while time.monotonic() < deadline:
                        result = self.native.result(token)
                        try:
                            observation = observer.read()
                            validate_connection(config, observation)
                            runtime = result.get("runtime", {})
                            if (observation.modified_at >= published_at
                                    and result.get("status") == "dump_requested"
                                    and result.get("kind") == "request_observation"
                                    and result.get("country_id") == config.country_id
                                    and result.get("token") == token
                                    and result.get("execution_source") == "window_message"
                                    and runtime.get("paused") is True
                                    and type(runtime.get("protected_pause")) is bool):
                                break
                        except (ValueError, OSError):
                            pass
                        if self.process.poll() is not None:
                            raise CampaignError("The owned game exited during save")
                        time.sleep(config.poll_seconds)
                    else:
                        raise CampaignError("No fresh paused native snapshot before save deadline")
                    saved = self.store.checkpoint_dump(
                        config.dump_path, session_token=self.session["token"],
                        expected_digest=observation.digest, expected_date=observation.state["date"])
                    self.native.cancel(token)
                    query_finished = True
                    if close:
                        if not self.format_verified and not trial_only:
                            raise CampaignError(
                                "Parsed checkpoint retained and game paused; closing requires a real-game "
                                "reload proof for this format/build")
                        (config.state_dir / "STOP").touch()
                        disarm_inbox(config)
                        # Close only the exact held process handle; never reopen a
                        # PID retained in a previous session's manifest.
                        self.process.terminate()
                        self.process.wait(timeout=20)
                        self.store.finish_session(self.session["token"])
                        remove_owned_redirect(self.store, self.game_dir)
                    return {"status": "saved_and_closed" if close else "checkpoint_verified",
                            "checkpoint": saved, "format_reload_proof_attached": self.format_verified,
                            "owned_process_closed": close}
                finally:
                    if not query_finished and self.process.poll() is None:
                        self.native.cancel(token)
                    disarm_inbox(config)
