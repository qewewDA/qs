"""Connection-budget tests without sleeping for the production 25/30 second waits."""
from __future__ import annotations

import io
import queue
import threading
from types import SimpleNamespace

import pytest

import stellaris_autopilot.attached_process as attached_process
import stellaris_autopilot.attached_runtime as attached_runtime
import stellaris_autopilot.native as native
from stellaris_autopilot.owned_process import OwnedProcessError


PID = 4321
CREATED = 1000050


class _Clock:
    def __init__(self, now=100.0):
        self.now = now

    def monotonic(self):
        return self.now


class _Kernel:
    def __init__(self, executable):
        self.executable = executable
        self.closed = []

    def open(self, pid):
        return "held-handle"

    def identity(self, handle):
        return str(self.executable), CREATED, False

    def wait(self, handle, timeout):
        return None

    def close(self, handle):
        self.closed.append(handle)


class _Stdin:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, value):
        self.writes.append(value)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _Worker:
    def __init__(self):
        self.stdin = _Stdin()
        self.returncode = None
        self.waits = []

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.waits.append(timeout)
        self.returncode = 0
        return 0


class _Queue:
    def __init__(self, messages):
        self.messages = list(messages)
        self.timeouts = []

    def get(self, block=True, timeout=None):
        self.timeouts.append(timeout)
        if not self.messages:
            raise queue.Empty
        return self.messages.pop(0)


def _patch_clock(monkeypatch, clock):
    monkeypatch.setattr(attached_process.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(attached_runtime.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(native.time, "monotonic", clock.monotonic)


def _bare_bridge(deadline, messages=None):
    bridge = object.__new__(native.NativeResearchBridge)
    bridge._deadline = deadline
    bridge.messages = messages if messages is not None else _Queue(['{"ok": true}'])
    bridge.game = SimpleNamespace(poll=lambda: None)
    bridge.process = _Worker()
    bridge.lock = threading.Lock()
    bridge.request_id = 0
    bridge.poisoned = False
    bridge.event_log = None
    bridge.error_log = io.StringIO()
    return bridge


def test_attach_game_passes_remaining_budget_to_wmi(tmp_path, monkeypatch):
    executable = tmp_path / "stellaris.exe"
    executable.write_bytes(b"fake executable")
    clock = _Clock(100.0)
    _patch_clock(monkeypatch, clock)
    monkeypatch.setattr(attached_process.sys, "platform", "win32")
    monkeypatch.setattr(attached_process, "_check_executable", lambda path: None)
    queries = []

    def query(path, **kwargs):
        queries.append((path, kwargs))
        return [{
            "pid": PID,
            "executable": str(executable.resolve()),
            "creation_filetime": str(CREATED),
            "command_line": str(executable.resolve()),
        }]

    monkeypatch.setattr(attached_process, "_query_candidates", query)
    kernel = _Kernel(executable.resolve())
    monkeypatch.setattr(attached_process, "_AttachmentKernel", lambda: kernel)

    process = attached_process.attach_game(executable, deadline=110.0)

    assert queries[0][0] == executable.resolve()
    assert queries[0][1]["timeout_seconds"] == pytest.approx(10.0)
    process.close()


def test_attach_game_expired_budget_stops_before_wmi(tmp_path, monkeypatch):
    clock = _Clock(100.0)
    _patch_clock(monkeypatch, clock)
    monkeypatch.setattr(attached_process.sys, "platform", "win32")
    queries = []
    monkeypatch.setattr(attached_process, "_query_candidates",
                        lambda *args, **kwargs: queries.append((args, kwargs)))

    with pytest.raises(OwnedProcessError, match="deadline"):
        attached_process.attach_game(tmp_path / "stellaris.exe", deadline=100.0)

    assert queries == []


def test_connect_existing_uses_one_deadline_for_process_and_native(tmp_path, monkeypatch):
    clock = _Clock(100.0)
    _patch_clock(monkeypatch, clock)
    attach_deadlines = []
    native_deadlines = []

    class Process:
        pid = PID

        def close(self):
            pass

    def attach(executable, *, pid=None, deadline=None):
        attach_deadlines.append(deadline)
        return Process()

    class Bridge:
        def __init__(self, process, executable, state_dir, *, initialization_deadline=None):
            native_deadlines.append(initialization_deadline)

        def close(self):
            pass

    class Runtime:
        def __init__(self, process, bridge, user_dir, state_dir, token, **options):
            self.options = options
            self.initialized = False

        def initialize(self):
            self.initialized = True
            return self

        def close(self):
            pass

    preflight_calls = []

    def preflight(executable, state_root, preview_journal, *, user_dir=None):
        # No-side-effect stub: the fake executable in tmp_path cannot be read/hashed,
        # so the real file/build preflight must not run in this budget-only test.
        preflight_calls.append((executable, state_root, preview_journal, user_dir))

    monkeypatch.setattr(attached_runtime, "check_connection_preflight", preflight)
    monkeypatch.setattr(attached_runtime, "attach_game", attach)
    monkeypatch.setattr(attached_runtime, "NativeResearchBridge", Bridge)
    monkeypatch.setattr(attached_runtime, "AttachedRuntime", Runtime)

    result = attached_runtime.connect_existing(
        tmp_path / "stellaris.exe",
        tmp_path / "state",
        user_dir=tmp_path / "user",
        timeout_seconds=12,
    )

    # The preflight must be reached before attach, with the resolved state root and
    # the caller-supplied executable/user_dir, so the deadline test still bisects the
    # same connection phases as production.
    assert preflight_calls == [
        (tmp_path / "stellaris.exe", (tmp_path / "state").resolve(),
         attached_runtime._PreviewJournal, tmp_path / "user"),
    ]
    assert attach_deadlines == [112.0]
    assert native_deadlines == [112.0]
    assert result.initialized is True
    assert result.options["deadline"] == 112.0


def test_native_read_uses_remaining_budget_then_finish_restores_25s(monkeypatch):
    clock = _Clock(100.0)
    _patch_clock(monkeypatch, clock)
    bridge = _bare_bridge(107.0, _Queue(['{"ok": true}', '{"ok": true}']))

    assert bridge._read() == {"ok": True}
    assert bridge.messages.timeouts == [7.0]

    bridge.finish_initialization()

    assert bridge._deadline is None
    assert bridge._read() == {"ok": True}
    assert bridge.messages.timeouts == [7.0, 25.0]


def test_native_expired_budget_does_not_send_new_rpc(monkeypatch):
    clock = _Clock(100.0)
    _patch_clock(monkeypatch, clock)
    bridge = _bare_bridge(100.0)

    with pytest.raises(ValueError, match="deadline exceeded"):
        bridge._call("result", "a" * 32)

    assert bridge.process.stdin.writes == []
    assert bridge.poisoned is True
