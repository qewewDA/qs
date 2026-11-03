"""Guarded Windows UI input for preconfigured Stellaris macros.

This module never evaluates text, opens a shell, or activates a window. Its
console transport accepts only three fixed project event IDs. Importing it is
safe on Linux; real input requires Windows and optional ``windows`` dependencies.
"""

from __future__ import annotations

import ctypes
import math
import ntpath
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DriverError(RuntimeError):
    """An input step was rejected or its target window became unsafe."""


_KEYS = frozenset(
    list("abcdefghijklmnopqrstuvwxyz0123456789`")
    + [f"f{n}" for n in range(1, 13)]
    + ["enter", "escape", "tab", "space", "backspace", "delete", "home", "end", "up", "down", "left", "right"]
)
_MODIFIERS = frozenset({"ctrl", "shift"})


def _number(value: Any, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DriverError(f"{field} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or not minimum <= value <= maximum:
        raise DriverError(f"{field} must be between {minimum} and {maximum}")
    return float(value)


def validate_steps(steps: list[dict]) -> None:
    """Validate the entire macro before any UI side effects occur.

    A macro has 1–64 steps, each with a ``kind`` field. Accepted forms:
    ``click(x, y, button='left')``, ``key(key)``, ``hotkey(keys)`` and
    ``wait(seconds)``. Coordinates are fractions of the client area. Waits may
    total at most 30 seconds, with at most five seconds in any single step.
    """
    if not isinstance(steps, list) or not 1 <= len(steps) <= 64:
        raise DriverError("a macro must contain 1–64 steps")
    total_wait = 0.0
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise DriverError(f"step {index} must be an object")
        if any(not isinstance(key, str) for key in step):
            raise DriverError(f"step {index} has a non-string field name")
        kind = step.get("kind")
        if kind == "click":
            allowed = {"kind", "x", "y", "button"}
            _number(step.get("x"), "x", 0, 1)
            _number(step.get("y"), "y", 0, 1)
            if step.get("button", "left") not in ("left", "right"):
                raise DriverError("click button must be left or right")
        elif kind == "key":
            allowed = {"kind", "key"}
            if not isinstance(step.get("key"), str) or step["key"] not in _KEYS:
                raise DriverError("key is outside the permitted game-key set")
        elif kind == "hotkey":
            allowed = {"kind", "keys"}
            keys = step.get("keys")
            if not isinstance(keys, list) or not 2 <= len(keys) <= 3:
                raise DriverError("hotkey must contain 2–3 keys")
            if any(not isinstance(key, str) or key not in _KEYS | _MODIFIERS for key in keys):
                raise DriverError("hotkey contains a forbidden key")
            if len(set(keys)) != len(keys):
                raise DriverError("hotkey keys must be unique")
            if keys[-1] not in _KEYS or any(key not in _MODIFIERS for key in keys[:-1]):
                raise DriverError("hotkeys must contain modifiers followed by one game key")
            if keys[-1] == "escape" and "ctrl" in keys:
                raise DriverError("Windows system hotkeys are forbidden")
        elif kind == "wait":
            allowed = {"kind", "seconds"}
            total_wait += _number(step.get("seconds"), "seconds", 0, 5)
        else:
            raise DriverError(f"unknown step kind at index {index}")
        if set(step) - allowed:
            raise DriverError(f"unknown fields in step {index}: {sorted(set(step) - allowed)}")
    if total_wait > 30:
        raise DriverError("total wait time exceeds 30 seconds")


@dataclass(frozen=True)
class _Window:
    handle: int
    title: str
    left: int
    top: int
    width: int
    height: int


def client_point(x: float, y: float, left: int, top: int, width: int, height: int) -> tuple[int, int]:
    """Map normalized coordinates to a pixel strictly inside the client rect."""
    _number(x, "x", 0, 1)
    _number(y, "y", 0, 1)
    if width < 1 or height < 1:
        raise DriverError("game client area has zero size")
    return left + round(x * (width - 1)), top + round(y * (height - 1))


class WindowsDriver:
    """Send validated input only while the real game is the foreground process.

    The game must already be foreground. Resolution checks use client pixels,
    excluding window borders, and fail closed if calibration no longer matches.
    Move the mouse to a screen corner for PyAutoGUI's emergency fail-safe.
    """

    def __init__(self, title_hint: str = "Stellaris", expected_resolution: tuple[int, int] | None = None,
                 stop_file: Path | None = None):
        if sys.platform != "win32":
            raise DriverError("Windows UI execution requires Windows 10/11")
        if not isinstance(title_hint, str) or not title_hint.strip():
            raise DriverError("title_hint must be nonempty")
        if expected_resolution is not None and (
            not isinstance(expected_resolution, (tuple, list))
            or len(expected_resolution) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in expected_resolution)
        ):
            raise DriverError("expected_resolution must contain two positive pixel integers")
        self.title_hint = title_hint.casefold()
        self.expected_resolution = tuple(expected_resolution) if expected_resolution is not None else None
        self.stop_file = Path(stop_file) if stop_file is not None else None
        self._target_pid: int | None = None
        self._bind_win32()
        self._set_dpi_awareness()
        try:
            import pyautogui
        except ImportError as exc:
            raise DriverError('install the Windows extra: pip install ".[windows]"') from exc
        self._gui = pyautogui
        self._gui.FAILSAFE = True
        self._gui.PAUSE = 0.08

    def _bind_win32(self) -> None:
        from ctypes import wintypes

        self._wintypes = wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user = self._user32
        kernel = self._kernel32
        user.GetForegroundWindow.argtypes = []
        user.GetForegroundWindow.restype = wintypes.HWND
        user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user.GetWindowTextLengthW.restype = ctypes.c_int
        user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetWindowTextW.restype = ctypes.c_int
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user.GetClientRect.restype = wintypes.BOOL
        user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        user.ClientToScreen.restype = wintypes.BOOL
        user.WindowFromPoint.argtypes = [wintypes.POINT]
        user.WindowFromPoint.restype = wintypes.HWND
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
        user.IsIconic.argtypes = [wintypes.HWND]
        user.IsIconic.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL

    def _set_dpi_awareness(self) -> None:
        # A thread override also handles hosts that previously initialized DPI
        # awareness. Windows 10 1703+ supports per-monitor awareness v2.
        user = self._user32
        if hasattr(user, "SetThreadDpiAwarenessContext"):
            user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            if user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
            if user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-3)):
                return
        # Legacy Windows 10 supports per-monitor process awareness via shcore.
        try:
            shcore = ctypes.WinDLL("shcore", use_last_error=True)
            shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
            shcore.SetProcessDpiAwareness.restype = ctypes.c_long
            if shcore.SetProcessDpiAwareness(2) == 0:
                return
        except (AttributeError, OSError):
            pass
        raise DriverError("cannot establish per-monitor DPI coordinates; refusing UI input")

    def _window(self) -> _Window:
        self._check_stop()
        wt = self._wintypes
        user = self._user32
        hwnd = user.GetForegroundWindow()
        if not hwnd or user.IsIconic(hwnd):
            raise DriverError("Stellaris must be the foreground, non-minimized window")
        title_length = user.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(max(title_length + 1, 2))
        user.GetWindowTextW(hwnd, title, len(title))
        if self.title_hint not in title.value.casefold():
            raise DriverError("foreground window title does not match Stellaris")
        process_id = wt.DWORD()
        if not user.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id)):
            raise DriverError("cannot inspect foreground process")
        if self._target_pid is not None and process_id.value != self._target_pid:
            raise DriverError("foreground game process changed; restart after inspecting the target campaign")
        process = self._kernel32.OpenProcess(0x1000, False, process_id.value)
        if not process:
            raise DriverError("cannot inspect Stellaris process; match process privilege levels")
        try:
            path = ctypes.create_unicode_buffer(32768)
            size = wt.DWORD(len(path))
            if not self._kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                raise DriverError("cannot read foreground executable identity")
            if ntpath.basename(path.value).casefold() != "stellaris.exe":
                raise DriverError("foreground executable must be stellaris.exe")
        finally:
            self._kernel32.CloseHandle(process)
        rect = wt.RECT()
        origin = wt.POINT(0, 0)
        if not user.GetClientRect(hwnd, ctypes.byref(rect)) or not user.ClientToScreen(hwnd, ctypes.byref(origin)):
            raise DriverError("cannot inspect Stellaris client coordinates")
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width < 1 or height < 1:
            raise DriverError("Stellaris has an empty client rectangle")
        if self.expected_resolution and self.expected_resolution != (width, height):
            raise DriverError(f"client resolution changed: expected {self.expected_resolution}, observed {(width, height)}")
        if user.GetForegroundWindow() != hwnd:
            raise DriverError("foreground window changed during identity check")
        self._target_pid = process_id.value
        return _Window(hwnd, title.value, origin.x, origin.y, width, height)

    def _check_click_target(self, window: _Window, x: int, y: int) -> None:
        """Reject overlays even when Stellaris still owns keyboard focus."""
        user = self._user32
        ga_root = 2
        root = user.GetAncestor(window.handle, ga_root)
        if not root or root != window.handle:
            raise DriverError("cannot verify Stellaris root window for click")
        hit = user.WindowFromPoint(self._wintypes.POINT(x, y))
        if not hit:
            raise DriverError("cannot identify the window at the click point")
        hit_root = user.GetAncestor(hit, ga_root)
        if not hit_root:
            raise DriverError("cannot identify the root window at the click point")
        if hit_root != root:
            raise DriverError(f"click point {(x, y)} is covered by another window; refusing input")

    def _call_gui(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Keep PyAutoGUI's emergency stop inside the driver's error boundary."""
        fail_safe_error = getattr(self._gui, "FailSafeException", ())
        try:
            return getattr(self._gui, method)(*args, **kwargs)
        except fail_safe_error as exc:
            raise DriverError("PyAutoGUI fail-safe triggered; game input interrupted") from exc

    def _check_stop(self) -> None:
        stop_file = getattr(self, "stop_file", None)
        if stop_file is not None and stop_file.exists():
            raise DriverError("STOP file exists; game input interrupted")
        gui = getattr(self, "_gui", None)
        if gui is not None and hasattr(gui, "failSafeCheck"):
            self._call_gui("failSafeCheck")

    def _wait(self, seconds: float) -> None:
        """Bounded waits remain interruptible by STOP, focus loss and fail-safe."""
        deadline = time.monotonic() + seconds
        while True:
            self._check_stop()
            self._window()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    def client_position(self) -> dict:
        """Return mouse coordinates for manual calibration, without clicking."""
        window = self._window()
        x, y = self._call_gui("position")
        if not window.left <= x < window.left + window.width or not window.top <= y < window.top + window.height:
            raise DriverError("mouse is outside Stellaris's client area")
        return {
            "x": (x - window.left) / max(window.width - 1, 1),
            "y": (y - window.top) / max(window.height - 1, 1),
            "width": window.width,
            "height": window.height,
            "title": window.title,
        }

    def execute(self, steps: list[dict]) -> None:
        validate_steps(steps)
        for step in steps:
            self._check_stop()
            window = self._window()
            kind = step["kind"]
            if kind == "click":
                x, y = client_point(step["x"], step["y"], window.left, window.top, window.width, window.height)
                self._check_click_target(window, x, y)
                self._call_gui("moveTo", x, y, duration=0.08)
                current = self._window()
                if current != window:
                    raise DriverError("Stellaris window moved or changed before click")
                self._check_click_target(current, x, y)
                self._call_gui("click", button=step.get("button", "left"))
            elif kind == "key":
                self._call_gui("press", step["key"])
            elif kind == "hotkey":
                self._call_gui("hotkey", *step["keys"])
            else:
                self._wait(step["seconds"])
        # A successful transport is not a successful game action. The caller
        # must wait for and verify a fresh game save before committing success.

    def execute_event(self, event_id: str, country_id: str, console_key: str = "`") -> None:
        """Send one fixed project-owned event, never arbitrary console text.

        Only the controller chooses event IDs; LLM-provided event strings must
        never reach this method. The caller rejects ironman and multiplayer.
        ``console_key`` must be calibrated on the actual keyboard/game setup.
        Returning means transport completed, not that the game applied effects.
        On interrupted input, leave the console state for local inspection
        instead of blindly toggling an unknown UI state in a cleanup handler.
        """
        if not isinstance(event_id, str) or event_id not in {"saa.1", "saa.2", "saa.20"}:
            raise DriverError("event is outside the project event allowlist")
        if not isinstance(country_id, str) or not country_id or not country_id.isascii() or not country_id.isdecimal():
            raise DriverError("country_id must contain ASCII decimal digits only")
        if len(country_id) > 10:
            raise DriverError("country_id is too long")
        if not isinstance(console_key, str) or console_key not in {"`", "f2", "f12", "\\"}:
            raise DriverError("console_key must be a supported calibrated console key")
        self._check_stop()
        self._window()
        self._call_gui("press", console_key)
        self._wait(0.15)
        for character in f"event {event_id} {country_id}":
            self._check_stop()
            self._window()
            self._call_gui("write", character, interval=0.03)
        self._check_stop()
        self._window()
        self._call_gui("press", "enter")
        self._wait(0.15)
        self._window()
        self._call_gui("press", console_key)
