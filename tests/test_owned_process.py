import gc
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

import stellaris_autopilot.owned_process as module
from stellaris_autopilot.owned_process import OwnedGameProcess, OwnedProcessError, launch_game


TOKEN = "a" * 32


class Kernel:
    def __init__(self, executable):
        self.executable, self.created, self.in_job = str(executable), 1000055, False
        self.code, self.calls = None, []

    def open(self, pid):
        self.calls.append(("open", pid))
        return "held-handle"

    def identity(self, handle):
        self.calls.append(("identity", handle))
        return self.executable, self.created, self.in_job

    def wait(self, handle, timeout):
        self.calls.append(("wait", handle, timeout))
        return self.code

    def terminate(self, handle):
        self.calls.append(("terminate", handle))
        self.code = 1

    def close(self, handle):
        self.calls.append(("close", handle))


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    executable = tmp_path / "game" / "stellaris.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"pinned-game")
    userdir = tmp_path / "campaign profile"
    userdir.mkdir()
    redirect = executable.parent / "userdir.txt"
    redirect.write_text(userdir.as_posix() + "/", encoding="utf-8")
    kernel = Kernel(executable)
    observed = {}

    def launch(command, image, environment):
        observed.update(command=command, executable=image, environment=environment)
        return {"pid": 1234, "executable": str(executable), "command_line": command,
                "creation_filetime": "1000050"}

    times = iter([1000000, 1000100])
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module, "_filetime_now", lambda: next(times))
    monkeypatch.setattr(module, "_check_executable", lambda path: None)
    monkeypatch.setattr(module, "check_steam_session", lambda: True)
    monkeypatch.setattr(module, "_launch_wmi", launch)
    monkeypatch.setattr(module, "_Kernel32", lambda: kernel)
    return executable, userdir, kernel, observed


def test_owned_process_uses_only_held_handle_and_release_never_kills(launcher):
    executable, userdir, kernel, observed = launcher
    process = launch_game(executable, userdir, TOKEN, environment={"SECRET": "not-for-output"})
    assert process.pid == 1234
    assert process.executable == executable
    assert process.userdir == userdir
    assert process.session_token == TOKEN
    assert process.poll() is None
    assert observed["environment"] == {"SECRET": "not-for-output", "SteamAppId": "281990",
                                       "SteamGameId": "281990"}
    assert f'-qsowned={TOKEN}' in observed["command"]
    assert str(userdir) not in observed["command"]
    assert observed["command"].endswith("-igc=run qs_campaign_bootstrap.txt")
    del process
    gc.collect()
    assert ("close", "held-handle") in kernel.calls
    assert not any(call[0] == "terminate" for call in kernel.calls)


def test_explicit_handle_release_is_idempotent_and_never_kills(launcher):
    executable, userdir, kernel, _ = launcher
    process = launch_game(executable, userdir, TOKEN)
    process.close()
    process.close()
    assert kernel.calls.count(("close", "held-handle")) == 1
    assert not any(call[0] == "terminate" for call in kernel.calls)
    with pytest.raises(OwnedProcessError, match="released"):
        process.terminate()
    with pytest.raises(OwnedProcessError, match="released"):
        process.poll()
    with pytest.raises(OwnedProcessError, match="released"):
        process.wait(0)


def test_terminate_wait_and_already_exited_use_original_kernel_handle(launcher):
    executable, userdir, kernel, _ = launcher
    process = launch_game(executable, userdir, TOKEN)
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(timeout=0.1)
    process.terminate()
    assert process.wait(timeout=20) == 1
    process.terminate()
    assert kernel.calls.count(("open", 1234)) == 1
    assert kernel.calls.count(("terminate", "held-handle")) == 1


@pytest.mark.parametrize("change", [
    {"pid": True}, {"pid": 0}, {"pid": 2**32}, {"command_line": "different"},
    {"executable": "C:/other/stellaris.exe"}, {"creation_filetime": 1000050},
    {"creation_filetime": "nan"},
])
def test_wmi_mismatch_never_opens_or_terminates_pid(launcher, monkeypatch, change):
    executable, userdir, kernel, _ = launcher
    original = module._launch_wmi
    monkeypatch.setattr(module, "_launch_wmi", lambda *args: {**original(*args), **change})
    with pytest.raises(OwnedProcessError, match="identity does not match"):
        launch_game(executable, userdir, TOKEN)
    assert kernel.calls == []


@pytest.mark.parametrize("identity", [
    ("C:/other/stellaris.exe", 1000055, False),
    (None, 999999, False), (None, 1000101, False), (None, 1000060, False),
])
def test_handle_mismatch_closes_handle_and_never_kills(launcher, identity):
    executable, userdir, kernel, _ = launcher
    kernel.executable, kernel.created, kernel.in_job = identity
    if kernel.executable is None:
        kernel.executable = str(executable)
    with pytest.raises(OwnedProcessError, match="handle identity does not match"):
        launch_game(executable, userdir, TOKEN)
    assert ("close", "held-handle") in kernel.calls
    assert not any(call[0] == "terminate" for call in kernel.calls)


def test_inherited_job_is_rejected_and_only_proved_new_handle_is_stopped(launcher):
    executable, userdir, kernel, _ = launcher
    kernel.in_job = True
    with pytest.raises(OwnedProcessError, match="inherited a Windows job"):
        launch_game(executable, userdir, TOKEN)
    assert ("terminate", "held-handle") in kernel.calls
    assert ("close", "held-handle") in kernel.calls


def test_redirect_mismatch_never_reaches_wmi(launcher):
    executable, userdir, kernel, observed = launcher
    (executable.parent / "userdir.txt").write_text("unrelated", encoding="utf-8")
    with pytest.raises(OwnedProcessError, match="redirect does not match"):
        launch_game(executable, userdir, TOKEN)
    assert not kernel.calls and not observed


@pytest.mark.parametrize("token", [None, "", "b" * 31, "A" * 32, "a\n" * 16])
def test_session_marker_is_strict_and_never_reaches_wmi(launcher, token):
    executable, userdir, kernel, observed = launcher
    with pytest.raises(ValueError, match="session token"):
        launch_game(executable, userdir, token)
    assert not kernel.calls and not observed


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), True, "1"])
def test_invalid_wait_timeout_cannot_become_infinite(launcher, timeout):
    executable, userdir, _, _ = launcher
    process = launch_game(executable, userdir, TOKEN)
    with pytest.raises(ValueError, match="timeout"):
        process.wait(timeout)


def test_constructor_cannot_claim_an_existing_pid():
    with pytest.raises(TypeError, match="Use launch_game"):
        OwnedGameProcess(None, pid=23, handle=123, kernel=None, executable=None,
                         userdir=None, token=TOKEN, command="")


def test_wmi_error_drops_stderr_stdout_and_environment_secrets(monkeypatch):
    secret = "credential-never-printed"

    def run(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    with pytest.raises(OwnedProcessError) as caught:
        module._launch_wmi("fixed command", Path("C:/game/stellaris.exe"), {"SECRET": secret})
    assert secret not in str(caught.value)
    assert caught.value.__suppress_context__ is True


def test_wmi_values_are_stdin_data_and_startup_is_hidden(monkeypatch):
    observed = {}

    def run(command, **kwargs):
        observed.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, stdout='{"pid":1234}', stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    assert module._launch_wmi("quoted fixed game command", Path("C:/game/stellaris.exe"),
                              {"SECRET": "private-value"}) == {"pid": 1234}
    assert "private-value" not in str(observed["command"])
    assert "private-value" in observed["input"]
    assert "ShowWindow = [uint16]0" in module._WMI_SCRIPT
    assert "Win32_Process" in module._WMI_SCRIPT


@pytest.mark.parametrize("user,pid,user_type,pid_type,expected", [
    (123, 456, 4, 4, True),
    (0, 456, 4, 4, False),
    (123, 0, 4, 4, False),
    ("123", 456, 1, 4, False),
    (123, "456", 4, 1, False),
    (True, 456, 4, 4, False),
    (123, True, 4, 4, False),
    (-1, 456, 4, 4, False),
    (123, 2**32, 4, 4, False),
])
def test_local_steam_session_is_boolean_and_requires_positive_dword_identity(
        monkeypatch, user, pid, user_type, pid_type, expected):
    class RegistryKey:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    values = {"ActiveUser": (user, user_type), "pid": (pid, pid_type)}
    registry = SimpleNamespace(HKEY_CURRENT_USER=object(), REG_DWORD=4,
                               OpenKey=lambda *args: RegistryKey(),
                               QueryValueEx=lambda key, name: values[name])
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setitem(module.sys.modules, "winreg", registry)
    assert module.check_steam_session() is expected


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError, OSError])
def test_missing_or_unreadable_steam_registry_is_unknown(monkeypatch, failure):
    def open_key(*args):
        raise failure("registry detail not returned")

    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setitem(module.sys.modules, "winreg", SimpleNamespace(HKEY_CURRENT_USER=object(),
                                                                    OpenKey=open_key))
    assert module.check_steam_session() is False


def test_steam_sign_in_failure_prevents_wmi_launch(launcher, monkeypatch):
    executable, userdir, kernel, observed = launcher
    monkeypatch.setattr(module, "check_steam_session", lambda: False)
    with pytest.raises(OwnedProcessError, match="sign in to the Steam client"):
        launch_game(executable, userdir, TOKEN)
    assert not observed and not kernel.calls
