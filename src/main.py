"""程序入口（详细设计 2.1 / B.1）。

用法::

    python -m src.main --profile cn360                    # 使用全局 ship.ship_id
    python -m src.main --profile cn360 --ship <ship_id>
    python -m src.main --account alt1                     # 指定本地账号档案
    python -m src.main --rotate                           # 按 accounts.yaml 轮换账号
    python -m src.main --dry-run                          # 只校验配置，不启动

安全口径（用户扩展需求）：
- 账号只使用本地别名（accounts.yaml），不保存真实账号/密码/令牌，不自动登录；
- 轮换到下一个账号时脚本停下来提示使用者手动切号并登录，按回车后继续；
- 同一时间只运行一个账号、一个游戏窗口。
"""

from __future__ import annotations

import argparse
import inspect
import logging
import random
import sys
import time
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from .behavior.scheduler import BehaviorScheduler
from .common.clock import Clock, SystemClock
from .common.errors import ConfigError
from .common.single_instance import SingleInstanceLock
from .common.types import Observation, Rect, State, WindowInfo
from .common.window import WindowLocator, set_dpi_awareness
from .config.loader import load_app, load_project, load_ship
from .config.models import AccountsConfig, AppConfig
from .config.validate import validate_app
from .execution.queue import InputExecutor
from .execution.safety import (
    SafetyEventKind,
    SafetyMonitor,
    SafetyWatchdog,
    StopController,
    Win32KeyStateSource,
)
from .execution.winput import make_backend
from .navigation.autopilot import AutoPilotNavigator
from .navigation.navigator import Navigator
from .perception.capture import GdiCapture
from .perception.engine import PerceptionEngine
from .state_machine.accounts import AccountRotator
from .state_machine.machine import MachineDeps, StateMachine
from .state_machine.session import SessionController
from .state_machine.ship_check import ShipChecker
from .state_machine.steps import StepResult, StepSequenceExecutor
from .storage.event_log import EventSink, configure_text_logging
from .storage.screenshots import ScreenshotManager

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_RUNTIME_ERROR = 1
#: 已有另一个脚本实例在运行（ISS-003）
EXIT_INSTANCE_RUNNING = 3


# ---------------------------------------------------------------------------
# 运行时装配
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
        if event.kind not in (SafetyEventKind.EMERGENCY_STOP, SafetyEventKind.HEARTBEAT_TIMEOUT):
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


def _build_runtime(app: AppConfig) -> Runtime:
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
        executor = InputExecutor(make_backend(config.input), config.input, clock=clock)
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
        )
    except BaseException:
        # ISS-008：装配失败也要回收已创建的输入线程/日志线程/截屏资源
        _cleanup_partial_runtime(started, text_logger)
        raise


_LAST_WINDOW: list[WindowInfo | None] = [None]


def _teardown(runtime: Runtime) -> None:
    logger = runtime.logger
    try:
        runtime.watchdog.stop()
    except Exception:
        pass
    try:
        if runtime.executor.is_running:
            runtime.executor.stop(drain=True)
    except Exception as exc:
        logger.warning("停止输入线程失败: %s", exc)
    try:
        runtime.screenshots.cleanup_session()
    except Exception:
        pass
    try:
        runtime.events.stop(drain=True)
    except Exception as exc:
        logger.warning("停止日志线程失败: %s", exc)
    try:
        runtime.engine.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 单账号会话主循环
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
    """启动时把游戏窗口切到前台（截屏要求游戏在前台）；失败返回 False。

    只用于启动阶段：运行中用户主动切走仍会按安全策略暂停，不会自动抢焦点。
    """
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


def _run_session(runtime: Runtime, *, max_battles: int | None = None) -> tuple[str, int]:
    """运行一个账号的一次会话，返回 (停止原因, 完成场次)。"""
    machine = runtime.machine
    clock = runtime.clock
    engine = runtime.engine
    executor = runtime.executor
    monitor = runtime.safety
    watchdog = runtime.watchdog
    logger_ = runtime.logger
    loop_hz = max(1, int(runtime.app.global_config.runtime.loop_hz))
    tick_s = 1.0 / loop_hz
    if max_battles is not None and max_battles > 0:
        machine.set_rotation_limit(max_battles)
    else:
        machine.set_rotation_limit(None)

    machine.start(clock.now())
    watchdog.start()
    _ensure_game_foreground(runtime)
    _LAST_WINDOW[0] = runtime.window_locator.find()
    last_window_at = 0.0
    was_paused = False
    next_tick = clock.now()
    try:
        while not machine.finished:
            now = clock.now()
            monitor.heartbeat(now)
            # 安全监控的 StopController：F12 停止、F11 暂停/恢复、心跳超时暂停都通过它下发
            controller = monitor.controller
            paused_now = _sync_safety_controller(machine, controller)
            if paused_now:
                was_paused = True
            elif was_paused:
                was_paused = False
                # ISS-009：输入后端失败导致的暂停必须先显式重置，恢复成功后才能真正继续
                if machine.pause_reason == "input_failed":
                    if _reset_input_after_failure(runtime):
                        machine.request_resume()
                    else:
                        machine.request_pause("input_failed", "输入后端失败，恢复前请排查驱动/权限")
                else:
                    machine.request_resume()

            # ISS-009：输入线程连续失败后，运行线程负责转入 PAUSED 并在日志/事件中暴露原因
            if executor.failed and machine.state not in (State.PAUSED, State.STOPPING):
                machine.request_pause("input_failed", str(executor.last_error or "输入后端连续失败"))

            if now - last_window_at >= 1.0:
                last_window_at = now
                info = runtime.window_locator.find()
                _LAST_WINDOW[0] = info
                machine.update_window_info(info)
                if info is None:
                    machine.request_pause("window_lost", "找不到游戏窗口")
                elif info.foreground:
                    _LAST_WINDOW[0] = info
                elif (
                    runtime.app.profile.window.require_foreground
                    and machine.state not in (State.PAUSED, State.STOPPING)
                ):
                    # 港口/菜单状态也要检查前台，否则 SendInput 点击会落在别的窗口上而静默失败
                    machine.request_pause("window_unfocused", "游戏窗口不在前台")

            info = _LAST_WINDOW[0]
            if info is not None:
                observation = engine.observe(state=machine.state, now=now)
                if observation.window_rect[2] > 0:
                    runtime.bridge.update_window_rect(Rect(*observation.window_rect))
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

            next_tick += tick_s
            sleep_s = next_tick - clock.now()
            if sleep_s > 0:
                time.sleep(sleep_s)
            else:
                next_tick = clock.now()
    except KeyboardInterrupt:
        logger_.warning("收到 Ctrl+C，停止会话")
        machine.request_stop("manual", "KeyboardInterrupt")
    except Exception as exc:
        # ISS-008：运行期异常先走 STOPPING 收尾（结束战斗/会话记录、释放输入），再向上抛给 finally 清理
        logger_.exception("会话运行异常，终止会话: %s", exc)
        try:
            machine.request_stop("runtime_error", str(exc))
        except Exception as stop_exc:
            logger_.warning("异常终止时收尾失败: %s", stop_exc)
        raise
    finally:
        try:
            watchdog.stop()
        except Exception:
            pass
    stop_reason = "unknown"
    if machine.stop_request is not None:
        stop_reason = str(machine.stop_request.reason)
    return stop_reason, machine.battles_completed


# ---------------------------------------------------------------------------
# 配置/账号加载
# ---------------------------------------------------------------------------


def _load_app(root: Path, profile: str, ship: str | None, alias: str | None) -> AppConfig:
    kwargs: dict[str, Any] = {"ship_id": ship}
    parameters = inspect.signature(load_app).parameters
    if alias:
        if "account" not in parameters:
            raise ConfigError(
                [
                    _issue(
                        "账号功能尚未就绪：src/config/loader.py 的 load_app 还没有 account 参数。"
                        "请等待账号支持任务（T7）完成，或先不使用 --account/--rotate。"
                    )
                ]
            )
        kwargs["account"] = alias
    return load_app(root, profile, **kwargs)


def _issue(message: str):  # 延迟导入，避免顶层循环依赖
    from .common.errors import Severity, ValidationIssue

    return ValidationIssue(Severity.ERROR, "account_unsupported", "src.main", message)


def _load_accounts(root: Path) -> AccountsConfig | None:
    try:
        from .config.loader import load_accounts
    except ImportError:
        return None
    try:
        return load_accounts(root)
    except TypeError:
        return None


def _list_ships(root: Path, profile_id: str) -> list[tuple[str, str]]:
    """列出客户端配置下所有船只档案的 (ship_id, display_name)。"""
    ships_dir = root / "profiles" / profile_id / "ships"
    ships: list[tuple[str, str]] = []
    for ship_yaml in sorted(ships_dir.glob("*/ship.yaml")):
        try:
            ship = load_ship(ship_yaml)
        except Exception:  # 单个船只配置损坏不影响其它船只的列出
            continue
        if ship.ship_id:
            ships.append((ship.ship_id, ship.display_name))
    return ships


def _resolve_ship(root: Path, profile_id: str, ship_id: str | None, ship_name: str | None) -> str | None:
    """把 --ship-name（显示名称/编号）解析为 ship_id；未指定名称时原样返回 ship_id。"""
    if not ship_name:
        return ship_id
    ships = _list_ships(root, profile_id)
    if not ships:
        raise ConfigError([_issue(f"客户端 {profile_id} 下没有任何船只档案（profiles/{profile_id}/ships/<id>/）")])
    exact = [item for item in ships if item[1] == ship_name or item[0] == ship_name]
    partial = [item for item in ships if ship_name in item[1]]
    matches = exact or partial
    if not matches:
        available = "、".join(f"{sid}({name})" for sid, name in ships)
        raise ConfigError([_issue(f"找不到船只名称 {ship_name!r}；现有船只：{available}")])
    if len(matches) > 1:
        available = "、".join(f"{sid}({name})" for sid, name in matches)
        raise ConfigError([_issue(f"船只名称 {ship_name!r} 匹配到多个档案：{available}；请改用 ship_id 精确指定")])
    return matches[0][0]


def _print_issues(issues: list[Any]) -> None:
    for issue in issues:
        print(issue.format(), file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="战舰世界联合作战（PvE）本地托管脚本（截屏+模拟键鼠，仅本地技术研究）",
    )
    parser.add_argument("--root", default=".", help="项目根目录（默认当前目录）")
    parser.add_argument("--profile", default="cn360", help="客户端配置 id（默认 cn360）")
    parser.add_argument("--ship", default=None, help="会话固定船只 id（默认用 config.yaml 的 ship.ship_id）")
    parser.add_argument(
        "--ship-name",
        default=None,
        help="按船只显示名称选择会话固定船只（如 列宁格勒）；脚本会在港口船只轮播条里找并选中它",
    )
    parser.add_argument("--list-ships", action="store_true", help="列出当前客户端配置下的船只档案后退出")
    parser.add_argument("--account", default=None, help="账号档案别名（accounts.yaml 中的 alias）")
    parser.add_argument("--rotate", action="store_true", help="按 accounts.yaml 轮换启用账号（需手动切号）")
    parser.add_argument("--max-battles", type=int, default=None, help="单账号本次会话最多场次（覆盖配置）")
    parser.add_argument("--dry-run", action="store_true", help="只校验配置并打印摘要，不启动脚本")
    parser.add_argument("--log-level", default=None, help="覆盖文本日志级别，例如 DEBUG")
    parser.add_argument("--version", action="version", version="wows_script 0.1.0")
    return parser


def _run_accounts(
    args: argparse.Namespace,
    root: Path,
    aliases: list[str | None],
    rotator: AccountRotator | None,
) -> int:
    """逐账号运行；每个账号的运行时构建/会话/清理由 try/finally 绑定（ISS-008）。"""
    rotating = bool(rotator is not None and len(aliases) > 1)
    no_battle_streak = 0
    try:
        while True:
            alias = rotator.current.alias if rotator is not None else aliases[0] if len(aliases) == 1 else None
            app = _load_app(root, args.profile, args.ship, alias)
            problems = validate_app(app)
            fatal = [issue for issue in problems if issue.is_error]
            if fatal:
                _print_issues(fatal)
                print(f"账号 {alias or '(默认)'} 的配置校验失败，停止", file=sys.stderr)
                return EXIT_CONFIG_ERROR
            if args.log_level:
                app = replace(
                    app,
                    global_config=replace(
                        app.global_config,
                        runtime=replace(app.global_config.runtime, log_level=args.log_level),
                    ),
                )
            _LAST_WINDOW[0] = None
            runtime: Runtime | None = None
            reason, battles = "runtime_error", 0
            try:
                runtime = _build_runtime(app)
                if app.account is not None:
                    runtime.logger.info("当前账号档案: %s", app.account.alias)
                limit = args.max_battles
                if limit is None and rotator is not None:
                    limit = rotator.current_limit()
                reason, battles = _run_session(runtime, max_battles=limit)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                # ISS-008：异常原因交给调用方显示，finally 保证统一清理完成
                logging.getLogger(__name__).exception("会话异常终止: %s", exc)
                print(f"会话异常终止：{exc}", file=sys.stderr)
                reason, battles = "runtime_error", 0
            finally:
                if runtime is not None:
                    _teardown(runtime)
            print(f"账号 {alias or '(默认)'} 会话结束：原因={reason} 场次={battles}")
            if rotator is not None:
                rotator.record_battle(alias)
            if not rotating:
                return EXIT_OK if reason not in ("game_crash", "runtime_error") else EXIT_RUNTIME_ERROR
            if reason in ("emergency_stop", "manual"):
                return EXIT_OK
            if battles == 0:
                no_battle_streak += 1
                if no_battle_streak >= max(1, len(aliases)):
                    print("所有账号本次都无法开始战斗（可能已达每日上限/不在允许时段），退出。")
                    return EXIT_OK
            else:
                no_battle_streak = 0
            rotator.advance()
            next_alias = rotator.current.alias
            print()
            print("=" * 72)
            print(f"请手动切换到账号「{next_alias}」：")
            print("  1. 在游戏客户端退出当前账号并登录目标账号（脚本不会代登录、不保存密码）；")
            print("  2. 确认已回到港口界面、船只与空装备符合配置；")
            print("  3. 回到本控制台按回车继续。")
            print("=" * 72)
            try:
                input("切换完成后按回车继续（Ctrl+C 退出）...")
            except EOFError:
                return EXIT_OK
    except KeyboardInterrupt:
        print("用户中断，退出。")
        return EXIT_OK


def _print_instance_holder(holder: dict[str, Any] | None) -> None:
    holder = holder or {}
    print(
        f"已有另一个脚本实例在运行（PID={holder.get('pid', '未知')}，"
        f"启动于 {holder.get('started_at', '未知')}），本次不启动游戏控制。",
        file=sys.stderr,
    )
    print(
        "如确认没有实例，请在任务管理器结束残留的 python/src.main 进程；"
        "命令行可用：taskkill /PID <pid> /T /F（结束进程树，避免只杀 py 启动器）。",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    if not (root / "config.yaml").is_file():
        print(f"找不到配置文件: {root / 'config.yaml'}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.list_ships:
        ships = _list_ships(root, args.profile)
        if not ships:
            print(f"客户端 {args.profile} 下没有船只档案（profiles/{args.profile}/ships/<id>/）", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        for ship_id, display_name in ships:
            print(f"{ship_id}\t{display_name}")
        return EXIT_OK

    # --ship-name 解析为 ship_id（脚本随后在船只轮播条里用卡片模板找并选中该船）
    try:
        ship_id = _resolve_ship(root, args.profile, args.ship, args.ship_name)
    except ConfigError as exc:
        _print_issues(exc.errors + exc.warnings)
        return EXIT_CONFIG_ERROR
    args.ship = ship_id

    # 账号档案
    accounts = _load_accounts(root) if (args.rotate or args.account) else None
    rotator: AccountRotator | None = None
    aliases: list[str | None]
    if args.rotate:
        if accounts is None:
            print("当前版本暂不支持 --rotate（账号支持任务尚未完成）", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        rotator = AccountRotator(accounts)
        if not rotator.enabled:
            print(
                "accounts.yaml 中没有启用的账号：请至少将一个 accounts[].enabled 设为 true，"
                "并把 rotation.enabled 设为 true",
                file=sys.stderr,
            )
            return EXIT_CONFIG_ERROR
        aliases = [account.alias for account in rotator.plan()]
    elif args.account:
        if accounts is None:
            print("当前版本暂不支持 --account（账号支持任务尚未完成）", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        if accounts.get(args.account) is None:
            print(f"accounts.yaml 中找不到账号别名: {args.account}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        aliases = [args.account]
    else:
        aliases = [None]

    # 校验（以第一个账号/默认 profile 为基准；后续每个账号运行前会再校验一次）
    try:
        loaded = load_project(root, args.profile, ship_id=args.ship)
    except ConfigError as exc:
        _print_issues(exc.errors + exc.warnings)
        return EXIT_CONFIG_ERROR
    issues = list(loaded.issues)
    try:
        app0 = _load_app(root, args.profile, args.ship, aliases[0])
    except ConfigError as exc:
        _print_issues(exc.errors + exc.warnings)
        return EXIT_CONFIG_ERROR
    issues.extend(validate_app(app0))
    errors = [issue for issue in issues if issue.is_error]
    warnings = [issue for issue in issues if not issue.is_error]
    _print_issues(errors)
    if warnings:
        print(f"校验警告 {len(warnings)} 条：", file=sys.stderr)
        _print_issues(warnings)
    if errors:
        print(f"配置校验失败：{len(errors)} 个错误，{len(warnings)} 个警告（退出码 2）", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    print(
        f"配置校验通过：profile={app0.profile.client_id} ship={app0.ship.ship_id or '(未配置)'} "
        f"maps={len(app0.maps)} 账号={app0.account_alias or '(未指定)'}"
    )
    if args.dry_run:
        print("--dry-run：未启动脚本。")
        return EXIT_OK

    # ISS-003：跨进程单实例锁（命令行与 GUI 共用），防止两个实例同时发送键鼠
    lock = SingleInstanceLock(root / app0.global_config.paths.data_dir)
    if not lock.acquire():
        _print_instance_holder(lock.holder)
        return EXIT_INSTANCE_RUNNING
    try:
        return _run_accounts(args, root, aliases, rotator)
    finally:
        lock.release()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
