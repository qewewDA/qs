"""Task owners are process incarnations, never only reusable process IDs."""
import ctypes
from ctypes import wintypes
import errno
import importlib
import json
import os
from pathlib import Path
import uuid

import pytest


@pytest.fixture
def module():
    return importlib.import_module("stellaris_autopilot.task_owner")


def owner(pid=1234, birth_id="windows:42", platform="win32"):
    return {"pid": pid, "birth_id": birth_id, "platform": platform}


def test_current_owner_has_json_identity_and_is_alive(module):
    identity = module.current_owner()
    assert identity["pid"] == os.getpid()
    assert identity["birth_id"]
    assert json.loads(json.dumps(identity)) == identity
    assert module.owner_alive(identity) is True
    assert module.current_owner() == identity


def test_current_owner_does_not_expose_executable_or_other_paths(module):
    identity = module.current_owner()
    assert set(identity) == {"pid", "birth_id", "platform"}
    assert "/" not in json.dumps(identity) and "\\" not in json.dumps(identity)


@pytest.mark.parametrize("record", [None, {}, [], {"pid": True}, owner(pid=-1),
                                         owner(pid=0), owner(pid=2**32), owner(pid="1234"),
                                         owner(birth_id=""), owner(birth_id=42),
                                         owner(birth_id="unrecognized:42"), owner(platform="other"),
                                         owner(birth_id="windows:" + "9" * 5000),
                                         owner(birth_id="nonce:not-a-process-nonce")])
def test_malformed_or_foreign_owner_is_unknown(module, monkeypatch, record):
    monkeypatch.setattr(module, "_PLATFORM", "win32")
    monkeypatch.setattr(module, "_identity", lambda _pid: pytest.fail("Invalid owners must not be probed"))
    assert module.owner_alive(record) is None


@pytest.mark.parametrize("live,birth,result", [
    (True, "windows:42", True), (True, "windows:43", False),
    (False, None, False), (None, None, None), (True, None, None),
])
def test_same_birth_reused_pid_dead_and_unknown_are_distinct(module, monkeypatch, live, birth, result):
    monkeypatch.setattr(module, "_PLATFORM", "win32")
    monkeypatch.setattr(module, "_identity", lambda _pid: (live, birth))
    assert module.owner_alive(owner()) is result


def test_unavailable_birth_uses_stable_process_nonce(module, monkeypatch):
    monkeypatch.setattr(module, "_identity", lambda _pid: (None, None))
    first = module.current_owner()
    assert first["birth_id"].startswith("nonce:")
    assert module.current_owner() == first
    assert module.owner_alive(first) is True
    assert module.owner_alive({**first, "birth_id": f"nonce:{uuid.uuid4().hex}"}) is False


@pytest.mark.parametrize("live,result", [(True, None), (None, None), (False, False)])
def test_other_process_nonce_never_proves_same_process(module, monkeypatch, live, result):
    monkeypatch.setattr(module, "_identity", lambda _pid: (live, "windows:42"))
    identity = {"pid": os.getpid() + 1234, "birth_id": f"nonce:{uuid.uuid4().hex}",
                "platform": module._PLATFORM}
    assert module.owner_alive(identity) is result


class Kernel:
    def __init__(self, *, handle=99, wait=0x102, times=True, created=42):
        self.handle, self.wait, self.times, self.created = handle, wait, times, created
        self.calls = []

    def OpenProcess(self, access, inherit, pid):
        self.calls.append(("open", access, inherit, pid))
        return self.handle

    def WaitForSingleObject(self, handle, timeout):
        self.calls.append(("wait", handle, timeout))
        return self.wait

    def GetProcessTimes(self, handle, created, exited, kernel, user):
        self.calls.append(("times", handle))
        value = ctypes.cast(created, ctypes.POINTER(wintypes.FILETIME)).contents
        value.dwHighDateTime, value.dwLowDateTime = self.created >> 32, self.created & 0xFFFFFFFF
        return self.times

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        return True


def windows(module, monkeypatch, kernel, error=5):
    monkeypatch.setattr(module, "_PLATFORM", "win32")
    monkeypatch.setattr(module, "_kernel32", lambda: kernel)
    monkeypatch.setattr(module, "_last_windows_error", lambda: error)
    monkeypatch.setattr(module.os, "kill", lambda *_args: pytest.fail("Windows kill(pid, 0) is forbidden"))


def test_windows_creation_filetime_and_read_only_rights(module, monkeypatch):
    kernel = Kernel(created=2**48 + 123)
    windows(module, monkeypatch, kernel)
    assert module._identity(1234) == (True, f"windows:{2**48 + 123}")
    assert kernel.calls == [("open", 0x1000 | 0x100000, False, 1234),
                            ("wait", 99, 0), ("times", 99), ("close", 99)]
    assert kernel.calls[0][1] & 0x0001 == 0


@pytest.mark.parametrize("error,result", [(87, False), (5, None), (6, None), (0, None)])
def test_windows_open_failure_only_missing_pid_is_dead(module, monkeypatch, error, result):
    kernel = Kernel(handle=0)
    windows(module, monkeypatch, kernel, error=error)
    assert module._identity(1234) == (result, None)
    assert kernel.calls == [("open", 0x1000 | 0x100000, False, 1234)]


@pytest.mark.parametrize("wait,times,created,result", [
    (0, True, 42, False), (0xFFFFFFFF, True, 42, None),
    (0x102, False, 42, None), (0x102, True, 0, None),
])
def test_windows_terminated_and_unreadable_handle_states_close(module, monkeypatch, wait, times, created, result):
    kernel = Kernel(wait=wait, times=times, created=created)
    windows(module, monkeypatch, kernel)
    assert module._identity(1234) == (result, None)
    assert kernel.calls[-1] == ("close", 99)


def test_windows_loader_failure_is_unknown_without_kill(module, monkeypatch):
    windows(module, monkeypatch, Kernel())

    def inaccessible():
        raise OSError("private provider path must not leak")

    monkeypatch.setattr(module, "_kernel32", inaccessible)
    assert module._identity(1234) == (None, None)


BOOT = "6af19d0a-f1f1-4d35-bd10-f45d6e259c11"


def stat_text(pid=1234, state="S", start="314159"):
    # comm can contain spaces and nested parentheses; starttime is field 22.
    return f"{pid} (worker (has spaces) and ) parens) {state} " + " ".join(["0"] * 18 + [start, "0"])


def linux(module, monkeypatch, stat=None, boot=BOOT):
    monkeypatch.setattr(module, "_PLATFORM", "linux")

    def read(path, **_kwargs):
        value = boot if str(path).endswith("boot_id") else (stat if stat is not None else stat_text())
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(Path, "read_text", read)


def test_linux_uses_boot_and_start_ticks_handles_complex_comm(module, monkeypatch):
    linux(module, monkeypatch)
    assert module._identity(1234) == (True, f"linux:{BOOT}:314159")


@pytest.mark.parametrize("state", ["Z", "X", "x"])
def test_linux_zombie_is_dead(module, monkeypatch, state):
    linux(module, monkeypatch, stat=stat_text(state=state))
    assert module._identity(1234) == (False, None)


@pytest.mark.parametrize("stat,boot", [
    ("not proc stat", BOOT), (stat_text(pid=5678), BOOT),
    (stat_text(state="?"), BOOT), (stat_text().replace("(worker (has spaces) and ) parens)", ")"), BOOT),
    (stat_text(start="not-a-number"), BOOT), (stat_text(start="-1"), BOOT),
    (stat_text(), ""), (stat_text(), "not-a-boot-id"),
    (PermissionError(errno.EACCES, "private proc path"), BOOT),
    (stat_text(), PermissionError(errno.EACCES, "private boot path")),
])
def test_linux_partial_or_denied_identity_is_unknown(module, monkeypatch, stat, boot):
    linux(module, monkeypatch, stat=stat, boot=boot)
    assert module._identity(1234) == (None, None)


@pytest.mark.parametrize("error,result", [(None, True), (errno.ESRCH, False),
                                          (errno.EPERM, None), (errno.EACCES, None), (errno.EIO, None)])
def test_posix_kill_probe_cannot_prove_birth(module, monkeypatch, error, result):
    monkeypatch.setattr(module, "_PLATFORM", "darwin")
    calls = []

    def probe(pid, signal):
        calls.append((pid, signal))
        if error is not None:
            raise OSError(error, "private path must not leak")

    monkeypatch.setattr(module.os, "kill", probe)
    assert module._identity(1234) == (result, None)
    assert calls == [(1234, 0)]
    identity = owner(platform="darwin", birth_id=f"nonce:{uuid.uuid4().hex}")
    assert module.owner_alive(identity) is (False if result is False else None)


@pytest.mark.parametrize("error,result", [(errno.ESRCH, False), (errno.EPERM, None), (None, True)])
def test_linux_missing_proc_uses_conservative_posix_probe(module, monkeypatch, error, result):
    linux(module, monkeypatch, stat=FileNotFoundError(errno.ENOENT, "proc unavailable"))

    def probe(_pid, _signal):
        if error is not None:
            raise OSError(error, "unknown process")

    monkeypatch.setattr(module.os, "kill", probe)
    assert module._identity(1234) == (result, None)


def test_nonce_resets_after_fork_so_same_pid_cannot_inherit_old_nonce(module, monkeypatch):
    monkeypatch.setattr(module, "_identity", lambda _pid: (None, None))
    identity = module.current_owner()
    module._after_fork()
    assert module.current_owner()["birth_id"] != identity["birth_id"]
    assert module.owner_alive(identity) is False


def test_reload_does_not_change_fallback_process_identity(module, monkeypatch):
    monkeypatch.setattr(module, "_identity", lambda _pid: (None, None))
    identity = module.current_owner()
    importlib.reload(module)
    assert module.owner_alive(identity) is True
