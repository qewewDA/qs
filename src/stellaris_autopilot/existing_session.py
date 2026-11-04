"""Keep MCP discovery independent of game attachment and expensive knowledge loading."""
from __future__ import annotations

import json
from pathlib import Path
import threading
import time

from .attached_runtime import connect_existing, resolve_user_dir
from .campaign import CampaignError, _atomic
from .connection_cancel import ConnectionCancelled, check_cancelled
from .direct import load_direct_config
from .knowledge import GameKnowledge


_METHODS = frozenset({
    "observe", "request_observation", "probe", "execute", "issue_order", "select_research",
    "queue_district", "queue_building", "set_paused", "queue_ship", "queue_colony_ship",
    "planet_command", "fleet_command",
})


class ExistingGameSession:
    """Publish a runtime only after verification; serialize its entire useful lifetime."""

    def __init__(self, game_dir: Path, state_root: Path, *, user_dir: Path | None = None,
                 pid: int | None = None, extended: bool = False, knowledge_bundle: Path | None = None,
                 timeout_seconds: float = 45):
        if type(timeout_seconds) not in (int, float) or not 5 <= timeout_seconds <= 120:
            raise ValueError("Connection timeout must be between 5 and 120 seconds")
        self.game_dir, self.state_root = Path(game_dir), Path(state_root)
        self.user_dir, self.pid, self.extended = user_dir, pid, extended
        self.knowledge_bundle, self.timeout_seconds = knowledge_bundle, timeout_seconds
        self._runtime = self._knowledge = None
        self._operation = threading.RLock()
        self._state_lock = threading.Lock()
        self._knowledge_lock = threading.Lock()
        self._state, self._phase_name = "disconnected", "mcp_ready"
        self._started = None
        self._elapsed = 0.0
        self._error = self._summary = None
        self._closed = False
        self._attempt_cancel = None

    def _phase(self, name: str):
        check_cancelled(self._attempt_cancel, name)
        with self._state_lock:
            self._phase_name = name
        _atomic(self.state_root / "connection-progress.json",
                (json.dumps(self.status(), ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def status(self) -> dict:
        """Read progress and, when idle, check only the handles we already hold."""
        if self._operation.acquire(blocking=False):
            try:
                self._refresh_liveness()
            finally:
                self._operation.release()
        with self._state_lock:
            elapsed = time.monotonic() - self._started if self._state == "connecting" else self._elapsed
            state = ("cancelling" if self._state == "connecting" and self._attempt_cancel is not None
                     and self._attempt_cancel.is_set() else self._state)
            return {"connection_mode": "existing_game", "mcp_ready": True,
                    "connected": state == "connected", "state": state,
                    "phase": self._phase_name, "elapsed_seconds": round(elapsed, 3),
                    "owns_game_process": False, "closes_game_on_disconnect": False,
                    "summary": self._summary, "last_error": self._error,
                    "knowledge_loaded": self._knowledge is not None,
                    "next_tool": None if self._state == "connected" else "connect_game"}

    def _refresh_liveness(self):
        """Called under the lifecycle lock; detecting a stale runtime never cleans it up."""
        runtime = self._runtime
        if self._closed or runtime is None or self._state in {"stale", "connecting", "failed"}:
            return
        phase = reason = None
        try:
            if runtime.closed:
                phase, reason = "connection_lost", "The previous game connection has been released."
            elif runtime.process.poll() is not None:
                phase, reason = "game_exited", "The connected game has exited."
            elif getattr(runtime.native, "poisoned", False):
                phase, reason = "connection_lost", "The native command connection is no longer usable."
            elif (getattr(runtime.native, "process", None) is not None
                  and runtime.native.process.poll() is not None):
                phase, reason = "connection_lost", "The native command worker has exited."
        except (RuntimeError, OSError, ValueError):
            phase, reason = "connection_lost", "The connected game's process identity can no longer be verified."
        if phase is not None:
            with self._state_lock:
                self._state, self._phase_name = "stale", phase
                self._summary = None
                self._error = {
                    "phase": phase, "reason": reason,
                    "next_step": "Open the intended save, pause it, then call connect_game(retry=true) once.",
                }

    def _require(self):
        self._refresh_liveness()
        if self._state == "stale":
            raise CampaignError(
                "The game connection is stale. Open a paused save, then call connect_game(retry=true) once.")
        if (self._closed or self._state in {"connecting", "failed"}
                or self._runtime is None or self._runtime.closed):
            raise CampaignError("Game is not connected. Call attached_status, then connect_game once.")
        return self._runtime

    def connect_game(self, *, retry: bool = False, _cancel_event: threading.Event | None = None) -> dict:
        if type(retry) is not bool:
            raise ValueError("retry must be boolean")
        if not self._operation.acquire(blocking=False):
            return {"status": "busy", **self.status(), "retry_automatically": False}
        try:
            if self._closed:
                return {"status": "blocked", "code": "server_closed", "retry_automatically": False}
            self._refresh_liveness()
            if self._runtime is not None and self._state != "stale" and self._error is None:
                self._require()
                return {"status": "connected", **self.status(), "already_connected": True}
            if self._error is not None and not retry:
                return {"status": "blocked", **self.status(), "cached_failure": True,
                        "retry_automatically": False,
                        "next_step": "Report the reason. Retry only after the user corrects it, with retry=true."}
            with self._state_lock:
                self._state, self._phase_name = "connecting", "resolve_user_directory"
                self._started, self._error = time.monotonic(), None
                self._summary = None
                self._attempt_cancel = _cancel_event
            runtime = None
            try:
                if self._runtime is not None:
                    self._phase("release_previous_session")
                    self._runtime.close()
                    self._runtime = None
                self._phase("resolve_user_directory")
                user_dir = resolve_user_dir(self.game_dir, self.user_dir)
                options = {"cancel_event": _cancel_event} if _cancel_event is not None else {}
                runtime = connect_existing(self.game_dir / "stellaris.exe", self.state_root,
                                           user_dir=user_dir, pid=self.pid, extended=self.extended,
                                           timeout_seconds=self.timeout_seconds, progress=self._phase, **options)
                check_cancelled(_cancel_event, "publish_connection")
            except (ValueError, RuntimeError, OSError) as exc:
                cleanup_error = None
                if runtime is not None:
                    try:
                        runtime.close()
                    except (ValueError, RuntimeError, OSError) as cleanup:
                        self._runtime = runtime
                        cleanup_error = str(cleanup)[:200]
                reason = str(exc)[:600]
                if cleanup_error is not None:
                    reason += "; cleanup failed: " + cleanup_error
                with self._state_lock:
                    self._elapsed = time.monotonic() - self._started
                    self._state = "failed"
                    self._error = {"phase": self._phase_name, "reason": reason}
                    if isinstance(exc, ConnectionCancelled):
                        self._error["code"] = "cancelled"
                        self._phase_name = "cancelled"
                return {"status": "blocked", **self.status(), "retry_automatically": False,
                        "next_step": "Report this reason once. Do not start probes, edit client code, or retry in a loop."}
            self._runtime = runtime
            with self._state_lock:
                self._state, self._phase_name = "connected", "ready"
                self._elapsed = time.monotonic() - self._started
                self._summary = runtime.initial_summary
            snapshot = self.status()
            return {"status": "connected" if snapshot["connected"] else "blocked", **snapshot,
                    "initial_observation_verified": True}
        finally:
            self._operation.release()

    def _release_cancelled_attempt(self, event: threading.Event) -> None:
        """Also handle cancellation after initialization finished but before its reply was delivered."""
        with self._operation:
            if self._attempt_cancel is not event or self._runtime is None:
                return
            try:
                self._runtime.close()
            except (ValueError, RuntimeError, OSError) as exc:
                error = "Cancellation cleanup failed: " + str(exc)[:500]
            else:
                self._runtime = None
                error = "Connection cancelled before its result was delivered; the game remains running."
            with self._state_lock:
                self._state, self._phase_name = "failed", "cancelled"
                self._summary = None
                self._error = {"phase": "cancelled", "reason": error, "code": "cancelled"}

    async def connect_game_async(self, *, retry: bool = False) -> dict:
        """Propagate MCP cancellation to the host, and join cleanup before the request ends."""
        import asyncio
        import anyio

        event = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(self.connect_game, retry=retry, _cancel_event=event))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            event.set()
            # MCP uses AnyIO cancellation scopes. Shield cleanup from that scope,
            # and keep explicit asyncio task cancellation from abandoning the host.
            with anyio.CancelScope(shield=True):
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                try:
                    task.result()
                finally:
                    cleanup = asyncio.create_task(asyncio.to_thread(self._release_cancelled_attempt, event))
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            continue
                    cleanup.result()
            raise

    def context(self):
        if not self._operation.acquire(blocking=False):
            raise CampaignError("Connection is busy. Use attached_status; do not start another connection.")
        try:
            runtime = self._require()
            return load_direct_config(runtime.config_path), runtime.native
        finally:
            self._operation.release()

    def knowledge(self) -> GameKnowledge:
        with self._knowledge_lock:
            if self._knowledge is None:
                if self.knowledge_bundle is None:
                    raise CampaignError("No local knowledge bundle was configured")
                self._knowledge = GameKnowledge.load(self.knowledge_bundle)
            return self._knowledge

    def __getattr__(self, name: str):
        if name not in _METHODS:
            raise AttributeError(name)

        def call(*args, **kwargs):
            if not self._operation.acquire(blocking=False):
                return {"status": "busy", "reason": "Another operation owns the connection; this request was not queued"}
            try:
                return getattr(self._require().connection, name)(*args, **kwargs)
            finally:
                self._operation.release()
        return call

    def run_plan(self, callback) -> dict:
        if not self._operation.acquire(blocking=False):
            return {"status": "busy", "reason": "Another operation owns the connection; this plan was not queued"}
        try:
            return self._require().connection.run_plan(callback)
        finally:
            self._operation.release()

    def checkpoint_game(self) -> dict:
        with self._operation:
            return self._require().checkpoint_game()

    def disconnect_game(self, *, save: bool = True) -> dict:
        if type(save) is not bool:
            raise ValueError("save must be boolean")
        with self._operation:
            runtime = self._runtime
            if runtime is None or self._closed:
                raise CampaignError("No game connection exists")
            self._refresh_liveness()
            if save and self._state == "stale":
                raise CampaignError(
                    "The previous connection is stale; no checkpoint was saved. "
                    "Use disconnect_game(save=false) to release it, or connect_game(retry=true) "
                    "after opening a paused save.")
            result = runtime.disconnect_game(save=save)
            self._runtime = None
            with self._state_lock:
                self._state, self._phase_name = "disconnected", "mcp_ready"
                self._summary, self._error = None, None
            return result

    def close(self):
        with self._operation:
            self._closed = True
            if self._runtime is not None:
                self._runtime.close()
                self._runtime = None
            with self._state_lock:
                self._state, self._phase_name = "closed", "closed"
                self._summary = None
