"""Cancellation and cleanup regressions for existing-game attachment; all in-memory."""
from __future__ import annotations

import asyncio
import io
import queue
import threading
import time
from types import SimpleNamespace

import anyio
import pytest

from stellaris_autopilot import existing_session
from stellaris_autopilot.connection_cancel import ConnectionCancelled
from stellaris_autopilot.existing_session import ExistingGameSession
from stellaris_autopilot import native


class _Process:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


class _Runtime:
    def __init__(self):
        self.closed = False
        self.close_calls = 0
        self.process = _Process()
        self.native = SimpleNamespace(poisoned=False, process=None)
        self.initial_summary = {"country_id": "0", "date": "2200.01.01", "paused": True}

    def close(self):
        self.closed = True
        self.close_calls += 1


def _session(tmp_path):
    return ExistingGameSession(tmp_path / "game", tmp_path / "state")


def _install_blocking_connector(monkeypatch, runtime, started, release, cancel_on_return=False):
    def resolve(game_dir, user_dir):
        return game_dir

    def connect(executable, state_root, *, user_dir, pid, extended, timeout_seconds, progress,
                cancel_event=None):
        started.set()
        assert release.wait(5)
        if cancel_on_return:
            assert cancel_event is not None
            cancel_event.set()
        return runtime

    monkeypatch.setattr(existing_session, "resolve_user_dir", resolve)
    monkeypatch.setattr(existing_session, "connect_existing", connect)


def test_asyncio_cancel_keeps_background_attach_and_waits_for_cleanup(tmp_path, monkeypatch):
    runtime = _Runtime()
    started, release = threading.Event(), threading.Event()
    _install_blocking_connector(monkeypatch, runtime, started, release)
    session = _session(tmp_path)

    async def scenario():
        task = asyncio.create_task(session.connect_game_async())
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        assert session.status()["state"] == "cancelling"
        busy = session.connect_game()
        assert busy["status"] == "busy"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    status = session.status()
    assert runtime.close_calls == 1
    assert status["state"] == "failed"
    assert status["connected"] is False
    assert status["last_error"]["code"] == "cancelled"


def test_anyio_cancel_scope_waits_for_cleanup(tmp_path, monkeypatch):
    runtime = _Runtime()
    started, release = threading.Event(), threading.Event()
    _install_blocking_connector(monkeypatch, runtime, started, release)
    session = _session(tmp_path)
    errors = []

    async def run_connection():
        try:
            await session.connect_game_async()
        except asyncio.CancelledError:
            errors.append("cancelled")

    async def scenario():
        with anyio.CancelScope() as scope:
            async with anyio.create_task_group() as group:
                group.start_soon(run_connection)
                assert await anyio.to_thread.run_sync(started.wait, 2)
                scope.cancel()
                with anyio.CancelScope(shield=True):
                    await anyio.sleep(0.05)
                    release.set()

    anyio.run(scenario)

    assert errors == ["cancelled"]
    assert runtime.close_calls == 1
    assert session.status()["state"] == "failed"


def test_cancelling_busy_request_does_not_cancel_other_attempt(tmp_path, monkeypatch):
    runtime = _Runtime()
    started, release = threading.Event(), threading.Event()
    _install_blocking_connector(monkeypatch, runtime, started, release)
    session = _session(tmp_path)

    async def scenario():
        first = asyncio.create_task(session.connect_game_async())
        assert await asyncio.to_thread(started.wait, 2)

        busy = session.connect_game()
        assert busy["status"] == "busy"

        second = asyncio.create_task(session.connect_game_async())
        await asyncio.sleep(0.05)
        second.cancel()
        try:
            await second
        except asyncio.CancelledError:
            pass

        release.set()
        result = await first
        assert result["status"] == "connected"

    asyncio.run(scenario())

    assert session._runtime is runtime
    assert session.status()["connected"] is True
    assert runtime.close_calls == 0


def test_cancelled_runtime_returned_before_publish_is_closed_and_not_connected(tmp_path, monkeypatch):
    runtime = _Runtime()
    started, release = threading.Event(), threading.Event()
    _install_blocking_connector(monkeypatch, runtime, started, release, cancel_on_return=True)
    session = _session(tmp_path)

    async def scenario():
        task = asyncio.create_task(session.connect_game_async())
        assert await asyncio.to_thread(started.wait, 2)
        release.set()
        return await task

    result = asyncio.run(scenario())

    assert result["status"] == "blocked"
    assert result["state"] == "failed"
    assert result["last_error"]["code"] == "cancelled"
    assert runtime.close_calls == 1
    assert session._runtime is None
    assert session.status()["connected"] is False


def test_cancel_after_runtime_publish_before_reply_delivery_closes_runtime(tmp_path, monkeypatch):
    runtime = _Runtime()
    started, release, published, release_status = (
        threading.Event(), threading.Event(), threading.Event(), threading.Event()
    )
    _install_blocking_connector(monkeypatch, runtime, started, release)
    session = _session(tmp_path)
    original_status = session.status

    def status_while_published():
        if session._runtime is runtime:
            published.set()
            assert release_status.wait(5)
        return original_status()

    session.status = status_while_published

    async def scenario():
        task = asyncio.create_task(session.connect_game_async())
        assert await asyncio.to_thread(started.wait, 2)
        release.set()
        assert await asyncio.to_thread(published.wait, 2)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        release_status.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    status = session.status()
    assert runtime.close_calls == 1
    assert session._runtime is None
    assert status["state"] == "failed"
    assert status["connected"] is False
    assert status["summary"] is None


class _Queue:
    def __init__(self, messages=None, on_get=None):
        self.messages = list(messages or [])
        self.on_get = on_get
        self.timeouts = []

    def get(self, block=True, timeout=None):
        self.timeouts.append(timeout)
        if self.on_get is not None:
            self.on_get()
        if not self.messages:
            raise queue.Empty
        return self.messages.pop(0)


class _Worker:
    def __init__(self):
        self.stdin = _Stdin()
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.returncode = 0
        return 0


class _Stdin:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _bridge(cancel_event, messages=None):
    result = object.__new__(native.NativeResearchBridge)
    result._deadline = None
    result._cancel_event = cancel_event
    result.messages = messages if messages is not None else _Queue()
    result.game = SimpleNamespace(poll=lambda: None)
    result.process = _Worker()
    result.lock = threading.Lock()
    result.request_id = 0
    result.poisoned = False
    result.event_log = None
    result.error_log = io.StringIO()
    return result


def test_native_reply_wait_observes_cancel_without_25_second_wait():
    event = threading.Event()
    bridge = _bridge(event, _Queue(on_get=event.set))

    started = time.perf_counter()
    with pytest.raises(ConnectionCancelled, match="cancelled"):
        bridge._read()
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0
    assert bridge.messages.timeouts == [0.1]


def test_finish_initialization_clears_old_cancel_event_for_normal_rpc():
    event = threading.Event()
    bridge = _bridge(event, _Queue(['{"ok": true}']))

    bridge.finish_initialization()
    event.set()

    assert bridge._cancel_event is None
    assert bridge._read() == {"ok": True}
    assert bridge.messages.timeouts == [25]
