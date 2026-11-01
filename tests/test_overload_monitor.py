"""过载监控器测试（src/execution/overload.py + 与 SafetyWatchdog 的集成）。

全部用 :class:`~src.common.clock.FakeClock` 与假指标源驱动，不真正 sleep、不读真实系统；
验证 1 Hz 采样节流、pause/stop/resume 请求、monitor 模式不动手、不可用降级与看门狗集成。
"""

from __future__ import annotations

import pytest
from src.common.clock import FakeClock
from src.common.sysmetrics import MetricsCheck, SystemSample
from src.config.models import OverloadConfig
from src.execution.overload import OverloadMonitor
from src.execution.safety import (
    FakeKeyStateSource,
    SafetyEventKind,
    SafetyMonitor,
    SafetyWatchdog,
    StopController,
)
from src.runtime.messages import PerformanceThresholds, PerformanceView

TOTAL_MB = 32_768.0
COMMIT_LIMIT_MB = 51_200.0
BASE_GTC_MS = 1_000_000
BASE_QUT_100NS = 1_000_000_000
WALL_BASE = 1_700_000_000.0


def make_sample(
    t: float,
    *,
    cpu: float = 10.0,
    mem_available: float = 16_000.0,
    commit: float = 30_000.0,
    script: float = 300.0,
    sleep_total_s: float = 0.0,
) -> SystemSample:
    return SystemSample(
        mono=float(t),
        wall=WALL_BASE + float(t),
        cpu_pct=cpu,
        mem_total_mb=TOTAL_MB,
        mem_available_mb=mem_available,
        commit_limit_mb=COMMIT_LIMIT_MB,
        commit_available_mb=commit,
        script_private_mb=script,
        script_cpu_pct=1.0,
        gtc64_ms=BASE_GTC_MS + int(round(t * 1000)),
        unbiased_100ns=BASE_QUT_100NS + int(round((t - sleep_total_s) * 10_000_000)),
    )


class FakeMetrics:
    """假指标源：按假时钟返回样本，可切换 Commit 值/不可用/异常。"""

    def __init__(self, clock: FakeClock, *, commit: float = 30_000.0) -> None:
        self.clock = clock
        self.commit = commit
        self.cpu = 10.0
        self.mem_available = 16_000.0
        self.script = 300.0
        self.sleep_total_s = 0.0
        self.fail: str | None = None
        self.samples = 0
        self.check_result: MetricsCheck | None = None

    @property
    def available(self) -> bool:
        return self.fail != "unavailable"

    def sample(self, now: float | None = None) -> SystemSample:
        self.samples += 1
        if self.fail == "exception":
            raise RuntimeError("模拟采样失败")
        moment = self.clock.now() if now is None else float(now)
        if self.fail == "unavailable":
            return SystemSample(mono=moment, wall=WALL_BASE + moment, available=False, errors=("模拟不可用",))
        return make_sample(
            moment,
            cpu=self.cpu,
            mem_available=self.mem_available,
            commit=self.commit,
            script=self.script,
            sleep_total_s=self.sleep_total_s,
        )

    def check(self) -> MetricsCheck:
        if self.check_result is not None:
            return self.check_result
        return MetricsCheck(ok=True, detail="fake metrics")


def make_monitor(
    *,
    mode: str = "pause_stop",
    metrics: FakeMetrics | None = None,
    clock: FakeClock | None = None,
    controller: StopController | None = None,
    **config_overrides,
) -> tuple[OverloadMonitor, FakeClock, FakeMetrics, StopController]:
    clock = clock or FakeClock(start=0.0)
    metrics = metrics or FakeMetrics(clock)
    controller = controller or StopController()
    config = OverloadConfig(mode=mode, **config_overrides)
    monitor = OverloadMonitor(config, controller, clock=clock, metrics=metrics)
    return monitor, clock, metrics, controller


def drive(monitor: OverloadMonitor, clock: FakeClock, seconds: int) -> list:
    events: list = []
    for _ in range(seconds):
        events.extend(monitor.poll())
        clock.advance(1.0)
    return events


# ---------------------------------------------------------------------------
# 快照（PerformanceView）
# ---------------------------------------------------------------------------


def test_snapshot_fills_performance_view() -> None:
    clock = FakeClock(start=0.0)
    metrics = FakeMetrics(clock)
    controller = StopController()
    monitor = OverloadMonitor(
        OverloadConfig(mode="pause_stop"),
        controller,
        clock=clock,
        metrics=metrics,
        capture_ms_provider=lambda: 12.5,
        loop_tick_p95_provider=lambda: 8.0,
    )
    monitor.set_context("BATTLE_ACTIVE", True, 0.0, True)
    drive(monitor, clock, 3)
    view = monitor.snapshot()
    assert isinstance(view, PerformanceView)
    assert view.enabled is True
    assert view.mode == "pause_stop"
    assert view.unavailable_reason is None
    assert view.cpu_pct == pytest.approx(10.0)
    assert view.mem_total_mb == pytest.approx(TOTAL_MB)
    assert view.commit_limit_mb == pytest.approx(COMMIT_LIMIT_MB)
    assert view.script_private_mb == pytest.approx(300.0)
    assert view.capture_ms_avg == pytest.approx(12.5)
    assert view.loop_tick_p95_ms == pytest.approx(8.0)
    assert view.sampled_at is not None
    assert view.sampled_wall is not None and "T" in view.sampled_wall
    assert view.sample_age_s is not None and view.sample_age_s >= 0.0
    thresholds = view.thresholds
    assert isinstance(thresholds, PerformanceThresholds)
    assert thresholds.mode == "pause_stop"
    assert thresholds.sample_hz == pytest.approx(1.0)
    assert thresholds.commit_pause_mb == pytest.approx(2560.0)
    assert thresholds.commit_fast_stop_mb == pytest.approx(512.0)
    assert thresholds.physical_warn_mb == pytest.approx(2621.44)


def test_snapshot_off_mode_disabled() -> None:
    monitor, clock, _metrics, _ = make_monitor(mode="off")
    monitor.poll()
    view = monitor.snapshot()
    assert view.enabled is False
    assert view.mode == "off"
    assert view.thresholds.mode == "off"


# ---------------------------------------------------------------------------
# monitor 模式：只记录，绝不动手
# ---------------------------------------------------------------------------


def test_monitor_mode_never_touches_controller() -> None:
    monitor, clock, metrics, controller = make_monitor(mode="monitor")
    monitor.set_context("BATTLE_ACTIVE", True, 0.0, True)
    metrics.commit = 1500.0
    events = drive(monitor, clock, 130)
    kinds = {event.kind for event in events}
    assert SafetyEventKind.OVERLOAD_PAUSE_REQUESTED not in kinds
    assert SafetyEventKind.OVERLOAD_STOP_REQUESTED not in kinds
    assert SafetyEventKind.OVERLOAD_RESUME_REQUESTED not in kinds
    assert controller.running is True
    assert controller.pause_request is None
    assert controller.stop_request is None
    view = monitor.snapshot()
    assert view.would_pause is True
    assert view.overload_state in ("pause_pending", "warning", "paused")


def test_monitor_mode_records_would_stop_on_fast_channel() -> None:
    monitor, clock, metrics, controller = make_monitor(mode="monitor")
    monitor.set_context("BATTLE_ACTIVE", True, 0.0, True)
    metrics.commit = 300.0
    drive(monitor, clock, 20)
    assert controller.running is True
    view = monitor.snapshot()
    assert view.would_stop is True
    assert view.overload_state == "stop_pending"


# ---------------------------------------------------------------------------
# pause / stop / resume 事件
# ---------------------------------------------------------------------------


def test_pause_request_event() -> None:
    monitor, clock, metrics, controller = make_monitor()
    monitor.set_context("BATTLE_ACTIVE", True, 0.0, True)
    metrics.commit = 1500.0
    events = drive(monitor, clock, 130)
    pauses = [event for event in events if event.kind is SafetyEventKind.OVERLOAD_PAUSE_REQUESTED]
    assert pauses, [event.kind.value for event in events]
    request = pauses[0].request
    assert request is not None
    assert request.reason == "overload_pause"
    assert request.hook == "overload"
    assert controller.paused is True
    view = monitor.snapshot()
    assert view.overload_state in ("paused", "warning")
    assert view.last_trigger is not None
    assert view.last_trigger.get("kind") == "pause"


def test_stop_request_event_and_reason() -> None:
    monitor, clock, metrics, controller = make_monitor()
    monitor.set_context("BATTLE_ACTIVE", True, 0.0, True)
    metrics.commit = 300.0
    events = drive(monitor, clock, 15)
    stops = [event for event in events if event.kind is SafetyEventKind.OVERLOAD_STOP_REQUESTED]
    assert stops
    request = stops[0].request
    assert request is not None
    assert request.reason == "overload_commit"
    assert request.hook == "overload"
    assert controller.stop_requested is True
    assert monitor.snapshot().would_stop is True


def test_auto_resume_event() -> None:
    monitor, clock, metrics, controller = make_monitor()
    monitor.set_context("BATTLE_ACTIVE", True, 0.0, True)
    metrics.commit = 1500.0
    pause_events = drive(monitor, clock, 130)
    assert any(event.kind is SafetyEventKind.OVERLOAD_PAUSE_REQUESTED for event in pause_events)
    assert controller.paused is True
    metrics.commit = 30_000.0
    resumed = False
    for _ in range(300):
        events = monitor.poll()
        clock.advance(1.0)
        if any(event.kind is SafetyEventKind.OVERLOAD_RESUME_REQUESTED for event in events):
            resumed = True
            break
    assert resumed
    assert controller.running is True
    assert monitor.policy.auto_resume_used == 1


def test_notify_resumed_clears_policy_state() -> None:
    monitor, clock, metrics, controller = make_monitor()
    monitor.set_context("BATTLE_ACTIVE", True, 0.0, True)
    metrics.commit = 1500.0
    drive(monitor, clock, 130)
    assert controller.paused is True
    assert controller.resume() is True
    monitor.notify_resumed()
    assert monitor.policy.paused is False
    assert monitor.policy.auto_resume_used == 0
    metrics.commit = 30_000.0
    events = drive(monitor, clock, 60)
    assert not any(event.kind is SafetyEventKind.OVERLOAD_RESUME_REQUESTED for event in events)


def test_notify_stopped_is_terminal() -> None:
    monitor, clock, metrics, _controller = make_monitor()
    monitor.set_context("BATTLE_ACTIVE", True, 0.0, True)
    drive(monitor, clock, 5)
    monitor.notify_stopped()
    metrics.commit = 300.0
    events = drive(monitor, clock, 20)
    assert not any(
        event.kind
        in (
            SafetyEventKind.OVERLOAD_PAUSE_REQUESTED,
            SafetyEventKind.OVERLOAD_STOP_REQUESTED,
        )
        for event in events
    )


def test_context_not_controlling_prevents_pause() -> None:
    monitor, clock, metrics, controller = make_monitor()
    monitor.set_context("PORT", False, 0.0, False)
    metrics.commit = 1500.0
    events = drive(monitor, clock, 130)
    assert not any(event.kind is SafetyEventKind.OVERLOAD_PAUSE_REQUESTED for event in events)
    assert controller.running is True


# ---------------------------------------------------------------------------
# 1 Hz 节流与异常降级
# ---------------------------------------------------------------------------


def test_poll_is_throttled_to_sample_hz() -> None:
    monitor, clock, metrics, _ = make_monitor()
    for _ in range(20):  # 20 × 0.25s = 5s
        monitor.poll()
        clock.advance(0.25)
    assert 4 <= metrics.samples <= 6


def test_sampler_exception_degrades_then_recovers() -> None:
    monitor, clock, metrics, _ = make_monitor()
    metrics.fail = "exception"
    assert monitor.poll() == []
    assert monitor.unavailable_reason is not None
    assert "采样异常" in monitor.unavailable_reason
    view = monitor.snapshot()
    assert view.unavailable_reason
    metrics.fail = None
    clock.advance(1.0)
    monitor.poll()
    assert monitor.unavailable_reason is None


def test_unavailable_sample_marks_unavailable() -> None:
    monitor, clock, metrics, _ = make_monitor()
    metrics.fail = "unavailable"
    events = drive(monitor, clock, 3)
    assert events == []
    view = monitor.snapshot()
    assert view.unavailable_reason is not None
    assert "模拟不可用" in view.unavailable_reason
    metrics.fail = None
    clock.advance(1.0)
    monitor.poll()
    assert monitor.unavailable_reason is None


def test_startup_check_issues_are_recorded_but_monitor_keeps_running() -> None:
    clock = FakeClock(start=0.0)
    metrics = FakeMetrics(clock)
    metrics.check_result = MetricsCheck(ok=False, issues=("结构体尺寸不一致",))
    monitor = OverloadMonitor(OverloadConfig(), StopController(), clock=clock, metrics=metrics)
    monitor.poll()
    assert monitor.startup_issues == ("结构体尺寸不一致",)
    assert monitor.startup_ok is False
    clock.advance(1.0)
    assert monitor.poll() == []  # 自检问题不阻断监控


def test_poll_tolerates_metrics_check_exception() -> None:
    clock = FakeClock(start=0.0)
    metrics = FakeMetrics(clock)

    def boom() -> MetricsCheck:
        raise RuntimeError("check 失败")

    metrics.check = boom  # type: ignore[method-assign]
    monitor = OverloadMonitor(OverloadConfig(), StopController(), clock=clock, metrics=metrics)
    monitor.poll()
    assert not monitor.startup_ok
    assert monitor.startup_issues


def test_snapshot_degrades_on_internal_error() -> None:
    monitor, _clock, _metrics, _ = make_monitor()

    def boom() -> None:
        raise RuntimeError("provider 失败")

    monitor._capture_ms_provider = boom  # type: ignore[assignment]
    view = monitor.snapshot()
    assert view.capture_ms_avg is None  # 提供方异常只让该字段不可用
    assert view.unavailable_reason is None


# ---------------------------------------------------------------------------
# 与 SafetyWatchdog 集成：20 Hz 循环内 1 Hz 过载轮询
# ---------------------------------------------------------------------------


def test_watchdog_polls_overload_at_1hz() -> None:
    clock = FakeClock(start=0.0)
    keys = FakeKeyStateSource()
    controller = StopController()
    safety = SafetyMonitor(
        keys, stop_key="f12", resume_key="f11", heartbeat_s=3600.0, controller=controller, clock=clock
    )
    metrics = FakeMetrics(clock)
    metrics.commit = 300.0
    overload = OverloadMonitor(OverloadConfig(mode="pause_stop"), controller, clock=clock, metrics=metrics)
    overload.set_context("BATTLE_ACTIVE", True, 0.0, True)
    received: list = []
    watchdog = SafetyWatchdog(safety, on_event=received.append, overload=overload)

    for _ in range(400):  # 20 Hz × 20s
        clock.advance(0.05)
        watchdog.run_once()
    assert 18 <= metrics.samples <= 23, metrics.samples
    assert any(event.kind is SafetyEventKind.OVERLOAD_STOP_REQUESTED for event in received)
    assert controller.stop_requested is True


def test_watchdog_f12_still_wins_with_overload_injected() -> None:
    clock = FakeClock(start=0.0)
    keys = FakeKeyStateSource()
    controller = StopController()
    safety = SafetyMonitor(keys, heartbeat_s=3600.0, controller=controller, clock=clock)
    metrics = FakeMetrics(clock)
    overload = OverloadMonitor(OverloadConfig(mode="pause_stop"), controller, clock=clock, metrics=metrics)
    watchdog = SafetyWatchdog(safety, overload=overload)
    keys.press("f12")
    clock.advance(0.05)
    events = watchdog.run_once()
    assert any(event.kind is SafetyEventKind.EMERGENCY_STOP for event in events)
    assert controller.stop_request is not None
    assert controller.stop_request.hook == "safety_hotkey"


def test_watchdog_without_overload_unchanged() -> None:
    clock = FakeClock(start=0.0)
    monitor = SafetyMonitor(FakeKeyStateSource(), heartbeat_s=3600.0, clock=clock)
    watchdog = SafetyWatchdog(monitor)
    assert watchdog.overload is None
    clock.advance(0.1)
    assert watchdog.run_once() == []


# ---------------------------------------------------------------------------
# runner/service 接线接口（M0 注入点）
# ---------------------------------------------------------------------------


def test_performance_view_and_view_aliases() -> None:
    monitor, clock, _metrics, _ = make_monitor()
    monitor.poll()
    clock.advance(1.0)
    assert isinstance(monitor.performance_view(), PerformanceView)
    assert isinstance(monitor.view(), PerformanceView)
    assert monitor.performance_view().mode == "pause_stop"


def test_build_overload_monitor_from_global_config() -> None:
    from src.config.models import GlobalConfig
    from src.execution.overload import build_overload_monitor

    clock = FakeClock(start=0.0)
    controller = StopController()
    monitor = build_overload_monitor(
        controller=controller,
        config=GlobalConfig(),
        clock=clock,
        safety=object(),
        app=object(),
    )
    assert isinstance(monitor, OverloadMonitor)
    assert monitor.mode == "monitor"
    monitor.poll()
    assert isinstance(monitor.performance_view(), PerformanceView)


def test_watchdog_attach_overload_monitor_via_runner_helper() -> None:
    from src.runtime.runner import attach_overload_monitor

    clock = FakeClock(start=0.0)
    safety = SafetyMonitor(FakeKeyStateSource(), heartbeat_s=3600.0, clock=clock)
    watchdog = SafetyWatchdog(safety)
    metrics = FakeMetrics(clock)
    monitor = OverloadMonitor(OverloadConfig(mode="monitor"), StopController(), clock=clock, metrics=metrics)
    assert attach_overload_monitor(watchdog, monitor) is True
    assert watchdog.overload is monitor
    clock.advance(0.05)
    watchdog.run_once()
    assert metrics.samples == 1
