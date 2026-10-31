import sys

import pytest

from stellaris_autopilot.windows import DriverError, WindowsDriver, client_point, validate_steps


def test_valid_game_macro():
    validate_steps([
        {"kind": "click", "x": 0.3, "y": 0.9},
        {"kind": "click", "x": 1, "y": 0, "button": "right"},
        {"kind": "hotkey", "keys": ["ctrl", "shift", "1"]},
        {"kind": "key", "key": "escape"},
        {"kind": "wait", "seconds": 0.2},
    ])


@pytest.mark.parametrize("steps", [
    [], {}, [{"kind": "shell", "command": "anything"}],
    [{"kind": "click", "x": True, "y": 0.5}],
    [{"kind": "click", "x": float("nan"), "y": 0.5}],
    [{"kind": "click", "x": 0.5, "y": float("inf")}],
    [{"kind": "click", "x": -0.01, "y": 0.5}],
    [{"kind": "click", "x": 0.5, "y": 0.5, "button": "middle"}],
    [{"kind": "key", "key": "win"}],
    [{"kind": "key", "key": "ctrl"}],
    [{"kind": "key", "key": []}],
    [{"kind": "key", "key": "enter", "text": "run anything"}],
    [{"kind": "hotkey", "keys": ["alt", "f4"]}],
    [{"kind": "hotkey", "keys": ["ctrl", "escape"]}],
    [{"kind": "hotkey", "keys": ["ctrl", "shift", "escape"]}],
    [{"kind": "hotkey", "keys": ["ctrl", "ctrl", "s"]}],
    [{"kind": "hotkey", "keys": ["a", "b"]}],
    [{"kind": "hotkey", "keys": ["ctrl", "s", "shift"]}],
    [{"kind": "wait", "seconds": True}],
    [{"kind": "wait", "seconds": 6}],
    [{"kind": "wait", "seconds": 5}] * 7,
    [{"kind": "key", "key": "1"}] * 65,
    [None],
])
def test_rejects_malformed_or_unsafe_input(steps):
    with pytest.raises(DriverError):
        validate_steps(steps)


def test_normalized_coordinates_stay_inside_window():
    assert client_point(0, 0, 100, 50, 1920, 1080) == (100, 50)
    assert client_point(1, 1, 100, 50, 1920, 1080) == (2019, 1129)
    assert client_point(0.5, 0.5, -1920, 0, 1920, 1080) == (-960, 540)
    with pytest.raises(DriverError):
        client_point(0.5, 0.5, 0, 0, 0, 1080)


@pytest.mark.skipif(sys.platform == "win32", reason="Linux-only platform rejection")
def test_linux_does_not_import_pyautogui_or_send_input():
    with pytest.raises(DriverError, match="Windows 10/11"):
        WindowsDriver()


def test_invalid_later_step_prevents_earlier_input():
    driver = object.__new__(WindowsDriver)
    driver._window = lambda: pytest.fail("must validate all steps before inspecting GUI")
    with pytest.raises(DriverError):
        driver.execute([{"kind": "key", "key": "1"}, {"kind": "key", "key": "win"}])


@pytest.mark.parametrize("event_id,country_id,console_key", [
    ("saa.999", "0", "`"),
    ("saa.1\nother_command", "0", "`"),
    ("saa.1", "-1", "`"),
    ("saa.1", "1\nother_command", "`"),
    ("saa.1", "１２", "`"),
    ("saa.1", "", "`"),
    ("saa.1", 0, "`"),
    ("saa.1", "0", "win"),
    ([], "0", "`"),
    ("saa.1", "0", []),
])
def test_event_command_rejects_text_injection(event_id, country_id, console_key):
    driver = object.__new__(WindowsDriver)
    driver._window = lambda: pytest.fail("bad event must be rejected before GUI access")
    with pytest.raises(DriverError):
        driver.execute_event(event_id, country_id, console_key)


def test_event_is_fixed_and_closes_console(monkeypatch):
    calls = []
    class GUI:
        def press(self, key):
            calls.append(("press", key))
        def write(self, text, interval):
            calls.append(("write", text, interval))
    driver = object.__new__(WindowsDriver)
    driver._window = lambda: calls.append(("guard",))
    driver._gui = GUI()
    monkeypatch.setattr(driver, "_wait", lambda seconds: None)
    driver.execute_event("saa.20", "3")
    assert "".join(call[1] for call in calls if call[0] == "write") == "event saa.20 3"
    assert calls[-1] == ("press", "`")
    assert calls[-2] == ("guard",)


def test_event_focus_loss_never_types_into_other_app(monkeypatch):
    calls = []
    guard_calls = 0
    def guard():
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls > 1:
            raise DriverError("focus lost")
    class GUI:
        def press(self, key):
            calls.append(("press", key))
        def write(self, text, interval):
            pytest.fail("must not type after focus loss")
    driver = object.__new__(WindowsDriver)
    driver._window = guard
    driver._gui = GUI()
    monkeypatch.setattr(driver, "_wait", lambda seconds: None)
    with pytest.raises(DriverError, match="focus lost"):
        driver.execute_event("saa.1", "0")
    assert calls == [("press", "`")]


def test_click_rechecks_window_after_mouse_move():
    calls = []
    guard_calls = 0
    from stellaris_autopilot.windows import _Window
    def guard():
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls > 1:
            raise DriverError("focus lost")
        return _Window(1, "Stellaris", 0, 0, 1920, 1080)
    class GUI:
        def moveTo(self, x, y, duration):
            calls.append((x, y))
        def click(self, button):
            pytest.fail("must not click after focus loss")
    driver = object.__new__(WindowsDriver)
    driver._window = guard
    driver._gui = GUI()
    with pytest.raises(DriverError, match="focus lost"):
        driver.execute([{"kind": "click", "x": 0.5, "y": 0.5}])
    assert calls == [(960, 540)]


def test_event_rechecks_focus_between_characters(monkeypatch):
    calls = []
    focus_lost = False
    def guard():
        if focus_lost:
            raise DriverError("focus lost")
    class GUI:
        def press(self, key):
            calls.append(("press", key))
        def write(self, text, interval):
            nonlocal focus_lost
            calls.append(("write", text))
            focus_lost = True
    driver = object.__new__(WindowsDriver)
    driver._window = guard
    driver._gui = GUI()
    monkeypatch.setattr(driver, "_wait", lambda seconds: None)
    with pytest.raises(DriverError, match="focus lost"):
        driver.execute_event("saa.1", "0")
    assert calls == [("press", "`"), ("write", "e")]


def test_interrupted_event_does_not_toggle_uncertain_console_state():
    calls = []
    class GUI:
        def press(self, key):
            calls.append(key)
        def write(self, text, interval):
            raise DriverError("partial input failure")
    driver = object.__new__(WindowsDriver)
    driver._window = lambda: None
    driver._wait = lambda seconds: None
    driver._gui = GUI()
    with pytest.raises(DriverError, match="partial input"):
        driver.execute_event("saa.1", "0")
    assert calls == ["`"]


def test_stop_file_interrupts_wait_before_next_input(tmp_path, monkeypatch):
    stop_file = tmp_path / "STOP"
    sleeps = []
    class GUI:
        def press(self, key):
            pytest.fail("STOP during wait must prevent the next key")
    driver = object.__new__(WindowsDriver)
    driver.stop_file = stop_file
    driver._window = lambda: None
    driver._gui = GUI()
    def sleep(seconds):
        sleeps.append(seconds)
        stop_file.touch()
    monkeypatch.setattr("stellaris_autopilot.windows.time.sleep", sleep)
    with pytest.raises(DriverError, match="STOP file"):
        driver.execute([{"kind": "wait", "seconds": 5}, {"kind": "key", "key": "1"}])
    assert sleeps == [0.05]


def test_stop_file_prevents_console_opening(tmp_path):
    stop_file = tmp_path / "STOP"
    stop_file.touch()
    driver = object.__new__(WindowsDriver)
    driver.stop_file = stop_file
    driver._window = lambda: pytest.fail("must stop before inspecting the game")
    with pytest.raises(DriverError, match="STOP file"):
        driver.execute_event("saa.1", "0")


def test_wait_observes_pyautogui_failsafe_without_next_input():
    class GUI:
        def failSafeCheck(self):
            raise DriverError("fail-safe triggered")
    driver = object.__new__(WindowsDriver)
    driver._window = lambda: None
    driver._gui = GUI()
    with pytest.raises(DriverError, match="fail-safe"):
        driver.execute([{"kind": "wait", "seconds": 5}])


def test_window_identity_is_pinned_to_first_verified_game_process():
    from ctypes import wintypes

    class User:
        pid = 123
        def GetForegroundWindow(self):
            return 1
        def IsIconic(self, hwnd):
            return False
        def GetWindowTextLengthW(self, hwnd):
            return len("Stellaris")
        def GetWindowTextW(self, hwnd, title, length):
            title.value = "Stellaris"
            return len(title.value)
        def GetWindowThreadProcessId(self, hwnd, process_id):
            process_id._obj.value = self.pid
            return 10
        def GetClientRect(self, hwnd, rect):
            rect._obj.right, rect._obj.bottom = 1920, 1080
            return True
        def ClientToScreen(self, hwnd, point):
            point._obj.x, point._obj.y = 10, 20
            return True
    class Kernel:
        opens = 0
        closed = []
        def OpenProcess(self, rights, inherit, pid):
            self.opens += 1
            return 17
        def QueryFullProcessImageNameW(self, handle, flags, path, size):
            path.value = r"C:\Games\Stellaris\stellaris.exe"
            return True
        def CloseHandle(self, handle):
            self.closed.append(handle)
            return True
    driver = object.__new__(WindowsDriver)
    driver._wintypes = wintypes
    driver._user32, driver._kernel32 = User(), Kernel()
    driver._target_pid = None
    driver.title_hint = "stellaris"
    driver.expected_resolution = (1920, 1080)
    window = driver._window()
    assert (window.left, window.top, window.width, window.height) == (10, 20, 1920, 1080)
    assert driver._target_pid == 123
    assert driver._kernel32.closed == [17]
    driver._user32.pid = 999
    with pytest.raises(DriverError, match="game process changed"):
        driver._window()
    assert driver._kernel32.opens == 1
