"""运行服务测试：FakeRunner + FakeClock 驱动的生命周期/清理/队列/快照（GUI 设计文档 14.2）。"""

from __future__ import annotations

import ast
import logging
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.common.clock import FakeClock
from src.runtime.messages import (
    CleanupView,
    RunCommand,
    RunCommandKind,
    RunLifecycle,
    RuntimeEventKind,
    StartOptions,
)
from src.runtime.runner import RunnerViews, empty_runner_views
from src.runtime.service import RUN_THREAD_NAME, RuntimeService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNING = RunLifecycle.RUNNING
PAUSED = RunLifecycle.PAUSED
PAUSING = RunLifecycle.PAUSING
STOPPING = RunLifecycle.STOPPING
STOPPED = RunLifecycle.STOPPED
ERROR = RunLifecycle.ERROR


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class FakeLock:
    """单实例锁替身（不创建命名互斥量/锁文件）。"""

    def __init__(self, *, acquired: bool = True, holder: dict[str, object] | None = None) -> None:
        self._acquired = acquired
        self._holder = holder
        self._held = False
        self.release_calls = 0

    @property
    def holder(self) -> dict[str, object] | None:
        return self._holder

    def acquire(self) -> bool:
        if not self._acquired:
            return False
        self._held = True
        return True

    def release(self) -> None:
        self.release_calls += 1
        self._held = False


def _cleanup_ok() -> CleanupView:
    return CleanupView(
        requested=True,
        input_released=True,
        input_thread_joined=True,
        watchdog_stopped=True,
        event_sink_stopped=True,
        screenshots_cleaned=True,
        capture_closed=True,
        single_instance_released=False,
        pending_items=(),
        completed=True,
    )


class FakeRunner:
    """可观察、可驱动的会话执行器替身（实现 SessionRunner 的最小公开表面）。"""

    def __init__(self, *, cleanup_view: CleanupView | None = None) -> None:
        self.plan = None
        self.start_calls = 0
        self.steps = 0
        self.cleanup_calls = 0
        self.cleanup_view = cleanup_view or _cleanup_ok()
        self.raise_on_cleanup = False
        self.raise_on_step: BaseException | None = None
        self._finished = False
        self._paused = False
        self.pause_reason = ""
        self.release_confirmed = False
        self.release_count = 0
        self.stop_reason = "unknown"
        self._stop_requested = False
        #: 暂停时是否视为“输入线程已执行释放”（False 用于验证 PAUSING 等待确认）
        self.confirm_release_on_pause = True
        #: 下一次恢复请求是否在前台校验阶段失败（对应 runner.resume_failed）
        self.reject_resume_next = False
        self.resume_failed = False
        self.resume_failed_detail = ""
        self._lock = threading.Lock()

    # --- 运行线程侧 ---
    def start(self) -> None:
        self.start_calls += 1

    def step(self) -> None:
        if self.raise_on_step is not None:
            raise self.raise_on_step
        with self._lock:
            self.steps += 1

    @property
    def finished(self) -> bool:
        with self._lock:
            return self._finished

    # --- 控制通道（任意线程） ---
    def request_pause(self, reason: str = "fake_pause", detail: str = "") -> None:
        self._paused = True
        self.pause_reason = reason
        self.release_count += 1
        self.release_confirmed = self.confirm_release_on_pause

    def request_resume(self) -> bool:
        if self._stop_requested:
            return False
        if self.reject_resume_next:
            self.reject_resume_next = False
            self.resume_failed = True
            self.resume_failed_detail = "未能切回游戏前台，请手动点回游戏后重试"
            return True
        self._paused = False
        self.release_confirmed = False
        return True

    def request_stop(self, reason: str = "fake_stop", detail: str = "") -> None:
        self._stop_requested = True
        self.stop_reason = reason
        with self._lock:
            self._finished = True

    # --- 查询（服务运行线程读取） ---
    @property
    def paused(self) -> bool:
        return self._paused

    # --- 快照/清理 ---
    def snapshot_state(self, now: float) -> RunnerViews:
        base = empty_runner_views()
        return replace(
            base,
            game=replace(
                base.game,
                state="PAUSED" if self.paused else "PORT",
                pause_reason=self.pause_reason or None,
            ),
            input=replace(
                base.input,
                release_count=self.release_count,
                release_confirmed=self.release_confirmed,
            ),
        )

    def cleanup(self) -> CleanupView:
        self.cleanup_calls += 1
        if self.raise_on_cleanup:
            raise RuntimeError("模拟清理异常")
        return self.cleanup_view

    # --- 测试驱动 ---
    def fail_input(self) -> None:
        self._paused = True
        self.pause_reason = "input_failed"
        self.release_count += 1
        self.release_confirmed = True


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _make_root(tmp_path: Path) -> Path:
    """构造满足快速预检的最小项目根（只落盘 config/profile 两个文件）。"""
    profile_dir = tmp_path / "profiles" / "cn360"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text("runtime: {}\npaths:\n  data_dir: data\n  log_dir: logs\n", encoding="utf-8")
    (profile_dir / "profile.yaml").write_text("client_id: cn360\ndisplay_name: 测试客户端\n", encoding="utf-8")
    return tmp_path


def _make_service(
    tmp_path: Path,
    factory,
    *,
    clock: FakeClock | None = None,
    lock_factory=None,
    **kwargs,
) -> tuple[RuntimeService, Path]:
    root = _make_root(tmp_path)
    service = RuntimeService(
        root,
        clock=clock or FakeClock(),
        runner_factory=factory,
        lock_factory=lock_factory or (lambda _data_dir: FakeLock()),
        snapshot_min_interval_s=0.0,
        **kwargs,
    )
    return service, root


def _wait(predicate, timeout: float = 3.0, message: str = "等待条件超时") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail(f"{message}（当前生命周期={getattr(predicate, '__name__', '')}）")


def _wait_lifecycle(service: RuntimeService, lifecycle: RunLifecycle, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if service.lifecycle is lifecycle:
            return
        time.sleep(0.005)
    pytest.fail(f"生命周期未进入 {lifecycle}，当前 {service.lifecycle}")


def _shutdown(service: RuntimeService) -> None:
    """尽力收尾（断言失败时也不留下运行线程/锁）。"""
    try:
        if service.lifecycle not in (STOPPED, ERROR):
            service.shutdown()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and service.lifecycle not in (STOPPED, ERROR):
            time.sleep(0.005)
    finally:
        pass


def _event_kinds(service: RuntimeService) -> set[RuntimeEventKind]:
    return {event.kind for event in service.drain_events(limit=1000)}


# ---------------------------------------------------------------------------
# 初始状态 / 快照
# ---------------------------------------------------------------------------


def test_initial_snapshot_is_stopped(tmp_path: Path) -> None:
    service, _root = _make_service(tmp_path, lambda _options: FakeRunner())
    try:
        snapshot = service.snapshot()
        assert snapshot.seq >= 1
        assert snapshot.lifecycle.lifecycle is STOPPED
        assert snapshot.cleanup.completed is True
        assert snapshot.plan is None
        assert snapshot.perf is None
        assert snapshot.game.state == "UNKNOWN"
        # M0 已接线能力：门禁/释放确认/单实例/异常清理/场次上下文/输入失败暂停
        assert snapshot.capabilities.input_gate is True
        assert snapshot.capabilities.release_confirmed is True
        assert snapshot.capabilities.single_instance is True
        assert snapshot.capabilities.cleanup_on_error is True
        assert service.dropped_events == 0
    finally:
        _shutdown(service)


def test_target_command_runs_on_session_thread_and_stop_rejects_more(tmp_path):
    calls = []
    runner = FakeRunner()
    runner.replace_target = lambda command: calls.append((command, threading.current_thread().name)) or True
    _snapshot = runner.snapshot_state
    runner.snapshot_state = lambda now: replace(
        _snapshot(now), game=replace(_snapshot(now).game, state="BATTLE_ACTIVE")
    )
    service, root = _make_service(tmp_path, lambda options: runner)
    command = RunCommand(RunCommandKind.REPLACE_TARGET, target=(0.4, 0.7), battle_id=7, frame_id=5)
    try:
        assert not service.submit_command(command)
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        assert service.submit_command(command)
        _wait(lambda: len(calls) == 1)
        assert calls == [(command, RUN_THREAD_NAME)]
        assert RuntimeEventKind.NAV_FEEDBACK in _event_kinds(service)
        service.request_stop("manual")
        assert not service.submit_command(command)
    finally:
        _shutdown(service)


def test_target_queue_log_reaches_project_logger_only_for_target_commands(tmp_path):
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    project_logger = logging.getLogger("wows_script")
    old_level = project_logger.level
    handler = _Capture()
    project_logger.addHandler(handler)
    project_logger.setLevel(logging.INFO)
    runner = FakeRunner()
    _snapshot = runner.snapshot_state
    runner.snapshot_state = lambda now: replace(
        _snapshot(now), game=replace(_snapshot(now).game, state="BATTLE_ACTIVE")
    )
    runner.replace_target = lambda command: True
    service, _root = _make_service(tmp_path, lambda options: runner)
    service._runner = runner
    service._lifecycle = RUNNING
    try:
        with service._lock:
            service._rebuild_snapshot_locked()
        assert service.submit_command(
            RunCommand(RunCommandKind.REPLACE_TARGET, target=(0.4, 0.7), battle_id=7, frame_id=5)
        )
        assert any("gui_target_queued accepted=True" in text for text in records)
        target_logs = len(records)
        assert service.submit_command(RunCommand(RunCommandKind.PAUSE, reason="manual"))
        assert len(records) == target_logs
    finally:
        project_logger.removeHandler(handler)
        project_logger.setLevel(old_level)
        service._runner = None


def test_idle_dashboard_samples_without_starting_control_or_inventing_thresholds(tmp_path):
    service, _ = _make_service(tmp_path, lambda options: pytest.fail("不应创建运行实例"))
    dashboard = service.read_dashboard()
    assert service.lifecycle is STOPPED
    assert dashboard.perf is not None and dashboard.perf.sampled_at is not None
    assert dashboard.perf.mode == "monitor"
    assert dashboard.perf.thresholds is None
    assert not dashboard.perf.would_stop


# ---------------------------------------------------------------------------
# 启动防重入 / 单实例
# ---------------------------------------------------------------------------


def test_repeated_start_creates_single_run_thread(tmp_path: Path) -> None:
    runners: list[FakeRunner] = []

    def factory(_options):  # noqa: ANN001 - 测试替身
        runner = FakeRunner()
        runners.append(runner)
        return runner

    service, root = _make_service(tmp_path, factory)
    try:
        first = service.start(StartOptions(root=root))
        assert first.accepted is True
        _wait_lifecycle(service, RUNNING)
        assert len(runners) == 1
        assert runners[0].start_calls == 1

        for _ in range(9):
            repeated = service.start(StartOptions(root=root))
            assert repeated.accepted is False
            assert repeated.reason == "already_active"
        assert len(runners) == 1
        assert sum(1 for thread in threading.enumerate() if thread.name == RUN_THREAD_NAME) == 1
    finally:
        _shutdown(service)


def test_start_rejects_when_lock_held(tmp_path: Path) -> None:
    holders = {"pid": 4321, "started_at": "2026-10-31 20:00:00"}

    def lock_factory(_data_dir: Path) -> FakeLock:
        return FakeLock(acquired=False, holder=holders)

    service, root = _make_service(tmp_path, lambda _options: FakeRunner(), lock_factory=lock_factory)
    try:
        result = service.start(StartOptions(root=root))
        assert result.accepted is False
        assert result.reason == "lock_held"
        assert service.lifecycle is STOPPED
        events = service.drain_events(limit=100)
        errors = [event for event in events if event.kind is RuntimeEventKind.ERROR]
        assert errors
        assert errors[-1].detail.get("reason") == "lock_held"
        assert errors[-1].detail.get("holder", {}).get("pid") == 4321
    finally:
        _shutdown(service)


def test_start_rejects_invalid_options(tmp_path: Path) -> None:
    service, root = _make_service(tmp_path, lambda _options: FakeRunner())
    try:
        result = service.start(StartOptions(root=root, max_battles=0, log_level="VERBOSE"))
        assert result.accepted is False
        assert result.reason == "validation_failed"
        codes = {issue.code for issue in result.issues}
        assert {"max_battles_invalid", "log_level_invalid"} <= codes
        assert service.lifecycle is STOPPED
        assert RuntimeEventKind.VALIDATION_FAILED in _event_kinds(service)
    finally:
        _shutdown(service)


# ---------------------------------------------------------------------------
# 暂停 / 恢复
# ---------------------------------------------------------------------------


def test_pause_closes_gate_and_confirms_release(tmp_path: Path) -> None:
    runner = FakeRunner()
    service, root = _make_service(tmp_path, lambda _options: runner)
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        assert service.request_pause("test_pause", "detail") is True
        _wait_lifecycle(service, PAUSED)
        snapshot = service.snapshot()
        assert snapshot.lifecycle.lifecycle is PAUSED
        assert snapshot.input.release_confirmed is True
        assert snapshot.input.release_count == 1
        assert snapshot.game.pause_reason == "test_pause"
        # 重复请求幂等
        assert service.request_pause() is True
        assert service.request_pause("other", "again") is True
        assert service.lifecycle is PAUSED
        kinds = _event_kinds(service)
        assert RuntimeEventKind.PAUSE_REQUESTED in kinds
        assert RuntimeEventKind.PAUSE_CONFIRMED in kinds
        # 恢复
        assert service.request_resume() is True
        _wait_lifecycle(service, RUNNING)
        assert runner.request_resume() is True or runner.paused is False
        kinds = _event_kinds(service)
        assert RuntimeEventKind.RESUME_REQUESTED in kinds
        assert RuntimeEventKind.RESUME_CONFIRMED in kinds
    finally:
        _shutdown(service)


def test_pause_without_release_confirmation_stays_pausing(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.confirm_release_on_pause = False
    service, root = _make_service(tmp_path, lambda _options: runner)
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        assert service.request_pause("no_confirm", "") is True
        _wait_lifecycle(service, PAUSING)
        time.sleep(0.05)
        assert service.lifecycle is PAUSING  # 未确认释放前不得进入 PAUSED
        runner.release_confirmed = True
        _wait_lifecycle(service, PAUSED)
        assert service.snapshot().input.release_confirmed is True
    finally:
        _shutdown(service)


def test_resume_failure_keeps_paused_and_clears_resume_pending(tmp_path: Path) -> None:
    """设计 11.3：恢复请求前台校验失败时保持 PAUSED、清 resume_pending 并给出原因。"""
    runner = FakeRunner()
    service, root = _make_service(tmp_path, lambda _options: runner)
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        service.request_pause("resume_case", "")
        _wait_lifecycle(service, PAUSED)
        runner.reject_resume_next = True
        assert service.request_resume() is True
        _wait(
            lambda: service.snapshot().lifecycle.resume_pending is False,
            message="恢复失败后 resume_pending 未清除",
        )
        assert service.lifecycle is PAUSED
        assert runner.paused is True
        events = service.drain_events(limit=1000)
        assert any(event.kind is RuntimeEventKind.ERROR for event in events)
        assert any("恢复失败" in event.message for event in events)
    finally:
        _shutdown(service)


def test_input_failure_pauses_with_reason(tmp_path: Path) -> None:
    runner = FakeRunner()
    service, root = _make_service(tmp_path, lambda _options: runner)
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        runner.fail_input()
        _wait_lifecycle(service, PAUSED)
        assert service.snapshot().game.pause_reason == "input_failed"
        assert RuntimeEventKind.INPUT_FAILED in _event_kinds(service)
    finally:
        _shutdown(service)


def test_start_paused_option(tmp_path: Path) -> None:
    runner = FakeRunner()
    service, root = _make_service(tmp_path, lambda _options: runner)
    try:
        assert service.start(StartOptions(root=root, start_paused=True)).accepted
        _wait_lifecycle(service, PAUSED)
        assert runner.pause_reason == "gui_start_paused"
    finally:
        _shutdown(service)


def test_command_queue_pause_is_applied_by_run_thread(tmp_path: Path) -> None:
    runner = FakeRunner()
    service, root = _make_service(tmp_path, lambda _options: runner)
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        assert service.submit_command(RunCommand(RunCommandKind.PAUSE, reason="cmd_pause")) is True
        _wait_lifecycle(service, PAUSED)
        assert runner.pause_reason == "cmd_pause"
        assert service.submit_command(RunCommand(RunCommandKind.START)) is False
    finally:
        _shutdown(service)


def test_command_queue_is_bounded(tmp_path: Path) -> None:
    service, _root = _make_service(tmp_path, lambda _options: FakeRunner(), command_max=2)
    try:
        for index in range(5):
            assert service.submit_command(RunCommand(RunCommandKind.STOP, reason=f"r{index}")) is True
        assert len(service._commands) == 2  # 有界：只保留最新两条
    finally:
        _shutdown(service)


# ---------------------------------------------------------------------------
# 停止与统一清理（STOPPED / ERROR）
# ---------------------------------------------------------------------------


def test_stop_cleanup_success_enters_stopped(tmp_path: Path) -> None:
    runners: list[FakeRunner] = []

    def factory(_options):  # noqa: ANN001 - 测试替身
        runner = FakeRunner()
        runners.append(runner)
        return runner

    service, root = _make_service(tmp_path, factory)
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        assert service.request_stop("user_stop", "manual") is True
        _wait_lifecycle(service, STOPPED)
        assert runners[0].cleanup_calls == 1
        snapshot = service.snapshot()
        assert snapshot.cleanup.completed is True
        assert snapshot.cleanup.single_instance_released is True
        assert snapshot.cleanup.pending_items == ()
        assert "user_stop" in snapshot.lifecycle.detail
        assert snapshot.error is None
        assert service.request_stop() is True  # 幂等
        assert RuntimeEventKind.CLEANUP_STEP in _event_kinds(service)
        # 停止后允许再次启动（清理已完成）
        assert service.start(StartOptions(root=root)).accepted is True
        _wait_lifecycle(service, RUNNING)
        assert len(runners) == 2
    finally:
        _shutdown(service)


def test_cleanup_failure_enters_error(tmp_path: Path) -> None:
    failure = replace(
        _cleanup_ok(),
        input_thread_joined=False,
        pending_items=("输入线程未退出",),
        completed=False,
    )
    service, root = _make_service(tmp_path, lambda _options: FakeRunner(cleanup_view=failure))
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        assert service.request_stop("cleanup_fail", "") is True
        _wait_lifecycle(service, ERROR)
        snapshot = service.snapshot()
        assert snapshot.cleanup.completed is False
        assert snapshot.cleanup.pending_items
        assert snapshot.error is not None
        assert snapshot.error.code == "cleanup_incomplete"
        assert snapshot.error.recoverable is False
        # ERROR 禁止再次启动；只允许 shutdown
        rejected = service.start(StartOptions(root=root))
        assert rejected.accepted is False
        assert service.request_stop("again", "") is False
        assert service.shutdown() is True
    finally:
        _shutdown(service)


def test_cleanup_exception_enters_error(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.raise_on_cleanup = True
    service, root = _make_service(tmp_path, lambda _options: runner)
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        service.request_stop("cleanup_boom", "")
        _wait_lifecycle(service, ERROR)
        assert service.snapshot().cleanup.completed is False
        assert service.snapshot().error is not None
    finally:
        _shutdown(service)


def test_run_exception_cleans_and_allows_restart(tmp_path: Path) -> None:
    runners: list[FakeRunner] = []

    def factory(_options):  # noqa: ANN001 - 测试替身
        runner = FakeRunner()
        if not runners:
            runner.raise_on_step = RuntimeError("step boom")
        runners.append(runner)
        return runner

    service, root = _make_service(tmp_path, factory)
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, STOPPED)
        assert runners[0].cleanup_calls == 1
        snapshot = service.snapshot()
        assert snapshot.cleanup.completed is True
        assert snapshot.error is not None
        assert snapshot.error.recoverable is True
        kinds = _event_kinds(service)
        assert RuntimeEventKind.ERROR in kinds
        assert RuntimeEventKind.SESSION_ENDED in kinds
        # 运行期异常但清理完成 -> 允许重启
        assert service.start(StartOptions(root=root)).accepted is True
        _wait_lifecycle(service, RUNNING)
    finally:
        _shutdown(service)


def test_shutdown_rejects_future_start(tmp_path: Path) -> None:
    service, root = _make_service(tmp_path, lambda _options: FakeRunner())
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        assert service.shutdown() is True
        _wait_lifecycle(service, STOPPED)
        assert service.shutdown_requested is True
        rejected = service.start(StartOptions(root=root))
        assert rejected.accepted is False
        assert rejected.reason == "shutdown"
    finally:
        _shutdown(service)


# ---------------------------------------------------------------------------
# 事件队列 / 快照序号
# ---------------------------------------------------------------------------


def test_event_queue_is_bounded_and_dropped_counted(tmp_path: Path) -> None:
    service, _root = _make_service(tmp_path, lambda _options: FakeRunner(), event_max=5)
    try:
        for index in range(12):
            service.publish_event(RuntimeEventKind.LOG_RECORD, "INFO", f"line {index}")
        events = service.drain_events(limit=100)
        assert len(events) == 5
        assert [event.seq for event in events] == [8, 9, 10, 11, 12]
        assert service.dropped_events == 7
    finally:
        _shutdown(service)


def test_drain_events_limit_and_empty(tmp_path: Path) -> None:
    service, _root = _make_service(tmp_path, lambda _options: FakeRunner())
    try:
        for index in range(3):
            service.publish_event(RuntimeEventKind.LOG_RECORD, "INFO", f"line {index}")
        first = service.drain_events(limit=2)
        second = service.drain_events(limit=2)
        assert len(first) == 2
        assert len(second) == 1
        assert service.drain_events(limit=2) == ()
    finally:
        _shutdown(service)


def test_snapshot_seq_is_monotonic(tmp_path: Path) -> None:
    service, root = _make_service(tmp_path, lambda _options: FakeRunner())
    try:
        initial = service.snapshot().seq
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        after_start = service.snapshot().seq
        assert after_start > initial
        service.request_stop("seq", "")
        _wait_lifecycle(service, STOPPED)
        after_stop = service.snapshot().seq
        assert after_stop > after_start
    finally:
        _shutdown(service)


# ---------------------------------------------------------------------------
# 账号轮换接口占位（M3 接线前的守卫）
# ---------------------------------------------------------------------------


def test_rotation_interfaces_are_guarded_before_m3(tmp_path: Path) -> None:
    service, root = _make_service(tmp_path, lambda _options: FakeRunner())
    try:
        assert service.confirm_account_switch() is False
        assert service.abort_rotation() is False
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        assert service.confirm_account_switch() is False
        assert service.abort_rotation() is False
    finally:
        _shutdown(service)


def test_run_thread_names_are_unique_in_service(tmp_path: Path) -> None:
    """运行线程名固定，便于诊断/验收“只创建一个控制实例”。"""
    service, root = _make_service(tmp_path, lambda _options: FakeRunner())
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        names = [thread.name for thread in threading.enumerate()]
        assert names.count(RUN_THREAD_NAME) == 1
    finally:
        _shutdown(service)


# ---------------------------------------------------------------------------
# 真实 SessionRunner 的视图映射/控制通道（不启动游戏/线程，用假 Runtime 对象）
# ---------------------------------------------------------------------------


def _fake_runtime():  # noqa: ANN202 - 测试替身
    from types import SimpleNamespace

    from src.common.types import State
    from src.execution.safety import StopController

    calls: list[str] = []
    controller = StopController()

    class _Machine:
        state = State.PORT
        transitions = [(0.0, State.INIT, State.PORT, "test")]
        pause_reason = ""
        session_id = 3
        battle_id = 7
        battle = None
        observation = None
        battles_completed = 2
        finished = False
        stop_request = None

    class _Executor:
        gate_open = True
        gate_reason = ""
        pending_actions = 0
        release_count = 0
        executed_actions = 1
        rejected_actions = 2
        dropped_actions = 3
        expired_actions = 4
        failed = False
        consecutive_errors = 0
        last_error = None
        is_running = False

        @staticmethod
        def close_gate_and_release(reason: str = "") -> int:
            calls.append(reason)
            return 1

    app = SimpleNamespace(
        global_config=SimpleNamespace(
            runtime=SimpleNamespace(loop_hz=10),
            navigation=SimpleNamespace(mode="autopilot", surface="minimap"),
            safety=SimpleNamespace(max_battles_per_session=20),
        ),
        account_alias="alt1",
    )
    engine = SimpleNamespace(
        last_frame=SimpleNamespace(frame_id=5, timestamp=0.0),
        stats=lambda: SimpleNamespace(capture_ms_avg=12.5, last_frame_id=5),
        close=lambda: None,
    )
    autopilot = SimpleNamespace(
        last_target=None,
        clicks=2,
        surface="full_map",
        degraded=False,
        blocks_behavior=True,
    )
    runtime = SimpleNamespace(
        app=app,
        clock=FakeClock(),
        machine=_Machine(),
        executor=_Executor(),
        safety=SimpleNamespace(controller=controller),
        controller=controller,
        engine=engine,
        window_locator=SimpleNamespace(find=lambda: None),
        session=SimpleNamespace(elapsed_s=1.5, last_battle_duration_s=None),
        autopilot=autopilot,
        logger=logging.getLogger("tests.runtime"),
        bridge=SimpleNamespace(update_window_rect=lambda _rect: None),
        watchdog=SimpleNamespace(stop=lambda: None, is_running=False),
        screenshots=SimpleNamespace(cleanup_session=lambda: True, session_dir=None),
        events=SimpleNamespace(stop=lambda drain=True: None, is_running=False),
        overload_monitor=None,
        plan=None,
    )
    return runtime, calls, controller


def test_session_snapshot_refreshes_daily_usage_from_controller() -> None:
    from src.runtime.runner import SessionRunner

    runtime, _, _ = _fake_runtime()
    count = [4]
    runtime.session.count_battles_today = lambda: count[0]
    runner = SessionRunner(runtime)

    assert runner.snapshot_state(0.0).session.daily_used == 4
    count[0] = 5
    assert runner.snapshot_state(1.0).session.daily_used == 5


def test_session_runner_view_mapping_and_controls() -> None:
    from src.runtime.runner import SessionRunner

    runtime, calls, controller = _fake_runtime()
    runner = SessionRunner(runtime)
    views = runner.snapshot_state(0.0)
    assert views.game.state == "PORT"
    assert views.game.session_id == 3
    assert views.game.battle_id == 7
    assert views.game.last_transition == "INIT -> PORT (test)"
    assert views.input.executed == 1
    assert views.input.rejected == 2
    assert views.input.release_confirmed is False
    assert views.navigation.mode == "autopilot"
    assert views.navigation.surface == "full_map"
    assert views.navigation.click_count == 2
    assert views.navigation.blocks_behavior is True
    assert views.perception.last_frame_id == 5
    assert views.perception.capture_ms_avg == 12.5
    assert views.session.account_alias == "alt1"
    assert views.session.elapsed_s == 1.5

    runner.request_pause("gui_pause", "clicked")
    assert controller.paused is True
    assert controller.pause_request is not None
    assert controller.pause_request.reason == "gui_pause"
    assert calls == ["service:gui_pause"]

    runner.request_resume()
    assert controller.paused is False
    assert runner._resume_foreground_check is True

    runner.request_stop("gui_stop", "closed")
    assert controller.stop_request is not None
    assert controller.stop_request.reason == "gui_stop"
    assert calls[-1] == "stop:gui_stop"


def test_cleanup_runtime_reports_checklist() -> None:
    from src.runtime.runner import cleanup_runtime

    runtime, _calls, _controller = _fake_runtime()
    report = cleanup_runtime(runtime)  # type: ignore[arg-type]
    assert report.requested is True
    assert report.completed is True
    assert report.pending_items == ()
    assert report.input_released is True
    assert report.watchdog_stopped is True
    assert report.event_sink_stopped is True
    assert report.screenshots_cleaned is True
    assert report.capture_closed is True
    assert report.single_instance_released is False  # 由 RuntimeService 回填


def test_cleanup_runtime_detects_unfinished_threads() -> None:
    from src.runtime.runner import cleanup_runtime

    runtime, _calls, _controller = _fake_runtime()
    runtime.events = SimpleNamespace(stop=lambda drain=True: None, is_running=True)
    runtime.screenshots = SimpleNamespace(cleanup_session=lambda: False, session_dir=Path("sessions/live"))
    report = cleanup_runtime(runtime)  # type: ignore[arg-type]
    assert report.completed is False
    assert report.event_sink_stopped is False
    assert report.screenshots_cleaned is False
    assert set(report.pending_items) >= {"日志线程未退出", "本场截图未清理"}


# ---------------------------------------------------------------------------
# 导入边界（设计 14.3：src/runtime/* 不导入 tkinter）
# ---------------------------------------------------------------------------


def test_runtime_package_does_not_import_tkinter() -> None:
    runtime_dir = PROJECT_ROOT / "src" / "runtime"
    for path in sorted(runtime_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(name.split(".")[0] == "tkinter" for name in names), path
