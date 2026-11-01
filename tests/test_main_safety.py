"""main 安全监控桥接测试：F12 停止 / F11 暂停恢复 / 心跳超时暂停。

回归背景：主循环曾把 ``SafetyMonitor.controller`` 属性当函数调用，启动即
``TypeError: 'StopController' object is not callable``。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from src.execution.safety import (
    FakeKeyStateSource,
    SafetyEvent,
    SafetyEventKind,
    SafetyMonitor,
    StopController,
)
from src.main import (
    EXIT_RUNTIME_ERROR,
    _cleanup_partial_runtime,
    _ensure_game_foreground,
    _EventBridge,
    _make_safety_event_handler,
    _reset_input_after_failure,
    _run_accounts,
    _sync_safety_controller,
)


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


# ---------------------------------------------------------------------------
# ISS-005：安全事件在监控线程内直接关闭输入门禁并请求释放
# ---------------------------------------------------------------------------


class _FakeGateExecutor:
    def __init__(self, *, fail: bool = False) -> None:
        self.closed: list[str] = []
        self.fail = fail

    def close_gate_and_release(self, reason: str = "") -> int:
        if self.fail:
            raise RuntimeError("关闭门禁失败")
        self.closed.append(reason)
        return 1


class _FakeLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, fmt: str, *args) -> None:  # noqa: ANN002 - 测试替身
        self.warnings.append(fmt % args if args else fmt)


@pytest.mark.parametrize(
    "kind",
    [SafetyEventKind.EMERGENCY_STOP, SafetyEventKind.HEARTBEAT_TIMEOUT],
)
def test_safety_event_handler_closes_input_gate(kind: SafetyEventKind) -> None:
    executor = _FakeGateExecutor()
    logger = _FakeLogger()
    handler = _make_safety_event_handler(executor, logger)  # type: ignore[arg-type]
    handler(SafetyEvent(kind, 1.0))
    assert executor.closed == [f"safety:{kind.value}"]


def test_safety_event_handler_ignores_resume_event() -> None:
    executor = _FakeGateExecutor()
    handler = _make_safety_event_handler(executor, _FakeLogger())  # type: ignore[arg-type]
    handler(SafetyEvent(SafetyEventKind.RESUME, 1.0))
    assert executor.closed == []


def test_safety_event_handler_logs_gate_failure() -> None:
    executor = _FakeGateExecutor(fail=True)
    logger = _FakeLogger()
    handler = _make_safety_event_handler(executor, logger)  # type: ignore[arg-type]
    handler(SafetyEvent(SafetyEventKind.EMERGENCY_STOP, 1.0))
    assert any("关闭输入门禁失败" in message for message in logger.warnings)


# ---------------------------------------------------------------------------
# ISS-008：装配中途失败回收已创建资源（逆序、异常隔离）
# ---------------------------------------------------------------------------


def test_cleanup_partial_runtime_reversed_and_error_isolated() -> None:
    calls: list[str] = []

    class _Resource:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        def stop(self, drain: bool = True) -> None:
            calls.append(f"stop:{self.name}")
            if self.fail:
                raise RuntimeError("stop failed")

        def close(self) -> None:
            calls.append(f"close:{self.name}")

    logger = _FakeLogger()
    started = [
        ("engine", _Resource("engine")),
        ("events", _Resource("events", fail=True)),
        ("executor", _Resource("executor")),
    ]
    _cleanup_partial_runtime(started, logger)  # type: ignore[arg-type]
    assert calls == ["stop:executor", "stop:events", "close:engine"]
    assert any("events" in message for message in logger.warnings)


def test_reset_input_after_failure_helper() -> None:
    class _Ok:
        def reset_after_failure(self) -> bool:
            return True

    class _Bad:
        def reset_after_failure(self) -> bool:
            raise RuntimeError("bad backend")

    class _Missing:
        pass

    logger = SimpleNamespace(warning=lambda *a, **k: None)
    assert _reset_input_after_failure(SimpleNamespace(executor=_Ok(), logger=logger)) is True
    assert _reset_input_after_failure(SimpleNamespace(executor=_Bad(), logger=logger)) is False
    assert _reset_input_after_failure(SimpleNamespace(executor=_Missing(), logger=logger)) is True


# ---------------------------------------------------------------------------
# ISS-008：会话异常也执行统一清理并返回运行时错误码
# ---------------------------------------------------------------------------


def test_run_accounts_tears_down_runtime_when_session_raises(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    import src.main as main_mod

    app = SimpleNamespace(account=None, global_config=SimpleNamespace(paths=SimpleNamespace(data_dir="data")))
    runtime = SimpleNamespace()
    teardown_calls: list[object] = []

    monkeypatch.setattr(main_mod, "_load_app", lambda *a, **k: app)
    monkeypatch.setattr(main_mod, "validate_app", lambda app_: [])
    monkeypatch.setattr(main_mod, "_build_runtime", lambda app_: runtime)
    monkeypatch.setattr(main_mod, "_teardown", lambda rt: teardown_calls.append(rt))

    def _boom(runtime_, *, max_battles=None):  # noqa: ANN001
        raise RuntimeError("模拟会话异常")

    monkeypatch.setattr(main_mod, "_run_session", _boom)
    args = SimpleNamespace(profile="cn360", ship="test", log_level=None, max_battles=None)
    assert _run_accounts(args, tmp_path, [None], None) == EXIT_RUNTIME_ERROR
    assert teardown_calls == [runtime]


# ---------------------------------------------------------------------------
# ISS-010：_EventBridge 使用状态机回调维护的当前场次编号
# ---------------------------------------------------------------------------


def test_event_bridge_uses_current_battle_context() -> None:
    recorded: list[tuple[int | None, str]] = []

    class _Sink:
        def log_action(self, battle_id, action_type, payload) -> None:  # noqa: ANN001
            recorded.append((battle_id, action_type))

    bridge = _EventBridge(_Sink())  # type: ignore[arg-type]
    bridge.on_nav("autopilot_click", {"event": "autopilot_click"})
    assert recorded[-1][0] is None
    bridge.set_battle_id(7)
    bridge.on_behavior("fire", {"event": "fire"})
    assert recorded[-1] == (7, "fire")
    bridge.set_battle_id(None)
    bridge.on_nav("autopilot_confirm", {})
    assert recorded[-1][0] is None
