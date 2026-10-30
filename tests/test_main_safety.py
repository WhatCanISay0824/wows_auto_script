"""main 安全监控桥接测试：F12 停止 / F11 暂停恢复 / 心跳超时暂停。

回归背景：主循环曾把 ``SafetyMonitor.controller`` 属性当函数调用，启动即
``TypeError: 'StopController' object is not callable``。
"""

from __future__ import annotations

from types import SimpleNamespace

from src.execution.safety import FakeKeyStateSource, SafetyMonitor, StopController
from src.main import _ensure_game_foreground, _sync_safety_controller


class _FakeState:
    value = "INIT"


class _FakeMachine:
    def __init__(self) -> None:
        self.state = _FakeState()
        self.stops: list[tuple[str, str]] = []
        self.pauses: list[tuple[str, str]] = []
        self.resumes = 0

    def request_stop(self, reason: str, detail: str = "") -> None:
        self.stops.append((reason, detail))

    def request_pause(self, reason: str, detail: str = "") -> None:
        self.pauses.append((reason, detail))

    def request_resume(self) -> None:
        self.resumes += 1


def test_monitor_controller_is_a_property_not_callable() -> None:
    controller = StopController()
    monitor = SafetyMonitor(FakeKeyStateSource(), controller=controller)
    assert monitor.controller is controller  # 回归：不能是 monitor.controller()


def test_stop_request_is_forwarded_to_machine() -> None:
    controller = StopController()
    machine = _FakeMachine()
    assert _sync_safety_controller(machine, controller) is False
    controller.request_stop("emergency_stop", "f12")
    assert _sync_safety_controller(machine, controller) is False
    assert machine.stops == [("emergency_stop", "f12")]


def test_pause_request_is_forwarded_and_resume_clears_it() -> None:
    controller = StopController()
    machine = _FakeMachine()
    controller.request_pause("heartbeat_timeout", "10s")
    assert _sync_safety_controller(machine, controller) is True
    assert machine.pauses == [("heartbeat_timeout", "10s")]
    controller.resume()
    assert _sync_safety_controller(machine, controller) is False
    assert machine.resumes == 0  # resume 由主循环的 was_paused 逻辑触发，不在此函数内


def test_stop_not_forwarded_when_already_stopping() -> None:
    controller = StopController()
    machine = _FakeMachine()
    machine.state.value = "STOPPING"
    controller.request_stop("emergency_stop", "f12")
    _sync_safety_controller(machine, controller)
    assert machine.stops == []


class _FakeLocator:
    def __init__(self, *, foreground: bool, focus_succeeds: bool) -> None:
        self.foreground = foreground
        self.focus_succeeds = focus_succeeds
        self.brings = 0

    def find(self) -> SimpleNamespace | None:
        return SimpleNamespace(hwnd=123, foreground=self.foreground)

    def bring_to_foreground(self, hwnd: int) -> bool:
        self.brings += 1
        if self.focus_succeeds:
            self.foreground = True
        return self.focus_succeeds


def test_ensure_game_foreground_focuses_window_when_background() -> None:
    locator = _FakeLocator(foreground=False, focus_succeeds=True)
    runtime = SimpleNamespace(window_locator=locator, logger=SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None))
    assert _ensure_game_foreground(runtime) is True
    assert locator.brings == 1


def test_ensure_game_foreground_returns_false_when_focus_fails() -> None:
    locator = _FakeLocator(foreground=False, focus_succeeds=False)
    runtime = SimpleNamespace(window_locator=locator, logger=SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None))
    assert _ensure_game_foreground(runtime) is False


def test_ensure_game_foreground_noop_when_already_foreground() -> None:
    locator = _FakeLocator(foreground=True, focus_succeeds=True)
    runtime = SimpleNamespace(window_locator=locator, logger=SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None))
    assert _ensure_game_foreground(runtime) is True
    assert locator.brings == 0
