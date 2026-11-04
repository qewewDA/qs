"""Read-only process incarnation checks for persisted task owners.

Only a matching birth identity proves that a PID still belongs to the owner.
Denied queries, unsupported birth sources and malformed records stay unknown.
No executable paths, subprocesses, game state or process-control rights are used.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import errno
import os
from pathlib import Path
import sys
import uuid


_PLATFORM = sys.platform
if "_nonce" not in globals():
    # importlib.reload must not create a new identity for this live interpreter.
    _nonce = uuid.uuid4().hex


def _after_fork() -> None:
    global _nonce
    _nonce = uuid.uuid4().hex


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def _last_windows_error() -> int:
    return ctypes.get_last_error()


def _windows_identity(pid: int) -> tuple[bool | None, str | None]:
    try:
        kernel = _kernel32()
        # Query limited information and synchronization only; no terminate rights.
        handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            # ERROR_INVALID_PARAMETER is the documented nonexistent PID case.
            # ERROR_ACCESS_DENIED (including protected processes) proves nothing.
            return (False if _last_windows_error() == 87 else None), None
        try:
            wait = kernel.WaitForSingleObject(handle, 0)
            if wait == 0:
                return False, None
            if wait != 0x102:
                return None, None
            created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(system), ctypes.byref(user)):
                return None, None
            birth = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return (True, f"windows:{birth}") if birth else (None, None)
        finally:
            kernel.CloseHandle(handle)
    except (OSError, AttributeError):
        return None, None


def _posix_probe(pid: int) -> bool | None:
    # Unlike POSIX, Windows os.kill(pid, 0) can terminate the process.
    if _PLATFORM == "win32":
        return None
    try:
        os.kill(pid, 0)
        return True
    except OSError as error:
        return False if error.errno == errno.ESRCH else None
    except OverflowError:
        return None


def _linux_identity(pid: int) -> tuple[bool | None, str | None]:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        # /proc may be unmounted or hidden: absence alone is not death.
        return _posix_probe(pid), None
    except (OSError, UnicodeError):
        return None, None
    try:
        prefix, separator, tail = stat.rpartition(")")
        fields = tail.split()
        process_id, opening, _command = prefix.partition("(")
        if not separator or not opening or int(process_id) != pid or len(fields) < 20:
            return None, None
        if fields[0] not in {"R", "S", "D", "Z", "T", "t", "W", "X", "x", "K", "P", "I"}:
            return None, None
        if fields[0] in {"Z", "X", "x"}:
            return False, None
        start = fields[19]
        if not start.isascii() or not start.isdecimal():
            return None, None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        boot = str(uuid.UUID(boot))
        return True, f"linux:{boot}:{int(start)}"
    except (OSError, ValueError, UnicodeError):
        return None, None


def _identity(pid: int) -> tuple[bool | None, str | None]:
    if _PLATFORM == "win32":
        return _windows_identity(pid)
    if _PLATFORM.startswith("linux"):
        return _linux_identity(pid)
    return _posix_probe(pid), None


def _valid_birth(birth: object, platform: str) -> bool:
    if not isinstance(birth, str):
        return False
    if birth.startswith("nonce:"):
        return len(birth) == 38 and all(character in "0123456789abcdef" for character in birth[6:])
    if birth.startswith("windows:") and platform == "win32":
        number = birth[8:]
        return (0 < len(number) <= 20 and number.isascii() and number.isdecimal()
                and 0 < int(number) < 2**64)
    if birth.startswith("linux:") and platform.startswith("linux"):
        parts = birth.split(":")
        try:
            return (len(parts) == 3 and str(uuid.UUID(parts[1])) == parts[1]
                    and 0 < len(parts[2]) <= 20 and parts[2].isascii() and parts[2].isdecimal()
                    and int(parts[2]) < 2**64)
        except ValueError:
            return False
    return False


def current_owner() -> dict:
    """Return JSON-safe PID, platform and birth ID, without private paths.

    The fallback nonce identifies this interpreter only. Other processes can
    establish that it died, but must return unknown while its birth is unreadable.
    """
    pid = os.getpid()
    alive, birth = _identity(pid)
    return {"pid": pid, "platform": _PLATFORM,
            "birth_id": birth if alive is True and birth else f"nonce:{_nonce}"}


def owner_alive(owner: dict) -> bool | None:
    """True means the same live process; False requires death/reuse evidence.

    None means insufficient evidence and must never authorize recovery or retry
    of an unverified command. A positive PID probe alone cannot prove identity.
    """
    if not isinstance(owner, dict):
        return None
    pid, birth, platform = owner.get("pid"), owner.get("birth_id"), owner.get("platform")
    if (type(pid) is not int or not 0 < pid < 2**32 or platform != _PLATFORM
            or not _valid_birth(birth, platform)):
        return None
    if birth.startswith("nonce:") and pid == os.getpid():
        return birth == f"nonce:{_nonce}"
    alive, actual_birth = _identity(pid)
    if alive is False:
        return False
    if alive is not True or not actual_birth or birth.startswith("nonce:"):
        return None
    return actual_birth == birth
