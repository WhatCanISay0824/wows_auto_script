"""账号轮换（M3，GUI 设计文档 §5.12/§10/§13.4）测试：WAIT_ACCOUNT、确认/结束、上限。

全部用 FakeRunner + FakeClock + 假预检，不启动游戏、不写仓库运行产物。
"""

from __future__ import annotations

import inspect
import threading
import time
from dataclasses import replace
from pathlib import Path

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
from src.runtime.plan import load_accounts_or_none, preflight
from src.runtime.runner import RunnerViews, empty_runner_views
from src.runtime.service import RUN_THREAD_NAME, RuntimeService
from src.state_machine.accounts import AccountRotator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WAIT_ACCOUNT = RunLifecycle.WAIT_ACCOUNT
RUNNING = RunLifecycle.RUNNING
STOPPED = RunLifecycle.STOPPED

ACCOUNTS_YAML = """\
rotation:
  enabled: true
  mode: round_robin
  max_battles_per_account: 4
accounts:
  - alias: alt1
    display_name: 小号一号
    profile: cn360
    ship_id: leningrad
    daily_battle_cap: 30
    max_battles_per_session: 3
    enabled: true
  - alias: alt2
    display_name: 小号二号
    profile: cn360
    ship_id: leningrad
    daily_battle_cap: 20
    max_battles_per_session: 2
    enabled: true
"""

SINGLE_ACCOUNT_YAML = """\
rotation:
  enabled: true
  mode: round_robin
  max_battles_per_account: 4
accounts:
  - alias: alt1
    display_name: 小号一号
    profile: cn360
    ship_id: leningrad
    max_battles_per_session: 3
    enabled: true
"""


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class FakeLock:
    def __init__(self, *, acquired: bool = True, holder: dict[str, object] | None = None) -> None:
        self._acquired = acquired
        self._holder = holder
        self.release_calls = 0

    @property
    def holder(self) -> dict[str, object] | None:
        return self._holder

    def acquire(self) -> bool:
        return self._acquired

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
    """最小可驱动 runner：N 步后自然结束并报告固定场次/停止原因。"""

    def __init__(self, *, battles: int = 0, stop_reason: str = "account_rotation") -> None:
        self.plan = None
        self.battles_completed = int(battles)
        self.stop_reason = stop_reason
        self.start_calls = 0
        self.steps = 0
        self.cleanup_calls = 0
        self.cleanup_view = _cleanup_ok()
        self._finished = False
        self._paused = False
        self._lock = threading.Lock()

    def start(self) -> None:
        self.start_calls += 1

    def step(self) -> None:
        with self._lock:
            self.steps += 1
            if self.steps >= 1:
                self._finished = True

    @property
    def finished(self) -> bool:
        with self._lock:
            return self._finished

    def request_pause(self, reason: str = "fake_pause", detail: str = "") -> None:
        self._paused = True

    def request_resume(self) -> bool:
        self._paused = False
        return True

    def request_stop(self, reason: str = "fake_stop", detail: str = "") -> None:
        self.stop_reason = reason
        with self._lock:
            self._finished = True

    @property
    def paused(self) -> bool:
        return self._paused

    release_count = 0
    release_confirmed = False

    def snapshot_state(self, now: float) -> RunnerViews:
        base = empty_runner_views()
        return replace(
            base,
            session=replace(base.session, battles_completed=self.battles_completed),
        )

    def cleanup(self) -> CleanupView:
        self.cleanup_calls += 1
        return self.cleanup_view


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _make_rotation_root(tmp_path: Path, accounts_yaml: str = ACCOUNTS_YAML) -> Path:
    profile_dir = tmp_path / "profiles" / "cn360"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(
        "runtime: {}\npaths:\n  data_dir: data\n  log_dir: logs\n", encoding="utf-8"
    )
    (profile_dir / "profile.yaml").write_text(
        "client_id: cn360\ndisplay_name: 测试客户端\n", encoding="utf-8"
    )
    (tmp_path / "accounts.yaml").write_text(accounts_yaml, encoding="utf-8")
    return tmp_path


def _wait(predicate, timeout: float = 5.0, message: str = "等待条件超时") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail(message)


def _wait_lifecycle(service: RuntimeService, lifecycle: RunLifecycle, timeout: float = 5.0) -> None:
    _wait(
        lambda: service.lifecycle is lifecycle,
        timeout,
        f"生命周期未进入 {lifecycle}，当前 {service.lifecycle}",
    )


def _alive_run_threads() -> int:
    return sum(1 for thread in threading.enumerate() if thread.name == RUN_THREAD_NAME)


def _shutdown(service: RuntimeService) -> None:
    try:
        if service.lifecycle not in (STOPPED, RunLifecycle.ERROR):
            service.shutdown()
        _wait(lambda: service.lifecycle in (STOPPED, RunLifecycle.ERROR), 5.0, "收尾超时")
    except Exception:  # pragma: no cover - 尽力收尾
        pass


def _make_service(
    tmp_path: Path,
    *,
    battles: dict[str | None, int] | None = None,
    precheck: tuple[bool, str] | None = (True, "游戏在前台且已识别到港口界面"),
    lock_results: list[bool] | None = None,
    accounts_yaml: str = ACCOUNTS_YAML,
    cleanup_view: CleanupView | None = None,
) -> tuple[RuntimeService, Path, list[StartOptions], list[FakeRunner], list[FakeLock]]:
    root = _make_rotation_root(tmp_path, accounts_yaml)
    battles_by_alias = {"alt1": 3, "alt2": 2}
    if battles is not None:
        battles_by_alias.update(battles)
    records: list[StartOptions] = []
    runners: list[FakeRunner] = []
    locks: list[FakeLock] = []

    def factory(options: StartOptions) -> FakeRunner:
        records.append(options)
        runner = FakeRunner(battles=battles_by_alias.get(options.account_alias, 0))
        if cleanup_view is not None:
            runner.cleanup_view = cleanup_view
        runners.append(runner)
        return runner

    results = list(lock_results or [True, True, True, True])

    def lock_factory(_data_dir: Path) -> FakeLock:
        acquired = results[len(locks)] if len(locks) < len(results) else True
        lock = FakeLock(acquired=acquired)
        locks.append(lock)
        return lock

    service = RuntimeService(
        root,
        clock=FakeClock(),
        runner_factory=factory,
        lock_factory=lock_factory,
        snapshot_min_interval_s=0.0,
        port_precheck=(lambda: precheck) if precheck is not None else None,
    )
    return service, root, records, runners, locks


# ---------------------------------------------------------------------------
# 达到轮换上限 -> WAIT_ACCOUNT
# ---------------------------------------------------------------------------


def test_reaches_rotation_limit_enters_wait_account(tmp_path: Path) -> None:
    service, root, records, runners, locks = _make_service(tmp_path)
    try:
        seq_before = service.snapshot().seq
        assert service.start(StartOptions(root=root, rotate=True)).accepted
        _wait_lifecycle(service, WAIT_ACCOUNT)
        assert _alive_run_threads() == 0  # 运行线程已退出，等待期间不持有控制线程

        snapshot = service.snapshot()
        assert snapshot.lifecycle.lifecycle is WAIT_ACCOUNT
        assert snapshot.session.account_alias == "alt1"
        assert snapshot.session.rotation_aliases == ("alt1", "alt2")
        assert snapshot.session.rotation_index == 0
        assert snapshot.session.next_alias == "alt2"
        assert snapshot.session.battles_completed == 3
        assert snapshot.cleanup.completed is True
        assert snapshot.cleanup.single_instance_released is True
        # 单实例锁在等待期间已释放（设计 5.10：控制锁只在 STARTING→STOPPED 持有）
        assert locks and locks[0].release_calls == 1
        # 本轮生效值：账号覆盖后的场次上限
        assert len(records) == 1
        assert records[0].account_alias == "alt1"
        assert records[0].max_battles == 3
        assert records[0].rotate is True
        assert runners[0].cleanup_calls == 1
        # 事件：ACCOUNT_SWITCH_WAITING 且含当前/下一账号与原因
        events = service.drain_events(limit=1000)
        assert snapshot.seq > seq_before  # 轮换期间快照 seq 仍单调递增
        assert [event.seq for event in events] == sorted(event.seq for event in events)
        waiting = [event for event in events if event.kind is RuntimeEventKind.ACCOUNT_SWITCH_WAITING]
        assert waiting
        detail = waiting[-1].detail
        assert detail["account_alias"] == "alt1"
        assert detail["next_alias"] == "alt2"
        assert detail["battles_completed"] == 3
        assert detail["reason"] == "account_rotation"
        # 等待期间日志桥接仍安装（GUI 日志页可继续观察）
        assert service._log_handler is not None
        # 等待期间暂停/恢复被拒绝（不是“已暂停”）
        assert service.request_pause("wait_pause") is False
        assert service.request_resume() is False
    finally:
        _shutdown(service)


def test_confirm_switch_starts_next_account_then_abort_ends_rotation(tmp_path: Path) -> None:
    service, root, records, runners, locks = _make_service(tmp_path)
    try:
        assert service.start(StartOptions(root=root, rotate=True)).accepted
        _wait_lifecycle(service, WAIT_ACCOUNT)
        assert service.confirm_account_switch() is True
        # 下一账号会话可能很快结束：以“工厂被再次调用 + 新 runner 已 start”作为确认依据
        _wait(lambda: len(records) == 2 and runners[1].start_calls == 1, message="下一账号未启动")
        _wait_lifecycle(service, WAIT_ACCOUNT)  # 第二个账号同样达到上限后再次等待
        # 下一账号：同一单实例锁契约（等待期间释放，确认时重新获取）
        assert len(locks) == 2
        assert locks[1].release_calls == 1  # 第二会话清理后再次释放（无重叠控制实例）
        assert len(records) == 2
        assert records[1].account_alias == "alt2"
        assert records[1].max_battles == 2
        snapshot = service.snapshot()
        assert snapshot.session.account_alias == "alt2"
        assert snapshot.session.rotation_index == 1
        assert snapshot.session.next_alias == "alt1"
        events = service.drain_events(limit=1000)
        assert any(event.kind is RuntimeEventKind.ACCOUNT_SWITCH_CONFIRMED for event in events)

        # 第二个账号达到上限 -> 再次等待；abort_rotation 结束轮换
        _wait_lifecycle(service, WAIT_ACCOUNT)
        assert _alive_run_threads() == 0
        assert service.abort_rotation() is True
        _wait_lifecycle(service, STOPPED)
        assert service.snapshot().cleanup.completed is True
        assert _alive_run_threads() == 0
        assert service._log_handler is None  # 终态移除日志桥接
        assert service.request_stop("after_abort") is True  # STOPPED 幂等
    finally:
        _shutdown(service)


def test_precheck_failure_stays_wait_account_and_keeps_index(tmp_path: Path) -> None:
    service, root, records, _runners, locks = _make_service(
        tmp_path, precheck=(False, "游戏窗口不在前台，请手动点回游戏后重试")
    )
    try:
        assert service.start(StartOptions(root=root, rotate=True)).accepted
        _wait_lifecycle(service, WAIT_ACCOUNT)
        release_before = locks[0].release_calls
        assert service.confirm_account_switch() is False
        _wait_lifecycle(service, WAIT_ACCOUNT)
        snapshot = service.snapshot()
        assert snapshot.session.rotation_index == 0
        assert snapshot.session.next_alias == "alt2"
        assert snapshot.session.account_alias == "alt1"
        assert len(records) == 1  # 未启动下一账号
        assert locks[0].release_calls == release_before  # 未额外获取锁
        events = service.drain_events(limit=1000)
        waiting = [event for event in events if event.kind is RuntimeEventKind.ACCOUNT_SWITCH_WAITING]
        assert waiting
        assert waiting[-1].detail["reason"] == "precheck_failed"
        assert "不在前台" in str(waiting[-1].detail["detail"])
        # 失败后仍可重试（假预检固定失败项保留；此处直接结束轮换）
        assert service.abort_rotation() is True
        _wait_lifecycle(service, STOPPED)
    finally:
        _shutdown(service)


def test_confirm_switch_lock_held_stays_wait_account(tmp_path: Path) -> None:
    service, root, records, _runners, _locks = _make_service(tmp_path, lock_results=[True, False])
    try:
        assert service.start(StartOptions(root=root, rotate=True)).accepted
        _wait_lifecycle(service, WAIT_ACCOUNT)
        assert service.confirm_account_switch() is False
        _wait_lifecycle(service, WAIT_ACCOUNT)
        assert len(records) == 1
        events = service.drain_events(limit=1000)
        waiting = [event for event in events if event.kind is RuntimeEventKind.ACCOUNT_SWITCH_WAITING]
        assert waiting
        assert waiting[-1].detail["reason"] == "lock_held"
        assert _alive_run_threads() == 0
        assert service.abort_rotation() is True
    finally:
        _shutdown(service)


def test_single_enabled_account_rotate_ends_stopped(tmp_path: Path) -> None:
    service, root, records, _runners, _locks = _make_service(tmp_path, accounts_yaml=SINGLE_ACCOUNT_YAML)
    try:
        assert service.start(StartOptions(root=root, rotate=True)).accepted
        _wait_lifecycle(service, STOPPED)
        assert len(records) == 1
        assert records[0].account_alias == "alt1"
        # 单账号轮换使用轮换上限（与 CLI --rotate 单账号行为一致）
        assert records[0].max_battles == 3
        assert service.snapshot().session.rotation_aliases == ("alt1",)
        assert service._log_handler is None
    finally:
        _shutdown(service)


def test_command_queue_confirm_switch_during_wait(tmp_path: Path) -> None:
    """WAIT_ACCOUNT 期间没有运行线程：CONFIRM_ACCOUNT_SWITCH 命令必须立即生效。"""
    service, root, records, runners, _locks = _make_service(tmp_path)
    try:
        assert service.start(StartOptions(root=root, rotate=True)).accepted
        _wait_lifecycle(service, WAIT_ACCOUNT)
        assert service.submit_command(RunCommand(RunCommandKind.CONFIRM_ACCOUNT_SWITCH)) is True
        _wait(lambda: len(records) == 2 and runners[1].start_calls == 1, message="命令未启动下一账号")
        assert records[1].account_alias == "alt2"
        _wait_lifecycle(service, WAIT_ACCOUNT)
        assert service.submit_command(RunCommand(RunCommandKind.ABORT_ROTATION)) is True
        _wait_lifecycle(service, STOPPED)
    finally:
        _shutdown(service)


def test_rotate_with_explicit_account_starts_from_selected_alias(tmp_path: Path) -> None:
    """GUI 同时选择账号 + 勾选轮换：首场从所选账号开始（与 plan_preview 生效值一致）。"""
    service, root, records, _runners, _locks = _make_service(tmp_path)
    try:
        assert service.start(StartOptions(root=root, rotate=True, account_alias="alt2")).accepted
        _wait_lifecycle(service, WAIT_ACCOUNT)
        assert len(records) == 1
        assert records[0].account_alias == "alt2"
        assert records[0].max_battles == 2
        snapshot = service.snapshot()
        assert snapshot.session.account_alias == "alt2"
        assert snapshot.session.rotation_index == 1
        assert snapshot.session.next_alias == "alt1"
        assert service.abort_rotation() is True
    finally:
        _shutdown(service)


def test_stop_or_shutdown_during_wait_account(tmp_path: Path) -> None:
    service, root, _records, _runners, _locks = _make_service(tmp_path)
    try:
        assert service.start(StartOptions(root=root, rotate=True)).accepted
        _wait_lifecycle(service, WAIT_ACCOUNT)
        assert service.request_stop("gui_stop_during_wait") is True
        _wait_lifecycle(service, STOPPED)
        assert _alive_run_threads() == 0
    finally:
        _shutdown(service)

    service2, root2, _records2, _runners2, _locks2 = _make_service(tmp_path / "second")
    try:
        assert service2.start(StartOptions(root=root2, rotate=True)).accepted
        _wait_lifecycle(service2, WAIT_ACCOUNT)
        assert service2.shutdown() is True
        _wait_lifecycle(service2, STOPPED)
        assert service2.shutdown_requested is True
        assert service2.start(StartOptions(root=root2, rotate=True)).accepted is False
        assert _alive_run_threads() == 0
    finally:
        _shutdown(service2)


def test_non_rotate_service_does_not_wait_account(tmp_path: Path) -> None:
    service, root, records, _runners, _locks = _make_service(tmp_path)
    try:
        assert service.start(StartOptions(root=root, rotate=False)).accepted
        _wait_lifecycle(service, STOPPED)
        assert len(records) == 1
        assert records[0].rotate is False
        assert service.snapshot().session.rotation_aliases == ()
    finally:
        _shutdown(service)


def test_rotation_wait_requires_cleanup_completed(tmp_path: Path) -> None:
    """清理未完成时必须进入 ERROR，而不是 WAIT_ACCOUNT（设计 5.3/5.9）。"""
    failed_cleanup = replace(
        _cleanup_ok(),
        input_thread_joined=False,
        pending_items=("输入线程未退出",),
        completed=False,
    )
    service, root, _records, _runners, _locks = _make_service(tmp_path, cleanup_view=failed_cleanup)
    try:
        assert service.start(StartOptions(root=root, rotate=True)).accepted
        _wait_lifecycle(service, RunLifecycle.ERROR)
        snapshot = service.snapshot()
        assert snapshot.cleanup.completed is False
        assert snapshot.error is not None and snapshot.error.recoverable is False
        assert _alive_run_threads() == 0
    finally:
        _shutdown(service)


def test_default_port_precheck_config_error_is_graceful(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """真实默认预检在配置加载失败时不抛异常，留在 WAIT_ACCOUNT 并发布原因。"""
    import src.runtime.service as service_module
    from src.common.errors import ConfigError, Severity, ValidationIssue

    service, root, records, _runners, _locks = _make_service(tmp_path, precheck=None)
    try:
        assert service.start(StartOptions(root=root, rotate=True)).accepted
        _wait_lifecycle(service, WAIT_ACCOUNT)

        def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
            raise ConfigError(
                [ValidationIssue(Severity.ERROR, "file_missing", "profiles/cn360/profile.yaml", "缺少配置")]
            )

        monkeypatch.setattr(service_module, "load_app_for_account", _boom)
        assert service.confirm_account_switch() is False
        _wait_lifecycle(service, WAIT_ACCOUNT)
        assert len(records) == 1
        events = service.drain_events(limit=1000)
        waiting = [event for event in events if event.kind is RuntimeEventKind.ACCOUNT_SWITCH_WAITING]
        assert waiting
        assert "配置校验失败" in str(waiting[-1].detail["detail"])
        assert service.abort_rotation() is True
    finally:
        _shutdown(service)


def test_default_port_precheck_no_window_is_graceful(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """真实默认预检在找不到游戏窗口时返回 (False, 原因)，不抛异常。"""
    from types import SimpleNamespace

    import src.runtime.service as service_module

    service, root, records, _runners, _locks = _make_service(tmp_path, precheck=None)
    try:
        assert service.start(StartOptions(root=root, rotate=True)).accepted
        _wait_lifecycle(service, WAIT_ACCOUNT)
        fake_app = SimpleNamespace(
            profile=SimpleNamespace(
                window=SimpleNamespace(
                    process_names=["WorldOfWarships.exe"],
                    title_patterns=["战舰世界"],
                    require_foreground=True,
                )
            )
        )

        class _MissingLocator:
            def __init__(self, *_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
                pass

            def find(self):  # noqa: ANN201
                return None

        monkeypatch.setattr(service_module, "load_app_for_account", lambda *_a, **_k: fake_app)
        monkeypatch.setattr(service_module, "WindowLocator", _MissingLocator)
        assert service.confirm_account_switch() is False
        _wait_lifecycle(service, WAIT_ACCOUNT)
        assert len(records) == 1
        events = service.drain_events(limit=1000)
        waiting = [event for event in events if event.kind is RuntimeEventKind.ACCOUNT_SWITCH_WAITING]
        assert waiting
        assert "找不到游戏窗口" in str(waiting[-1].detail["detail"])
        assert service.abort_rotation() is True
    finally:
        _shutdown(service)


def test_preflight_rotation_limit_for_explicit_account(tmp_path: Path) -> None:
    """rotate + 显式账号：预检构造的轮换器起点与上限必须与首场一致（GUI 生效值）。"""
    root = _make_rotation_root(tmp_path)
    pre = preflight(root, StartOptions(root=root, rotate=True, account_alias="alt2"))
    assert pre.ok, pre.issues
    assert pre.account_alias == "alt2"
    assert pre.rotator is not None
    assert pre.rotator.current is not None and pre.rotator.current.alias == "alt2"
    assert pre.rotator.current_limit() == 2


def test_cli_rotate_uses_same_account_rotator_semantics(tmp_path: Path) -> None:
    """CLI 保留 `_run_accounts` 循环，但必须与 service 共用同一 AccountRotator 语义。

    对照点：current_limit() 作为本轮生效上限、record_battle() 后 needs_rotation() 为真、
    advance() 后的 next_alias 与快照一致。
    """
    root = _make_rotation_root(tmp_path)
    accounts = load_accounts_or_none(root)
    assert accounts is not None
    rotator = AccountRotator(accounts)
    assert rotator.current is not None and rotator.current.alias == "alt1"
    assert rotator.current_limit() == 3
    for _ in range(3):  # 每个账号每完成一场记一次，达到 current_limit 后需要轮换
        rotator.record_battle("alt1")
    assert rotator.needs_rotation() is True
    assert rotator.next_alias() == "alt2"
    rotator.advance()
    assert rotator.current is not None and rotator.current.alias == "alt2"
    assert rotator.current_limit() == 2

    # CLI 的 _run_accounts 确实用同一套 rotator 接口（不自行实现计数/上限）
    source = inspect.getsource(_cli_run_accounts())
    for token in ("current_limit()", "record_battle(", "advance()"):
        assert token in source, token


def _cli_run_accounts():
    from src.main import _run_accounts

    return _run_accounts
