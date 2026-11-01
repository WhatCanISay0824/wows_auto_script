"""运行服务日志桥接测试（M3，GUI 设计文档 4.6/12.1）。

验证：INFO+ 日志转 LOG_RECORD 事件、DEBUG 默认不桥接、detail 含 logger/级别、
事件队列有界、服务锁被占用时日志线程不阻塞、STOPPED/shutdown 移除 handler、无递归。
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from src.common.clock import FakeClock
from src.runtime.messages import CleanupView, RunLifecycle, RuntimeEventKind, StartOptions
from src.runtime.runner import RunnerViews, empty_runner_views
from src.runtime.service import RuntimeService, _RuntimeLogHandler

RUNNING = RunLifecycle.RUNNING
STOPPED = RunLifecycle.STOPPED
LOG_RECORD = RuntimeEventKind.LOG_RECORD


# ---------------------------------------------------------------------------
# 测试替身 / 工具
# ---------------------------------------------------------------------------


class FakeLock:
    def __init__(self) -> None:
        self.holder = None
        self.release_calls = 0

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        self.release_calls += 1


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
    """不自然结束的 runner；由 request_stop / shutdown 停止。"""

    def __init__(self) -> None:
        self.plan = None
        self.battles_completed = 0
        self.stop_reason = "user_stop"
        self.start_calls = 0
        self.steps = 0
        self.cleanup_calls = 0
        self._finished = False
        self._paused = False

    def start(self) -> None:
        self.start_calls += 1

    def step(self) -> None:
        self.steps += 1

    @property
    def finished(self) -> bool:
        return self._finished

    def request_pause(self, reason: str = "", detail: str = "") -> None:
        self._paused = True

    def request_resume(self) -> bool:
        self._paused = False
        return True

    def request_stop(self, reason: str = "user_stop", detail: str = "") -> None:
        self.stop_reason = reason
        self._finished = True

    @property
    def paused(self) -> bool:
        return self._paused

    release_count = 0
    release_confirmed = False

    def snapshot_state(self, now: float) -> RunnerViews:
        return replace(empty_runner_views())

    def cleanup(self) -> CleanupView:
        self.cleanup_calls += 1
        return _cleanup_ok()


def _make_root(tmp_path: Path) -> Path:
    profile_dir = tmp_path / "profiles" / "cn360"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(
        "runtime: {}\npaths:\n  data_dir: data\n  log_dir: logs\n", encoding="utf-8"
    )
    (profile_dir / "profile.yaml").write_text(
        "client_id: cn360\ndisplay_name: 测试客户端\n", encoding="utf-8"
    )
    return tmp_path


def _make_service(
    tmp_path: Path,
    logger_name: str | None,
    *,
    event_max: int = 2000,
    log_bridge_level: int = logging.INFO,
) -> tuple[RuntimeService, Path, FakeRunner]:
    root = _make_root(tmp_path)
    runner = FakeRunner()

    def factory(_options: StartOptions) -> FakeRunner:
        return runner

    service = RuntimeService(
        root,
        clock=FakeClock(),
        runner_factory=factory,
        lock_factory=lambda _data_dir: FakeLock(),
        snapshot_min_interval_s=0.0,
        event_max=event_max,
        log_bridge_logger=logger_name,
        log_bridge_level=log_bridge_level,
    )
    return service, root, runner


def _make_logger(name: str) -> logging.Logger:
    lg = logging.getLogger(name)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False  # 测试隔离：不向根 logger 冒泡
    for handler in list(lg.handlers):
        lg.removeHandler(handler)
    return lg


def _wait(predicate, timeout: float = 3.0, message: str = "等待条件超时") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail(message)


def _wait_lifecycle(service: RuntimeService, lifecycle: RunLifecycle, timeout: float = 5.0) -> None:
    _wait(lambda: service.lifecycle is lifecycle, timeout, f"生命周期未进入 {lifecycle}")


def _drain_log_events(service: RuntimeService, timeout: float = 2.0) -> list:
    """轮询事件队列，收集一段时间内的全部 LOG_RECORD 事件（不丢其它事件）。"""
    collected: list = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in service.drain_events(limit=1000):
            if event.kind is LOG_RECORD:
                collected.append(event)
        if collected:
            return collected
        time.sleep(0.01)
    return collected


def _shutdown(service: RuntimeService) -> None:
    try:
        if service.lifecycle not in (STOPPED, RunLifecycle.ERROR):
            service.shutdown()
        _wait(lambda: service.lifecycle in (STOPPED, RunLifecycle.ERROR), 5.0, "收尾超时")
    except Exception:  # pragma: no cover - 尽力收尾
        pass


# ---------------------------------------------------------------------------
# 基本桥接
# ---------------------------------------------------------------------------


def test_info_log_becomes_log_record_event(tmp_path: Path) -> None:
    logger_name = "tests.bridge.info"
    lg = _make_logger(logger_name)
    service, root, _runner = _make_service(tmp_path, logger_name)
    try:
        assert service._log_handler is None  # 未运行时不安装
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        assert service._log_handler is not None
        service.drain_events(limit=1000)  # 清空启动阶段事件
        lg.info("桥接测试消息 %s", "hello")
        events = _drain_log_events(service)
        assert events, "INFO 日志未桥接为 LOG_RECORD 事件"
        event = events[0]
        assert "桥接测试消息 hello" in event.message
        assert event.level == "INFO"
        assert event.detail["logger"] == logger_name
        assert event.detail["level"] == "INFO"
        assert "thread" in event.detail
    finally:
        _shutdown(service)


def test_warning_and_error_levels_are_preserved(tmp_path: Path) -> None:
    logger_name = "tests.bridge.levels"
    lg = _make_logger(logger_name)
    service, root, _runner = _make_service(tmp_path, logger_name)
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        service.drain_events(limit=1000)
        lg.warning("警告桥接")
        lg.error("错误桥接")
        assert _wait_messages(service, ("警告桥接", "错误桥接")), "WARNING/ERROR 未桥接"
    finally:
        _shutdown(service)


def _wait_messages(service: RuntimeService, needles: tuple[str, ...], timeout: float = 2.0) -> bool:
    """轮询事件队列直到所有关键字都出现在 LOG_RECORD 中（单次 drain 内判定，避免互相吞）。"""
    seen: set[str] = set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in service.drain_events(limit=1000):
            if event.kind is not LOG_RECORD:
                continue
            for needle in needles:
                if needle in event.message:
                    seen.add(needle)
        if seen >= set(needles):
            return True
        time.sleep(0.01)
    return False


def test_debug_not_bridged_by_default(tmp_path: Path) -> None:
    logger_name = "tests.bridge.debug_default"
    lg = _make_logger(logger_name)
    service, root, _runner = _make_service(tmp_path, logger_name)
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        service.drain_events(limit=1000)
        lg.debug("DEBUG 不应桥接")
        time.sleep(0.1)
        events = [event for event in service.drain_events(limit=1000) if event.kind is LOG_RECORD]
        assert events == []
    finally:
        _shutdown(service)


def test_debug_can_be_enabled_explicitly(tmp_path: Path) -> None:
    logger_name = "tests.bridge.debug_on"
    lg = _make_logger(logger_name)
    service, root, _runner = _make_service(tmp_path, logger_name, log_bridge_level=logging.DEBUG)
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        service.drain_events(limit=1000)
        lg.debug("DEBUG 允许桥接")
        events = _drain_log_events(service)
        assert any("DEBUG 允许桥接" in event.message for event in events)
    finally:
        _shutdown(service)


# ---------------------------------------------------------------------------
# 有界 / 非阻塞 / 无递归
# ---------------------------------------------------------------------------


def test_bridged_events_are_bounded(tmp_path: Path) -> None:
    logger_name = "tests.bridge.bounded"
    lg = _make_logger(logger_name)
    service, root, _runner = _make_service(tmp_path, logger_name, event_max=5)
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        service.drain_events(limit=1000)
        for index in range(30):
            lg.info("高频日志 %d", index)
        time.sleep(0.1)
        events = service.drain_events(limit=1000)
        log_events = [event for event in events if event.kind is LOG_RECORD]
        assert len(log_events) <= 5  # 队列上限（含并发丢弃）
        assert service.dropped_events > 0  # 丢弃有计数，界面可提示“已丢弃 N 条”
    finally:
        _shutdown(service)


def test_bridge_does_not_block_when_service_lock_held(tmp_path: Path) -> None:
    logger_name = "tests.bridge.nonblock"
    lg = _make_logger(logger_name)
    service, root, _runner = _make_service(tmp_path, logger_name)
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        service.drain_events(limit=1000)
        dropped_before = service.dropped_events
        locked = threading.Event()
        release = threading.Event()

        def _hold_lock() -> None:
            with service._lock:
                locked.set()
                release.wait(3.0)

        holder = threading.Thread(target=_hold_lock, name="test-lock-holder", daemon=True)
        holder.start()
        assert locked.wait(2.0)
        started = time.monotonic()
        lg.info("锁被占用时的日志")
        elapsed = time.monotonic() - started
        release.set()
        holder.join(2.0)
        assert elapsed < 0.5, f"日志线程被阻塞 {elapsed:.3f}s"
        # 锁被占用期间事件被丢弃并计数，而不是阻塞等待
        assert service.dropped_events > dropped_before
        events = [event for event in service.drain_events(limit=1000) if event.kind is LOG_RECORD]
        assert all("锁被占用时的日志" not in event.message for event in events)
    finally:
        _shutdown(service)


def test_log_bridge_does_not_recurse() -> None:
    """handler 内不得再调用 logging（AST 约束）+ 单条日志只产生一个事件。"""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(_RuntimeLogHandler.emit)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            base = func.value
            if isinstance(base, ast.Name) and base.id in {"logging", "logger", "log"}:
                pytest.fail(f"日志桥接 handler 内出现 logging 调用: {ast.dump(node)}")
    source = inspect.getsource(_RuntimeLogHandler.emit)
    assert "self._publish" in source


def test_single_log_record_event_per_record(tmp_path: Path) -> None:
    logger_name = "tests.bridge.recursion"
    lg = _make_logger(logger_name)
    service, root, _runner = _make_service(tmp_path, logger_name)
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        service.drain_events(limit=1000)
        lg.info("仅一条")
        events = _drain_log_events(service)
        assert len(events) == 1
        assert "仅一条" in events[0].message
        time.sleep(0.05)
        extra = [event for event in service.drain_events(limit=1000) if event.kind is LOG_RECORD]
        assert extra == []
    finally:
        _shutdown(service)


# ---------------------------------------------------------------------------
# 生命周期：安装/移除
# ---------------------------------------------------------------------------


def test_bridge_handler_removed_on_stop_and_no_more_events(tmp_path: Path) -> None:
    logger_name = "tests.bridge.removed"
    lg = _make_logger(logger_name)
    service, root, _runner = _make_service(tmp_path, logger_name)
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        handler = service._log_handler
        assert handler is not None and handler in lg.handlers
        assert service.request_stop("test_stop", "") is True
        _wait_lifecycle(service, STOPPED)
        assert service._log_handler is None
        assert handler not in lg.handlers
        service.drain_events(limit=1000)
        lg.info("停止后不应桥接")
        time.sleep(0.1)
        events = [event for event in service.drain_events(limit=1000) if event.kind is LOG_RECORD]
        assert events == []
    finally:
        _shutdown(service)


def test_bridge_handler_removed_on_shutdown(tmp_path: Path) -> None:
    logger_name = "tests.bridge.shutdown"
    _make_logger(logger_name)
    service, root, _runner = _make_service(tmp_path, logger_name)
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        assert service.shutdown() is True
        _wait_lifecycle(service, STOPPED)
        assert service._log_handler is None
        assert logging.getLogger(logger_name).handlers == []
    finally:
        _shutdown(service)


def test_bridge_handler_removed_on_error(tmp_path: Path) -> None:
    """清理未完成进入 ERROR 时同样移除日志桥接（设计 4.6 移除时机）。"""
    logger_name = "tests.bridge.error"
    lg = _make_logger(logger_name)
    service, root, runner = _make_service(tmp_path, logger_name)
    failed = replace(_cleanup_ok(), input_thread_joined=False, pending_items=("输入线程未退出",), completed=False)
    runner.cleanup = lambda: failed  # type: ignore[method-assign]
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        assert service._log_handler is not None
        assert service.request_stop("cleanup_fail", "") is True
        _wait_lifecycle(service, RunLifecycle.ERROR)
        assert service._log_handler is None
        assert lg.handlers == []
    finally:
        _shutdown(service)


def test_bridge_survives_configure_text_logging_refresh(tmp_path: Path) -> None:
    """运行时装配会调用 configure_text_logging（清空 wows_script handler），桥接必须自动重装。"""
    from src.storage.event_log import configure_text_logging

    project_logger = logging.getLogger("wows_script")
    old_level = project_logger.level
    old_propagate = project_logger.propagate
    old_handlers = list(project_logger.handlers)
    project_logger.setLevel(logging.INFO)
    project_logger.propagate = False
    for handler in list(project_logger.handlers):
        project_logger.removeHandler(handler)

    root = _make_root(tmp_path)
    runner = FakeRunner()

    def factory(_options: StartOptions) -> FakeRunner:
        # 等价于 _build_runtime -> configure_text_logging 对 logger handler 的重置
        configure_text_logging(tmp_path / "logs", level="INFO", console=False)
        return runner

    service = RuntimeService(
        root,
        clock=FakeClock(),
        runner_factory=factory,
        lock_factory=lambda _data_dir: FakeLock(),
        snapshot_min_interval_s=0.0,
    )
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        handlers = list(service._log_handlers)
        assert handlers
        assert any(handler in project_logger.handlers for handler in handlers)
        service.drain_events(limit=1000)
        project_logger.info("configure 重置后仍桥接")
        events = _drain_log_events(service)
        assert any("configure 重置后仍桥接" in event.message for event in events)
    finally:
        _shutdown(service)
        for handler in list(project_logger.handlers):
            project_logger.removeHandler(handler)
        for handler in old_handlers:
            project_logger.addHandler(handler)
        project_logger.setLevel(old_level)
        project_logger.propagate = old_propagate


def test_default_bridge_covers_project_logger_and_root(tmp_path: Path) -> None:
    """默认同时桥接根 logger 与项目文本 logger（wows_script，configure 后 propagate=False）。"""
    project_logger = logging.getLogger("wows_script")
    old_level = project_logger.level
    project_logger.setLevel(logging.INFO)
    service, root, _runner = _make_service(tmp_path, None)
    handlers: list = []
    try:
        assert service.start(StartOptions(root=root)).accepted
        _wait_lifecycle(service, RUNNING)
        handlers = list(service._log_handlers)
        assert len(handlers) == 2
        assert any(handler in project_logger.handlers for handler in handlers)
        assert any(handler in logging.getLogger().handlers for handler in handlers)
        service.drain_events(limit=1000)
        project_logger.info("项目文本日志桥接")
        events = _drain_log_events(service)
        assert len(events) == 1  # 两个 handler 看到同一记录，只能桥接一次
        assert "项目文本日志桥接" in events[0].message
        assert events[0].detail["logger"] == "wows_script"
    finally:
        _shutdown(service)
        project_logger.setLevel(old_level)
        for handler in handlers:
            if handler in project_logger.handlers:
                project_logger.removeHandler(handler)
            if handler in logging.getLogger().handlers:
                logging.getLogger().removeHandler(handler)
