"""Deterministic tests for observing an already-running pinned game.

WMI inventory, executable hashing and the kernel handle are replaced with
in-memory fakes. Nothing here starts, attaches to or terminates a real game.
"""
import base64
import gc
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

import stellaris_autopilot.attached_process as module
import stellaris_autopilot.owned_process as owned_process
from stellaris_autopilot.owned_process import OwnedProcessError


PID = 4321
CREATED = 1000050


def candidate(executable: Path, **changes) -> dict:
    record = {
        "pid": PID,
        "executable": str(executable),
        "creation_filetime": str(CREATED),
        "command_line": f'"{executable}" -continuelastsave',
    }
    record.update(changes)
    return record


class Kernel:
    """Query-only fake. It deliberately has no terminate method at all."""

    def __init__(self, executable: Path, created: int = CREATED, code: int | None = None):
        self.executable, self.created, self.code = executable, created, code
        self.calls: list[tuple] = []

    def open(self, pid):
        self.calls.append(("open", pid))
        return "held-handle"

    def identity(self, handle):
        self.calls.append(("identity", handle))
        return str(self.executable), self.created, False

    def wait(self, handle, timeout):
        self.calls.append(("wait", handle, timeout))
        return self.code

    def close(self, handle):
        self.calls.append(("close", handle))


@pytest.fixture
def environment(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    executable = game / "stellaris.exe"
    executable.write_bytes(b"not the pinned executable")
    other = tmp_path / "other" / "stellaris.exe"
    other.parent.mkdir()
    other.write_bytes(b"another executable")
    kernel = Kernel(executable)
    state = {"records": [candidate(executable)], "queries": []}

    def query(path):
        state["queries"].append(Path(path))
        return [dict(record) for record in state["records"]]

    monkeypatch.setattr(module, "_query_candidates", query)
    monkeypatch.setattr(module, "_check_executable", lambda path: None)
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module, "_AttachmentKernel", lambda: kernel)
    return SimpleNamespace(executable=executable, other=other, kernel=kernel, state=state)


def test_unique_running_candidate_attaches_with_query_only_identity(environment):
    process = module.attach_game(environment.executable)
    assert process.pid == PID
    assert process.executable == environment.executable.resolve()
    assert process.created == CREATED
    assert process.poll() is None
    assert environment.state["queries"] == [environment.executable.resolve()]
    assert environment.kernel.calls[0] == ("open", PID)
    assert sum(call[0] == "open" for call in environment.kernel.calls) == 1
    assert not hasattr(process, "terminate")
    assert not hasattr(process, "kill")
    process.close()
    assert ("close", "held-handle") in environment.kernel.calls


def test_multiple_candidates_require_a_local_pid_choice(environment):
    environment.state["records"] = [
        candidate(environment.executable, pid=111),
        candidate(environment.executable, pid=222),
    ]
    with pytest.raises(OwnedProcessError, match="Stellaris"):
        module.attach_game(environment.executable)
    assert environment.kernel.calls == []


def test_explicit_pid_selects_one_of_multiple_running_candidates(environment):
    environment.state["records"] = [
        candidate(environment.executable, pid=111),
        candidate(environment.executable, pid=222),
    ]
    process = module.attach_game(environment.executable, pid=222)
    assert process.pid == 222
    assert environment.kernel.calls[0] == ("open", 222)
    process.close()


def test_explicit_pid_must_match_a_running_candidate(environment):
    with pytest.raises(OwnedProcessError, match="Stellaris"):
        module.attach_game(environment.executable, pid=9999)
    assert environment.kernel.calls == []


@pytest.mark.parametrize("pid", [True, False, 0, -1, 2**32, "4321", 1.5])
def test_invalid_pid_is_rejected_before_wmi(environment, pid):
    with pytest.raises((ValueError, OwnedProcessError)):
        module.attach_game(environment.executable, pid=pid)
    assert environment.state["queries"] == []
    assert environment.kernel.calls == []


def test_non_windows_is_rejected_before_wmi(environment, monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "linux")
    with pytest.raises(OwnedProcessError, match="Windows"):
        module.attach_game(environment.executable)
    assert environment.state["queries"] == []
    assert environment.kernel.calls == []


def test_unpinned_executable_hash_is_rejected_before_wmi(tmp_path, monkeypatch):
    executable = tmp_path / "stellaris.exe"
    executable.write_bytes(b"an unapproved executable")
    queried = []
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module, "_query_candidates", lambda path: queried.append(path) or [])
    with pytest.raises(OwnedProcessError, match="Unsupported game executable"):
        module.attach_game(executable)
    assert queried == []


@pytest.mark.parametrize("marker", ["-qsowned=", "-QSOWNED="])
def test_candidate_managed_by_another_launcher_is_rejected(environment, marker):
    environment.state["records"][0]["command_line"] += " " + marker + "a" * 32
    with pytest.raises(OwnedProcessError, match="launcher-managed"):
        module.attach_game(environment.executable)
    assert environment.kernel.calls == []


@pytest.mark.parametrize("change", [
    {"pid": True},
    {"pid": 0},
    {"pid": 2**32},
    {"pid": None},
    {"executable": ""},
    {"executable": 123},
    {"creation_filetime": 1000050},
    {"creation_filetime": "nan"},
    {"command_line": ""},
    {"command_line": None},
])
def test_malformed_wmi_record_never_opens_a_handle(environment, change):
    environment.state["records"][0].update(change)
    with pytest.raises(OwnedProcessError, match="identity"):
        module.attach_game(environment.executable)
    assert environment.kernel.calls == []


def test_wmi_record_path_must_match_the_requested_executable(environment):
    environment.state["records"] = [candidate(environment.other)]
    with pytest.raises(OwnedProcessError, match="identity"):
        module.attach_game(environment.executable)
    assert environment.kernel.calls == []


def test_held_handle_path_must_match_wmi_and_executable(environment):
    environment.kernel.executable = environment.other
    with pytest.raises(OwnedProcessError, match="handle identity"):
        module.attach_game(environment.executable)
    assert ("close", "held-handle") in environment.kernel.calls
    assert not any(call[0] == "terminate" for call in environment.kernel.calls)


def test_held_handle_creation_time_must_match_wmi_to_reject_pid_reuse(environment):
    environment.kernel.created = CREATED + 1_000_000
    with pytest.raises(OwnedProcessError, match="handle identity"):
        module.attach_game(environment.executable)
    assert environment.kernel.calls.count(("open", PID)) == 1
    assert ("close", "held-handle") in environment.kernel.calls
    assert not any(call[0] == "terminate" for call in environment.kernel.calls)


def test_exited_candidate_is_rejected_and_handle_is_released(environment):
    environment.kernel.code = 7
    with pytest.raises(OwnedProcessError, match="handle identity"):
        module.attach_game(environment.executable)
    assert ("close", "held-handle") in environment.kernel.calls
    assert not any(call[0] == "terminate" for call in environment.kernel.calls)


def test_poll_returns_exit_code_without_reopening_the_pid(environment):
    process = module.attach_game(environment.executable)
    environment.kernel.code = 5
    assert process.poll() == 5
    assert process.poll() == 5
    assert environment.kernel.calls.count(("open", PID)) == 1
    process.close()


def test_poll_revalidates_image_and_creation_time(environment):
    process = module.attach_game(environment.executable)
    environment.kernel.created = CREATED + 1
    with pytest.raises(OwnedProcessError, match="identity changed"):
        process.poll()
    process.close()


def test_close_is_idempotent_and_never_kills(environment):
    process = module.attach_game(environment.executable)
    process.close()
    process.close()
    assert environment.kernel.calls.count(("close", "held-handle")) == 1
    assert not any(call[0] == "terminate" for call in environment.kernel.calls)
    with pytest.raises(OwnedProcessError, match="released"):
        process.poll()


def test_gc_releases_only_the_handle(environment):
    process = module.attach_game(environment.executable)
    del process
    gc.collect()
    assert environment.kernel.calls.count(("close", "held-handle")) == 1
    assert not any(call[0] == "terminate" for call in environment.kernel.calls)


def test_constructor_cannot_forge_an_attachment(environment):
    with pytest.raises(TypeError, match="attach_game"):
        module.AttachedGameProcess(None, pid=1, handle="x", kernel=None,
                                   executable=environment.executable, created=CREATED)


def test_attachment_kernel_requests_query_and_synchronize_but_not_terminate():
    class Dll:
        def __init__(self, result="handle"):
            self.result, self.calls = result, []

        def OpenProcess(self, access, inherit, pid):
            self.calls.append((access, inherit, pid))
            return self.result

    dll = Dll()
    kernel = object.__new__(module._AttachmentKernel)
    kernel.dll = dll
    assert isinstance(kernel, module._Kernel32)
    assert kernel.open(1234) == "handle"
    assert dll.calls == [(0x1000 | 0x100000, False, 1234)]
    assert dll.calls[0][0] & 0x0001 == 0


def test_attachment_kernel_reports_a_missing_handle(monkeypatch):
    class Dll:
        def OpenProcess(self, access, inherit, pid):
            return 0

    kernel = object.__new__(module._AttachmentKernel)
    kernel.dll = Dll()
    with pytest.raises(OwnedProcessError, match="observation handle"):
        kernel.open(1234)


def test_query_candidates_parses_read_only_wmi_inventory(monkeypatch):
    records = [{"pid": PID, "executable": "C:/game/stellaris.exe",
                "creation_filetime": str(CREATED), "command_line": "fixed"}]
    observed = {}

    def run(command, **kwargs):
        observed.update(command=command, kwargs=kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(records), stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    assert module._query_candidates(Path("C:/game/stellaris.exe")) == records

    command = observed["command"]
    assert "-EncodedCommand" in command
    encoded = command[command.index("-EncodedCommand") + 1]
    script = base64.b64decode(encoded).decode("utf-16-le")
    assert "Get-CimInstance" in script and "Win32_Process" in script
    assert "Invoke-CimMethod" not in script
    assert "Win32_Process -MethodName Create" not in script
    assert "TerminateProcess" not in script


def test_query_candidates_rejects_unknown_output(monkeypatch):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout='{"pid": 1}', stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    with pytest.raises(OwnedProcessError, match="Cannot inspect"):
        module._query_candidates(Path("C:/game/stellaris.exe"))


def test_query_candidates_does_not_echo_provider_errors(monkeypatch):
    secret = "provider-secret-must-not-leak"

    def run(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    with pytest.raises(OwnedProcessError) as caught:
        module._query_candidates(Path("C:/game/stellaris.exe"))
    assert secret not in str(caught.value)


def test_no_candidate_never_launches_a_new_game(environment):
    environment.state["records"] = []
    with pytest.raises(OwnedProcessError, match="Open one Stellaris"):
        module.attach_game(environment.executable)
    assert environment.state["queries"] == [environment.executable.resolve()]
    assert environment.kernel.calls == []


def _forbid_launch(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("attach mode must never launch or spawn a process")

    for target in (module, owned_process):
        for name in ("launch_game", "_launch_wmi"):
            if hasattr(target, name):
                monkeypatch.setattr(target, name, forbidden)
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    monkeypatch.setattr(module.subprocess, "Popen", forbidden, raising=False)


def test_running_candidate_never_spawns_or_launches(environment, monkeypatch):
    _forbid_launch(monkeypatch)
    process = module.attach_game(environment.executable)
    assert process.poll() is None
    process.close()


def test_missing_candidate_never_spawns_or_launches(environment, monkeypatch):
    _forbid_launch(monkeypatch)
    environment.state["records"] = []
    with pytest.raises(OwnedProcessError, match="Open one Stellaris"):
        module.attach_game(environment.executable)
    assert environment.kernel.calls == []
