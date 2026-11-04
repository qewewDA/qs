"""Hold an existing game's identity without acquiring any termination rights."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from .owned_process import OwnedProcessError, _Kernel32, _check_executable, _same_path
from .connection_cancel import check_cancelled


_ATTACH_KEY = object()
_QUERY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$items = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='stellaris.exe'" | ForEach-Object {
    @{
        pid = [uint32]$_.ProcessId
        executable = [string]$_.ExecutablePath
        command_line = [string]$_.CommandLine
        creation_filetime = [string]$_.CreationDate.ToUniversalTime().ToFileTimeUtc()
    }
})
[Console]::Out.Write((ConvertTo-Json -InputObject $items -Compress))
"""


def _query_candidates(executable: Path, *, timeout_seconds: float = 30) -> list[dict]:
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand",
             base64.b64encode(_QUERY.encode("utf-16-le")).decode("ascii")],
            capture_output=True, text=True, encoding="utf-8", timeout=timeout_seconds,
            creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        items = json.loads(result.stdout)
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError("Invalid process inventory")
        return items
    except Exception:
        raise OwnedProcessError("Cannot inspect existing Stellaris processes") from None


class _AttachmentKernel(_Kernel32):
    def open(self, pid: int):
        # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, never TERMINATE.
        handle = self.dll.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            raise OwnedProcessError("Cannot acquire the existing game's observation handle")
        return handle


class AttachedGameProcess:
    """Factory-only observation handle. Deliberately has no terminate method."""

    def __init__(self, key, *, pid, handle, kernel, executable, created):
        if key is not _ATTACH_KEY:
            raise TypeError("Use attach_game to inspect an existing game")
        self._pid, self._handle, self._kernel = pid, handle, kernel
        self.executable, self.created = executable, created
        self._lock = threading.Lock()

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> int | None:
        with self._lock:
            if self._handle is None:
                raise OwnedProcessError("The attached process handle has been released")
            code = self._kernel.wait(self._handle, 0)
            if code is None:
                image, created, _ = self._kernel.identity(self._handle)
                if created != self.created or not _same_path(image, self.executable):
                    raise OwnedProcessError("Attached game identity changed")
            return code

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._kernel.close(self._handle)
                self._handle = None

    def __del__(self):
        if getattr(self, "_handle", None) is not None:
            try:
                self.close()
            except Exception:
                pass


def attach_game(executable: Path, *, pid: int | None = None,
                deadline: float | None = None, cancel_event: threading.Event | None = None) -> AttachedGameProcess:
    if sys.platform != "win32":
        raise OwnedProcessError("Existing game attachment requires Windows")
    if pid is not None and (type(pid) is not int or not 0 < pid < 2**32):
        raise ValueError("PID must be a positive Windows process identifier")

    def remaining() -> float:
        check_cancelled(cancel_event, "verify_running_process")
        seconds = 30 if deadline is None else min(30, deadline - time.monotonic())
        if seconds <= 0:
            raise OwnedProcessError("Connection deadline exceeded while checking the game process")
        return seconds

    remaining()
    executable = Path(executable).resolve(strict=True)
    if executable.name.lower() != "stellaris.exe" or not executable.is_file():
        raise ValueError("Select the exact Stellaris executable")
    _check_executable(executable)
    options = {"timeout_seconds": remaining()} if deadline is not None else {}
    records = _query_candidates(executable, **options)
    remaining()
    if pid is not None:
        records = [item for item in records if type(item.get("pid")) is int and item["pid"] == pid]
    if len(records) != 1:
        raise OwnedProcessError("Open one Stellaris game and load a save; select a PID locally if several exist")
    record = records[0]
    selected_pid, stamp = record.get("pid"), record.get("creation_filetime")
    command = record.get("command_line")
    if (type(selected_pid) is not int or not 0 < selected_pid < 2**32
            or not isinstance(stamp, str) or not re.fullmatch(r"[0-9]{1,20}", stamp, re.ASCII)
            or not isinstance(record.get("executable"), str)
            or not _same_path(record["executable"], executable) or not isinstance(command, str) or not command):
        raise OwnedProcessError("Existing process identity does not match the pinned game")
    if "-qsowned=" in command.lower():
        raise OwnedProcessError("This game is already launcher-managed; use its existing campaign connection")
    kernel, handle = _AttachmentKernel(), None
    try:
        handle = kernel.open(selected_pid)
        image, created, _ = kernel.identity(handle)
        if (not _same_path(image, executable) or not int(stamp) <= created < int(stamp) + 10
                or kernel.wait(handle, 0) is not None):
            raise OwnedProcessError("Existing process handle identity does not match")
        _check_executable(executable)
        remaining()
        result = AttachedGameProcess(_ATTACH_KEY, pid=selected_pid, handle=handle, kernel=kernel,
                                     executable=executable, created=created)
        handle = None
        return result
    finally:
        if handle is not None:
            kernel.close(handle)
