"""Lifecycle tests for an attached MCP session without a real game or native worker."""
from __future__ import annotations

import threading
import time
import json
from types import SimpleNamespace

import pytest

from stellaris_autopilot import existing_session
from stellaris_autopilot.campaign import CampaignError
from stellaris_autopilot.existing_session import ExistingGameSession


class _Process:
    def __init__(self):
        self.code = None
        self.error = None
        self.polls = 0

    def poll(self):
        self.polls += 1
        if self.error is not None:
            raise self.error
        return self.code


class _Runtime:
    def __init__(self, root, *, process=None, native=None):
        self.closed = False
        self.process = process or _Process()
        self.native = native or SimpleNamespace(poisoned=False, process=None)
        self.config_path = root / "direct.json"
        self.connection = SimpleNamespace()
        self.initial_summary = {
            "country_id": "0",
            "date": "2200.01.01",
            "campaign_id": "campaign-a",
            "snapshot": "digest-a",
            "paused": True,
        }
        self.close_calls = 0
        self.checkpoint_calls = 0
        self.disconnect_calls = []

    def close(self):
        self.closed = True
        self.close_calls += 1

    def checkpoint_game(self):
        self.checkpoint_calls += 1
        return {"status": "checkpoint_saved"}

    def disconnect_game(self, *, save=True):
        self.disconnect_calls.append(save)
        return {"status": "disconnected", "saved": save, "game_closed": False}


def _session(tmp_path, **kwargs):
    return ExistingGameSession(tmp_path / "game", tmp_path / "state", **kwargs)


def _install_connector(monkeypatch, runtime, calls, resolves):
    def resolve(game_dir, user_dir):
        resolves.append(1)
        return game_dir

    def connect(executable, state_root, *, user_dir, pid, extended, timeout_seconds, progress):
        calls.append(1)
        return runtime

    monkeypatch.setattr(existing_session, "resolve_user_dir", resolve)
    monkeypatch.setattr(existing_session, "connect_existing", connect)


@pytest.mark.parametrize(
    ("failure", "phase"),
    [("exit", "game_exited"), ("handle", "connection_lost"), ("released", "connection_lost")],
)
def test_status_marks_stale_without_cleanup_or_reattach(tmp_path, monkeypatch, failure, phase):
    runtime = _Runtime(tmp_path)
    session = _session(tmp_path)
    calls, resolves = [], []
    _install_connector(monkeypatch, runtime, calls, resolves)

    assert session.connect_game()["status"] == "connected"
    if failure == "exit":
        runtime.process.code = 7
    elif failure == "handle":
        runtime.process.error = OSError("handle released")
    else:
        runtime.closed = True

    status = session.status()

    assert status["state"] == "stale"
    assert status["connected"] is False
    assert status["summary"] is None
    assert status["last_error"]["phase"] == phase
    persisted = json.loads((session.state_root / "connection-progress.json").read_text(encoding="utf-8"))
    assert persisted["state"] == "stale"
    assert persisted["summary"] is None
    assert persisted["last_error"] == status["last_error"]
    assert session._runtime is runtime
    assert runtime.close_calls == 0
    assert len(calls) == 1
    assert len(resolves) == 1
    polls = runtime.process.polls
    session.status()
    assert runtime.process.polls == polls


def test_status_returns_cached_state_while_lifecycle_lock_is_held_elsewhere(tmp_path):
    runtime = _Runtime(tmp_path)
    session = _session(tmp_path)
    session._runtime = runtime
    session._state = "connected"
    session._summary = runtime.initial_summary
    entered = threading.Event()
    release = threading.Event()

    def hold_lifecycle_lock():
        with session._operation:
            entered.set()
            release.wait(2)

    thread = threading.Thread(target=hold_lifecycle_lock, daemon=True)
    thread.start()
    assert entered.wait(2)
    started = time.perf_counter()
    status = session.status()
    elapsed = time.perf_counter() - started
    release.set()
    thread.join(2)

    assert elapsed < 1.0
    assert status["state"] == "connected"
    assert status["summary"] == runtime.initial_summary
    assert runtime.process.polls == 0


def test_stale_connect_without_retry_does_not_reattach(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path)
    session = _session(tmp_path)
    calls, resolves = [], []
    _install_connector(monkeypatch, runtime, calls, resolves)

    assert session.connect_game()["status"] == "connected"
    runtime.process.code = 7

    blocked = session.connect_game()

    assert blocked["status"] == "blocked"
    assert blocked["state"] == "stale"
    assert blocked["cached_failure"] is True
    assert blocked["retry_automatically"] is False
    assert len(calls) == 1
    assert runtime.close_calls == 0
    assert session._runtime is runtime


def test_retry_releases_old_runtime_before_connecting_new(tmp_path, monkeypatch):
    old, new = _Runtime(tmp_path), _Runtime(tmp_path)
    session = _session(tmp_path)
    runtimes = [old, new]
    events = []

    def resolve(game_dir, user_dir):
        return game_dir

    def connect(executable, state_root, *, user_dir, pid, extended, timeout_seconds, progress):
        events.append("connect")
        return runtimes.pop(0)

    monkeypatch.setattr(existing_session, "resolve_user_dir", resolve)
    monkeypatch.setattr(existing_session, "connect_existing", connect)
    assert session.connect_game()["status"] == "connected"
    events.clear()
    old.process.code = 7

    def close_old():
        events.append("close_old")
        old.closed = True
        old.close_calls += 1

    old.close = close_old
    result = session.connect_game(retry=True)

    assert result["status"] == "connected"
    assert result["summary"] == new.initial_summary
    assert events == ["close_old", "connect"]
    assert old.closed is True
    assert session._runtime is new


def test_retry_cleanup_failure_does_not_establish_new_connection(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path)
    session = _session(tmp_path)
    calls, resolves = [], []
    _install_connector(monkeypatch, runtime, calls, resolves)
    assert session.connect_game()["status"] == "connected"
    runtime.process.code = 7

    def fail_close():
        raise OSError("cannot release old handle")

    runtime.close = fail_close
    blocked = session.connect_game(retry=True)

    assert blocked["status"] == "blocked"
    assert blocked["state"] == "failed"
    assert blocked["last_error"]["phase"] == "release_previous_session"
    assert blocked["retry_automatically"] is False
    assert len(calls) == 1
    assert session._runtime is runtime
    assert runtime.closed is False


def test_stale_runtime_refuses_checkpoint_and_saveful_disconnect_then_releases(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path)
    session = _session(tmp_path)
    calls, resolves = [], []
    _install_connector(monkeypatch, runtime, calls, resolves)
    assert session.connect_game()["status"] == "connected"
    runtime.process.code = 7
    assert session.status()["state"] == "stale"

    with pytest.raises(CampaignError, match="stale"):
        session.checkpoint_game()
    assert runtime.checkpoint_calls == 0

    with pytest.raises(CampaignError, match="stale"):
        session.disconnect_game(save=True)
    assert runtime.disconnect_calls == []

    result = session.disconnect_game(save=False)

    assert runtime.disconnect_calls == [False]
    assert result == {"status": "disconnected", "saved": False, "game_closed": False}
    assert session._runtime is None
    assert session.status()["state"] == "disconnected"


@pytest.mark.parametrize("failure", ["poisoned", "worker_exit"])
def test_native_worker_failure_marks_session_stale(tmp_path, monkeypatch, failure):
    native_process = _Process()
    runtime = _Runtime(tmp_path, native=SimpleNamespace(poisoned=False, process=native_process))
    session = _session(tmp_path)
    calls, resolves = [], []
    _install_connector(monkeypatch, runtime, calls, resolves)
    assert session.connect_game()["status"] == "connected"
    if failure == "poisoned":
        runtime.native.poisoned = True
    else:
        native_process.code = 5

    status = session.status()

    assert status["state"] == "stale"
    assert status["connected"] is False
    assert status["summary"] is None
    assert status["last_error"]["phase"] == "connection_lost"
    assert "native" in status["last_error"]["reason"]
    assert runtime.close_calls == 0
    assert session._runtime is runtime
