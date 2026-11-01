"""运行服务：命令行与 GUI 共用的唯一运行入口（GUI 设计文档第 4/5/10 节）。

职责边界（3.3）：

- 生命周期、单实例锁、命令/事件有界队列、快照、统一清理；
- 账号轮换（5.12 / 10.2）：会话结束且达到本账号轮换上限时进入 ``WAIT_ACCOUNT``，
  手动确认后做“前台 + 港口”预检并启动下一账号（单实例锁在等待期间释放、确认时重新获取）；
- ``LOG_RECORD`` 日志桥接（4.6 / 12.1）：运行期安装 ``logging.Handler``，把 INFO+ 日志
  非阻塞地镜像成事件；移除时机为 STOPPED/ERROR/shutdown；
- 运行线程（``runtime-session``）只做“装配 + 主循环单步 + 发布快照/事件 + 清理”；
- 不创建任何 GUI 对象、不直接发送键鼠（输入仍由 ``InputExecutor`` 输入线程执行）、
  不改变状态机的游戏流程决策。

线程安全约定：

- 所有对外的 ``snapshot()/drain_events()/lifecycle`` 与 ``request_*()/start()/shutdown()``
  都是非阻塞、线程安全、幂等的；内部状态由一把 ``RLock`` 保护；
- 真正的线程退出/清理由运行线程在后台推进，GUI 通过快照观察（设计 4.7）；
- ``request_pause()/request_stop()`` 走快速路径：写共享 ``StopController`` + 让输入线程
  关闭门禁并释放（非阻塞），状态机转移由运行线程下一轮完成（设计 5.4）。

测试注入点：``clock``（FakeClock）、``runner_factory``（FakeRunner）、``lock_factory``
（假单实例锁）、``port_precheck``（切号预检，测试注入假实现），过载监控通过
``overload_factory/overload_monitor`` 透传给 runner 装配。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..common.clock import Clock, SystemClock, clock_iso
from ..common.errors import ConfigError
from ..common.single_instance import SingleInstanceLock
from ..common.types import State
from ..common.window import WindowLocator
from ..perception.capture import GdiCapture
from ..perception.engine import PerceptionEngine
from ..state_machine.accounts import AccountRotator
from . import capabilities as _capabilities
from .messages import (
    CleanupView,
    DashboardView,
    EffectivePlan,
    ErrorView,
    LifecycleView,
    PerformanceView,
    PlanResult,
    RunCommand,
    RunCommandKind,
    RunLifecycle,
    RunSnapshot,
    RuntimeEvent,
    RuntimeEventKind,
    StartOptions,
    StartResult,
    ValidationIssueView,
)
from .plan import (
    DEFAULT_PROFILE_ID,
    issue_views,
    load_app_for_account,
    preflight,
    resolve_data_dir,
    resolve_start_plan,
    with_log_level,
)
from .plan import (
    plan_preview as _plan_preview,
)
from .runner import (
    RunnerViews,
    SessionRunner,
    _build_runtime,
    empty_runner_views,
)

logger = logging.getLogger("wows_script.runtime.service")

#: 运行线程名（测试/诊断用于确认只有一个控制实例）
RUN_THREAD_NAME = "runtime-session"
#: 事件队列默认上限（设计 5.5：满了丢最旧并计数）
EVENT_QUEUE_MAX = 2000
#: 命令队列默认上限（设计 5.5：RunCommand 队列 64）
COMMAND_QUEUE_MAX = 64
#: 仅这些暂停原因允许战斗内显式选点；人工/异常暂停不在此列。
_FOCUS_PAUSE_REASONS = frozenset({"window_lost", "window_unfocused", "window_unavailable"})
#: 快照默认发布间隔（设计 6.3：200ms）
SNAPSHOT_MIN_INTERVAL_S = 0.2
#: 日志桥接默认级别（设计 4.6：DEBUG 默认不桥接，避免高频日志拖慢界面）
LOG_BRIDGE_LEVEL = logging.INFO
#: 轮换等待时，这些停止原因不进入下一账号（紧急/手动/关闭/崩溃/运行异常）
_NO_ROTATE_REASONS: frozenset[str] = frozenset(
    {
        "emergency_stop",
        "manual",
        "shutdown",
        "game_crash",
        "runtime_error",
        "validation_failed",
        "cancelled",
    }
)
#: 会话日志桥接的记录格式（与 logs/ 文本日志保持一致口径）
LOG_BRIDGE_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"

RunnerFactory = Callable[[StartOptions], Any]
LockFactory = Callable[[Path], Any]
PortPrecheck = Callable[[], tuple[bool, str]]

_UNSET = object()


class _RuntimeLogHandler(logging.Handler):
    """把 INFO+ 日志镜像为 ``LOG_RECORD`` 事件（设计 4.6 / 12.1）。

    硬性约束：

    - **绝不递归**：``emit`` 内不调用任何 logging 方法，发布事件也不产生日志；
    - **不阻塞调用线程**：通过 ``block=False`` 发布，服务锁被占用时直接丢弃并计数；
    - 自身异常全部吞掉，不能影响业务日志线程。
    """

    def __init__(self, publish: Callable[..., bool], *, level: int) -> None:
        super().__init__(level=level)
        self._publish = publish
        self._formatter = logging.Formatter(LOG_BRIDGE_FORMAT)
        self.attached_logger: logging.Logger | None = None
        self.closed = False

    def emit(self, record: logging.LogRecord) -> None:
        if self.closed:  # 移除后到达的在途记录直接丢弃
            return
        if getattr(record, "_wows_runtime_bridge_seen", False):
            # 同一记录可能被多个目标 logger 的 handler 看到（根 + wows_script），只桥接一次
            return
        try:
            record._wows_runtime_bridge_seen = True  # type: ignore[attr-defined]
            message = self._formatter.format(record)
            self._publish(
                RuntimeEventKind.LOG_RECORD,
                record.levelname,
                message,
                {
                    "logger": record.name,
                    "level": record.levelname,
                    "thread": record.threadName,
                    "module": record.module,
                },
                block=False,
            )
        except Exception:  # 日志桥接失败不得影响原日志/运行线程
            return


def _completed_cleanup_view() -> CleanupView:
    """未请求清理/无资源需要清理时的完成态清单。"""
    return CleanupView(
        requested=False,
        input_released=False,
        input_thread_joined=False,
        watchdog_stopped=False,
        event_sink_stopped=False,
        screenshots_cleaned=False,
        capture_closed=False,
        single_instance_released=False,
        pending_items=(),
        completed=True,
    )


class RuntimeService:
    """命令行与 GUI 共用的唯一运行入口；自身不创建任何 GUI 对象。"""

    def __init__(
        self,
        root: Path,
        *,
        clock: Clock | None = None,
        runner_factory: RunnerFactory | None = None,
        lock_factory: LockFactory | None = None,
        event_max: int = EVENT_QUEUE_MAX,
        command_max: int = COMMAND_QUEUE_MAX,
        snapshot_min_interval_s: float = SNAPSHOT_MIN_INTERVAL_S,
        overload_factory: Callable[..., Any] | None = None,
        overload_monitor: Any | None = None,
        port_precheck: PortPrecheck | None = None,
        log_bridge_logger: logging.Logger | str | None = None,
        log_bridge_level: int = LOG_BRIDGE_LEVEL,
    ) -> None:
        self._root = Path(root).expanduser().resolve()
        self._clock: Clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._runner_factory: RunnerFactory = runner_factory or self._build_default_runner
        self._lock_factory: LockFactory = lock_factory or SingleInstanceLock
        self._event_max = max(1, int(event_max))
        self._command_max = max(1, int(command_max))
        self._snapshot_min_interval_s = max(0.0, float(snapshot_min_interval_s))
        self._overload_factory = overload_factory
        self._overload_monitor = overload_monitor
        self._gui_enabled = False
        self._idle_metrics = None
        self._port_precheck = port_precheck
        self._log_bridge_logger = log_bridge_logger
        self._log_bridge_level = int(log_bridge_level)
        self._log_handlers: list[_RuntimeLogHandler] = []
        #: 兼容单 handler 场景（自定义 logger 时等于唯一 handler；默认多目标时为第一个）
        self._log_handler: _RuntimeLogHandler | None = None

        self._lifecycle: RunLifecycle = RunLifecycle.STOPPED
        self._since_at: float = self._clock.now()
        self._since_wall: str = clock_iso(self._clock)
        self._detail: str = "未运行"
        self._pending_action: str | None = None
        self._resume_pending: bool = False

        self._events: deque[RuntimeEvent] = deque()
        self._commands: deque[RunCommand] = deque()
        self._event_seq = 0
        self._snapshot_seq = 0
        self._dropped_events = 0

        self._plan: EffectivePlan | None = None
        self._cleanup: CleanupView = _completed_cleanup_view()
        self._error: ErrorView | None = None
        self._capabilities = _capabilities.default_capabilities()
        self._views: RunnerViews = empty_runner_views()
        self._last_game_state: str | None = None
        self._snapshot: RunSnapshot | None = None
        self._last_publish_at = self._since_at - 1_000_000.0

        self._options: StartOptions | None = None
        self._start_options: StartOptions | None = None
        self._runner: Any | None = None
        self._thread: threading.Thread | None = None
        self._instance_lock: Any | None = None
        self._stop_requested = False
        self._stop_reason: str | None = None
        self._shutdown = False

        # --- 账号轮换（设计 5.12 / 10.2） ---
        self._rotator: AccountRotator | None = None
        self._rotate = False
        self._rotation_aliases: tuple[str, ...] = ()
        self._rotation_index: int | None = None
        self._current_alias: str | None = None
        self._account_switch_inflight = False
        self._account_precheck_reason: str | None = None

        with self._lock:
            self._rebuild_snapshot_locked()

    # ------------------------------------------------------------------
    # 观察接口（GUI 主线程安全调用）
    # ------------------------------------------------------------------
    @property
    def lifecycle(self) -> RunLifecycle:
        with self._lock:
            return self._lifecycle

    @property
    def dropped_events(self) -> int:
        """因事件队列满而丢弃的事件数（GUI 日志页显示“已丢弃 N 条”）。"""
        with self._lock:
            return self._dropped_events

    @property
    def shutdown_requested(self) -> bool:
        with self._lock:
            return self._shutdown

    def snapshot(self) -> RunSnapshot:
        """返回最近一次发布的只读快照（非阻塞、不会等待运行线程）。"""
        with self._lock:
            assert self._snapshot is not None
            return self._snapshot

    def drain_events(self, limit: int = 200) -> tuple[RuntimeEvent, ...]:
        """取出至多 ``limit`` 条事件（非阻塞）；序号单调递增，跳变表示有丢弃。"""
        with self._lock:
            count = max(0, min(int(limit), len(self._events)))
            items = [self._events.popleft() for _ in range(count)]
        return tuple(items)

    # ------------------------------------------------------------------
    # 预检与启动
    # ------------------------------------------------------------------
    def plan_preview(self, options: StartOptions) -> PlanResult:
        """只读预检：可重复调用，不产生运行资源（GUI“校验配置”按钮）。"""
        return _plan_preview(self._root, options)

    def start(self, options: StartOptions) -> StartResult:
        """非阻塞启动：快速预检 → 取单实例锁 → 创建运行线程 → 立即返回。

        预检失败/锁被占时保持 ``STOPPED`` 并返回拒绝原因（设计 5.3/11.2）。
        """
        pre = preflight(self._root, options)
        if not pre.ok:
            issues = issue_views(pre.issues)
            self.publish_event(
                RuntimeEventKind.VALIDATION_FAILED,
                "ERROR",
                f"启动预检失败：{len(issues)} 个错误",
                {"issues": [_issue_to_dict(item) for item in issues]},
            )
            with self._lock:
                self._rebuild_snapshot_locked()
            return StartResult(accepted=False, reason="validation_failed", issues=issues)

        with self._lock:
            if self._shutdown:
                return StartResult(accepted=False, reason="shutdown")
            thread = self._thread
            if self._lifecycle is not RunLifecycle.STOPPED or (thread is not None and thread.is_alive()):
                return StartResult(accepted=False, reason="already_active")
            instance_lock = self._make_instance_lock()
            if not instance_lock.acquire():
                holder = getattr(instance_lock, "holder", None)
                self._emit_locked(
                    RuntimeEventKind.ERROR,
                    "WARNING",
                    "已有另一个脚本实例在运行，本次不启动游戏控制",
                    {"reason": "lock_held", "holder": holder or {}},
                )
                return StartResult(accepted=False, reason="lock_held")
            self._instance_lock = instance_lock
            self._options = replace(options, root=self._root)
            self._stop_requested = False
            self._stop_reason = None
            self._error = None
            self._cleanup = _completed_cleanup_view()
            self._commands.clear()
            self._last_game_state = None
            self._account_precheck_reason = None
            self._account_switch_inflight = False
            # --- 账号轮换状态（设计 5.12）：轮换计划来自预检构造的 AccountRotator ---
            self._start_options = replace(options, root=self._root)
            self._rotator = pre.rotator if options.rotate else None
            self._rotation_aliases = tuple(self._rotator.aliases) if self._rotator is not None else ()
            self._rotation_index = self._rotator.index if self._rotator is not None else None
            self._rotate = bool(self._rotator is not None and len(self._rotation_aliases) > 1)
            if self._rotator is not None and options.account_alias in self._rotation_aliases:
                # GUI 同时选择账号与“会话间轮换”时，以所选账号为轮换起点（与预检生效值一致）
                self._position_rotator(str(options.account_alias))
            if self._rotator is not None:
                self._rotation_index = self._rotator.index
            alias = (
                self._rotator.current.alias
                if self._rotator is not None and self._rotator.current is not None
                else pre.account_alias
            )
            self._current_alias = alias
            self._options = self._build_effective_options(alias)
            self._install_log_bridge_locked()
            start_detail = f"账号「{alias}」" if alias else "默认账号"
            self._set_lifecycle_locked(
                RunLifecycle.STARTING,
                f"正在启动 {start_detail}：取锁、加载配置、装配运行时、切前台中",
                pending_action=None,
                resume_pending=False,
            )
            thread = threading.Thread(target=self._run_thread, name=RUN_THREAD_NAME, daemon=True)
            self._thread = thread
        thread.start()
        return StartResult(accepted=True)

    # ------------------------------------------------------------------
    # 控制接口（线程安全、非阻塞、幂等）
    # ------------------------------------------------------------------
    def request_pause(self, reason: str = "gui_pause", detail: str = "") -> bool:
        """请求暂停；暂停优先于恢复、停止优先于暂停（重复请求返回 True 只第一次生效）。"""
        with self._lock:
            if self._shutdown:
                return False
            lifecycle = self._lifecycle
            if lifecycle in (RunLifecycle.PAUSING, RunLifecycle.PAUSED):
                return True
            if lifecycle is RunLifecycle.STARTING:
                self._pending_action = "pause"
                self._rebuild_snapshot_locked()
                return True
            if lifecycle is not RunLifecycle.RUNNING:
                return False
            runner = self._runner
            self._pending_action = "pause"
            self._resume_pending = False
            # 先进入 PAUSING：避免运行线程先观察到 runner 已暂停而跳过“释放确认”判定
            self._set_lifecycle_locked(
                RunLifecycle.PAUSING,
                f"暂停中：{reason}",
                pending_action="pause",
                resume_pending=False,
            )
        self.publish_event(
            RuntimeEventKind.PAUSE_REQUESTED,
            "WARNING",
            f"请求暂停：{reason}",
            {"reason": reason, "detail": detail, "source": "gui"},
        )
        if runner is not None:
            try:
                runner.request_pause(reason, detail)
            except Exception:
                logger.exception("转发暂停请求失败")
                with self._lock:
                    if self._lifecycle is RunLifecycle.PAUSING:
                        self._set_lifecycle_locked(
                            RunLifecycle.RUNNING,
                            "运行中（暂停请求转发失败，已回退）",
                            pending_action=None,
                            resume_pending=False,
                        )
                return False
        return True

    def request_resume(self) -> bool:
        """请求恢复：仅 ``PAUSED`` 生效；运行线程先做前台校验再转发状态机（设计 11.3）。"""
        with self._lock:
            if self._shutdown or self._lifecycle is not RunLifecycle.PAUSED:
                return False
            runner = self._runner
            self._pending_action = "resume"
            self._resume_pending = True
            self._rebuild_snapshot_locked()
        self.publish_event(
            RuntimeEventKind.RESUME_REQUESTED,
            "INFO",
            "请求恢复运行",
            {"source": "gui"},
        )
        accepted = True
        if runner is not None:
            try:
                accepted = bool(runner.request_resume())
            except Exception:
                logger.exception("转发恢复请求失败")
                accepted = False
        if not accepted:
            with self._lock:
                if self._lifecycle is RunLifecycle.PAUSED:
                    self._pending_action = None
                    self._resume_pending = False
                    self._rebuild_snapshot_locked()
            return False
        return True

    def request_stop(self, reason: str = "gui_stop", detail: str = "") -> bool:
        """请求停止：停止优先于暂停；进入 ``STOPPING`` 后由运行线程统一清理。"""
        with self._lock:
            lifecycle = self._lifecycle
            if lifecycle is RunLifecycle.WAIT_ACCOUNT:
                # 等待切号期间“停止”= 结束轮换（设计 10.2：未运行任何会话，无需清理）
                self._stop_requested = True
                self._stop_reason = reason
                self._pending_action = None
                self._resume_pending = False
                self._emit_locked(
                    RuntimeEventKind.STOP_REQUESTED,
                    "WARNING",
                    f"请求停止：{reason}（结束账号轮换）",
                    {"reason": reason, "detail": detail, "source": "gui"},
                )
                self._uninstall_log_bridge_locked()
                self._set_lifecycle_locked(
                    RunLifecycle.STOPPED,
                    f"已结束账号轮换（停止请求：{reason}）",
                    pending_action=None,
                    resume_pending=False,
                )
                return True
            if lifecycle in (RunLifecycle.STOPPING, RunLifecycle.STOPPED):
                return True
            if lifecycle is RunLifecycle.ERROR:
                # ERROR 只允许 shutdown（设计 5.3/11.6）
                return False
            if self._lifecycle not in (
                RunLifecycle.STARTING,
                RunLifecycle.RUNNING,
                RunLifecycle.PAUSING,
                RunLifecycle.PAUSED,
            ):
                return False
            runner = self._runner
            self._stop_requested = True
            if self._stop_reason is None:
                self._stop_reason = reason
            self._pending_action = "stop"
            self._resume_pending = False
            self._set_lifecycle_locked(
                RunLifecycle.STOPPING,
                f"停止中：{reason}",
                pending_action="stop",
                resume_pending=False,
            )
        self.publish_event(
            RuntimeEventKind.STOP_REQUESTED,
            "WARNING",
            f"请求停止：{reason}",
            {"reason": reason, "detail": detail, "source": "gui"},
        )
        if runner is not None:
            try:
                runner.request_stop(reason, detail)
            except Exception:
                logger.exception("转发停止请求失败")
        return True

    def shutdown(self) -> bool:
        """关闭窗口用：等价 stop + 禁止再次 start（``ERROR`` 只允许 shutdown，设计 6.5/10.2）。"""
        with self._lock:
            self._shutdown = True
            lifecycle = self._lifecycle
            if lifecycle is RunLifecycle.WAIT_ACCOUNT:
                # 等待切号时关闭 = 结束轮换并退出，不留下控制线程（设计 10.2）
                self._pending_action = None
                self._uninstall_log_bridge_locked()
                self._set_lifecycle_locked(
                    RunLifecycle.STOPPED,
                    "已结束账号轮换（关闭窗口）",
                    pending_action=None,
                    resume_pending=False,
                )
                return True
        if lifecycle in (RunLifecycle.STOPPED, RunLifecycle.ERROR):
            return True
        return self.request_stop("shutdown", "关闭窗口")

    def confirm_account_switch(self) -> bool:
        """确认已手动切号：前台 + ``detect_state()==PORT`` 预检通过后启动下一账号。

        预检可注入（``port_precheck``，测试用假实现）；真实默认预检使用
        ``WindowLocator`` + ``GdiCapture`` + ``PerceptionEngine`` 只读判断，失败不抛异常。
        预检失败/取锁失败/配置错误时保持 ``WAIT_ACCOUNT`` 并发布原因（设计 5.12/10.2）。
        """
        with self._lock:
            if self._shutdown or self._lifecycle is not RunLifecycle.WAIT_ACCOUNT:
                return False
            if self._rotator is None or not self._rotate or self._account_switch_inflight:
                return False
            next_alias = self._rotator.next_alias()
            if next_alias is None:  # pragma: no cover - _rotate 已保证 >=2 账号
                return False
            self._account_switch_inflight = True
            self._pending_action = "account_switch"
            self._account_precheck_reason = None
            self._rebuild_snapshot_locked()
        ok = False
        reason = "预检未执行"
        try:
            precheck = self._port_precheck or self._default_port_precheck
            result = precheck()
            ok = bool(result[0])
            reason = str(result[1]) if len(result) > 1 else ""
        except Exception as exc:  # 预检异常不得让服务崩溃，保持等待态
            ok = False
            reason = f"切号预检异常：{exc}"
        if not ok:
            with self._lock:
                self._account_switch_inflight = False
                if self._lifecycle is RunLifecycle.WAIT_ACCOUNT:
                    self._wait_account_failed_locked("precheck_failed", reason or "切号预检未通过", next_alias)
            return False

        # 新一轮 STARTING：重新获取单实例锁（等待期间锁已释放）
        instance_lock = self._make_instance_lock()
        if not instance_lock.acquire():
            holder = getattr(instance_lock, "holder", None)
            with self._lock:
                self._account_switch_inflight = False
                if self._lifecycle is RunLifecycle.WAIT_ACCOUNT:
                    self._wait_account_failed_locked(
                        "lock_held",
                        "已有另一个脚本实例在运行，无法继续轮换",
                        next_alias,
                    )
            logger.warning("轮换确认时单实例锁被占用: %s", holder or {})
            return False

        with self._lock:
            if self._shutdown or self._lifecycle is not RunLifecycle.WAIT_ACCOUNT:
                # 预检期间被 abort_rotation/shutdown 取消：释放刚拿到的锁，不启动
                self._account_switch_inflight = False
                self._release_lock_object(instance_lock)
                return False
            self._instance_lock = instance_lock
            previous_alias = self._current_alias
            self._rotator.advance()
            self._rotation_index = self._rotator.index
            current = self._rotator.current
            self._current_alias = current.alias if current is not None else next_alias
            self._options = self._build_effective_options(self._current_alias)
            self._stop_requested = False
            self._stop_reason = None
            self._error = None
            self._cleanup = _completed_cleanup_view()
            self._last_game_state = None
            self._pending_action = None
            self._account_switch_inflight = False
            self._account_precheck_reason = None
            self._install_log_bridge_locked()
            self._emit_locked(
                RuntimeEventKind.ACCOUNT_SWITCH_CONFIRMED,
                "INFO",
                f"已确认切号：『{previous_alias or '(默认)'}』→『{self._current_alias or '(默认)'}』",
                {
                    "previous_alias": previous_alias,
                    "account_alias": self._current_alias,
                    "rotation_index": self._rotation_index,
                },
            )
            self._set_lifecycle_locked(
                RunLifecycle.STARTING,
                f"正在启动账号『{self._current_alias or '(默认)'}』：加载配置、装配运行时中",
                pending_action=None,
                resume_pending=False,
            )
            thread = threading.Thread(target=self._run_thread, name=RUN_THREAD_NAME, daemon=True)
            self._thread = thread
        thread.start()
        return True

    def abort_rotation(self) -> bool:
        """结束轮换并回到 ``STOPPED``（不启动下一账号、不留下控制线程/锁）。"""
        with self._lock:
            if self._lifecycle is not RunLifecycle.WAIT_ACCOUNT:
                return False
            self._pending_action = None
            self._resume_pending = False
            self._account_switch_inflight = False
            self._account_precheck_reason = None
            self._uninstall_log_bridge_locked()
            self._set_lifecycle_locked(
                RunLifecycle.STOPPED,
                "已结束账号轮换（未启动下一账号）",
                pending_action=None,
                resume_pending=False,
            )
        return True

    # ------------------------------------------------------------------
    # 内部：轮换等待辅助
    # ------------------------------------------------------------------
    def _wait_account_failed_locked(self, code: str, reason_text: str, next_alias: str | None) -> None:
        """预检/取锁失败：保持 WAIT_ACCOUNT，把原因写进生命周期 detail 并发布事件。"""
        self._pending_action = None
        self._account_precheck_reason = str(reason_text)
        self._detail = f"等待手动切换账号：{'继续失败（' + reason_text + '）' if reason_text else ''}"
        self._emit_locked(
            RuntimeEventKind.ACCOUNT_SWITCH_WAITING,
            "WARNING",
            f"切号未继续：{reason_text}",
            {
                "reason": code,
                "detail": str(reason_text),
                "next_alias": next_alias,
                "account_alias": self._current_alias,
                "rotation_index": self._rotation_index,
            },
        )
        self._rebuild_snapshot_locked()

    def _release_lock_object(self, instance_lock: Any) -> None:
        try:
            instance_lock.release()
        except Exception:  # pragma: no cover - 释放失败不阻塞 GUI
            logger.exception("释放单实例锁失败")

    # ------------------------------------------------------------------
    # 命令队列（设计 5.5：GUI → 运行线程的有界命令通道）
    # ------------------------------------------------------------------
    def submit_command(self, command: RunCommand) -> bool:
        """把一条 ``RunCommand`` 放入有界命令队列，由运行线程下一轮处理。

        ``START`` 不允许走命令队列（必须用 :meth:`start` 同步返回预检结果）；
        ``PAUSE/RESUME/STOP/SHUTDOWN`` 也可以直接调用同名方法走快速路径（设计 5.4）。
        """
        if command.kind is RunCommandKind.START:
            return False
        if command.kind is RunCommandKind.REPLACE_TARGET:
            with self._lock:
                snapshot = self._snapshot
                game = snapshot.game if snapshot is not None else None
                active = bool(
                    self._lifecycle is RunLifecycle.RUNNING
                    and game is not None
                    and game.state == "BATTLE_ACTIVE"
                )
                focus_pause = bool(
                    game is not None
                    and game.state == "PAUSED"
                    and self._lifecycle in (
                        RunLifecycle.RUNNING,
                        RunLifecycle.PAUSING,
                        RunLifecycle.PAUSED,
                    )
                    and game.pause_reason in _FOCUS_PAUSE_REASONS
                )
                if self._stop_requested or not (active or focus_pause):
                    logger.warning(
                        "gui_target_queued accepted=False battle=%s frame=%s target=%s reason=%s",
                        command.battle_id,
                        command.frame_id,
                        command.target,
                        "stopped" if self._stop_requested else "lifecycle_or_pause",
                    )
                    return False
        with self._lock:
            if self._lifecycle is RunLifecycle.WAIT_ACCOUNT:
                # 等待切号期间没有运行线程：立即执行，避免命令永不被处理
                self._apply_command(command)
                return True
            if len(self._commands) >= self._command_max:
                dropped = self._commands.popleft()
                logger.warning(
                    "命令队列已满（上限 %d），丢弃最旧命令 %s（原因为保留最新控制请求）",
                    self._command_max,
                    dropped.kind.value,
                )
            self._commands.append(command)
        if command.kind is RunCommandKind.REPLACE_TARGET:
            logger.info(
                "gui_target_queued accepted=True battle=%s frame=%s target=%s",
                command.battle_id,
                command.frame_id,
                command.target,
            )
        return True

    def _drain_commands(self) -> None:
        while True:
            with self._lock:
                if not self._commands:
                    return
                command = self._commands.popleft()
            self._apply_command(command)

    def _apply_command(self, command: RunCommand) -> None:
        kind = command.kind
        if kind is RunCommandKind.PAUSE:
            self.request_pause(command.reason or "gui_pause", command.detail)
        elif kind is RunCommandKind.RESUME:
            self.request_resume()
        elif kind is RunCommandKind.STOP:
            self.request_stop(command.reason or "gui_stop", command.detail)
        elif kind is RunCommandKind.SHUTDOWN:
            self.shutdown()
        elif kind is RunCommandKind.CONFIRM_ACCOUNT_SWITCH:
            self.confirm_account_switch()
        elif kind is RunCommandKind.ABORT_ROTATION:
            self.abort_rotation()
        elif kind is RunCommandKind.REPLACE_TARGET:
            runner = self._runner
            accepted = bool(runner is not None and runner.replace_target(command))
            status = str(getattr(runner, "target_status", "") or "") if runner is not None else ""
            message = status or ("目标已接收" if accepted else "目标未接受：场次已变化或当前不可控制")
            self.publish_event(
                RuntimeEventKind.NAV_FEEDBACK,
                "INFO" if accepted else "WARNING",
                message,
                {
                    "accepted": accepted,
                    "target_status": status,
                    "battle_id": command.battle_id,
                    "frame_id": command.frame_id,
                    "target": command.target,
                    "reason": "" if accepted else status,
                },
            )
            if accepted:
                logger.info("target_received battle=%s frame=%s target=%s", command.battle_id,
                            command.frame_id, command.target)
            else:
                logger.warning("target_rejected battle=%s frame=%s target=%s reason=%s",
                               command.battle_id, command.frame_id, command.target, message)

    def enable_gui_cooperation(self) -> None:
        self._gui_enabled = True

    def read_dashboard(self, account_alias: str | None = None) -> DashboardView:
        """供界面后台线程读取本地记录和闲置采样，不持有运行锁进行文件操作。"""
        import yaml

        from ..common.sysmetrics import create_metrics_source
        from .history import read_history

        with self._lock:
            plan = self._plan
            perf = self._snapshot.perf if self._snapshot is not None else None
            active = self._lifecycle not in (RunLifecycle.STOPPED, RunLifecycle.WAIT_ACCOUNT)
        if plan is not None:
            path = plan.db_path
        else:
            try:
                config = yaml.safe_load((self._root / "config.yaml").read_text(encoding="utf-8")) or {}
                directory = config.get("paths", {}).get("data_dir", "data")
            except (OSError, ValueError, yaml.YAMLError, AttributeError):
                directory = "data"
            path = self._root / directory / "wows_script.db"
        history = read_history(path, account_alias)
        if active and perf is not None:
            return DashboardView(history, perf)
        if self._idle_metrics is None:
            self._idle_metrics = create_metrics_source()
        sample = self._idle_metrics.sample()
        # 闲置采样仅展示系统状态，不连接运行控制器，不执行过载动作。
        perf = PerformanceView(enabled=True, mode="monitor", sampled_at=sample.mono,
            sampled_wall=datetime.fromtimestamp(sample.wall).astimezone().strftime("%H:%M:%S"),
            cpu_pct=sample.cpu_pct, mem_total_mb=sample.mem_total_mb, mem_available_mb=sample.mem_available_mb,
            commit_limit_mb=sample.commit_limit_mb, commit_available_mb=sample.commit_available_mb,
            script_private_mb=sample.script_private_mb, script_cpu_pct=sample.script_cpu_pct,
            unavailable_reason=sample.unavailable_reason, thresholds=None, sample_age_s=0.0)
        return DashboardView(history, perf)

    def _gui_foreground(self) -> bool:
        if not self._gui_enabled:
            return False
        from ..common.window import is_process_foreground

        return is_process_foreground()

    # ------------------------------------------------------------------
    # 事件发布（服务内部 + M1 的 LOG_RECORD 日志桥接）
    # ------------------------------------------------------------------
    def publish_event(
        self,
        kind: RuntimeEventKind,
        level: str,
        message: str,
        detail: dict[str, Any] | None = None,
        *,
        session_id: int | None = None,
        battle_id: int | None = None,
        block: bool = True,
    ) -> bool:
        """发布一条状态级事件（有界队列；满了丢最旧并累计 ``dropped_events``）。

        ``block=False``（日志桥接使用）时：服务锁被其它线程占用则直接丢弃并计数，
        保证 ``logging`` 调用线程永不阻塞。返回是否成功入队。
        """
        acquired = self._lock.acquire() if block else self._lock.acquire(blocking=False)
        if not acquired:
            # 无锁计数器：只为“已丢弃 N 条”展示，允许轻微竞争
            self._dropped_events += 1
            return False
        try:
            self._emit_locked(
                kind,
                level,
                message,
                detail,
                session_id=session_id,
                battle_id=battle_id,
            )
        finally:
            self._lock.release()
        return True

    # ------------------------------------------------------------------
    # 内部：事件/快照/生命周期
    # ------------------------------------------------------------------
    def _emit_locked(
        self,
        kind: RuntimeEventKind,
        level: str,
        message: str,
        detail: dict[str, Any] | None = None,
        *,
        session_id: int | None = None,
        battle_id: int | None = None,
    ) -> None:
        if len(self._events) >= self._event_max:
            self._events.popleft()
            self._dropped_events += 1
        self._event_seq += 1
        self._events.append(
            RuntimeEvent(
                seq=self._event_seq,
                at=self._clock.now(),
                wall_at=clock_iso(self._clock),
                kind=kind,
                level=level,
                message=message,
                session_id=session_id,
                battle_id=battle_id,
                detail=dict(detail or {}),
            )
        )

    def _set_lifecycle_locked(
        self,
        lifecycle: RunLifecycle,
        detail: str,
        *,
        pending_action: Any = _UNSET,
        resume_pending: Any = _UNSET,
    ) -> None:
        self._lifecycle = lifecycle
        self._since_at = self._clock.now()
        self._since_wall = clock_iso(self._clock)
        self._detail = detail
        if pending_action is not _UNSET:
            self._pending_action = pending_action
        if resume_pending is not _UNSET:
            self._resume_pending = bool(resume_pending)
        if lifecycle in (RunLifecycle.PAUSED, RunLifecycle.RUNNING, RunLifecycle.STOPPED):
            self._resume_pending = False
        level = "ERROR" if lifecycle is RunLifecycle.ERROR else "INFO"
        self._emit_locked(
            RuntimeEventKind.LIFECYCLE_CHANGED,
            level,
            f"生命周期：{lifecycle.value}（{detail}）",
            {"lifecycle": lifecycle.value, "detail": detail},
        )
        self._rebuild_snapshot_locked()

    def _rebuild_snapshot_locked(self) -> None:
        now = self._clock.now()
        runner = self._runner
        views: RunnerViews | None = None
        if runner is not None:
            getter = getattr(runner, "snapshot_state", None)
            if callable(getter):
                try:
                    views = getter(now)
                except Exception:
                    logger.exception("生成运行快照失败，沿用上一份视图")
        if views is not None:
            self._views = views
        views = self._views
        if self._rotator is not None and self._start_options is not None and self._start_options.rotate:
            # 轮换模式下由服务统一提供账号轮换上下文（runner 视图不含轮换信息）
            try:
                next_alias = self._rotator.next_alias()
            except Exception:  # pragma: no cover - 轮换器替身异常
                next_alias = None
            views = replace(
                views,
                session=replace(
                    views.session,
                    account_alias=self._current_alias,
                    rotation_index=self._rotation_index,
                    rotation_aliases=self._rotation_aliases,
                    next_alias=next_alias,
                ),
            )
        perf = views.perf
        if perf is None and runner is not None:
            perf_getter = getattr(runner, "performance_view", None)
            if callable(perf_getter):
                try:
                    perf = perf_getter()
                except Exception:  # pragma: no cover - 过载监控替身异常不影响快照
                    logger.exception("读取性能视图失败")
        self._snapshot_seq += 1
        self._snapshot = RunSnapshot(
            seq=self._snapshot_seq,
            at=now,
            wall_at=clock_iso(self._clock),
            lifecycle=LifecycleView(
                lifecycle=self._lifecycle,
                since_at=self._since_at,
                since_wall=self._since_wall,
                detail=self._detail,
                pending_action=self._pending_action,
                resume_pending=self._resume_pending,
            ),
            game=views.game,
            perception=views.perception,
            input=views.input,
            navigation=views.navigation,
            session=views.session,
            plan=self._plan,
            cleanup=self._cleanup,
            error=self._error,
            capabilities=self._capabilities,
            perf=perf,
            dropped_events=self._dropped_events,
            minimap=views.minimap,
            gui_waiting=views.gui_waiting,
            minimap_status=views.minimap_status,
        )
        self._last_publish_at = now
        state = views.game.state
        if self._last_game_state is not None and state != self._last_game_state:
            self._emit_locked(
                RuntimeEventKind.GAME_STATE_CHANGED,
                "INFO",
                f"游戏流程状态：{self._last_game_state} → {state}",
                {"previous": self._last_game_state, "state": state},
            )
        self._last_game_state = state

    def _publish_periodic(self) -> None:
        """按 ``snapshot_min_interval_s`` 节流发布快照；时钟不前进（假时钟）时每轮发布。"""
        with self._lock:
            now = self._clock.now()
            last = self._last_publish_at
            if now > last and (now - last) < self._snapshot_min_interval_s:
                return
            self._rebuild_snapshot_locked()

    # ------------------------------------------------------------------
    # 内部：运行线程
    # ------------------------------------------------------------------
    def _make_instance_lock(self) -> Any:
        data_dir = resolve_data_dir(self._root)
        try:
            return self._lock_factory(data_dir)
        except TypeError:  # pragma: no cover - 自定义锁工厂签名不同
            return SingleInstanceLock(data_dir)

    def _build_default_runner(self, options: StartOptions) -> SessionRunner:
        """默认 runner 工厂：完整校验 → 装配运行时 → 返回可单步执行的 SessionRunner。"""
        app, plan = resolve_start_plan(options, root=self._root)
        app = with_log_level(app, options.log_level)
        runtime = _build_runtime(
            app,
            overload_factory=self._overload_factory,
            overload_monitor=self._overload_monitor,
        )
        return SessionRunner(
            runtime,
            plan=plan,
            max_battles=options.max_battles,
            start_paused=options.start_paused,
            gui_foreground=self._gui_foreground,
        )

    # ------------------------------------------------------------------
    # 内部：账号轮换辅助
    # ------------------------------------------------------------------
    def _rotation_plan(self, plan: EffectivePlan | None) -> EffectivePlan | None:
        """把 EffectivePlan 的轮换上限校正为“当前账号”的真实值（预检里的是首个账号）。

        同时把 ``max_battles_override`` 还原为用户显式输入：服务为执行轮换上限会把
        本账号上限塞进 StartOptions.max_battles，但界面上的“本次显式上限”不应被它污染。
        """
        if plan is None or self._rotator is None or self._start_options is None:
            return plan
        if not self._start_options.rotate:
            return plan
        try:
            limit = self._rotator.current_limit()
        except Exception:  # pragma: no cover - 轮换器替身异常
            return plan
        return replace(
            plan,
            rotation_limit=limit,
            max_battles_override=self._start_options.max_battles,
        )

    def _position_rotator(self, alias: str) -> None:
        """把轮换器当前位置移到指定账号（round_robin 起点；最多绕一圈）。"""
        rotator = self._rotator
        if rotator is None or not alias:
            return
        for _ in range(len(self._rotation_aliases) + 1):
            current = rotator.current
            if current is not None and current.alias == alias:
                return
            rotator.advance()

    def _build_effective_options(self, alias: str | None) -> StartOptions:
        """为指定账号构造本轮生效的 StartOptions（账号覆盖后的场次上限）。"""
        base = self._start_options or self._options
        assert base is not None
        max_battles = base.max_battles
        if (
            max_battles is None
            and self._rotator is not None
            and self._start_options is not None
            and self._start_options.rotate
        ):
            try:
                max_battles = self._rotator.current_limit()
            except Exception:  # pragma: no cover
                max_battles = None
        return replace(base, root=self._root, account_alias=alias, max_battles=max_battles)

    def _should_wait_account(self, stop_reason: str, battles: int) -> bool:
        """会话结束后是否进入 WAIT_ACCOUNT（设计 5.12 / 10.2）。

        条件：轮换模式、>1 个启用账号、非紧急/手动/关闭/异常停止，
        且停止原因是 ``account_rotation`` 或已达到本账号轮换上限。
        """
        if not self._rotate or self._rotator is None:
            return False
        if len(self._rotation_aliases) < 2:
            return False
        if stop_reason in _NO_ROTATE_REASONS:
            return False
        if stop_reason == "account_rotation":
            return True
        limit = self._options.max_battles if self._options is not None else None
        return bool(limit is not None and int(battles) >= int(limit))

    # ------------------------------------------------------------------
    # 内部：日志桥接（LOG_RECORD，设计 4.6 / 12.1）
    # ------------------------------------------------------------------
    def _refresh_log_bridge_locked(self) -> None:
        """重建日志桥接 handler。

        ``runner.py`` 的 ``_build_runtime`` 会调用 ``configure_text_logging``，该函数会
        清空 ``wows_script`` logger 上的全部 handler（含我们的桥接 handler）；因此每次
        装配完运行时都要重装一次，保证文本日志继续镜像到 LOG_RECORD。
        """
        self._uninstall_log_bridge_locked()
        self._install_log_bridge_locked()

    def _resolve_log_bridge_loggers(self) -> list[logging.Logger]:
        """日志桥接目标 logger。

        - 默认同时挂根 logger 与项目文本日志 logger（``wows_script``）：
          项目模块 logger 是 ``src.*``（冒泡到根），而 ``configure_text_logging``
          把 ``wows_script`` 设为 ``propagate=False``，只挂根会漏掉状态机/会话日志；
        - ``log_bridge_logger`` 显式指定时只挂该 logger（测试隔离/自定义日志体系）。
        """
        spec = self._log_bridge_logger
        if spec is None:
            return [logging.getLogger(), logging.getLogger("wows_script")]
        if isinstance(spec, logging.Logger):
            return [spec]
        return [logging.getLogger(str(spec))]

    def _install_log_bridge_locked(self) -> None:
        """安装日志桥接 handler（运行期安装；重复调用幂等）。"""
        if self._log_handlers:
            return
        try:
            for target_logger in self._resolve_log_bridge_loggers():
                handler = _RuntimeLogHandler(self.publish_event, level=self._log_bridge_level)
                handler.attached_logger = target_logger
                target_logger.addHandler(handler)
                self._log_handlers.append(handler)
            self._log_handler = self._log_handlers[0] if self._log_handlers else None
        except Exception:  # pragma: no cover - 桥接失败不影响托管
            self._uninstall_log_bridge_locked()
            logger.exception("安装日志桥接失败（不影响运行）")

    def _uninstall_log_bridge_locked(self) -> None:
        """移除全部日志桥接 handler（STOPPED/ERROR/shutdown 时调用；幂等）。"""
        handlers = self._log_handlers
        self._log_handlers = []
        self._log_handler = None
        for handler in handlers:
            handler.closed = True
            try:
                target_logger = handler.attached_logger
                if target_logger is not None:
                    target_logger.removeHandler(handler)
            except Exception:  # pragma: no cover - 移除失败不影响终态
                logger.exception("移除日志桥接失败")
            finally:
                handler.attached_logger = None

    # ------------------------------------------------------------------
    # 内部：切号真实预检（前台 + 港口；只读，失败不抛异常）
    # ------------------------------------------------------------------
    def _default_port_precheck(self) -> tuple[bool, str]:
        """真实预检：游戏窗口存在且在前台 + ``detect_state()==PORT``（设计 5.12）。

        用现有 ``WindowLocator``/``GdiCapture``/``PerceptionEngine`` 只读检测；
        不启动主循环、不发送输入；任何异常都转成 ``(False, 中文原因)``。
        """
        next_alias = None
        if self._rotator is not None:
            try:
                next_alias = self._rotator.next_alias()
            except Exception:  # pragma: no cover
                next_alias = None
        if self._start_options is None and self._options is None:
            return False, "服务尚未启动过会话，无法执行切号预检"
        options = self._build_effective_options(next_alias)
        try:
            app = load_app_for_account(
                self._root,
                options.profile_id or DEFAULT_PROFILE_ID,
                options.ship_id,
                next_alias,
            )
        except ConfigError as exc:
            first = exc.errors[0].message if exc.errors else str(exc)
            return False, f"下一账号配置校验失败：{first}"
        except Exception as exc:
            return False, f"加载下一账号配置失败：{exc}"
        clock = SystemClock()
        capture = None
        engine = None
        try:
            locator = WindowLocator(app.profile.window.process_names, app.profile.window.title_patterns)
            info = locator.find()
            if info is None:
                return False, "找不到游戏窗口，请先回到游戏客户端"
            if not info.foreground:
                locator.bring_to_foreground(info.hwnd)
                time.sleep(0.3)
                info = locator.find()
                if info is None or not info.foreground:
                    return False, "游戏窗口不在前台，请手动点回游戏后重试"
            capture = GdiCapture(
                locator,
                clock=clock,
                require_foreground=app.profile.window.require_foreground,
                capture_budget_ms=app.global_config.timeouts.capture_ms,
                capture_method=app.global_config.perception.capture_method,
            )
            engine = PerceptionEngine(app, capture, clock)
            state = engine.detect_state(now=clock.now())
        except Exception as exc:
            return False, f"切号预检失败：{exc}"
        finally:
            try:
                if engine is not None:
                    engine.close()
                elif capture is not None:
                    capture.close()
            except Exception:  # pragma: no cover - 关闭只读资源失败不影响结论
                pass
        if state is not State.PORT:
            state_text = getattr(state, "value", state) or "未知"
            return False, f"未识别到港口界面（当前识别：{state_text}）"
        return True, "游戏在前台且已识别到港口界面"

    def _run_thread(self) -> None:
        runner: Any | None = None
        try:
            options = self._options
            assert options is not None
            runner = self._runner_factory(options)
            with self._lock:
                self._runner = runner
                self._plan = self._rotation_plan(getattr(runner, "plan", None))
                # 运行时装配调用 configure_text_logging 会清空 wows_script handler：重装桥接
                self._refresh_log_bridge_locked()
            if self._stop_requested:
                self._cleanup_and_finish(runner, stop_reason=self._stop_reason or "cancelled")
                return
            runner.start()
            with self._lock:
                self._plan = self._rotation_plan(getattr(runner, "plan", self._plan))
                self._set_lifecycle_locked(
                    RunLifecycle.RUNNING,
                    "运行中",
                    pending_action=None,
                    resume_pending=False,
                )
            self.publish_event(
                RuntimeEventKind.SESSION_STARTED,
                "INFO",
                "会话已启动",
                {
                    "session_id": getattr(runner, "session_id", None),
                    "account_alias": getattr(getattr(runner, "plan", None), "account_alias", None),
                },
            )
            if options.start_paused:
                self.request_pause("gui_start_paused", "启动后先暂停")
            self._loop(runner)
            self._cleanup_and_finish(runner, stop_reason=self._runner_stop_reason(runner))
        except ConfigError as exc:
            issues = issue_views(exc.issues)
            self.publish_event(
                RuntimeEventKind.VALIDATION_FAILED,
                "ERROR",
                f"启动校验失败：{len(exc.errors)} 个错误",
                {"issues": [_issue_to_dict(item) for item in issues]},
            )
            self._set_recoverable_error("validation_failed", "启动校验失败", str(exc))
            self._cleanup_and_finish(runner, stop_reason="validation_failed")
        except Exception as exc:
            logger.exception("运行线程异常: %s", exc)
            if runner is not None:
                handler = getattr(runner, "handle_exception", None)
                if callable(handler):
                    try:
                        handler(exc)
                    except Exception:  # pragma: no cover - 收尾失败不覆盖原始异常
                        logger.exception("运行线程异常收尾失败")
            self.publish_event(
                RuntimeEventKind.ERROR,
                "CRITICAL",
                f"运行异常：{exc}",
                {"source": "runtime_thread", "error": repr(exc)},
            )
            self._set_recoverable_error("runtime_error", "会话运行异常", str(exc))
            self._cleanup_and_finish(runner, stop_reason="runtime_error")

    def _loop(self, runner: Any) -> None:
        """运行线程主循环：单步推进 + 状态观察 + 快照发布。"""
        while True:
            if bool(getattr(runner, "finished", False)):
                return
            started = time.monotonic()
            runner.step()
            self._drain_commands()
            self._observe_runner(runner)
            self._publish_periodic()
            if bool(getattr(runner, "finished", False)):
                return
            if (time.monotonic() - started) < 0.001:
                # 只对“单步零耗时”的假实现节流，避免测试替身空转占满 CPU；
                # 真实 SessionRunner.step() 自带 loop_hz 节拍休眠，不会走到这里。
                time.sleep(0.001)

    def _observe_runner(self, runner: Any) -> None:
        """把运行期状态（暂停确认/恢复/停止请求）同步到生命周期。"""
        with self._lock:
            lifecycle = self._lifecycle
            if lifecycle not in (
                RunLifecycle.RUNNING,
                RunLifecycle.PAUSING,
                RunLifecycle.PAUSED,
            ):
                return
            paused = getattr(runner, "paused", None)
            if paused is None:
                return
            confirmed = bool(getattr(runner, "release_confirmed", False))
            reason = str(getattr(runner, "pause_reason", "") or "unknown")
            if lifecycle is RunLifecycle.RUNNING and paused:
                if not confirmed:
                    # 非 GUI 触发的暂停（失焦/心跳/校验失败）：先进入 PAUSING 等释放确认
                    self._set_lifecycle_locked(
                        RunLifecycle.PAUSING,
                        f"暂停中：{reason}（等待输入释放确认）",
                        pending_action="pause",
                        resume_pending=False,
                    )
                    return
                self._set_lifecycle_locked(
                    RunLifecycle.PAUSED,
                    f"已暂停：{reason}",
                    pending_action=None,
                    resume_pending=False,
                )
                self._emit_locked(
                    RuntimeEventKind.PAUSE_CONFIRMED,
                    "WARNING",
                    f"已暂停（原因：{reason}）",
                    {
                        "reason": reason,
                        "release_count": int(getattr(runner, "release_count", 0) or 0),
                        "release_confirmed": confirmed,
                    },
                )
                if reason == "input_failed":
                    self._emit_locked(
                        RuntimeEventKind.INPUT_FAILED,
                        "ERROR",
                        "输入后端连续失败，已暂停；恢复前请排查驱动/权限",
                        {"reason": reason, "release_confirmed": confirmed},
                    )
            elif lifecycle is RunLifecycle.PAUSING and paused is False:
                # 暂停确认前被 F11/状态机自行恢复：回到 RUNNING，避免卡在 PAUSING
                self._set_lifecycle_locked(
                    RunLifecycle.RUNNING,
                    "运行中（暂停请求已被恢复）",
                    pending_action=None,
                    resume_pending=False,
                )
                self._emit_locked(
                    RuntimeEventKind.RESUME_CONFIRMED,
                    "INFO",
                    "已恢复运行（暂停请求被取消）",
                    {},
                )
            elif lifecycle is RunLifecycle.PAUSING and paused and confirmed:
                self._set_lifecycle_locked(
                    RunLifecycle.PAUSED,
                    f"已暂停：{reason}",
                    pending_action=None,
                    resume_pending=False,
                )
                self._emit_locked(
                    RuntimeEventKind.PAUSE_CONFIRMED,
                    "WARNING",
                    f"已暂停（输入已释放，release #{int(getattr(runner, 'release_count', 0) or 0)}）",
                    {
                        "reason": reason,
                        "release_count": int(getattr(runner, "release_count", 0) or 0),
                        "release_confirmed": True,
                    },
                )
            elif lifecycle is RunLifecycle.PAUSED and paused:
                # 恢复请求因前台校验失败而未生效：保持 PAUSED、清 resume_pending 并说明原因
                if self._resume_pending and bool(getattr(runner, "resume_failed", False)):
                    detail = str(getattr(runner, "resume_failed_detail", "") or "未能切回游戏前台")
                    self._resume_pending = False
                    self._pending_action = None
                    self._emit_locked(
                        RuntimeEventKind.ERROR,
                        "WARNING",
                        f"恢复失败：{detail}",
                        {"reason": "resume_no_foreground", "detail": detail},
                    )
                    self._rebuild_snapshot_locked()
            elif lifecycle is RunLifecycle.PAUSED and not paused:
                self._set_lifecycle_locked(
                    RunLifecycle.RUNNING,
                    "运行中（已恢复）",
                    pending_action=None,
                    resume_pending=False,
                )
                self._emit_locked(
                    RuntimeEventKind.RESUME_CONFIRMED,
                    "INFO",
                    "已恢复运行",
                    {},
                )

    @staticmethod
    def _runner_stop_reason(runner: Any) -> str:
        reason = getattr(runner, "stop_reason", None)
        if callable(reason):  # pragma: no cover - 防御性
            try:
                reason = reason()
            except Exception:
                reason = None
        return str(reason or "unknown")

    def _set_recoverable_error(self, code: str, message: str, detail: str) -> None:
        with self._lock:
            self._error = ErrorView(
                code=code,
                message=message,
                source="src.runtime.service",
                detail=detail,
                at_wall=clock_iso(self._clock),
                recoverable=True,
            )
            self._rebuild_snapshot_locked()

    # ------------------------------------------------------------------
    # 内部：统一清理与终态
    # ------------------------------------------------------------------
    def _release_instance_lock(self) -> bool:
        lock = self._instance_lock
        self._instance_lock = None
        if lock is None:
            return True
        try:
            lock.release()
            return True
        except Exception:
            logger.exception("释放单实例锁失败")
            return False

    def _empty_cleanup_report(self) -> CleanupView:
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

    def _cleanup_and_finish(self, runner: Any | None, *, stop_reason: str) -> None:
        """统一清理（正常/异常/取消启动共用），按清理结果进入 STOPPED 或 ERROR。"""
        with self._lock:
            if self._lifecycle not in (RunLifecycle.STOPPING, RunLifecycle.STOPPED, RunLifecycle.ERROR):
                self._set_lifecycle_locked(
                    RunLifecycle.STOPPING,
                    f"停止中：{stop_reason}",
                    pending_action="stop",
                    resume_pending=False,
                )
            self._stop_reason = stop_reason
        self.publish_event(
            RuntimeEventKind.SESSION_ENDED,
            "INFO",
            f"会话结束：原因={stop_reason}",
            {"reason": stop_reason},
        )
        if runner is not None:
            cleanup = getattr(runner, "cleanup", None)
            if callable(cleanup):
                try:
                    report = cleanup()
                except Exception:
                    logger.exception("统一清理异常")
                    report = self._empty_cleanup_report()
                    report = replace(
                        report,
                        pending_items=("清理过程异常",),
                        completed=False,
                    )
            else:
                report = self._empty_cleanup_report()
        else:
            report = self._empty_cleanup_report()

        lock_released = self._release_instance_lock()
        pending = tuple(report.pending_items)
        if not lock_released:
            pending = pending + ("单实例锁未释放",)
        completed = bool(report.completed) and not pending
        final_cleanup = replace(
            report,
            requested=True,
            single_instance_released=lock_released,
            pending_items=pending,
            completed=completed,
        )
        for name in (
            "input_released",
            "input_thread_joined",
            "watchdog_stopped",
            "event_sink_stopped",
            "screenshots_cleaned",
            "capture_closed",
            "single_instance_released",
        ):
            self.publish_event(
                RuntimeEventKind.CLEANUP_STEP,
                "INFO" if getattr(final_cleanup, name) else "ERROR",
                f"清理项 {name}：{'完成' if getattr(final_cleanup, name) else '未完成'}",
                {"item": name, "ok": bool(getattr(final_cleanup, name))},
            )
        with self._lock:
            # 轮换计数：每完成一次会话尝试记一场（与 CLI `_run_accounts` 的 rotator.record_battle 一致）
            if self._rotate and self._rotator is not None and runner is not None:
                try:
                    self._rotator.record_battle(self._current_alias)
                except Exception:  # pragma: no cover - 轮换器替身异常
                    logger.exception("记录轮换场次失败")
            battles = 0
            if runner is not None:
                try:
                    battles = int(getattr(runner, "battles_completed", 0) or 0)
                except Exception:  # pragma: no cover
                    battles = 0
            wait_account = bool(completed and self._should_wait_account(stop_reason, battles))
            self._cleanup = final_cleanup
            self._runner = None
            self._thread = None
            if not completed:
                self._uninstall_log_bridge_locked()
                self._error = ErrorView(
                    code="cleanup_incomplete",
                    message="清理未完成，禁止启动",
                    source="src.runtime.service",
                    detail="、".join(pending),
                    at_wall=clock_iso(self._clock),
                    recoverable=False,
                )
                self._set_lifecycle_locked(
                    RunLifecycle.ERROR,
                    "清理未完成（未确认安全），禁止启动；仅允许诊断与退出",
                    pending_action=None,
                    resume_pending=False,
                )
            elif wait_account and self._rotator is not None:
                next_alias = self._rotator.next_alias()
                detail = (
                    f"等待手动切换账号：当前『{self._current_alias or '(默认)'}』本次已完成 "
                    f"{battles} 场，下一步切换到『{next_alias or '(无)'}』；"
                    "请在游戏客户端手动切号并回到港口后点“确认已切号”"
                )
                self._set_lifecycle_locked(
                    RunLifecycle.WAIT_ACCOUNT,
                    detail,
                    pending_action="account_switch",
                    resume_pending=False,
                )
                self._emit_locked(
                    RuntimeEventKind.ACCOUNT_SWITCH_WAITING,
                    "WARNING",
                    detail,
                    {
                        "account_alias": self._current_alias,
                        "next_alias": next_alias,
                        "battles_completed": battles,
                        "rotation_index": self._rotation_index,
                        "rotation_aliases": list(self._rotation_aliases),
                        "reason": stop_reason,
                    },
                )
                self._rebuild_snapshot_locked()
            else:
                self._uninstall_log_bridge_locked()
                self._set_lifecycle_locked(
                    RunLifecycle.STOPPED,
                    f"已停止：会话结束（{stop_reason}）",
                    pending_action=None,
                    resume_pending=False,
                )
        if not completed:
            self.publish_event(
                RuntimeEventKind.ERROR,
                "CRITICAL",
                "清理未完成：停止确认失败，禁止再次启动",
                {"pending_items": list(pending), "reason": stop_reason},
            )


def _issue_to_dict(issue: ValidationIssueView) -> dict[str, Any]:
    return {
        "severity": issue.severity,
        "code": issue.code,
        "location": issue.location,
        "message": issue.message,
    }


__all__ = [
    "COMMAND_QUEUE_MAX",
    "EVENT_QUEUE_MAX",
    "LOG_BRIDGE_FORMAT",
    "LOG_BRIDGE_LEVEL",
    "PortPrecheck",
    "RUN_THREAD_NAME",
    "SNAPSHOT_MIN_INTERVAL_S",
    "RuntimeService",
]
