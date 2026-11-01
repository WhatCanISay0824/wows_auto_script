"""运行时会话执行器（GUI 设计文档 3.2 / 13.1，M0 从 ``src/main.py`` 迁入）。

本模块承接原 ``src/main.py`` 的运行时装配与主循环：

- :class:`Runtime` / :func:`_build_runtime` / :func:`_teardown` / :func:`_run_session` /
  :class:`_EventBridge` / ``_ensure_game_foreground`` / ``_sync_safety_controller`` /
  ``_reset_input_after_failure`` / ``_cleanup_partial_runtime``：逐字迁移，行为/日志/异常路径一致；
- :class:`SessionRunner`：把主循环拆成可单步推进的 ``start()/step()``，供
  :class:`~src.runtime.service.RuntimeService` 的运行线程每轮调用；同时暴露暂停/恢复/停止的
  非阻塞控制通道（写共享 ``StopController`` + 关闭输入门禁并释放，供 GUI 线程调用）；
- :func:`cleanup_runtime`：统一清理与清理清单（``CleanupView``），正常/异常路径共用（ISS-008）；
- 过载监控注入点：``_build_runtime(app, overload_factory=..., overload_monitor=...)``，
  M0 不构造过载对象；有注入时挂到 ``SafetyWatchdog``（具体接口由过载任务/Lead 接线）。

``src/main.py`` 通过导入别名保留同名函数，旧测试的 monkeypatch 行为不变。
"""

from __future__ import annotations

import inspect
import logging
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from ..behavior.scheduler import BehaviorScheduler
from ..common.clock import Clock, SystemClock, clock_iso
from ..common.types import Observation, Rect, State, WindowInfo
from ..common.window import WindowLocator, set_dpi_awareness
from ..config.models import AppConfig
from ..execution.queue import InputExecutor
from ..execution.safety import (
    SafetyEventKind,
    SafetyMonitor,
    SafetyWatchdog,
    StopController,
    Win32KeyStateSource,
)
from ..execution.winput import make_backend
from ..navigation.autopilot import AutoPilotNavigator
from ..navigation.navigator import Navigator
from ..perception.capture import GdiCapture
from ..perception.engine import PerceptionEngine
from ..state_machine.machine import MachineDeps, StateMachine
from ..state_machine.session import SessionController
from ..state_machine.ship_check import ShipChecker
from ..state_machine.steps import StepResult, StepSequenceExecutor
from ..storage.event_log import EventSink, configure_text_logging
from ..storage.screenshots import ScreenshotManager
from .messages import (
    CleanupView,
    EffectivePlan,
    GameView,
    InputView,
    MinimapView,
    NavigationView,
    PerceptionView,
    PerformanceView,
    RunCommand,
    SessionView,
)

logger = logging.getLogger(__name__)

#: 仅这些暂停原因允许 GUI 在战斗内提交显式目标；人工/异常暂停不在内。
_FOCUS_PAUSE_REASONS = frozenset({"window_lost", "window_unfocused", "window_unavailable"})
#: 切出游戏后等待前台稳定的最长时间（期间不发送任何输入）。
_FOCUS_GRACE_S = 0.5


# ---------------------------------------------------------------------------
# 运行时装配（迁移自 src/main.py）
# ---------------------------------------------------------------------------


@dataclass
class Runtime:
    """一次会话需要的全部对象。"""

    app: AppConfig
    clock: Clock
    window_locator: WindowLocator
    capture: GdiCapture
    engine: PerceptionEngine
    events: EventSink
    screenshots: ScreenshotManager
    executor: InputExecutor
    steps: StepSequenceExecutor
    navigator: Navigator
    behavior: BehaviorScheduler
    session: SessionController
    ship_checker: ShipChecker
    machine: StateMachine
    safety: SafetyMonitor
    watchdog: SafetyWatchdog
    controller: StopController
    bridge: Any
    logger: logging.Logger
    # M0 新增（均有默认值，保持既有构造/测试兼容）：
    autopilot: Any | None = None  # AutoPilotNavigator（快照导航视图使用）
    overload_monitor: Any | None = None  # 过载监控注入对象（Lead/过载任务接线）


class _EventBridge:
    """把导航/行为/步骤回调桥接到日志线程，并维护当前战斗编号。"""

    def __init__(self, events: EventSink) -> None:
        self.events = events
        self.battle_id: int | None = None

    def set_battle_id(self, battle_id: int | None) -> None:
        """ISS-010：状态机创建/结束/中断战斗时更新场次上下文。"""
        self.battle_id = battle_id

    def _log(self, action_type: str, detail: Any) -> None:
        payload = dict(detail) if isinstance(detail, dict) else {"detail": detail}
        try:
            self.events.log_action(self.battle_id, action_type, payload)
        except Exception:  # 日志失败不阻塞运行
            pass

    def on_nav(self, event: str, detail: Any = None, **extra: Any) -> None:
        payload: dict[str, Any] = dict(detail) if isinstance(detail, dict) else {}
        payload.update(extra)
        self._log(event, payload)

    def on_behavior(self, event: str, detail: Any = None, **extra: Any) -> None:
        self.on_nav(event, detail, **extra)

    def on_step(self, result: StepResult) -> None:
        self._log("exit_step", result.to_dict())

    def on_session(self, event: str, detail: Any = None) -> None:
        payload: dict[str, Any] = dict(detail) if isinstance(detail, dict) else {"detail": detail}
        self._log(f"session_{event}", payload)

    def to_screen(self, wx: int, wy: int) -> tuple[int, int]:
        # 步骤执行器的客户区像素 -> 屏幕像素；由主循环更新窗口矩形
        rect = self._window_rect
        return rect.x + int(wx), rect.y + int(wy)

    _window_rect: Rect = Rect(0, 0, 0, 0)

    def update_window_rect(self, rect: Rect) -> None:
        self._window_rect = rect


def _make_safety_event_handler(executor: InputExecutor, logger_: logging.Logger):
    """ISS-005：安全事件的独立输入门禁回调（在监控线程内执行，不等待主循环）。

    F12 紧急停止与心跳超时都必须在监控线程内立即关闭业务输入门禁并请求输入线程释放，
    否则主循环卡在截屏/识别/数据库等待时仍可能继续执行排队的键鼠动作。
    该回调只入队释放动作（非阻塞），状态机转移与日志收尾仍由运行线程负责。
    """

    def _on_safety_event(event: Any) -> None:
        if event.kind not in (
            SafetyEventKind.EMERGENCY_STOP,
            SafetyEventKind.HEARTBEAT_TIMEOUT,
            SafetyEventKind.OVERLOAD_PAUSE_REQUESTED,
            SafetyEventKind.OVERLOAD_STOP_REQUESTED,
        ):
            return
        try:
            executor.close_gate_and_release(f"safety:{event.kind.value}")
        except Exception as exc:
            logger_.warning("安全事件关闭输入门禁失败: %s", exc)
        logger_.warning("安全事件 %s：已关闭输入门禁并请求释放键鼠", event.kind.value)

    return _on_safety_event


def _cleanup_partial_runtime(started: list[tuple[str, Any]], logger_: logging.Logger) -> None:
    """ISS-008：运行时装配中途失败时回收已创建的资源（幂等、不抛异常）。"""
    for kind, resource in reversed(started):
        try:
            if kind in ("executor", "events"):
                resource.stop(drain=True)
            elif kind == "engine":
                resource.close()
        except Exception as exc:  # 单个资源回收失败不影响其它资源
            logger_.warning("装配失败回收 %s 时出错: %s", kind, exc)


def _overload_attach_target(watchdog: Any) -> Any:
    """返回可挂接过载监控的看门狗对象（未来接口变化的单点适配）。"""
    return watchdog


def attach_overload_monitor(watchdog: Any, monitor: Any) -> bool:
    """把过载监控挂到看门狗上（M0 注入点；接口未落地时返回 False 并只记一次警告）。

    过载任务（src/execution/overload.py，由 Lead/性能任务接线）落地后，只需让
    ``SafetyWatchdog`` 提供 ``attach_overload_monitor(monitor)``（或 ``set_overload_monitor``）
    即可；服务/runner 侧不再改动。
    """
    target = _overload_attach_target(watchdog)
    for name in ("attach_overload_monitor", "set_overload_monitor", "add_overload_monitor"):
        hook = getattr(target, name, None)
        if callable(hook):
            hook(monitor)
            return True
    poller = getattr(target, "add_poller", None)
    if callable(poller):
        try:
            poller(monitor)
            return True
        except TypeError:  # pragma: no cover - 未来接口签名差异
            pass
    logger.info("过载监控已注入但看门狗暂未提供挂接接口（等待过载任务接线）")
    return False


def _build_runtime(
    app: AppConfig,
    *,
    overload_factory: Callable[..., Any] | None = None,
    overload_monitor: Any | None = None,
) -> Runtime:
    config = app.global_config
    clock: Clock = SystemClock()
    text_logger = configure_text_logging(
        app.project_root / config.paths.log_dir,
        level=config.runtime.log_level,
        max_bytes=config.logging.log_max_bytes,
        backup_count=config.logging.log_backup_count,
    )
    started: list[tuple[str, Any]] = []
    try:
        dpi_mode = set_dpi_awareness()
        text_logger.info("DPI 感知模式: %s（当前不是游戏运行环境属正常）", dpi_mode)

        locator = WindowLocator(app.profile.window.process_names, app.profile.window.title_patterns)
        capture = GdiCapture(
            locator,
            clock=clock,
            require_foreground=app.profile.window.require_foreground,
            capture_budget_ms=config.timeouts.capture_ms,
            capture_method=config.perception.capture_method,
        )
        engine = PerceptionEngine(app, capture, clock)
        started.append(("engine", engine))
        # 每船独立血条模板：换船后血条面板上的舰名不同，必须用当前船的模板，否则会误判“血条消失”
        engine.configure_ship_hp_bar(app.ship)
        events = EventSink(
            app.project_root / config.paths.data_dir / "wows_script.db",
            logger=text_logger,
            fallback_log=app.project_root / config.logging.db_fallback_log,
        )
        events.start()
        started.append(("events", events))
        screenshots = ScreenshotManager.from_config(app.project_root, config, clock=clock, logger=text_logger)
        def can_execute() -> bool:
            info = _LAST_WINDOW[0]
            return info is not None and locator.is_foreground(info.hwnd)

        executor = InputExecutor(make_backend(config.input), config.input, clock=clock, can_execute=can_execute)
        executor.start()
        started.append(("executor", executor))

        bridge = _EventBridge(events)
        navigator = Navigator(config.navigation, on_event=bridge.on_nav)

        autopilot = AutoPilotNavigator(
            config.navigation,
            app.profile,
            rng=random.Random(),
            on_event=bridge.on_nav,
            logger_=text_logger,
            keymap=app.keymap,
            minimap_provider=_make_minimap_provider(engine, clock),
        )
        behavior = BehaviorScheduler(config.behavior, app.keymap, on_event=bridge.on_behavior)
        steps = StepSequenceExecutor(app.keymap, to_screen=bridge.to_screen, on_step=bridge.on_step)
        alias = app.account_alias

        def _count_today(day: date) -> int | None:
            try:
                parameters = inspect.signature(events.count_battles_on).parameters
            except (TypeError, ValueError):  # pragma: no cover
                parameters = {}
            if alias and "account_alias" in parameters:
                return events.count_battles_on(day, account_alias=alias)  # type: ignore[call-arg]
            return events.count_battles_on(day)

        session = SessionController(
            config.session,
            config.safety,
            clock,
            count_battles_on=_count_today,
            rng=random.Random(),
            on_event=bridge.on_session,
        )
        ship_checker = ShipChecker(app, engine, steps, logger_=text_logger)
        # ISS-006：安全监控与状态机共用同一个 StopController，F11/暂停状态保持同步
        controller = StopController()
        safety = SafetyMonitor.from_config(
            config.safety, Win32KeyStateSource(), controller=controller, clock=clock
        )

        # ISS-005：安全事件在监控线程内直接关闭输入门禁并请求释放，不再等待主循环同步
        _on_safety_event = _make_safety_event_handler(executor, text_logger)
        watchdog = SafetyWatchdog(safety, on_event=_on_safety_event)
        machine = StateMachine(
            MachineDeps(
                config=app,
                clock=clock,
                perception=engine,
                executor=executor,
                events=events,
                steps=steps,
                navigator=navigator,
                behavior=behavior,
                session=session,
                screenshots=screenshots,
                ship_checker=ship_checker,
                autopilot=autopilot,
                controller=controller,
                on_battle_context=bridge.set_battle_id,
                window_info_provider=None,
                rng=random.Random(),
                logger_=text_logger,
            )
        )

        # 过载监控注入点：有 monitor 直接挂接；只有 factory 时先构造。
        # 未显式注入时使用内置工厂（`overload.mode: off` 时构造后仍不动作），
        # 保证 CLI 与 GUI 默认都有性能面板与过载保护；导入失败只记录警告不阻塞运行。
        monitor_obj = overload_monitor
        factory = overload_factory
        if monitor_obj is None and factory is None:
            try:
                from ..execution.overload import build_overload_monitor as factory  # noqa: PLC0415
            except Exception as exc:  # pragma: no cover - 非 Windows/依赖缺失
                logger.warning("过载监控不可用（导入失败）: %s", exc)
                factory = None
        if monitor_obj is None and factory is not None:
            try:
                monitor_obj = factory(
                    safety=safety,
                    controller=controller,
                    clock=clock,
                    config=config,
                    app=app,
                )
            except Exception as exc:  # pragma: no cover - 配置/接口异常不阻塞托管
                logger.warning("过载监控构造失败（继续无性能面板运行）: %s", exc)
                monitor_obj = None
        if monitor_obj is not None:
            attach_overload_monitor(watchdog, monitor_obj)

        return Runtime(
            app=app,
            clock=clock,
            window_locator=locator,
            capture=capture,
            engine=engine,
            events=events,
            screenshots=screenshots,
            executor=executor,
            steps=steps,
            navigator=navigator,
            behavior=behavior,
            session=session,
            ship_checker=ship_checker,
            machine=machine,
            safety=safety,
            watchdog=watchdog,
            controller=controller,
            bridge=bridge,
            logger=text_logger,
            autopilot=autopilot,
            overload_monitor=monitor_obj,
        )
    except BaseException:
        # ISS-008：装配失败也要回收已创建的输入线程/日志线程/截屏资源
        _cleanup_partial_runtime(started, text_logger)
        raise


_LAST_WINDOW: list[WindowInfo | None] = [None]


def reset_window_cache() -> None:
    """清空窗口缓存（每次会话开始前调用；原 ``main._run_accounts`` 的 ``_LAST_WINDOW[0]=None``）。"""
    _LAST_WINDOW[0] = None


def _teardown(runtime: Runtime) -> None:
    """旧版清理入口（CLI 账号循环使用）；行为与迁移前一致、不抛异常。

    ``RuntimeService`` 使用 :func:`cleanup_runtime` 以取得清理清单；两者共用同一清理实现，
    区别只在于后者返回 ``CleanupView`` 并做逐项确认。
    """
    cleanup_runtime(runtime)


# ---------------------------------------------------------------------------
# 统一清理（CleanupView 清单，GUI 设计文档 5.9 / 10.8）
# ---------------------------------------------------------------------------


def _release_count(executor: Any) -> int:
    try:
        return int(getattr(executor, "release_count", 0))
    except Exception:  # pragma: no cover - 第三方替身
        return 0


def cleanup_runtime(runtime: Runtime) -> CleanupView:
    """统一清理并返回清理清单（幂等、不抛异常）。

    清单与 ``CleanupView`` 字段一一对应；``single_instance_released`` 由
    :class:`~src.runtime.service.RuntimeService` 在释放控制锁后回填。
    关键项（输入释放/输入线程退出/日志线程退出）未完成时 ``completed=False``，
    由服务判定进入 ``ERROR``（设计 5.9）。
    """
    logger_ = runtime.logger
    pending: list[str] = []

    # 1~3. 输入门禁/释放/输入线程：executor.stop(drain=True) 契约是返回时键鼠已释放并 join。
    executor = getattr(runtime, "executor", None)
    input_released = False
    input_thread_joined = False
    if executor is None:
        input_released = input_thread_joined = True
    else:
        try:
            if getattr(executor, "is_running", False):
                executor.stop(drain=True)
            input_thread_joined = not bool(getattr(executor, "is_running", False))
            input_released = input_thread_joined
        except Exception as exc:
            logger_.warning("停止输入线程失败: %s", exc)
        if not input_thread_joined:
            pending.append("输入线程未退出")
            input_released = False

    # 4. 看门狗
    watchdog_stopped = False
    try:
        watchdog = getattr(runtime, "watchdog", None)
        if watchdog is not None:
            watchdog.stop()
            watchdog_stopped = not bool(getattr(watchdog, "is_running", False))
        else:
            watchdog_stopped = True
    except Exception as exc:
        logger_.warning("停止安全监控线程失败: %s", exc)
    if not watchdog_stopped:
        pending.append("安全监控线程未停止")

    # 5~6. 截图清理
    screenshots_cleaned = False
    try:
        screenshots = getattr(runtime, "screenshots", None)
        if screenshots is None:
            screenshots_cleaned = True
        else:
            had_session = getattr(screenshots, "session_dir", None) is not None
            result = bool(screenshots.cleanup_session())
            screenshots_cleaned = result or not had_session
    except Exception as exc:
        logger_.warning("清理本场截图失败: %s", exc)
    if not screenshots_cleaned:
        pending.append("本场截图未清理")

    # 7. 日志线程（SQLite 写入收尾）
    event_sink_stopped = False
    try:
        events = getattr(runtime, "events", None)
        if events is None:
            event_sink_stopped = True
        else:
            events.stop(drain=True)
            event_sink_stopped = not bool(getattr(events, "is_running", False))
    except Exception as exc:
        logger_.warning("停止日志线程失败: %s", exc)
    if not event_sink_stopped:
        pending.append("日志线程未退出")

    # 8. 截屏资源
    capture_closed = False
    try:
        engine = getattr(runtime, "engine", None)
        if engine is not None:
            engine.close()
        capture_closed = True
    except Exception as exc:
        logger_.warning("关闭截屏资源失败: %s", exc)
    if not capture_closed:
        pending.append("截屏资源未关闭")

    return CleanupView(
        requested=True,
        input_released=input_released,
        input_thread_joined=input_thread_joined,
        watchdog_stopped=watchdog_stopped,
        event_sink_stopped=event_sink_stopped,
        screenshots_cleaned=screenshots_cleaned,
        capture_closed=capture_closed,
        single_instance_released=False,  # 由 RuntimeService 回填
        pending_items=tuple(pending),
        completed=not pending,
    )


# ---------------------------------------------------------------------------
# 主循环辅助（迁移自 src/main.py）
# ---------------------------------------------------------------------------


def _sync_safety_controller(machine: Any, controller: StopController) -> bool:
    """把安全监控器的停止/暂停请求同步给状态机，返回当前是否处于暂停状态。

    安全监控（F12 紧急停止、F11 暂停/恢复、心跳超时）只产生 StopController 请求，
    由本函数在主循环里转成状态机动作（避免在监控线程里直接操作状态机）。
    """
    stop_request = controller.stop_request
    if stop_request is not None and machine.state.value != "STOPPING":
        machine.request_stop(str(stop_request.reason), stop_request.detail)
    pause_request = controller.pause_request
    if pause_request is not None:
        machine.request_pause(str(pause_request.reason), pause_request.detail)
    return controller.paused


def _reset_input_after_failure(runtime: Runtime) -> bool:
    """ISS-009：输入后端连续失败后，恢复前必须显式重置失败状态并重新打开门禁。"""
    executor = runtime.executor
    reset = getattr(executor, "reset_after_failure", None)
    if not callable(reset):
        return True
    try:
        ok = bool(reset())
    except Exception as exc:
        runtime.logger.warning("重置输入后端失败: %s", exc)
        return False
    if ok:
        runtime.logger.warning("输入后端失败状态已重置（门禁重新打开），继续会话")
    return ok


def _ensure_game_foreground(runtime: Runtime) -> bool:
    """启动、显式恢复或提交目标时尝试切回游戏，失败不循环抢焦点。"""
    try:
        info = runtime.window_locator.find()
        if info is None:
            return False
        if info.foreground:
            return True
        if not runtime.window_locator.bring_to_foreground(info.hwnd):
            return False
        time.sleep(0.3)
        info = runtime.window_locator.find()
        focused = bool(info is not None and info.foreground)
        if focused:
            runtime.logger.info("已把游戏窗口切换到前台")
        return focused
    except Exception as exc:  # 非 Windows/窗口消失等都不应阻塞启动
        runtime.logger.debug("切换游戏窗口到前台失败: %s", exc)
        return False


# ---------------------------------------------------------------------------
# 快照视图（RuntimeService 使用；FakeRunner 可返回 None 使用空视图）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunnerViews:
    """运行期对象映射出的只读视图（``RunSnapshot`` 的除生命周期/清理/错误/能力外字段）。"""

    game: GameView
    perception: PerceptionView
    input: InputView
    navigation: NavigationView
    session: SessionView
    perf: PerformanceView | None = None
    minimap: MinimapView | None = None
    gui_waiting: bool = False
    minimap_status: str = ""


def empty_runner_views() -> RunnerViews:
    """运行线程尚未创建时的空视图（快照字段齐全，界面不会因缺字段崩溃）。"""
    return RunnerViews(
        game=GameView(
            state="UNKNOWN",
            state_since_wall=None,
            pause_reason=None,
            pause_detail=None,
            session_id=None,
            battle_id=None,
            battle_active=False,
            last_transition=None,
        ),
        perception=PerceptionView(
            last_frame_id=0,
            last_frame_at=None,
            last_frame_wall=None,
            last_frame_age_s=None,
            capture_ms_avg=None,
            window_found=False,
            window_foreground=False,
            window_client_rect=None,
            capture_error=None,
        ),
        input=InputView(
            gate_open=False,
            gate_closed_reason=None,
            pending_actions=0,
            release_count=0,
            release_confirmed=False,
            last_release_wall=None,
            executed=0,
            rejected=0,
            dropped=0,
            expired=0,
            failed=False,
            consecutive_errors=0,
            last_error=None,
        ),
        navigation=NavigationView(
            mode="autopilot",
            surface=None,
            click_count=0,
            last_click_wall=None,
            last_click_target_norm=None,
            last_click_from_own_marker=None,
            move_confirmed=False,
            move_confirmed_wall=None,
            verify_failed=False,
            blocks_behavior=False,
            ship_speed_mps=None,
            ship_marker_source=None,
            ship_marker_confidence=None,
        ),
        session=SessionView(
            account_alias=None,
            battles_completed=0,
            battles_target=None,
            battle_in_progress=False,
            elapsed_s=0.0,
            last_battle_duration_s=None,
            daily_used=None,
            rotation_index=None,
            rotation_aliases=(),
            next_alias=None,
        ),
        perf=None,
    )


def _value(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name)
    except Exception:  # pragma: no cover - 替身/资源已关闭
        return default


def _target_text(target: tuple[float, float] | None) -> str:
    if target is None or len(target) != 2:
        return "none"
    try:
        return f"({float(target[0]):.3f},{float(target[1]):.3f})"
    except (TypeError, ValueError):
        return "invalid"


def _make_minimap_provider(engine: Any, clock: Clock) -> Callable[[Observation], Any]:
    """构造导航开图前的同帧小地图回调；只读缓存帧，不触发截屏。"""
    def provide(observation):
        frame = getattr(engine, "last_frame", None)
        if frame is None or observation.window_missing or observation.capture_error:
            return None
        try:
            if int(frame.frame_id) != int(observation.frame_id):
                return None
            if frame.window_rect.to_tuple() != tuple(observation.window_rect):
                return None
            if float(frame.timestamp) != float(observation.frame_timestamp):
                return None
            age = clock.now() - float(frame.timestamp)
            if age < 0.0 or age > 2.0:
                return None
            crop = getattr(engine, "minimap_image", None)
            if not callable(crop):
                return None
            result = crop(frame)
            return None if result is None else result[0]
        except Exception:  # 回调失败不阻塞导航；不做任何截屏重试
            return None

    return provide


# ---------------------------------------------------------------------------
# 可单步驱动的会话执行器（RuntimeService 运行线程使用）
# ---------------------------------------------------------------------------


class SessionRunner:
    """一次会话的执行器：装配好的 ``Runtime`` + 主循环单步 + 非阻塞控制 + 统一清理。

    线程约定：

    - ``start()/step()`` 只允许运行线程调用；
    - ``request_pause()/request_resume()/request_stop()`` 可由任意线程调用（GUI 主线程）：
      只写线程安全的共享 ``StopController`` 并请求输入线程释放（非阻塞），
      状态机转移在下一轮 ``step()`` 内完成（与现有 F12/F11 路径一致）；
    - ``snapshot_state()/cleanup()`` 由运行线程调用；快照字段读取只读属性。
    """

    def __init__(
        self,
        runtime: Runtime,
        *,
        plan: EffectivePlan | None = None,
        max_battles: int | None = None,
        start_paused: bool = False,
        gui_foreground: Callable[[], bool] | None = None,
    ) -> None:
        self.runtime = runtime
        self.plan = plan
        self.max_battles = max_battles
        self.start_paused = start_paused
        self._gui_foreground = gui_foreground or (lambda: False)
        self._gui_waiting = False
        self._gui_map_interrupted = False
        self._gui_map_close_after: float | None = None
        self._minimap: MinimapView | None = None
        self._minimap_status = ""
        self._target_status = ""
        self._pending_target: RunCommand | None = None
        self._target_expires_at = 0.0
        self._target_requested_at = 0.0
        self._target_window_rect: tuple[int, int, int, int] | None = None
        self._target_waiting_reason = ""
        self._focus_grace_until: float | None = None
        self._focus_grace_reason = ""
        self._focus_grace_detail = ""
        self._focus_grace_frame_floor = 0.0
        self._map_metadata: dict[int, tuple[float, int, tuple[int, int, int, int]]] = {}
        self._logger = runtime.logger
        self._loop_hz = max(1, int(runtime.app.global_config.runtime.loop_hz))
        self._tick_s = 1.0 / self._loop_hz
        self._next_tick = 0.0
        self._last_window_at = 0.0
        self._was_paused = False
        self._resume_foreground_check = False
        self._resume_failed = False
        self._resume_failed_detail = ""
        self._release_at_pause = 0
        self._pause_confirmed = False
        self._last_release_count = 0
        self._last_release_wall: str | None = None
        self._last_frame_id = 0
        self._last_frame_wall: str | None = None
        self._last_state = runtime.machine.state
        self._state_since_wall = clock_iso(runtime.clock)
        self._started = False
        self._finished_at: float | None = None
        self._perf_error_logged = False
        # 过载监控上下文 / 主循环耗时统计（性能面板与告警用，不参与停止判据）
        self._tick_samples: deque[float] = deque(maxlen=120)
        self._last_step_at: float | None = None
        self._overload_state: State | None = None
        self._overload_state_since_at = 0.0

    # --- 运行线程：启动/单步 ---------------------------------------
    def start(self, *, max_battles: int | None = None) -> None:
        """状态机会话开始 + 看门狗启动 + 切前台（等价原 ``_run_session`` 的启动段）。"""
        runtime = self.runtime
        machine = runtime.machine
        limit = max_battles if max_battles is not None else self.max_battles
        if limit is not None and limit > 0:
            machine.set_rotation_limit(limit)
        else:
            machine.set_rotation_limit(None)
        machine.start(runtime.clock.now())
        runtime.watchdog.start()
        _ensure_game_foreground(runtime)
        _LAST_WINDOW[0] = runtime.window_locator.find()
        self._last_window_at = 0.0
        self._next_tick = runtime.clock.now()
        self._last_state = machine.state
        self._state_since_wall = clock_iso(runtime.clock)
        self._overload_state = machine.state
        self._overload_state_since_at = runtime.clock.now()
        self._started = True

    def _wait_for_gui(self, now: float) -> bool:
        """切到 GUI 时让出输入；截图或识别中切换也走相同路径。"""
        machine = self.runtime.machine
        controller = self.runtime.safety.controller
        if (not self._gui_foreground() or machine.state in (State.PAUSED, State.STOPPING)
                or controller.paused or controller.stop_requested):
            return False
        if not self._gui_waiting:
            entering_from_focus = self._focus_grace_until is not None
            self._gui_waiting = True
            if not entering_from_focus:
                self._gui_map_interrupted = bool(getattr(self.runtime.autopilot, "map_open", False))
                machine.suspend_for_gui()
            self._focus_grace_until = None
            self._close_gate("gui_foreground")
            self._logger.info("gui_wait reason=gui_foreground battle=%s", self.battle_id)
        elif bool(_value(self.runtime.executor, "gate_open", False)):
            # 关图门禁短暂打开后用户又切回本工具：不能把旧动作落进游戏。
            self._close_gate("gui_reentered")
            self._logger.info("gui_wait reason=gui_reentered battle=%s", self.battle_id)
        self._update_overload_context(now)
        time.sleep(self._tick_s)
        return True

    def _cached_game_foreground(self) -> bool:
        """用已缓存窗口做低成本的每轮前台检查；找不到窗口时交给完整查找。"""
        info = _LAST_WINDOW[0]
        if info is None:
            return False
        check = getattr(self.runtime.window_locator, "is_foreground", None)
        if callable(check):
            try:
                return bool(check(info.hwnd))
            except Exception:
                pass
        return bool(info.foreground)

    def _focus_reason(self, info: WindowInfo | None) -> tuple[str, str]:
        if info is None:
            return "window_lost", "找不到游戏窗口"
        if not info.available:
            return "window_unavailable", "游戏窗口暂不可用"
        return "window_unfocused", "游戏窗口不在前台"

    def _start_focus_grace(self, info: WindowInfo | None, now: float) -> None:
        reason, detail = self._focus_reason(info)
        self._focus_grace_until = now + _FOCUS_GRACE_S
        self._focus_grace_reason = reason
        self._focus_grace_detail = detail
        frame = _value(self.runtime.engine, "last_frame")
        self._focus_grace_frame_floor = float(getattr(frame, "timestamp", 0.0) or 0.0)
        self._gui_map_interrupted = bool(getattr(self.runtime.autopilot, "map_open", False))
        self._close_gate(f"focus_grace:{reason}")
        self.runtime.machine.suspend_for_gui()
        self._logger.info(
            "gui_wait reason=%s battle=%s grace_s=%.1f detail=%s",
            reason,
            self.battle_id,
            _FOCUS_GRACE_S,
            detail,
        )

    def _clear_focus_grace(self) -> None:
        self._focus_grace_until = None
        self._focus_grace_reason = ""
        self._focus_grace_detail = ""

    def _focus_return_to_game(self, observation: Observation, now: float) -> bool:
        """前台恢复后必须拿到失焦后的新画面，再走既有 continue_after_gui。"""
        frame_ts = observation.frame_timestamp
        valid = not (
            observation.window_missing
            or observation.capture_error
            or frame_ts is None
            or frame_ts <= self._focus_grace_frame_floor
        )
        if not valid:
            if now >= (self._focus_grace_until or now):
                reason = "window_unavailable"
                detail = observation.capture_error or "失焦恢复后未取得新的游戏画面"
                self._clear_focus_grace()
                self.runtime.machine.request_pause(reason, detail)
                self._logger.warning(
                    "gui_return reason=%s battle=%s detail=%s", reason, self.battle_id, detail
                )
            else:
                time.sleep(self._tick_s)
            return True
        self._clear_focus_grace()
        if self._gui_map_interrupted:
            # 不开图时直接 continue_after_gui 会重启导航，但游戏里的 M 地图仍开着。
            # 交回既有 GUI 返回分支：先关图，再等关图后的新帧，最后 continue_after_gui。
            self._gui_waiting = True
            self._last_window_at = float("-inf")
            self._logger.info("gui_return reason=map_interrupted battle=%s", self.battle_id)
            return False
        self._gui_waiting = False
        self._last_window_at = float("-inf")
        if observation.window_rect[2] > 0:
            self.runtime.bridge.update_window_rect(Rect(*observation.window_rect))
        self.runtime.machine.continue_after_gui(observation, now)
        self._logger.info("gui_return reason=window_foreground battle=%s", self.battle_id)
        return True

    def _focus_watch(self, now: float) -> bool:
        """快速检查失焦；稳定期结束仍失焦才交给状态机 PAUSED。"""
        machine = self.runtime.machine
        if machine.state in (State.PAUSED, State.STOPPING):
            if self._focus_grace_until is not None:
                self._clear_focus_grace()
            return False
        if self._focus_grace_until is None:
            if _LAST_WINDOW[0] is None or self._cached_game_foreground():
                return False
            info = self.runtime.window_locator.find()
            self._last_window_at = now
            _LAST_WINDOW[0] = info
            machine.update_window_info(info)
            if self._wait_for_gui(now):
                return True
            if info is not None and info.foreground:
                return False
            self._start_focus_grace(info, now)
            time.sleep(self._tick_s)
            return True

        info = self.runtime.window_locator.find()
        self._last_window_at = now
        _LAST_WINDOW[0] = info
        machine.update_window_info(info)
        if self._wait_for_gui(now):
            return True
        if info is not None and info.foreground:
            engine = self.runtime.engine
            latest = getattr(engine, "latest_frame", None)
            if callable(latest):
                try:
                    latest(now, force=True)
                except Exception as exc:
                    self._logger.debug("失焦恢复截帧失败: %s", exc)
            observation = engine.observe(state=machine.state, now=now)
            if self._wait_for_gui(now):
                return True
            return self._focus_return_to_game(observation, now)
        if now < (self._focus_grace_until or now):
            time.sleep(self._tick_s)
            return True
        reason, detail = self._focus_reason(info)
        self._clear_focus_grace()
        machine.request_pause(reason, detail)
        self._logger.warning("gui_wait reason=%s battle=%s detail=%s", reason, self.battle_id, detail)
        return False

    def step(self) -> None:
        """主循环一轮（迁移原 ``_run_session`` 的 while 循环体，逐行保持行为一致）。"""
        runtime = self.runtime
        machine = runtime.machine
        clock = runtime.clock
        engine = runtime.engine
        executor = runtime.executor
        monitor = runtime.safety
        logger_ = runtime.logger

        prev_state = machine.state
        pre_step_release = _release_count(executor)

        now = clock.now()
        if self._last_step_at is not None:
            self._tick_samples.append(max(0.0, now - self._last_step_at))
        self._last_step_at = now
        if self._overload_state is None or machine.state is not self._overload_state:
            self._overload_state = machine.state
            self._overload_state_since_at = now
        monitor.heartbeat(now)
        # 安全监控的 StopController：F12 停止、F11 暂停/恢复、心跳超时暂停都通过它下发
        controller = monitor.controller
        paused_now = _sync_safety_controller(machine, controller)
        if paused_now:
            self._was_paused = True
        elif self._was_paused or (machine.state is State.PAUSED and self._resume_foreground_check):
            self._was_paused = False
            # ISS-009：输入后端失败导致的暂停必须先显式重置，恢复成功后才能真正继续
            if machine.state is not State.PAUSED:
                self._resume_foreground_check = False
            elif machine.pause_reason == "input_failed":
                if _reset_input_after_failure(runtime):
                    machine.request_resume()
                else:
                    machine.request_pause("input_failed", "输入后端失败，恢复前请排查驱动/权限")
            elif self._resume_foreground_check:
                # GUI 恢复（设计 11.3）：先切游戏前台并复查；失败保持暂停、不反复抢焦点
                self._resume_foreground_check = False
                if _ensure_game_foreground(runtime):
                    self._last_window_at = float("-inf")
                    self._resume_failed = False
                    self._resume_failed_detail = ""
                    machine.request_resume()
                else:
                    self._resume_failed = True
                    self._resume_failed_detail = "未能切回游戏前台，请手动点回游戏后重试"
                    self._logger.warning("恢复失败：%s", self._resume_failed_detail)
                    controller.request_pause(
                        "resume_no_foreground", self._resume_failed_detail
                    )
                    machine.request_pause(
                        "resume_no_foreground", self._resume_failed_detail
                    )
            else:
                machine.request_resume()

        # ISS-009：输入线程连续失败后，运行线程负责转入 PAUSED 并在日志/事件中暴露原因
        if executor.failed and machine.state not in (State.PAUSED, State.STOPPING):
            machine.request_pause("input_failed", str(executor.last_error or "输入后端连续失败"))

        if self._wait_for_gui(now):
            return

        if self._focus_watch(now):
            return

        if now - self._last_window_at >= 1.0 or self._gui_waiting:
            self._last_window_at = now
            info = runtime.window_locator.find()
            if self._wait_for_gui(now):
                return
            _LAST_WINDOW[0] = info
            machine.update_window_info(info)
            if info is not None and info.foreground:
                _LAST_WINDOW[0] = info
            elif machine.state not in (State.PAUSED, State.STOPPING):
                # 港口/菜单状态也要检查前台，否则 SendInput 点击会落在别的窗口上而静默失败。
                # 先给前台 0.5s 稳定期；期间不发送任何输入，切到本工具则进入 GUI 等待。
                self._start_focus_grace(info, now)
                time.sleep(self._tick_s)
                return

        info = _LAST_WINDOW[0]
        if info is not None:
            observation = engine.observe(state=machine.state, now=now)
            if self._wait_for_gui(now):
                return
            if self._gui_waiting and info.foreground:
                if self._gui_map_interrupted and machine.state not in (State.PAUSED, State.STOPPING):
                    try:
                        actions = runtime.autopilot.close_map_after_gui(now)
                        if not executor.open_gate("gui_close_map"):
                            raise RuntimeError("输入门禁未能恢复")
                        executor.submit(actions)
                    except (ValueError, RuntimeError) as exc:
                        machine.request_pause("gui_map_interrupted", str(exc))
                        return
                    self._gui_map_interrupted = False
                    self._gui_map_close_after = now
                    time.sleep(self._tick_s)
                    return
                if self._gui_map_close_after is not None and (
                        now - self._gui_map_close_after < 0.5 or _value(executor, "pending_actions", 0)):
                    if now - self._gui_map_close_after > 5:
                        machine.request_pause("gui_map_return_failed", "关图动作未能及时完成")
                    time.sleep(self._tick_s)
                    return
                engine.latest_frame(now, force=True)
                observation = engine.observe(state=machine.state, now=now)
                if self._wait_for_gui(now):
                    return
                if self._gui_map_close_after is not None and (
                        observation.window_missing or observation.capture_error
                        or observation.frame_timestamp <= self._gui_map_close_after):
                    if now - self._gui_map_close_after > 5:
                        machine.request_pause("gui_map_return_failed", "关图后未取得新的游戏画面")
                    time.sleep(self._tick_s)
                    return
                self._gui_map_close_after = None
                self._gui_waiting = False
                if machine.state not in (State.PAUSED, State.STOPPING):
                    machine.continue_after_gui(observation, now)
            if self._pending_target is not None and info.foreground:
                self._apply_pending_target(observation, now)
            if observation.window_rect[2] > 0:
                runtime.bridge.update_window_rect(Rect(*observation.window_rect))
            if info.foreground:
                self._minimap_view(now)
            if self._pending_target is not None and machine.state is State.BATTLE_ACTIVE:
                # 目标已接收但新帧尚未到：不要先跑默认导航，否则会提前按 M。
                self._log_target_waiting("fresh_frame")
                self._update_overload_context(now)
                time.sleep(self._tick_s)
                return
            actions = machine.step(observation, now)
            if actions:
                executor.submit(actions)
        else:
            observation = Observation(
                timestamp=now,
                window_rect=(0, 0, 0, 0),
                window_missing=True,
                capture_error="游戏窗口不存在",
            )
            machine.step(observation, now)

        # 暂停释放确认：状态机进入 PAUSED 时（10.4 会关闭门禁并释放）采样释放计数
        if prev_state is not State.PAUSED and machine.state is State.PAUSED:
            self._was_paused = True
            self._release_at_pause = pre_step_release
            self._pause_confirmed = False
        elif machine.state is State.PAUSED and _release_count(executor) > self._release_at_pause:
            self._pause_confirmed = True
        elif machine.state is not State.PAUSED:
            self._pause_confirmed = False

        # 过载监控上下文：只有“脚本控制中”（前台 + 门禁打开 + 非暂停/停止）才允许 T1/T2；
        # 采样本身在安全看门狗线程中持续进行，GUI 前台等待/暂停期间仍展示性能。
        self._update_overload_context(now)

        next_tick = self._next_tick + self._tick_s
        sleep_s = next_tick - clock.now()
        if sleep_s > 0:
            time.sleep(sleep_s)
        else:
            next_tick = clock.now()
        self._next_tick = next_tick
        if logger_ is None:  # pragma: no cover - 保底
            pass

    # --- 查询 -------------------------------------------------------
    @property
    def started(self) -> bool:
        return self._started

    @property
    def finished(self) -> bool:
        return bool(self.runtime.machine.finished)

    @property
    def stop_reason(self) -> str:
        request = self.runtime.machine.stop_request
        if request is None:
            return "unknown"
        return str(getattr(request, "reason", request))

    @property
    def battles_completed(self) -> int:
        return int(self.runtime.machine.battles_completed)

    @property
    def session_id(self) -> int | None:
        return self.runtime.machine.session_id

    @property
    def battle_id(self) -> int | None:
        return self.runtime.machine.battle_id

    @property
    def paused(self) -> bool:
        """状态机是否已确认 PAUSED（设计 5.3：PAUSING→PAUSED 需状态机确认）。"""
        return self.runtime.machine.state is State.PAUSED

    @property
    def pause_reason(self) -> str:
        machine = self.runtime.machine
        if machine.pause_reason:
            return str(machine.pause_reason)
        request = self.runtime.safety.controller.pause_request
        return str(request.reason) if request is not None else ""

    @property
    def release_count(self) -> int:
        return _release_count(self.runtime.executor)

    @property
    def resume_failed(self) -> bool:
        """GUI 恢复请求是否因前台校验失败而保持暂停（设计 11.3；服务据此清 resume_pending）。"""
        return self._resume_failed

    @property
    def resume_failed_detail(self) -> str:
        return self._resume_failed_detail

    @property
    def release_confirmed(self) -> bool:
        """暂停/停止后输入线程是否已执行释放（设计 5.4/5.9 的“停止确认”依据）。"""
        if not self.paused:
            return False
        if self._pause_confirmed:
            return True
        if self.release_count > self._release_at_pause:
            self._pause_confirmed = True
        return self._pause_confirmed

    @property
    def target_status(self) -> str:
        """最近一次目标请求的可见结果（服务拒绝反馈使用）。"""
        return self._target_status

    # --- 控制通道（任意线程、非阻塞、幂等） ---------------------------
    def _close_gate(self, reason: str) -> None:
        close = getattr(self.runtime.executor, "close_gate_and_release", None)
        if not callable(close):
            return
        try:
            close(reason)
        except Exception as exc:
            self._logger.warning("关闭输入门禁失败: %s", exc)

    def request_pause(self, reason: str = "service_pause", detail: str = "") -> None:
        """请求暂停：写共享 StopController + 立即关闭输入门禁并请求释放（非阻塞）。"""
        controller = self.runtime.safety.controller
        self._release_at_pause = self.release_count
        self._pause_confirmed = False
        try:
            controller.request_pause(reason, detail, hook="service")
        except Exception as exc:  # pragma: no cover - 控制器替身异常不阻塞 GUI
            self._logger.warning("写入暂停请求失败: %s", exc)
        self._close_gate(f"service:{reason}")
        self._logger.info("已请求暂停：%s%s", reason, f"（{detail}）" if detail else "")

    def request_resume(self) -> bool:
        """请求恢复：清除共享暂停标记，下一轮主循环先做前台校验再让状态机恢复。"""
        controller = self.runtime.safety.controller
        if controller.stop_requested:
            return False
        self._resume_foreground_check = True
        self._resume_failed = False
        self._resume_failed_detail = ""
        try:
            was_paused = bool(controller.resume())
        except Exception as exc:  # pragma: no cover - 控制器替身异常不阻塞 GUI
            self._logger.warning("清除暂停请求失败: %s", exc)
            was_paused = False
        if not was_paused and not self.paused:
            self._resume_foreground_check = False
            return False
        return True

    def request_stop(self, reason: str = "service_stop", detail: str = "") -> None:
        """请求停止：写共享 StopController + 关闭输入门禁并请求释放（停止优先于暂停）。"""
        controller = self.runtime.safety.controller
        try:
            controller.request_stop(reason, detail, hook="service")
        except Exception as exc:  # pragma: no cover
            self._logger.warning("写入停止请求失败: %s", exc)
        self._close_gate(f"stop:{reason}")
        self._logger.info("已请求停止：%s%s", reason, f"（{detail}）" if detail else "")

    # --- 运行期异常/中断（与原 _run_session 一致） ---------------------
    def handle_keyboard_interrupt(self) -> None:
        self._logger.warning("收到 Ctrl+C，停止会话")
        self.runtime.machine.request_stop("manual", "KeyboardInterrupt")

    def handle_exception(self, exc: BaseException) -> None:
        self._logger.exception("会话运行异常，终止会话: %s", exc)
        try:
            self.runtime.machine.request_stop("runtime_error", str(exc))
        except Exception as stop_exc:
            self._logger.warning("异常终止时收尾失败: %s", stop_exc)

    def stop_watchdog(self) -> None:
        try:
            self.runtime.watchdog.stop()
        except Exception:
            pass

    def run(self, *, max_battles: int | None = None) -> tuple[str, int]:
        """阻塞运行一次会话（命令行兼容路径，等价原 ``_run_session``）。"""
        self.start(max_battles=max_battles)
        try:
            while not self.finished:
                self.step()
        except KeyboardInterrupt:
            self.handle_keyboard_interrupt()
        except Exception as exc:
            self.handle_exception(exc)
            raise
        finally:
            self.stop_watchdog()
            self._notify_overload_stopped()
        return self.stop_reason, self.battles_completed

    # --- 清理 -------------------------------------------------------
    def cleanup(self) -> CleanupView:
        """统一清理，返回清理清单（幂等、不抛异常）。"""
        return cleanup_runtime(self.runtime)

    # --- 过载监控上下文 / 主循环耗时 -------------------------------
    def _update_overload_context(self, now: float) -> None:
        """把当前“谁在控制输入”的上下文交给过载监控（监控线程只读，不碰状态机）。"""
        monitor = getattr(self.runtime, "overload_monitor", None)
        setter = getattr(monitor, "set_context", None)
        if not callable(setter):
            return
        info = _LAST_WINDOW[0]
        foreground = bool(getattr(info, "foreground", False)) if info is not None else False
        state = self.runtime.machine.state
        gate_open = bool(getattr(self.runtime.executor, "gate_open", True))
        controlling = state not in (State.PAUSED, State.STOPPING) and foreground and gate_open
        try:
            setter(
                state=state.value,
                controlling=controlling,
                transition_at=self._overload_state_since_at,
                foreground=foreground,
            )
        except Exception as exc:  # pragma: no cover - 监控异常不影响主循环
            self._logger.debug("更新过载监控上下文失败: %s", exc)

    def _notify_overload_stopped(self) -> None:
        monitor = getattr(self.runtime, "overload_monitor", None)
        notifier = getattr(monitor, "notify_stopped", None)
        if not callable(notifier):
            return
        try:
            notifier()
        except Exception:  # pragma: no cover
            pass

    def _loop_tick_p95_ms(self) -> float | None:
        """最近主循环节拍 p95（毫秒）；仅展示/告警，不参与过载停止判据。"""
        values = self._tick_samples
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
        return round(ordered[index] * 1000.0, 3)

    # --- 快照 -------------------------------------------------------
    def performance_view(self) -> PerformanceView | None:
        """过载监控的性能视图；补充主循环节拍与截屏耗时（监控器不反向依赖感知/运行循环）。"""
        monitor = getattr(self.runtime, "overload_monitor", None)
        if monitor is None:
            return None
        getter = getattr(monitor, "performance_view", None)
        if not callable(getter):
            getter = getattr(monitor, "view", None)
        if not callable(getter):
            return None
        try:
            view = getter()
        except Exception as exc:
            if not self._perf_error_logged:
                self._perf_error_logged = True
                self._logger.warning("读取性能视图失败: %s", exc)
            return None
        if not isinstance(view, PerformanceView):
            return None
        capture_ms: float | None = None
        try:
            capture_ms = float(self.runtime.engine.stats().capture_ms_avg)
        except Exception:  # pragma: no cover - 感知异常时保留 None
            capture_ms = None
        return replace(
            view,
            capture_ms_avg=capture_ms,
            loop_tick_p95_ms=self._loop_tick_p95_ms(),
        )

    def _battles_target(self) -> int | None:
        candidates: list[int] = []
        if self.max_battles is not None and self.max_battles > 0:
            candidates.append(int(self.max_battles))
        if self.plan is not None:
            if self.plan.max_battles_override:
                candidates.append(int(self.plan.max_battles_override))
            if self.plan.rotation_limit:
                candidates.append(int(self.plan.rotation_limit))
            candidates.append(int(self.plan.safety_battle_cap))
        positive = [value for value in candidates if value > 0]
        return min(positive) if positive else None

    def _game_view(self, now: float) -> GameView:
        machine = self.runtime.machine
        state = machine.state
        if state is not self._last_state:
            self._last_state = state
            self._state_since_wall = clock_iso(self.runtime.clock)
        transitions = _value(machine, "transitions", []) or []
        last_transition = None
        if transitions:
            moment, source, target, reason = transitions[-1]
            last_transition = f"{getattr(source, 'value', source)} -> {getattr(target, 'value', target)} ({reason})"
        pause_request = self.runtime.safety.controller.pause_request
        return GameView(
            state=str(getattr(state, "value", state)),
            state_since_wall=self._state_since_wall,
            pause_reason=machine.pause_reason or None,
            pause_detail=(pause_request.detail if pause_request is not None else None),
            session_id=machine.session_id,
            battle_id=machine.battle_id,
            battle_active=bool(getattr(state, "is_battle", False)),
            last_transition=last_transition,
        )

    def _perception_view(self, now: float) -> PerceptionView:
        runtime = self.runtime
        engine = runtime.engine
        frame = _value(engine, "last_frame")
        stats = _value(engine, "stats")
        last_frame_id = 0
        capture_ms: float | None = None
        if callable(stats):
            try:
                snapshot = stats()
                last_frame_id = int(getattr(snapshot, "last_frame_id", 0) or 0)
                capture_ms = float(getattr(snapshot, "capture_ms_avg", 0.0) or 0.0)
            except Exception:  # pragma: no cover - 统计失败不影响快照
                pass
        elif frame is not None:
            last_frame_id = int(getattr(frame, "frame_id", 0) or 0)
        last_frame_at: float | None = None
        if frame is not None:
            last_frame_at = float(getattr(frame, "timestamp", 0.0) or 0.0)
        if last_frame_id != self._last_frame_id:
            self._last_frame_id = last_frame_id
            self._last_frame_wall = clock_iso(runtime.clock)
        age = (now - last_frame_at) if last_frame_at is not None else None
        info = _LAST_WINDOW[0]
        observation = _value(runtime.machine, "observation")
        return PerceptionView(
            last_frame_id=last_frame_id,
            last_frame_at=last_frame_at,
            last_frame_wall=self._last_frame_wall,
            last_frame_age_s=age,
            capture_ms_avg=capture_ms,
            window_found=info is not None,
            window_foreground=bool(info.foreground) if info is not None else False,
            window_client_rect=(
                tuple(info.client_rect.to_tuple())  # type: ignore[arg-type]
                if info is not None and getattr(info, "client_rect", None) is not None
                else None
            ),
            capture_error=getattr(observation, "capture_error", None) if observation is not None else None,
        )

    def _input_view(self) -> InputView:
        executor = self.runtime.executor
        count = _release_count(executor)
        if count != self._last_release_count:
            self._last_release_count = count
            self._last_release_wall = clock_iso(self.runtime.clock)
        last_error = _value(executor, "last_error")
        return InputView(
            gate_open=bool(_value(executor, "gate_open", False)),
            gate_closed_reason=(str(_value(executor, "gate_reason", "")) or None),
            pending_actions=int(_value(executor, "pending_actions", 0) or 0),
            release_count=count,
            release_confirmed=self.release_confirmed,
            last_release_wall=self._last_release_wall,
            executed=int(_value(executor, "executed_actions", 0) or 0),
            rejected=int(_value(executor, "rejected_actions", 0) or 0),
            dropped=int(_value(executor, "dropped_actions", 0) or 0),
            expired=int(_value(executor, "expired_actions", 0) or 0),
            failed=bool(_value(executor, "failed", False)),
            consecutive_errors=int(_value(executor, "consecutive_errors", 0) or 0),
            last_error=str(last_error) if last_error is not None else None,
        )

    def _navigation_view(self) -> NavigationView:
        navigation = self.runtime.app.global_config.navigation
        autopilot = getattr(self.runtime, "autopilot", None)
        observation = _value(self.runtime.machine, "observation")
        ship = getattr(observation, "ship", None) if observation is not None else None
        target = _value(autopilot, "last_target") if autopilot is not None else None
        return NavigationView(
            mode=str(navigation.mode),
            surface=str(_value(autopilot, "surface", navigation.surface)) if autopilot is not None else None,
            click_count=int(_value(autopilot, "clicks", 0) or 0) if autopilot is not None else 0,
            last_click_wall=None,
            last_click_target_norm=(tuple(target.normalized) if target is not None else None),  # type: ignore[arg-type]
            last_click_from_own_marker=(bool(target.from_own_marker) if target is not None else None),
            move_confirmed=bool(_value(autopilot, "move_confirmed", False)),
            move_confirmed_wall=None,
            verify_failed=bool(_value(autopilot, "verify_failed", False)) if autopilot is not None else False,
            blocks_behavior=bool(_value(autopilot, "blocks_behavior", False)) if autopilot is not None else False,
            ship_speed_mps=(float(ship.speed_mps) if getattr(ship, "speed_mps", None) is not None else None),
            ship_marker_source=getattr(ship, "source", None) if ship is not None else None,
            ship_marker_confidence=(
                float(ship.confidence) if getattr(ship, "confidence", None) is not None else None
            ),
            phase=str(_value(autopilot, "phase", "idle")),
            activation_detected=bool(_value(autopilot, "activation_detected", False)),
            target_override=_value(autopilot, "target_override"),
            target_status=self._target_status,
        )

    def replace_target(self, command: RunCommand) -> bool:
        """运行线程接收目标，回到游戏且取得新画面后才交给导航器。"""
        import math

        now = self.runtime.clock.now()
        target = command.target
        metadata = self._map_metadata.get(command.frame_id)
        machine = self.runtime.machine
        controller = self.runtime.safety.controller
        focus_request = controller.pause_request
        focus_pause = bool(
            machine.state is State.PAUSED
            and machine.pause_reason in _FOCUS_PAUSE_REASONS
            and focus_request is not None
            and focus_request.reason in _FOCUS_PAUSE_REASONS
            and not controller.stop_requested
        )
        if (target is None or len(target) != 2
                or not all(math.isfinite(v) and 0 <= v <= 1 for v in target)
                or metadata is None or command.battle_id != self.battle_id
                or command.battle_id != metadata[1] or now < metadata[0]
                or controller.stop_requested
                or (controller.paused and not focus_pause)
                or (machine.state is not State.BATTLE_ACTIVE and not focus_pause)
                or self.runtime.app.global_config.navigation.mode != "autopilot"):
            self._target_status = "目标未接受：场次已变化或当前不可控制（人工暂停、停止或非战斗界面）"
            self._target_waiting_reason = ""
            self._logger.warning(
                "target_rejected battle=%s frame=%s target=%s reason=%s",
                command.battle_id,
                command.frame_id,
                _target_text(target),
                self._target_status,
            )
            return False
        self._pending_target = command
        self._target_window_rect = metadata[2]
        self._target_expires_at = now + 30.0
        self._target_requested_at = now
        self._target_waiting_reason = ""
        self._logger.info(
            "target_received battle=%s frame=%s target=%s focus_pause=%s",
            command.battle_id,
            command.frame_id,
            _target_text(target),
            focus_pause,
        )
        info = None
        if focus_pause:
            try:
                info = self.runtime.window_locator.find()
            except Exception:
                info = None
            already_foreground = bool(info is not None and info.foreground)
            focused = already_foreground or _ensure_game_foreground(self.runtime)
            if focused:
                self._last_window_at = float("-inf")
                if controller.resume(expected_pause=focus_request):
                    machine.request_resume()
                    self._target_status = "目标已接收，正在恢复游戏"
                    self._log_target_waiting("focus_resume")
                else:
                    self._target_status = "目标已接收，暂停请求已变化，等待手动恢复"
                    self._log_target_waiting("pause_changed")
            else:
                self._target_status = "切回游戏失败，请手动切回以应用目标"
                self._log_target_waiting("focus_failed")
        elif self._gui_foreground():
            if _ensure_game_foreground(self.runtime):
                self._last_window_at = float("-inf")
                self._target_status = "目标已接收，等待游戏前台"
            else:
                self._target_status = "切回游戏失败，请手动切回以应用目标"
        else:
            self._target_status = "目标已接收，等待游戏前台"
        if not self._target_waiting_reason and "已接收" in self._target_status:
            self._log_target_waiting("game_foreground" if not self._gui_foreground() else "new_frame")
        return True

    def _log_target_waiting(self, reason: str) -> None:
        if self._pending_target is None or reason == self._target_waiting_reason:
            return
        self._target_waiting_reason = reason
        command = self._pending_target
        self._logger.info(
            "target_waiting battle=%s frame=%s target=%s reason=%s",
            command.battle_id,
            command.frame_id,
            _target_text(command.target),
            reason,
        )

    def _cancel_pending_target(self, reason: str) -> None:
        command = self._pending_target
        if command is None:
            return
        self._pending_target = None
        self._target_waiting_reason = ""
        self._target_status = reason
        self._logger.warning(
            "target_cancelled battle=%s frame=%s target=%s reason=%s",
            command.battle_id,
            command.frame_id,
            _target_text(command.target),
            reason,
        )

    def _apply_pending_target(self, observation: Observation, now: float) -> None:
        command = self._pending_target
        if command is None:
            return
        machine = self.runtime.machine
        controller = self.runtime.safety.controller
        if (command.battle_id != self.battle_id or now > self._target_expires_at
                or tuple(observation.window_rect) != self._target_window_rect):
            self._cancel_pending_target("目标已取消：场次、窗口已变化或等待超时")
            return
        if controller.stop_requested or machine.state is State.STOPPING:
            self._cancel_pending_target("目标已取消：运行已停止")
            return
        focus_request = controller.pause_request
        focus_controller_pause = bool(
            focus_request is not None and focus_request.reason in _FOCUS_PAUSE_REASONS
        )
        if controller.paused and not focus_controller_pause:
            self._cancel_pending_target("目标已取消：当前暂停已变为人工或异常暂停")
            return
        if (machine.state is State.PAUSED and machine.pause_reason in _FOCUS_PAUSE_REASONS
                and (not controller.paused or focus_controller_pause)):
            return
        if controller.paused or machine.state is not State.BATTLE_ACTIVE:
            self._cancel_pending_target("目标已取消：当前不是可控制的战斗画面")
            return
        if (observation.window_missing or observation.capture_error
                or observation.frame_timestamp is None or observation.frame_timestamp <= now - 2.0
                or observation.frame_timestamp <= self._target_requested_at):
            self._log_target_waiting("fresh_frame")
            return
        if self.runtime.engine.detect_state(now) is not State.BATTLE_ACTIVE:
            self._cancel_pending_target("目标已取消：当前不是战斗画面")
            return
        try:
            self.runtime.autopilot.replace_target(command.target, now)
        except ValueError as exc:
            self._cancel_pending_target(f"目标未应用：{exc}")
            return
        self._pending_target = None
        self._target_waiting_reason = ""
        self._target_status = "目标已交给自动驾驶"
        self._logger.info(
            "target_applied battle=%s frame=%s target=%s",
            command.battle_id,
            command.frame_id,
            _target_text(command.target),
        )

    def _minimap_view(self, now: float) -> MinimapView | None:
        machine = self.runtime.machine
        self._minimap_status = ""
        if self.battle_id is None or (self._minimap is not None and self._minimap.battle_id != self.battle_id):
            self._minimap = None
            self._map_metadata.clear()
        if machine.state is not State.BATTLE_ACTIVE:
            self._minimap_status = "已暂停，保留最近画面" if machine.state is State.PAUSED else "等待战斗画面"
            if machine.state is not State.PAUSED:
                self._minimap = None
                self._map_metadata.clear()
            return self._minimap
        if self.battle_id is None:
            self._minimap_status = "等待场次记录"
            return None
        engine = self.runtime.engine
        frame = _value(engine, "last_frame")
        crop = getattr(engine, "minimap_image", None)
        if self._gui_waiting:
            self._minimap_status = "等待游戏前台，保留最近画面"
            return self._minimap
        if frame is None or not callable(crop):
            self._minimap_status = "尚未取得游戏截图" if frame is None else "小地图裁剪不可用"
            return self._minimap
        if bool(_value(self.runtime.autopilot, "blocks_minimap", _value(self.runtime.autopilot, "blocks_behavior", False))):
            self._minimap_status = "地图操作中，保留最近画面"
            return self._minimap
        frame_id = int(getattr(frame, "frame_id", 0))
        if self._minimap is not None and self._minimap.frame_id == frame_id:
            return self._minimap
        if now - frame.timestamp > 2.0:
            self._minimap_status = "游戏截图已过期"
            return self._minimap
        result = crop(frame)
        if result is not None:
            import cv2

            pixels, _ = result
            ok, encoded = cv2.imencode(".png", pixels)
            if ok:
                self._minimap = MinimapView(frame_id, frame.timestamp, self.battle_id,
                    tuple(frame.window_rect.to_tuple()), encoded.tobytes(), pixels.shape[1], pixels.shape[0])
                self._map_metadata = {key: value for key, value in self._map_metadata.items()
                                      if 0 <= now - value[0] <= 5.0 and value[1] == self.battle_id}
                self._map_metadata[frame_id] = (frame.timestamp, self.battle_id, tuple(frame.window_rect.to_tuple()))
        else:
            self._minimap_status = "小地图内容区不可用，请检查标定"
        return self._minimap

    def _session_view(self) -> SessionView:
        runtime = self.runtime
        session = runtime.session
        return SessionView(
            account_alias=runtime.app.account_alias,
            battles_completed=self.battles_completed,
            battles_target=self._battles_target(),
            battle_in_progress=_value(runtime.machine, "battle") is not None,
            elapsed_s=float(_value(session, "elapsed_s", 0.0) or 0.0),
            last_battle_duration_s=_value(session, "last_battle_duration_s"),
            daily_used=(self.plan.daily_used if self.plan is not None else None),
            rotation_index=None,
            rotation_aliases=(),
            next_alias=None,
        )

    def snapshot_state(self, now: float) -> RunnerViews:
        """把运行时对象映射成只读视图（由运行线程调用，GUI 不直接触碰运行时对象）。"""
        minimap = self._minimap_view(now)
        return RunnerViews(
            game=self._game_view(now),
            perception=self._perception_view(now),
            input=self._input_view(),
            navigation=self._navigation_view(),
            session=self._session_view(),
            perf=self.performance_view(),
            minimap=minimap,
            gui_waiting=self._gui_waiting,
            minimap_status=self._minimap_status,
        )


def _run_session(runtime: Runtime, *, max_battles: int | None = None) -> tuple[str, int]:
    """运行一个账号的一次会话，返回 (停止原因, 完成场次)。（原 ``main._run_session``）"""
    return SessionRunner(runtime).run(max_battles=max_battles)


__all__ = [
    "CleanupView",
    "RunnerViews",
    "Runtime",
    "SessionRunner",
    "attach_overload_monitor",
    "cleanup_runtime",
    "empty_runner_views",
    "reset_window_cache",
]
