"""安全控制单元测试（详细设计 2.4 / 9.5 / 13.5，评审补充 P1-1）。

``GetAsyncKeyState`` 通过 :class:`FakeKeyStateSource` 注入，不读取真实键盘；
看门狗逻辑全部用假时钟单步驱动，不真正等待 20 Hz 轮询。
"""

from __future__ import annotations

import threading

import pytest
from src.common.clock import FakeClock
from src.common.errors import SafetyStop
from src.config.models import SafetyConfig
from src.execution.safety import (
    FakeKeyStateSource,
    KeyStateSource,
    SafetyEvent,
    SafetyEventKind,
    SafetyMonitor,
    SafetyWatchdog,
    StopController,
    Win32KeyStateSource,
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=0.0)


@pytest.fixture
def keys() -> FakeKeyStateSource:
    return FakeKeyStateSource()


def make_monitor(
    keys: FakeKeyStateSource,
    clock: FakeClock,
    *,
    heartbeat_s: float = 10.0,
    controller: StopController | None = None,
) -> SafetyMonitor:
    return SafetyMonitor(
        keys,
        stop_key="f12",
        resume_key="f11",
        heartbeat_s=heartbeat_s,
        controller=controller,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# 键盘状态源
# ---------------------------------------------------------------------------


def test_fake_key_state_source() -> None:
    source = FakeKeyStateSource()
    assert isinstance(source, KeyStateSource)
    assert source.is_down("f12") is False

    source.press("f12")
    assert source.is_down("f12") is True
    assert source.is_down("F12") is True  # 大小写不敏感
    source.release("f12")
    assert source.is_down("f12") is False

    source.press("mouse_left")
    assert source.is_down("mouse_left") is True
    assert source.is_down("lbutton") is True  # 别名解析为同一规范名
    assert source.down_keys == frozenset({"mouse_left"})
    source.release_all()
    assert source.down_keys == frozenset()


def test_key_state_source_protocol_accepts_fake() -> None:
    assert isinstance(FakeKeyStateSource(), KeyStateSource)
    # 真实实现只做 GetAsyncKeyState 查询，构造本身不触发任何键鼠事件
    assert isinstance(Win32KeyStateSource, type)


# ---------------------------------------------------------------------------
# StopController 共享标志


def test_conditional_resume_cannot_clear_new_pause_or_stop():
    controller = StopController()
    old = controller.request_pause("window_unfocused")
    assert controller.resume(expected_pause=old)
    manual = controller.request_pause("manual")
    assert not controller.resume(expected_pause=old)
    assert controller.pause_request is manual
    controller.request_stop("hotkey")
    assert not controller.resume(expected_pause=manual)
    assert controller.stop_requested
# ---------------------------------------------------------------------------


def test_stop_controller_keeps_first_stop_reason() -> None:
    controller = StopController()
    assert controller.running
    first = controller.request_stop("emergency_stop", "F12", "safety_hotkey")
    second = controller.request_stop("session_limit")
    assert first is second
    assert controller.stop_request is not None
    assert controller.stop_request.reason == "emergency_stop"
    assert controller.stop_requested is True
    assert controller.running is False
    # 停止状态下的暂停请求被忽略
    assert controller.request_pause("unfocused") is None
    assert controller.paused is False
    assert controller.resume() is False


def test_stop_controller_pause_and_resume() -> None:
    controller = StopController()
    request = controller.request_pause("unfocused", "窗口失去焦点")
    assert request is not None
    assert request.reason == "unfocused"
    assert controller.paused is True
    assert controller.running is False

    # 暂停原因幂等：保留第一次
    controller.request_pause("heartbeat_timeout")
    assert controller.pause_request is not None
    assert controller.pause_request.reason == "unfocused"

    assert controller.resume() is True
    assert controller.paused is False
    assert controller.running is True
    assert controller.resume() is False  # 未暂停时恢复返回 False


def test_stop_controller_raise_if_stopped() -> None:
    controller = StopController()
    controller.raise_if_stopped()  # 运行中不抛异常
    request = controller.request_stop("emergency_stop", "F12")
    with pytest.raises(SafetyStop) as excinfo:
        controller.raise_if_stopped()
    assert excinfo.value.request is request


def test_stop_controller_reset() -> None:
    controller = StopController()
    controller.request_stop("emergency_stop")
    controller.reset()
    assert controller.running is True
    assert controller.stop_request is None
    assert controller.pause_request is None


# ---------------------------------------------------------------------------
# 紧急停止 / 恢复热键
# ---------------------------------------------------------------------------


def test_emergency_stop_hotkey_edge_detection(keys, clock) -> None:
    monitor = make_monitor(keys, clock)
    monitor.heartbeat(0.0)

    keys.press("f12")
    events = monitor.poll(0.1)
    assert len(events) == 1
    assert events[0].kind is SafetyEventKind.EMERGENCY_STOP
    assert events[0].request is not None
    assert events[0].request.reason == "emergency_stop"
    assert monitor.controller.stop_requested is True

    # 按住不重复触发
    assert monitor.poll(0.2) == []
    keys.release("f12")
    assert monitor.poll(0.3) == []
    # 已停止后再次按下也不重复产生事件
    keys.press("f12")
    assert monitor.poll(0.4) == []
    assert monitor.controller.stop_request is not None
    assert monitor.controller.stop_request.reason == "emergency_stop"


def test_f11_resume_from_pause(keys, clock) -> None:
    controller = StopController()
    controller.request_pause("unfocused", "窗口失去焦点")
    monitor = make_monitor(keys, clock, controller=controller)
    monitor.heartbeat(0.0)

    keys.press("f11")
    events = monitor.poll(0.1)
    assert len(events) == 1
    assert events[0].kind is SafetyEventKind.RESUME
    assert controller.running is True

    # 继续按住不会反复恢复
    assert monitor.poll(0.2) == []
    keys.release("f11")
    assert monitor.poll(0.3) == []


def test_f11_without_pause_emits_nothing(keys, clock) -> None:
    monitor = make_monitor(keys, clock)
    monitor.heartbeat(0.0)
    keys.press("f11")
    assert monitor.poll(0.1) == []
    assert monitor.controller.running is True


def test_f11_cannot_resume_emergency_stop(keys, clock) -> None:
    monitor = make_monitor(keys, clock)
    monitor.heartbeat(0.0)
    keys.press("f12")
    monitor.poll(0.1)

    keys.release("f12")
    keys.press("f11")
    events = monitor.poll(0.2)
    assert all(event.kind is not SafetyEventKind.RESUME for event in events)
    assert monitor.controller.stop_requested is True
    assert monitor.controller.running is False

    keys.release("f11")
    keys.release("f12")
    assert monitor.poll(0.3) == []


def test_initial_held_stop_key_does_not_trigger(keys, clock) -> None:
    keys.press("f12")
    monitor = make_monitor(keys, clock)
    assert monitor.poll(0.1) == []
    assert monitor.controller.stop_requested is False

    keys.release("f12")
    monitor.poll(0.2)
    keys.press("f12")
    events = monitor.poll(0.3)
    assert [event.kind for event in events] == [SafetyEventKind.EMERGENCY_STOP]


# ---------------------------------------------------------------------------
# 心跳看门狗
# ---------------------------------------------------------------------------


def test_heartbeat_timeout_latches_once(keys, clock) -> None:
    monitor = make_monitor(keys, clock, heartbeat_s=10.0)
    monitor.heartbeat(0.0)

    assert monitor.poll(10.0) == [], "恰好等于阈值不触发（严格大于）"
    events = monitor.poll(10.001)
    assert len(events) == 1
    assert events[0].kind is SafetyEventKind.HEARTBEAT_TIMEOUT
    assert events[0].request is not None
    assert events[0].request.reason == "heartbeat_timeout"
    assert monitor.controller.paused is True

    # 锁存：没有新的心跳时不会反复产生事件
    assert monitor.poll(11.0) == []
    assert monitor.poll(30.0) == []
    assert monitor.controller.pause_request is not None
    assert monitor.controller.pause_request.reason == "heartbeat_timeout"


def test_heartbeat_resets_after_resume(keys, clock) -> None:
    monitor = make_monitor(keys, clock, heartbeat_s=10.0)
    monitor.heartbeat(0.0)
    monitor.poll(10.5)
    assert monitor.controller.paused is True

    assert monitor.controller.resume() is True
    monitor.heartbeat(12.0)
    assert monitor.poll(13.0) == []

    events = monitor.poll(22.5)  # 距离上次心跳 10.5s > 10s
    assert [event.kind for event in events] == [SafetyEventKind.HEARTBEAT_TIMEOUT]
    assert monitor.controller.paused is True


def test_f11_resumes_heartbeat_pause(keys, clock) -> None:
    monitor = make_monitor(keys, clock, heartbeat_s=5.0)
    monitor.heartbeat(0.0)
    events = monitor.poll(6.0)
    assert [event.kind for event in events] == [SafetyEventKind.HEARTBEAT_TIMEOUT]
    assert monitor.controller.paused is True

    keys.press("f11")
    events = monitor.poll(6.1)
    assert [event.kind for event in events] == [SafetyEventKind.RESUME]
    assert monitor.controller.running is True


def test_heartbeat_not_checked_when_already_paused(keys, clock) -> None:
    controller = StopController()
    controller.request_pause("unfocused", "窗口失去焦点")
    monitor = make_monitor(keys, clock, heartbeat_s=1.0, controller=controller)
    assert monitor.poll(100.0) == []
    assert controller.pause_request is not None
    assert controller.pause_request.reason == "unfocused"


def test_invalid_heartbeat_rejected(keys) -> None:
    with pytest.raises(ValueError, match="心跳超时时间必须为正数"):
        SafetyMonitor(keys, heartbeat_s=0)


# ---------------------------------------------------------------------------
# 配置构造与事件序列化
# ---------------------------------------------------------------------------


def test_monitor_from_safety_config(keys, clock) -> None:
    config = SafetyConfig(emergency_stop_key="f12", resume_key="f11", heartbeat_s=5.0)
    controller = StopController()
    monitor = SafetyMonitor.from_config(config, keys, controller=controller, clock=clock)
    assert monitor.heartbeat_s == 5.0
    assert monitor.controller is controller

    keys.press("f12")
    events = monitor.poll(0.1)
    assert [event.kind for event in events] == [SafetyEventKind.EMERGENCY_STOP]


def test_safety_event_to_dict() -> None:
    request = StopController().request_stop("emergency_stop", "F12", "safety_hotkey")
    event = SafetyEvent(SafetyEventKind.EMERGENCY_STOP, 12.3456, request)
    data = event.to_dict()
    assert data["kind"] == "emergency_stop"
    assert data["reason"] == "emergency_stop"
    assert data["timestamp"] == pytest.approx(12.346)


# ---------------------------------------------------------------------------
# 看门狗线程外壳
# ---------------------------------------------------------------------------


def test_watchdog_run_once_dispatches_events(keys, clock) -> None:
    monitor = make_monitor(keys, clock)
    monitor.heartbeat(0.0)
    received: list[SafetyEvent] = []
    watchdog = SafetyWatchdog(monitor, on_event=received.append)

    keys.press("f12")
    returned = watchdog.run_once()
    assert returned == received
    assert [event.kind for event in received] == [SafetyEventKind.EMERGENCY_STOP]


def test_watchdog_thread_start_stop(keys, clock) -> None:
    monitor = make_monitor(keys, clock)
    monitor.heartbeat(0.0)
    received: list[SafetyEvent] = []
    got_event = threading.Event()

    def on_event(event: SafetyEvent) -> None:
        received.append(event)
        got_event.set()

    watchdog = SafetyWatchdog(monitor, on_event=on_event, interval_s=0.005)
    keys.press("f12")  # 启动前按下，首个轮询即可检测到上升沿

    watchdog.start()
    try:
        assert got_event.wait(2.0), "看门狗线程未产生紧急停止事件"
    finally:
        watchdog.stop()
    assert not watchdog.is_running
    assert received[0].kind is SafetyEventKind.EMERGENCY_STOP


def test_watchdog_rejects_invalid_interval(keys, clock) -> None:
    monitor = make_monitor(keys, clock)
    with pytest.raises(ValueError, match="轮询间隔必须为正数"):
        SafetyWatchdog(monitor, interval_s=0)


# ---------------------------------------------------------------------------
# 过载保护事件（可选 OverloadMonitor 注入，不改变 F12/心跳/恢复行为）
# ---------------------------------------------------------------------------


def test_overload_event_kinds_available() -> None:
    assert SafetyEventKind.OVERLOAD_WARNING.value == "overload_warning"
    assert SafetyEventKind.OVERLOAD_PAUSE_REQUESTED.value == "overload_pause_requested"
    assert SafetyEventKind.OVERLOAD_STOP_REQUESTED.value == "overload_stop_requested"
    assert SafetyEventKind.OVERLOAD_RESUME_REQUESTED.value == "overload_resume_requested"


def test_safety_event_detail_is_serialized() -> None:
    event = SafetyEvent(
        SafetyEventKind.OVERLOAD_WARNING,
        1.5,
        None,
        detail={"kind": "cpu", "message": "CPU 过高"},
    )
    data = event.to_dict()
    assert data["kind"] == "overload_warning"
    assert data["extra"] == {"kind": "cpu", "message": "CPU 过高"}
    assert "reason" not in data
    frozen = SafetyEvent(SafetyEventKind.OVERLOAD_WARNING, 1.5)
    assert frozen.to_dict()["kind"] == "overload_warning"
    assert "extra" not in frozen.to_dict()


def test_safety_monitor_exposes_clock(keys, clock) -> None:
    monitor = make_monitor(keys, clock)
    assert monitor.clock is clock


def test_watchdog_dispatches_overload_events_at_1hz(keys, clock) -> None:
    class StubOverload:
        poll_interval_s = 1.0

        def __init__(self) -> None:
            self.calls: list[float] = []

        def poll(self, now: float | None = None) -> list[SafetyEvent]:
            self.calls.append(float(now or 0.0))
            return [SafetyEvent(SafetyEventKind.OVERLOAD_WARNING, float(now or 0.0))]

    monitor = make_monitor(keys, clock)
    monitor.heartbeat(0.0)
    overload = StubOverload()
    received: list[SafetyEvent] = []
    watchdog = SafetyWatchdog(monitor, on_event=received.append, overload=overload)
    assert watchdog.overload is overload

    clock.advance(0.05)
    events = watchdog.run_once()
    assert [event.kind for event in events] == [SafetyEventKind.OVERLOAD_WARNING]
    assert received == events
    # 20 Hz 循环内只按 1 Hz 调用过载 poll
    for _ in range(19):
        clock.advance(0.05)
        watchdog.run_once()
    assert len(overload.calls) == 1
    clock.advance(1.0)
    watchdog.run_once()
    assert len(overload.calls) == 2


def test_watchdog_overload_exception_does_not_kill_watchdog(keys, clock) -> None:
    class BrokenOverload:
        poll_interval_s = 1.0

        def poll(self, now: float | None = None) -> list[SafetyEvent]:
            raise RuntimeError("过载监控崩了")

    monitor = make_monitor(keys, clock)
    clock.advance(0.05)
    watchdog = SafetyWatchdog(monitor, overload=BrokenOverload())
    assert watchdog.run_once() == []
    # 后续轮询仍然工作：F12 仍能产生紧急停止
    keys.press("f12")
    clock.advance(2.0)
    events = watchdog.run_once()
    assert any(event.kind is SafetyEventKind.EMERGENCY_STOP for event in events)
