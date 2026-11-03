"""Launch one pinned campaign game outside the MCP client's Windows job.

The WMI provider creates the process. Ownership is granted only after the
provider record and a held kernel handle agree on the exact image, private
command line, and creation time. No API attaches to an existing PID. Closing
this Python object releases its handle; it does not terminate a running game.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Mapping


class OwnedProcessError(RuntimeError):
    """A safe, credential-free launcher or ownership error."""


_FACTORY_KEY = object()
_FILETIME_EPOCH = 116444736000000000
_TOKEN = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)

# All values, including environment variables, arrive on stdin as JSON. They
# are never interpolated into PowerShell source or printed on errors.
_WMI_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    $launch = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $startup = New-CimInstance -Namespace root/cimv2 -ClassName Win32_ProcessStartup -ClientOnly -Property @{
        ShowWindow = [uint16]0
        EnvironmentVariables = [string[]]$launch.environment
    }
    $created = Invoke-CimMethod -Namespace root/cimv2 -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = [string]$launch.command_line
        CurrentDirectory = [string]$launch.cwd
        ProcessStartupInformation = $startup
    }
    if ($created.ReturnValue -ne 0) { throw 'Create failed' }
    $record = Get-CimInstance -Namespace root/cimv2 -ClassName Win32_Process -Filter (
        'ProcessId=' + [string][uint32]$created.ProcessId)
    if ($null -eq $record) { throw 'Missing created process' }
    $result = @{
        pid = [uint32]$created.ProcessId
        executable = [string]$record.ExecutablePath
        command_line = [string]$record.CommandLine
        creation_filetime = [string]$record.CreationDate.ToUniversalTime().ToFileTimeUtc()
    }
    [Console]::Out.Write(($result | ConvertTo-Json -Compress))
} catch {
    [Console]::Out.Write('{"error":"WMI launch or identity observation failed"}')
    exit 1
}
"""


def _filetime_now() -> int:
    return time.time_ns() // 100 + _FILETIME_EPOCH


def _same_path(left: str | Path, right: str | Path) -> bool:
    # WMI can return a junction spelling while the kernel returns the final
    # target path. Resolve both before comparing the exact canonical image.
    try:
        left, right = Path(left).resolve(strict=True), Path(right).resolve(strict=True)
    except (OSError, ValueError):
        return False
    return ntpath.normcase(ntpath.normpath(str(left))) == ntpath.normcase(ntpath.normpath(str(right)))


def _check_executable(executable: Path):
    # A local import avoids a cycle when native.py accepts this trusted type.
    from .native import PINNED_EXE_SHA256
    if hashlib.sha256(executable.read_bytes()).hexdigest() != PINNED_EXE_SHA256:
        raise OwnedProcessError("Unsupported game executable")


def check_steam_session() -> bool:
    """Read local sign-in state without returning account IDs or requiring network.

    Offline Steam sessions with a signed-in user are supported. Missing or
    unreadable registry state is unknown and cannot authorize a game launch.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess") as key:
            user, user_type = winreg.QueryValueEx(key, "ActiveUser")
            pid, pid_type = winreg.QueryValueEx(key, "pid")
        return (user_type == winreg.REG_DWORD and pid_type == winreg.REG_DWORD
                and type(user) is int and 0 < user < 2**32
                and type(pid) is int and 0 < pid < 2**32)
    except (ImportError, OSError, ValueError, TypeError):
        return False


def _launch_wmi(command: str, executable: Path, environment: Mapping[str, str]) -> dict:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    powershell = Path(system_root) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    encoded = base64.b64encode(_WMI_SCRIPT.encode("utf-16-le")).decode("ascii")
    payload = {"command_line": command, "cwd": str(executable.parent),
               "environment": [f"{key}={value}" for key, value in environment.items()]}
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=45, check=False)
        if result.returncode:
            raise OwnedProcessError("WMI failed")
        record = json.loads(result.stdout)
        if not isinstance(record, dict) or "error" in record:
            raise OwnedProcessError("WMI failed")
        return record
    except Exception:
        # A creation might already have succeeded. Never guess a PID or kill
        # an unverified process in an error recovery path.
        raise OwnedProcessError(
            "WMI launch identity was not verified; a game may be running and requires local inspection"
        ) from None


class _Kernel32:
    """Small handle-only kernel adapter, isolated for deterministic tests."""

    def __init__(self):
        self.dll = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "QueryFullProcessImageNameW": (
                [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)],
                wintypes.BOOL),
            "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            "IsProcessInJob": ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.dll, name)
            function.argtypes, function.restype = arguments, result

    def open(self, pid: int):
        # Query limited information, synchronization, termination. Never inherit
        # the handle, and never reopen a PID during the object's lifetime.
        handle = self.dll.OpenProcess(0x1000 | 0x100000 | 0x0001, False, pid)
        if not handle:
            raise OwnedProcessError("Cannot acquire the newly created process handle")
        return handle

    def identity(self, handle) -> tuple[str, int, bool]:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        in_job = wintypes.BOOL()
        if (not self.dll.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length))
                or not self.dll.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                               ctypes.byref(kernel), ctypes.byref(user))
                or not self.dll.IsProcessInJob(handle, None, ctypes.byref(in_job))):
            raise OwnedProcessError("Cannot validate the newly created process identity")
        return buffer.value, (created.dwHighDateTime << 32) | created.dwLowDateTime, bool(in_job.value)

    def wait(self, handle, timeout: float | None) -> int | None:
        milliseconds = 0xFFFFFFFF if timeout is None else min(math.ceil(timeout * 1000), 0xFFFFFFFE)
        result = self.dll.WaitForSingleObject(handle, milliseconds)
        if result == 0x102:
            return None
        if result != 0:
            raise OwnedProcessError("Cannot wait on the owned process handle")
        code = wintypes.DWORD()
        if not self.dll.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise OwnedProcessError("Cannot read the owned process exit code")
        return code.value

    def terminate(self, handle):
        if not self.dll.TerminateProcess(handle, 1):
            if self.wait(handle, 0) is None:
                raise OwnedProcessError("Cannot terminate the owned process handle")

    def close(self, handle):
        self.dll.CloseHandle(handle)


class OwnedGameProcess:
    """Factory-only process ownership; PID is informative, never a kill target."""

    def __init__(self, key, *, pid, handle, kernel, executable, userdir, token, command):
        if key is not _FACTORY_KEY:
            raise TypeError("Use launch_game to create an owned game process")
        self._pid, self._handle, self._kernel = pid, handle, kernel
        self.executable, self.userdir, self.session_token = executable, userdir, token
        self.args, self.returncode = command, None
        self._lock = threading.Lock()

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> int | None:
        with self._lock:
            if self._handle is None:
                if self.returncode is not None:
                    return self.returncode
                raise OwnedProcessError("The owned process handle has been released")
            self.returncode = self._kernel.wait(self._handle, 0)
            return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Process wait timeout must be nonnegative and finite")
        with self._lock:
            if self._handle is None:
                if self.returncode is not None:
                    return self.returncode
                raise OwnedProcessError("The owned process handle has been released")
            code = self._kernel.wait(self._handle, timeout)
            if code is None:
                raise subprocess.TimeoutExpired(self.args, timeout)
            self.returncode = code
            return code

    def terminate(self):
        with self._lock:
            if self._handle is None:
                raise OwnedProcessError("The owned process handle has been released")
            if self._kernel.wait(self._handle, 0) is None:
                self._kernel.terminate(self._handle)

    def close(self):
        """Release observation rights without stopping a running game."""
        with self._lock:
            if self._handle is not None:
                self._kernel.close(self._handle)
                self._handle = None

    def __del__(self):
        # A failed save or client disconnect must leave the game alive. A
        # handle close has no kill-on-close behavior and owns no Windows job.
        handle = getattr(self, "_handle", None)
        if handle is not None:
            try:
                self.close()
            except Exception:
                pass


def launch_game(executable: Path, userdir: Path, session_token: str, *,
                environment: Mapping[str, str] | None = None) -> OwnedGameProcess:
    """Launch the fixed campaign bootstrap and prove exact process ownership.

    Paths and session token are host configuration, never MCP arguments.
    The launcher must install its own userdir.txt before calling this factory.
    WMI creation deliberately cannot redirect stdout/stderr to launcher pipes.
    """
    if sys.platform != "win32":
        raise OwnedProcessError("Owned game launch requires Windows")
    if not isinstance(session_token, str) or not _TOKEN.fullmatch(session_token):
        raise ValueError("A fresh canonical session token is required")
    if not check_steam_session():
        raise OwnedProcessError("Steam is not signed in; sign in to the Steam client, then reconnect")
    executable, userdir = Path(executable).resolve(strict=True), Path(userdir).resolve(strict=True)
    if (executable.name.lower() != "stellaris.exe" or not executable.is_file() or not userdir.is_dir()
            or any(character in str(path) for path in (executable, userdir) for character in '\"\x00\r\n')):
        raise ValueError("Invalid fixed game executable or campaign directory")
    redirect = executable.parent / "userdir.txt"
    if not redirect.is_file() or redirect.read_text(encoding="utf-8") != userdir.as_posix() + "/":
        raise OwnedProcessError("The campaign user directory redirect does not match")
    _check_executable(executable)
    env = dict(os.environ if environment is None else environment)
    env.update(SteamAppId="281990", SteamGameId="281990")
    if any(not isinstance(key, str) or not key or "=" in key or "\x00" in key
           or not isinstance(value, str) or "\x00" in value for key, value in env.items()):
        raise ValueError("Invalid game environment")
    # Keep the private marker a single word: this engine's legacy parser also
    # splits argument interiors at '-'. The profile is bound by its redirect,
    # not a command-line path that may itself contain hyphens.
    command = (f'"{executable}" -gdpr-compliant --continuelastsave '
               f'-qsowned={session_token} '
               '-igc=run qs_campaign_bootstrap.txt')
    before = _filetime_now()
    record = _launch_wmi(command, executable, env)
    after = _filetime_now()
    pid, stamp = record.get("pid"), record.get("creation_filetime")
    if (type(pid) is not int or not 0 < pid < 2**32 or not isinstance(stamp, str)
            or not re.fullmatch(r"[0-9]{1,20}", stamp, re.ASCII)
            or record.get("command_line") != command
            or not isinstance(record.get("executable"), str)
            or not _same_path(record["executable"], executable)):
        raise OwnedProcessError("The created game identity does not match; inspect any running game locally")
    kernel, handle = _Kernel32(), None
    try:
        handle = kernel.open(pid)
        image, created, in_job = kernel.identity(handle)
        if (not _same_path(image, executable) or not before <= created <= after
                or not int(stamp) <= created < int(stamp) + 10 or kernel.wait(handle, 0) is not None):
            raise OwnedProcessError("The created process handle identity does not match")
        _check_executable(executable)
        if in_job:
            # Identity is already proved, so this exact newly created process
            # can be stopped. Never alter or relax the MCP client's job.
            kernel.terminate(handle)
            raise OwnedProcessError("The game inherited a Windows job; durable campaign launch is unavailable")
        owned = OwnedGameProcess(_FACTORY_KEY, pid=pid, handle=handle, kernel=kernel,
                                 executable=executable, userdir=userdir, token=session_token, command=command)
        handle = None
        return owned
    finally:
        if handle is not None:
            kernel.close(handle)
