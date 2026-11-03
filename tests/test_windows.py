import ctypes
import sys
from ctypes import wintypes
from types import SimpleNamespace

import pytest

from stellaris_autopilot.windows import DriverError, WindowsDriver, client_point, validate_steps


class FakePyAutoGUIException(Exception):
    pass


class FakeFailSafeException(FakePyAutoGUIException):
    """Match PyAutoGUI's hierarchy without importing its native input backend."""


class FakeHitTestUser32:
    """A point may hit a child or overlay while the game remains foreground."""

    def __init__(self, hit=1, roots=None):
        self.hit = hit
        self.roots = {1: 1} if roots is None else roots
        self.points = []

    def WindowFromPoint(self, point):
        self.points.append((point.x, point.y))
        return self.hit

    def GetAncestor(self, hwnd, flags):
        assert flags == 2  # GA_ROOT, preserving child-window membership.
        return self.roots.get(hwnd, 0)


def install_fake_hit_test(driver, user=None):
    driver._wintypes = wintypes
    driver._user32 = FakeHitTestUser32() if user is None else user


@pytest.fixture
def click_driver():
    from stellaris_autopilot.windows import _Window

    calls = []
    class GUI:
        def moveTo(self, x, y, duration):
            calls.append(("moveTo", x, y))
        def click(self, button):
            calls.append(("click", button))
        def press(self, key):
            calls.append(("press", key))
    driver = object.__new__(WindowsDriver)
    driver._window = lambda: _Window(1, "Stellaris", 0, 0, 1920, 1080)
    driver._gui = GUI()
    install_fake_hit_test(driver)
    return driver, calls


def test_hit_test_win32_bindings_use_point_by_value_and_pointer_sized_handles(monkeypatch):
    class Library:
        def __getattr__(self, name):
            function = SimpleNamespace()
            setattr(self, name, function)
            return function
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: Library(), raising=False)
    driver = object.__new__(WindowsDriver)
    driver._bind_win32()
    assert driver._user32.WindowFromPoint.argtypes == [wintypes.POINT]
    assert driver._user32.WindowFromPoint.restype is wintypes.HWND
    assert driver._user32.GetAncestor.argtypes == [wintypes.HWND, wintypes.UINT]
    assert driver._user32.GetAncestor.restype is wintypes.HWND


@pytest.mark.parametrize("hit", [1, 2])
def test_click_accepts_verified_game_root_and_game_child(click_driver, hit):
    driver, calls = click_driver
    install_fake_hit_test(driver, FakeHitTestUser32(hit=hit, roots={1: 1, 2: 1}))
    driver.execute([{"kind": "click", "x": 0.5, "y": 0.5}])
    assert calls == [("moveTo", 960, 540), ("click", "left")]
    assert driver._user32.points == [(960, 540), (960, 540)]


@pytest.mark.parametrize("hit,roots", [(2, {1: 1, 2: 2}), (3, {1: 1, 3: 2})])
def test_click_rejects_foreign_overlay_before_moving_even_with_game_foreground(click_driver, hit, roots):
    driver, calls = click_driver
    install_fake_hit_test(driver, FakeHitTestUser32(hit=hit, roots=roots))
    with pytest.raises(DriverError, match="covered by another window"):
        driver.execute([{"kind": "click", "x": 0.5, "y": 0.5}, {"kind": "key", "key": "2"}])
    assert calls == []


@pytest.mark.parametrize("hit,roots,message", [
    (0, {1: 1}, "cannot identify the window"),
    (2, {1: 1}, "cannot identify the root window"),
    (1, {}, "cannot verify Stellaris root window"),
    (1, {1: 2}, "cannot verify Stellaris root window"),
])
def test_click_hit_test_failure_prevents_all_input(click_driver, hit, roots, message):
    driver, calls = click_driver
    install_fake_hit_test(driver, FakeHitTestUser32(hit=hit, roots=roots))
    with pytest.raises(DriverError, match=message):
        driver.execute([{"kind": "click", "x": 0.5, "y": 0.5}, {"kind": "key", "key": "2"}])
    assert calls == []


@pytest.mark.parametrize("new_hit,roots,message", [
    (2, {1: 1, 2: 2}, "covered by another window"),
    (0, {1: 1}, "cannot identify the window"),
    (2, {1: 1}, "cannot identify the root window"),
])
def test_click_rechecks_point_after_mouse_move(click_driver, new_hit, roots, message):
    driver, calls = click_driver
    user = FakeHitTestUser32(roots=roots)
    install_fake_hit_test(driver, user)
    def move_to(x, y, duration):
        calls.append(("moveTo", x, y))
        user.hit = new_hit
    driver._gui.moveTo = move_to
    with pytest.raises(DriverError, match=message):
        driver.execute([{"kind": "click", "x": 0.5, "y": 0.5}, {"kind": "key", "key": "2"}])
    assert calls == [("moveTo", 960, 540)]
    assert user.points == [(960, 540), (960, 540)]


def test_click_still_rejects_game_window_movement_after_mouse_move(click_driver):
    from stellaris_autopilot.windows import _Window

    driver, calls = click_driver
    windows = iter([
        _Window(1, "Stellaris", 0, 0, 1920, 1080),
        _Window(1, "Stellaris", 100, 0, 1920, 1080),
    ])
    driver._window = lambda: next(windows)
    with pytest.raises(DriverError, match="window moved or changed"):
        driver.execute([{"kind": "click", "x": 0.5, "y": 0.5}, {"kind": "key", "key": "2"}])
    assert calls == [("moveTo", 960, 540)]


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
    install_fake_hit_test(driver)
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
        FailSafeException = FakeFailSafeException
        def failSafeCheck(self):
            raise FakeFailSafeException("corner reached")
        def press(self, key):
            pytest.fail("must not send input after the emergency stop")
    driver = object.__new__(WindowsDriver)
    driver._window = lambda: None
    driver._gui = GUI()
    with pytest.raises(DriverError, match="fail-safe") as caught:
        driver.execute([{"kind": "wait", "seconds": 5}, {"kind": "key", "key": "1"}])
    assert isinstance(caught.value.__cause__, FakeFailSafeException)


@pytest.mark.parametrize("method,step,expected_calls", [
    ("moveTo", {"kind": "click", "x": 0.5, "y": 0.5}, ["moveTo"]),
    ("click", {"kind": "click", "x": 0.5, "y": 0.5}, ["moveTo", "click"]),
    ("press", {"kind": "key", "key": "1"}, ["press"]),
    ("hotkey", {"kind": "hotkey", "keys": ["ctrl", "s"]}, ["hotkey"]),
])
def test_input_call_failsafe_is_driver_error_and_prevents_later_input(method, step, expected_calls):
    from stellaris_autopilot.windows import _Window

    calls = []
    class GUI:
        FailSafeException = FakeFailSafeException
        def __getattr__(self, name):
            def invoke(*args, **kwargs):
                if name == "failSafeCheck":
                    return
                calls.append(name)
                if name == method:
                    raise FakeFailSafeException("corner reached during input")
            return invoke
    driver = object.__new__(WindowsDriver)
    driver._window = lambda: _Window(1, "Stellaris", 0, 0, 1920, 1080)
    driver._gui = GUI()
    install_fake_hit_test(driver)
    with pytest.raises(DriverError, match="fail-safe"):
        driver.execute([step, {"kind": "key", "key": "2"}])
    assert calls == expected_calls


def test_console_write_failsafe_preserves_partial_input_without_cleanup():
    calls = []
    class GUI:
        FailSafeException = FakeFailSafeException
        def press(self, key):
            calls.append(("press", key))
        def write(self, text, interval):
            calls.append(("write", text))
            raise FakeFailSafeException("corner reached while typing")
    driver = object.__new__(WindowsDriver)
    driver._window = lambda: None
    driver._wait = lambda seconds: None
    driver._gui = GUI()
    with pytest.raises(DriverError, match="fail-safe"):
        driver.execute_event("saa.1", "0")
    assert calls == [("press", "`"), ("write", "e")]


@pytest.mark.parametrize("error", [KeyboardInterrupt(), ValueError("unrelated GUI error")])
def test_gui_boundary_preserves_other_exceptions_without_later_input(error):
    calls = []
    class GUI:
        FailSafeException = FakeFailSafeException
        def press(self, key):
            calls.append(key)
            raise error
    driver = object.__new__(WindowsDriver)
    driver._window = lambda: None
    driver._gui = GUI()
    with pytest.raises(type(error)) as caught:
        driver.execute([{"kind": "key", "key": "1"}, {"kind": "key", "key": "2"}])
    assert caught.value is error
    assert calls == ["1"]


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
