"""主状态机（详细设计 6.1 / 6.2 / 6.4 / 10）。

设计口径（见 design/详细设计评审与实现补充.md）：
- 控制流唯一来源是状态机；步骤序列只描述“单状态内的界面操作”；
- 退出流程由 EXIT_* 状态驱动，客户端配置只提供按键与界面特征；
- 战斗结束（存活）路径同样经过 EXITING；
- 所有等待都是超时事件，主循环 10Hz 每轮调用 step() 一次，不阻塞；
- 子流程（导航/行为/死亡检测）每轮推进，异常被隔离，只停该子流程。

StateMachine.step(observation, now) -> list[Action]：返回本轮要提交给输入线程的动作，
不直接调用键鼠、不直接写数据库（通过 EventSink 事件队列）。
"""

from __future__ import annotations

import logging
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..common.clock import Clock
from ..common.coords import BattleCalibration
from ..common.types import Action, MatchResult, Observation, Rect, State, WindowInfo
from ..config.models import AppConfig, ClientProfile, MapPack
from ..execution.queue import InputExecutor
from ..navigation.navigator import Navigator
from ..navigation.route import select_route_for_map
from ..perception.engine import PerceptionEngine
from ..storage.event_log import EventSink
from ..storage.screenshots import ScreenshotManager
from .session import SessionController
from .ship_check import ShipChecker, ShipCheckStatus
from .steps import StepSequenceExecutor, StepStatus

logger = logging.getLogger(__name__)

# 结算特征优先于死亡信号（评审 P1-5：同一帧同时命中时按战斗结束处理）
_RESULT_FEATURE_FALLBACK = "battle_results"


@dataclass
class _BattleRuntime:
    """一场战斗的运行期上下文，进入战斗时创建、回港时销毁（详细设计 6.3）。"""

    started_at: float
    map_id: str | None = None
    spawn_id: str | None = None
    route_id: str | None = None
    calibration: BattleCalibration | None = None
    bootstrapped: bool = False
    nav_started: bool = False
    behavior_started: bool = False
    random_profile_id: int | None = None
    seed: int = 0
    map_attempts: int = 0
    warning_logged: bool = False


@dataclass
class MachineDeps:
    """状态机依赖注入容器（便于离线测试替换为 fake）。"""

    config: AppConfig
    clock: Clock
    perception: PerceptionEngine
    executor: InputExecutor
    events: EventSink
    steps: StepSequenceExecutor
    navigator: Navigator
    behavior: Any  # BehaviorScheduler（避免循环导入，鸭子类型）
    session: SessionController
    screenshots: ScreenshotManager
    ship_checker: ShipChecker
    autopilot: Any | None = None  # AutoPilotNavigator（简化导航模式，不识别地图/航线）
    # 共享 StopController（ISS-006）：暂停/恢复与安全监控保持同一来源；鸭子类型，可为 None
    controller: Any | None = None
    # 当前战斗编号上下文回调（ISS-010）：导航/行为/步骤事件据此关联场次
    on_battle_context: Callable[[int | None], None] | None = None
    window_info_provider: Callable[[], WindowInfo | None] | None = None
    rng: random.Random | None = None
    logger_: logging.Logger | None = None


class StateMachine:
    """显式转移表 + 每状态处理器；不持久化，重启后从 INIT 开始（详细设计 6.5）。"""

    def __init__(self, deps: MachineDeps) -> None:
        self.deps = deps
        self.config = deps.config
        self.profile: ClientProfile = deps.config.profile
        self._logger = deps.logger_ or logger
        self._rng = deps.rng or random.Random()
        self._state = State.INIT
        self._entered_at = 0.0
        self._now = 0.0
        self._observation: Observation | None = None
        self._window_info: WindowInfo | None = None
        self._session_id: int | None = None
        self._battle_id: int | None = None
        self._battle_result: str = ""
        self._exit_method: str = "none"
        self._exit_error: str | None = None
        self._battle: _BattleRuntime | None = None
        self._stop_request: Any | None = None
        self._controller = deps.controller
        self._pause_reason: str = ""
        self._resume_requested = False
        self._finished = False
        self._started = False
        self._rotation_limit: int | None = None
        # PORT 子阶段
        self._port_phase = "idle"
        self._port_wait_until = 0.0
        self._port_start_attempts = 0
        self._port_loadout_started = False
        # EXIT 子状态
        self._exit_retried = False
        self._confirm_seen = False
        self._dead_wait_until = 0.0
        self._pending_exit_actions: list[Action] = []
        # 结算页按钮选择（2026-10-31）：优先“继续战斗！”直接下一局，否则“回到港口”
        self._result_continue_clicked = False
        self._result_choice_until = 0.0
        # 回港弹窗
        self._popup_rounds = 0
        self._popup_wait_until = 0.0
        # 异常恢复
        self._recovery_step = 0
        self._recovery_wait_until = 0.0
        self._recovery_times: deque[float] = deque(maxlen=16)
        self._recovery_failures = 0
        # 统计
        self._battles_completed = 0
        self._actions_emitted = 0
        self._transitions: list[tuple[float, State, State, str]] = []

    # ------------------------------------------------------------------
    # 对外查询
    # ------------------------------------------------------------------
    @property
    def state(self) -> State:
        return self._state

    @property
    def entered_at(self) -> float:
        return self._entered_at

    @property
    def observation(self) -> Observation | None:
        return self._observation

    @property
    def session_id(self) -> int | None:
        return self._session_id

    @property
    def battle_id(self) -> int | None:
        return self._battle_id

    @property
    def battle(self) -> _BattleRuntime | None:
        return self._battle

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def stop_request(self) -> Any | None:
        return self._stop_request

    @property
    def pause_reason(self) -> str:
        return self._pause_reason

    @property
    def battles_completed(self) -> int:
        return self._battles_completed

    @property
    def actions_emitted(self) -> int:
        return self._actions_emitted

    @property
    def transitions(self) -> list[tuple[float, State, State, str]]:
        return list(self._transitions)

    # ------------------------------------------------------------------
    # 生命周期/外部请求
    # ------------------------------------------------------------------
    def start(self, now: float | None = None) -> None:
        """开始会话：写会话记录，进入 INIT。"""
        now = self._tick_clock(now)
        self._entered_at = now
        alias = getattr(self.config, "account_alias", None)
        begin_session = self.deps.events.begin_session
        try:
            parameters = __import__("inspect").signature(begin_session).parameters
        except (TypeError, ValueError):  # pragma: no cover - 伪对象
            parameters = {}
        if alias and "account_alias" in parameters:
            self._session_id = begin_session(self.profile.client_id, self.config.config_hash, account_alias=alias)
        else:
            self._session_id = begin_session(self.profile.client_id, self.config.config_hash)
        self.deps.screenshots.begin_session(self._session_id)
        self.deps.screenshots.cleanup_stale_live()
        self.deps.session.start()
        self._started = True
        self._log_state(State.INIT, "session_start")

    def set_rotation_limit(self, limit: int | None) -> None:
        """本账号单次会话场次上限（账号轮换用）；None 表示不限制。"""
        self._rotation_limit = limit

    def request_stop(self, reason: str, detail: str = "") -> None:
        self._now = self.deps.clock.now()
        if self._stop_request is None:
            self._stop_request = self.deps.session.stop(reason, detail)
        # ISS-006：停止请求统一同步到共享 StopController（与安全监控/GUI 同源）
        if self._controller is not None and not getattr(self._controller, "stop_requested", True):
            try:
                self._controller.request_stop(str(reason), detail, hook="state_machine")
            except Exception:  # 控制器实现异常不阻塞停止
                pass
        if self._state not in (State.STOPPING, State.PAUSED):
            self._transition(State.STOPPING, f"stop:{reason}")

    def request_pause(self, reason: str, detail: str = "") -> None:
        self._now = self.deps.clock.now()
        if self._state is State.PAUSED or self._state is State.STOPPING:
            return
        self._pause_reason = reason
        # ISS-006：把暂停同步到共享 StopController，保证 F11/主循环/GUI 能看到同一状态
        if self._controller is not None:
            try:
                self._controller.request_pause(str(reason), detail, hook="state_machine")
            except Exception:
                pass
        self._transition(State.PAUSED, f"pause:{reason}:{detail}".rstrip(":"))

    def request_resume(self) -> None:
        self._resume_requested = True

    def update_observation(self, observation: Observation) -> None:
        self._observation = observation

    def update_window_info(self, info: WindowInfo | None) -> None:
        self._window_info = info

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def step(self, observation: Observation, now: float | None = None) -> list[Action]:
        now = self._tick_clock(now)
        self._observation = observation
        if self._state is State.STOPPING:
            return []
        if self._finished:
            return []
        if self._stop_request is not None and self._state is not State.STOPPING:
            self._transition(State.STOPPING, "stop_requested")
            return []
        actions = self._dispatch(observation, now)
        if actions:
            self._actions_emitted += len(actions)
        return actions

    def _dispatch(self, observation: Observation, now: float) -> list[Action]:
        state = self._state
        handler = getattr(self, f"_handle_{state.name.lower()}", None)
        if handler is None:
            self._logger.error("状态 %s 没有处理器，进入异常恢复", state)
            self._transition(State.ERROR_RECOVERY, f"no_handler:{state}")
            return []
        if state not in (State.PAUSED, State.STOPPING, State.ERROR_RECOVERY):
            paused = self._check_runtime_guards(observation, now)
            if paused:
                return []
        timeout_reason = self._check_timeout(now)
        if timeout_reason is not None:
            if state is State.INIT:
                self._transition(State.PAUSED, timeout_reason)
            elif state in (State.STOPPING, State.PAUSED):
                pass
            else:
                self._transition(State.ERROR_RECOVERY, timeout_reason)
            handler = getattr(self, f"_handle_{self._state.name.lower()}", None)
            if handler is None:
                return []
        return handler(observation, now)

    # ------------------------------------------------------------------
    # 守卫与超时
    # ------------------------------------------------------------------
    def _check_runtime_guards(self, observation: Observation, now: float) -> bool:
        if observation.window_missing:
            self.request_pause("window_lost", observation.capture_error or "")
            return True
        info = self._window_info
        if info is not None and not info.available:
            self.request_pause("window_unavailable", "")
            return True
        if (
            info is not None
            and self.config.global_config.safety.pause_when_unfocused
            and not info.foreground
            and self._state.is_battle
        ):
            self.request_pause("window_unfocused", "")
            return True
        return False

    def _check_timeout(self, now: float) -> str | None:
        timeouts = self.config.global_config.timeouts
        mapping: dict[State, tuple[float, str]] = {
            State.INIT: (timeouts.init_s, "init_timeout"),
            State.PORT: (timeouts.port_state_s, "port_timeout"),
            State.START_COOP: (timeouts.start_coop_s, "start_coop_timeout"),
            State.QUEUEING: (timeouts.queue_s, "queue_timeout"),
            State.LOADING: (timeouts.loading_s, "loading_timeout"),
            State.BATTLE_FINISHED: (timeouts.battle_finished_s, "battle_finished_timeout"),
            State.DEAD_PENDING_EXIT: (timeouts.dead_wait_s, "dead_pending_timeout"),
        }
        entry = mapping.get(self._state)
        if entry is None:
            return None
        limit, reason = entry
        if limit > 0 and now - self._entered_at > limit:
            return reason
        return None

    # ------------------------------------------------------------------
    # 状态切换
    # ------------------------------------------------------------------
    def _transition(self, target: State, reason: str = "") -> None:
        if target is self._state:
            return
        previous = self._state
        self._state = target
        self._entered_at = self._now
        self._transitions.append((self._now, previous, target, reason))
        self._logger.info("状态 %s -> %s (%s)", previous.value, target.value, reason)
        self._log_state(target, reason, extra={"from": previous.value})
        self.deps.perception.set_state(target)
        enter = getattr(self, f"_enter_{target.name.lower()}", None)
        if enter is not None:
            try:
                enter(self._now, reason)
            except Exception as exc:  # 进入动作不允许把主循环打崩
                self._logger.exception("进入状态 %s 失败: %s", target.value, exc)

    def _log_state(self, state: State, reason: str = "", extra: dict[str, Any] | None = None) -> None:
        detail: dict[str, Any] = {"reason": reason}
        if extra:
            detail.update(extra)
        try:
            self.deps.events.log_state(self._session_id, self._battle_id, state.value, None, detail)
        except Exception as exc:  # 日志失败不阻塞主流程
            self._logger.debug("写状态事件失败: %s", exc)

    def _log_action(self, action_type: str, detail: dict[str, Any]) -> None:
        try:
            self.deps.events.log_action(self._battle_id, action_type, detail, self._random_profile_id())
        except Exception as exc:
            self._logger.debug("写动作事件失败: %s", exc)

    def _random_profile_id(self) -> int | None:
        return self._battle.random_profile_id if self._battle else None

    # ------------------------------------------------------------------
    # 子流程控制
    # ------------------------------------------------------------------
    def _stop_subflows(self) -> None:
        try:
            self.deps.navigator.stop()
        except Exception:
            pass
        if self.deps.autopilot is not None:
            try:
                self.deps.autopilot.stop()
            except Exception:
                pass
        try:
            self.deps.behavior.stop()
        except Exception:
            pass
        self.deps.steps.reset()

    def _advance_steps(self, observation: Observation, now: float) -> tuple[StepStatus, list[Action]]:
        try:
            return self.deps.steps.advance(now, observation)
        except Exception as exc:
            self._logger.exception("步骤执行器异常: %s", exc)
            return StepStatus.FAILED, []

    # ==================================================================
    # INIT
    # ==================================================================
    def _enter_init(self, now: float, reason: str) -> None:
        self._stop_subflows()
        # ISS-006：离开战斗上下文但未走完结算时必须显式收尾，不能静默清空 battle_id
        self._abort_battle(now, f"enter_init:{reason}")
        self._battle_result = ""
        self._exit_method = "none"
        self._exit_error = None

    def _handle_init(self, observation: Observation, now: float) -> list[Action]:
        detected = self._safe_detect_state(now)
        if detected is None:
            return []
        if detected is State.PORT:
            self._transition(State.PORT, "init_port")
        elif detected is State.QUEUEING:
            self._begin_battle_record(now, map_id=None)
            self._transition(State.QUEUEING, "init_queueing")
        elif detected in (State.LOADING, State.BATTLE_ACTIVE, State.BATTLE_FINISHED, State.DEAD_PENDING_EXIT):
            self.request_pause("restart_mid_battle", f"detected={detected.value}")
        elif detected is State.POST_EXIT_INTERFACE:
            self._transition(State.POST_EXIT_INTERFACE, "init_popup")
        elif detected in (State.EXIT_MENU_OPEN, State.EXIT_CONFIRMING):
            self._transition(State.ERROR_RECOVERY, "init_exit_menu")
        else:
            self._transition(State.PORT, f"init_{detected.value.lower()}")
        return []

    def _safe_detect_state(self, now: float) -> State | None:
        try:
            return self.deps.perception.detect_state(now)
        except Exception as exc:
            self._logger.debug("状态识别失败: %s", exc)
            return None

    # ==================================================================
    # PORT（准备开局）
    # ==================================================================
    def _enter_port(self, now: float, reason: str) -> None:
        self._stop_subflows()
        self._abort_battle(now, f"enter_port:{reason}")
        self._battle_result = ""
        self._exit_method = "none"
        self._exit_error = None
        self._port_phase = "idle"
        # 本会话尚未完成任何战斗（含暂停恢复后的重新进港口）时不等待；
        # 只有“两场之间”才使用随机冷却
        between = 0.0 if self._battles_completed == 0 else self.deps.session.sample_between_battle_s()
        self._port_wait_until = now + between
        self._port_start_attempts = 0
        self._port_loadout_started = False
        self.deps.ship_checker.reset()
        self._log_action("state_change", {"to": "PORT", "between_battle_s": round(between, 2)})

    def _handle_port(self, observation: Observation, now: float) -> list[Action]:
        phase = self._port_phase
        if phase == "idle":
            stop = self.deps.session.check_stop()
            if stop is not None:
                self.request_stop(str(stop.reason), stop.detail)
                return []
            if self._rotation_limit is not None and self._battles_completed >= self._rotation_limit:
                self.request_stop("account_rotation", f"本账号已完成 {self._battles_completed} 场")
                return []
            if now < self._port_wait_until:
                return []
            # ISS-007：先确认已选中目标船，再检查该船信号旗，避免用上一艘船的装备页结果开局
            if self.deps.ship_checker.verify_each_battle:
                self._port_phase = "verify"
            else:
                self._port_phase = "start"
            return []
        if phase == "loadout":
            # 装备页信号旗校验：切到装备页 → 等待“信号旗 0/8”空槽特征
            sequence = self.config.profile.ship_check_sequence
            if not sequence:
                self._port_phase = "start"
                return []
            if not self._port_loadout_started:
                self._port_loadout_started = True
                try:
                    self.deps.steps.start(sequence, label="loadout_check")
                except Exception as exc:
                    self._port_phase = "start"
                    self._logger.warning("装备校验序列启动失败: %s", exc)
                    return []
            status, actions = self._advance_steps(observation, now)
            if status is StepStatus.DONE:
                self._port_phase = "start"
                self._log_action("loadout_verify", {"result": "ok", "signal_flags": 0})
            elif status is StepStatus.FAILED:
                reason = getattr(self.deps.steps, "last_error", None)
                detail = f"信号旗未清空或装备页不可达: {reason}"
                self._exit_error = str(detail)
                self.request_pause("loadout_check_failed", detail)
            return actions
        if phase == "verify":
            if not self.deps.ship_checker.verify_each_battle:
                self._port_phase = "start"
                return []
            status, actions = self.deps.ship_checker.tick(now, observation)
            if status is ShipCheckStatus.OK:
                # 船已确认选中：之后才检查该船的信号旗
                self._log_action("ship_verify", {"result": "ok", "ship_id": self.config.ship.ship_id})
                self._port_phase = "loadout" if self.config.profile.ship_check_sequence else "start"
                return actions
            if status is ShipCheckStatus.FAILED:
                self._exit_error = self.deps.ship_checker.failure_reason
                self.request_pause("ship_check_failed", self.deps.ship_checker.failure_reason)
                return actions
            return actions
        if phase == "start":
            self._port_phase = "starting"
            self._port_start_attempts += 1
            sequence = self.config.profile.start_sequence
            if not sequence:
                self._on_start_failed(now, "没有配置 start_sequence")
                return []
            try:
                self.deps.steps.start(sequence, label="start_fast")
            except Exception as exc:
                self._on_start_failed(now, f"开局序列启动失败: {exc}")
            return []
        if phase == "starting":
            status, actions = self._advance_steps(observation, now)
            if status is StepStatus.DONE:
                self.deps.session.record_start_success()
                self._transition(State.START_COOP, "start_sequence_done")
            elif status is StepStatus.FAILED:
                self._port_phase = "start_full"
                sequence = self.config.profile.start_sequence_full or self.config.profile.start_sequence
                if not sequence:
                    self._on_start_failed(now, "快速路径失败且没有完整开局路径")
                else:
                    try:
                        self.deps.steps.start(sequence, label="start_full")
                    except Exception as exc:
                        self._on_start_failed(now, f"完整开局序列启动失败: {exc}")
            return actions
        if phase == "start_full":
            status, actions = self._advance_steps(observation, now)
            if status is StepStatus.DONE:
                self.deps.session.record_start_success()
                self._transition(State.START_COOP, "start_full_done")
            elif status is StepStatus.FAILED:
                self._on_start_failed(now, "完整开局路径失败")
            return actions
        return []

    def _on_start_failed(self, now: float, detail: str) -> None:
        self._log_action("recovery", {"kind": "start_failed", "detail": detail})
        stop = self.deps.session.record_start_failure(detail)
        if stop is not None:
            self.request_stop(str(stop.reason), stop.detail)
            return
        self._transition(State.ERROR_RECOVERY, f"start_failed:{detail}")

    # ==================================================================
    # START_COOP / QUEUEING / LOADING
    # ==================================================================
    def _handle_start_coop(self, observation: Observation, now: float) -> list[Action]:
        if self._observed_feature(observation, "queue_indicator"):
            self._begin_battle_record(now, map_id=None)
            self._transition(State.QUEUEING, "queue_detected")
        return []

    def _enter_queueing(self, now: float, reason: str) -> None:
        if self._battle_id is not None:
            try:
                self.deps.events.mark_queued(self._battle_id)
            except Exception:
                pass

    def _handle_queueing(self, observation: Observation, now: float) -> list[Action]:
        # 加载界面模板可缺省：直接检测战斗 HUD，排队结束后一步进入战斗（用户确认可跳过加载界面）
        if self._battle_hud_visible(observation):
            self._transition(State.BATTLE_ACTIVE, "battle_hud_detected_from_queue")
            return []
        if self._observed_feature(observation, "loading_indicator") or self._observed_feature(
            observation, "loading_tip_area"
        ):
            self._transition(State.LOADING, "loading_detected")
        return []

    def _battle_hud_visible(self, observation: Observation) -> bool:
        return self._observed_feature(observation, "battle_hud_indicator") or self._observed_feature(
            observation, "hp_bar_present"
        )

    def _handle_loading(self, observation: Observation, now: float) -> list[Action]:
        if self._battle_hud_visible(observation):
            self._transition(State.BATTLE_ACTIVE, "battle_hud_detected")
        return []

    # ==================================================================
    # BATTLE_ACTIVE
    # ==================================================================
    def _enter_battle_active(self, now: float, reason: str) -> None:
        self._stop_subflows()
        # ISS-006：同一场次内从暂停恢复时保留 _battle/_battle_id，不重建场次上下文
        resuming = self._battle is not None and self._battle_id is not None
        if self._battle is None:
            self._battle = _BattleRuntime(started_at=now, seed=self._rng.randrange(2**31))
        else:
            self._battle.nav_started = False
            self._battle.behavior_started = False
            if self._autopilot_mode():
                self._battle.bootstrapped = True
            else:  # waypoints 恢复需要重新识别地图/出生点
                self._battle.bootstrapped = False
        try:
            # 恢复时重置死亡判定：必须重新积累“血条归零”证据才能再次授权退出
            self.deps.perception.begin_battle(now)
        except Exception as exc:
            self._logger.warning("感知引擎 begin_battle 失败: %s", exc)
        self._log_action("state_change", {"to": "BATTLE_ACTIVE", "resumed": resuming})
        if resuming:
            self._resume_battle_flows(now)

    def _resume_battle_flows(self, now: float) -> None:
        """战斗内暂停恢复：重启自动驾驶与随机行为，保留原 battle_id/场次记录。"""
        battle = self._battle
        if battle is None:
            return
        if self._autopilot_mode():
            autopilot = self.deps.autopilot
            if autopilot is not None:
                try:
                    autopilot.start(now)
                    battle.nav_started = True
                    self._log_action("autopilot_resume", {"mode": "autopilot"})
                except Exception as exc:
                    self._logger.warning("autopilot 恢复失败: %s", exc)
            else:
                self._logger.warning("autopilot 模式但未注入自动导航器，恢复后导航不启动")
        self._start_behavior(now, battle, persist=False)

    def _handle_battle_active(self, observation: Observation, now: float) -> list[Action]:
        # 结算优先于死亡（评审 P1-5）
        if self._result_visible(observation, now):
            self._battle_result = "finished"
            self._stop_subflows()
            self._transition(State.BATTLE_FINISHED, "battle_results")
            return []
        # ISS-002：只有“血条确认归零”的证据才授权提前退出；
        # 死亡提示/血条名称消失/标记丢失不能单独作为退出授权。
        if self._exit_authorized(observation, now):
            death = observation.death
            self._battle_result = "dead"
            if self._battle_id is not None:
                try:
                    self.deps.events.mark_death(self._battle_id)
                except Exception:
                    pass
                self._log_action("death_detected", death.to_dict() if death is not None else {})
            self._save_diagnostic("death", now)
            self._transition(State.DEAD_PENDING_EXIT, "death_detected")
            return []
        actions = self._bootstrap_battle(observation, now)
        battle = self._battle
        if battle is not None and battle.nav_started:
            if self._autopilot_mode():
                actions.extend(self._safe_tick_autopilot(observation, now))
            else:
                actions.extend(self._safe_tick_navigator(observation, now))
        # ISS-001：autopilot 打开全屏地图点选期间屏蔽随机行为，避免误触其它战斗输入
        autopilot_blocking = bool(getattr(self.deps.autopilot, "blocks_behavior", False))
        if battle is not None and battle.behavior_started and not autopilot_blocking:
            actions.extend(self._safe_tick_behavior(observation, now))
        return actions

    def _autopilot_mode(self) -> bool:
        return str(getattr(self.config.global_config.navigation, "mode", "autopilot")) == "autopilot"

    def _result_visible(self, observation: Observation, now: float) -> bool:
        feature = self.profile.battle_hud.result_feature or _RESULT_FEATURE_FALLBACK
        if observation.found(feature):
            return True
        try:
            return self.deps.perception.match_feature(feature, now=now).found
        except Exception:
            return False

    def _bootstrap_battle(self, observation: Observation, now: float) -> list[Action]:
        """战斗内启动：autopilot 模式只启动自动驾驶与随机行为；waypoints 模式识别地图/航线。"""
        battle = self._battle
        if battle is None or battle.bootstrapped:
            return []
        if self._autopilot_mode():
            battle.bootstrapped = True
            self._start_behavior(now, battle)
            autopilot = self.deps.autopilot
            if autopilot is not None:
                try:
                    autopilot.start(now)
                    battle.nav_started = True
                    self._log_action("autopilot_start", {"mode": "autopilot", "target": self.config.global_config.navigation.autopilot_target})
                except Exception as exc:
                    self._logger.warning("autopilot 启动失败: %s", exc)
                    battle.nav_started = False
            else:
                self._logger.warning("autopilot 模式但未注入自动导航器，导航不启动")
            if self._battle_id is not None:
                try:
                    self.deps.events.set_battle_context(self._battle_id)
                except Exception:
                    pass
            return []
        config = self.config.global_config
        forced = config.map.forced_map_id
        match = None
        if forced and forced in self.config.maps:
            # 强制地图：直接构造标定（调试/标定用）
            match = self._force_map(observation, forced, now)
        else:
            try:
                match = self.deps.perception.identify_map(now)
            except Exception as exc:
                self._logger.warning("地图识别异常: %s", exc)
                match = None
        if match is None:
            battle.map_attempts += 1
            if now - battle.started_at > 15.0 and not battle.warning_logged:
                battle.warning_logged = True
                battle.bootstrapped = True
                self._logger.warning("地图识别超时，导航不启动，仅保留死亡监测与随机行为")
                self._log_action("recovery", {"kind": "map_identify_timeout"})
            return []
        pack = self.config.maps.get(match.map_id)
        calibration = self.deps.perception.calibration
        if pack is None or calibration is None:
            battle.bootstrapped = True
            self._logger.warning("地图 %s 缺包或标定失败，导航不启动", match.map_id)
            return []
        battle.map_id = match.map_id
        battle.calibration = calibration
        spawn_id = self._identify_spawn(observation, pack)
        battle.spawn_id = spawn_id
        route = None
        if spawn_id is not None:
            try:
                route = select_route_for_map(pack, spawn_id, seed=battle.seed)
            except Exception as exc:
                self._logger.warning("选择航线失败: %s", exc)
        battle.bootstrapped = True
        if route is None or calibration is None:
            if spawn_id is not None:
                self._logger.warning("出生点 %s 没有可用航线，导航不启动", spawn_id)
                self._log_action("nav_abandon", {"reason": "no_route", "spawn_id": spawn_id})
            return []
        battle.route_id = route.route_id
        try:
            self.deps.navigator.start(route, calibration, now)
            battle.nav_started = True
        except Exception as exc:
            self._logger.warning("导航启动失败: %s", exc)
        self._start_behavior(now, battle)
        if self._battle_id is not None:
            try:
                self.deps.events.set_battle_context(
                    self._battle_id,
                    map_id=battle.map_id,
                    spawn_id=battle.spawn_id,
                    route_id=battle.route_id,
                )
            except Exception:
                pass
        self._log_action(
            "nav_select_route",
            {"map_id": battle.map_id, "spawn_id": battle.spawn_id, "route_id": battle.route_id},
        )
        return []

    def _start_behavior(self, now: float, battle: _BattleRuntime, *, persist: bool = True) -> None:
        from ..behavior.random_profile import RandomProfile

        try:
            profile = RandomProfile.generate(self.config.global_config.behavior, seed=battle.seed)
            self.deps.behavior.start(profile, now)
            battle.behavior_started = True
            if (
                persist
                and self._battle_id is not None
                and battle.random_profile_id is None
            ):
                try:
                    battle.random_profile_id = self.deps.events.save_random_profile(self._battle_id, profile.to_dict())
                except Exception as exc:
                    self._logger.debug("保存随机档案失败: %s", exc)
        except Exception as exc:
            self._logger.warning("随机行为启动失败: %s", exc)

    def _force_map(self, observation: Observation, map_id: str, now: float) -> Any:
        try:
            return self.deps.perception.force_map(map_id, now)
        except Exception as exc:
            self._logger.debug("强制地图标定失败: %s", exc)
            return None

    def _identify_spawn(self, observation: Observation, pack: MapPack) -> str | None:
        config = self.config.global_config.map
        ship = observation.ship
        best_id: str | None = None
        best_distance = float("inf")
        if ship is not None and ship.world_xy is not None:
            for spawn in pack.spawns:
                distance = _distance(ship.world_xy, spawn.position)
                if distance < best_distance:
                    best_distance = distance
                    best_id = spawn.spawn_id
            if best_id is not None and best_distance <= float(config.spawn_tolerance_m):
                return best_id
            if best_id is not None:
                self._logger.warning(
                    "出生点识别不确定：最近 %s 距离 %.0f 米 > 容差 %.0f 米", best_id, best_distance, config.spawn_tolerance_m
                )
                self._log_action("recovery", {"kind": "spawn_tolerance", "spawn_id": best_id, "distance_m": best_distance})
                return best_id
        fallback = config.default_spawn_id
        if fallback:
            return fallback
        self._logger.warning("无法识别出生点，使用地图第一个出生点")
        return pack.spawns[0].spawn_id if pack.spawns else None

    def _safe_tick_navigator(self, observation: Observation, now: float) -> list[Action]:
        try:
            return list(self.deps.navigator.tick(observation, now))
        except Exception as exc:
            self._logger.exception("导航子流程异常: %s", exc)
            self._log_action("recovery", {"kind": "subflow_error", "subflow": "navigator", "error": str(exc)})
            try:
                self.deps.navigator.stop()
            except Exception:
                pass
            if self._battle is not None:
                self._battle.nav_started = False
            return []

    def _safe_tick_autopilot(self, observation: Observation, now: float) -> list[Action]:
        if self.deps.autopilot is None:
            return []
        try:
            return list(self.deps.autopilot.tick(observation, now))
        except Exception as exc:
            self._logger.exception("autopilot 子流程异常: %s", exc)
            self._log_action("recovery", {"kind": "subflow_error", "subflow": "autopilot", "error": str(exc)})
            try:
                self.deps.autopilot.stop()
            except Exception:
                pass
            if self._battle is not None:
                self._battle.nav_started = False
            return []

    def _safe_tick_behavior(self, observation: Observation, now: float) -> list[Action]:
        try:
            from ..behavior.scheduler import BehaviorContext

            info = self._window_info
            ctx = BehaviorContext(
                in_battle=True,
                modal_visible=self._modal_visible(observation),
                foreground=info.foreground if info is not None else True,
                window_rect=Rect(*observation.window_rect),
            )
            return list(self.deps.behavior.tick(observation, now, ctx))
        except Exception as exc:
            self._logger.exception("随机行为子流程异常: %s", exc)
            self._log_action("recovery", {"kind": "subflow_error", "subflow": "behavior", "error": str(exc)})
            try:
                self.deps.behavior.stop()
            except Exception:
                pass
            if self._battle is not None:
                self._battle.behavior_started = False
            return []

    def _modal_visible(self, observation: Observation) -> bool:
        for feature_id in self.profile.popup_features:
            if observation.found(feature_id):
                return True
        return self._observed_feature(observation, "exit_menu_panel") or self._observed_feature(
            observation, "exit_confirm_dialog"
        )

    def _stop_subflows_and_return(self) -> list[Action]:
        self._stop_subflows()
        return []    # ==================================================================
    # BATTLE_FINISHED
    # ==================================================================
    def _enter_battle_finished(self, now: float, reason: str) -> None:
        self._stop_subflows()
        self._result_continue_clicked = False
        self._result_choice_until = now + 20.0  # 结算按钮等待上限（结算动画/网络延迟）
        sequence = self.config.profile.result_sequence
        if sequence:
            try:
                self.deps.steps.start(sequence, label="battle_result")
            except Exception as exc:
                self._logger.warning("结算序列启动失败: %s", exc)

    def _handle_battle_finished(self, observation: Observation, now: float) -> list[Action]:
        status, actions = self._advance_steps(observation, now)
        if status is StepStatus.DONE:
            done_actions = self._choose_result_button(observation, now)
            return actions + done_actions
        if status is StepStatus.FAILED:
            self._transition(State.ERROR_RECOVERY, "result_sequence_failed")
        return actions

    def _choose_result_button(self, observation: Observation, now: float) -> list[Action]:
        """结算页：有“继续战斗！”直接点击（下一局），否则点“回到港口”（2026-10-31）。"""
        hud = self.profile.battle_hud
        continue_feature = hud.result_continue_feature
        if continue_feature:
            match = self._match_feature(continue_feature)
            if match is not None and match.found:
                self._result_continue_clicked = True
                self._mark_exit_requested()
                self._log_action("result_step", {"step": "continue_battle", "feature": continue_feature})
                self._transition(State.EXITING, "result_continue_click")
                return [_click_action(match, observation, "result_continue")]
        return_feature = hud.result_return_feature
        if return_feature:
            match = self._match_feature(return_feature)
            if match is not None and match.found:
                self._mark_exit_requested()
                self._log_action("result_step", {"step": "return_port", "feature": return_feature})
                self._transition(State.EXITING, "result_return_click")
                return [_click_action(match, observation, "result_return_port")]
        # 按钮尚未出现（结算动画/网络延迟）：短暂重试，避免误入异常恢复
        if now >= self._result_choice_until:
            self._transition(State.ERROR_RECOVERY, "result_buttons_missing")
        return []

    # ==================================================================
    # DEAD_PENDING_EXIT
    # ==================================================================
    def _enter_dead_pending_exit(self, now: float, reason: str) -> None:
        self._stop_subflows()
        wait = self._rng.uniform(0.8, 2.0)
        self._dead_wait_until = now + wait
        self._log_action("state_change", {"to": "DEAD_PENDING_EXIT", "wait_s": round(wait, 2)})

    def _handle_dead_pending_exit(self, observation: Observation, now: float) -> list[Action]:
        if now < self._dead_wait_until:
            return []
        # ISS-002：发送退出操作前再次校验“血条确认归零”证据仍在有效期内；
        # 出现存活/未知证据时撤销尚未执行的退出，回到战斗继续等待自然结算。
        if not self._exit_authorized(observation, now):
            if self._result_visible(observation, now):
                self._battle_result = "finished"
                self._transition(State.BATTLE_FINISHED, "exit_revoked_result")
            else:
                self._log_action(
                    "exit_revoked",
                    {"reason": "hp_evidence_not_authorized", "death": observation.death.to_dict() if observation.death else None},
                )
                self._transition(State.BATTLE_ACTIVE, "exit_revoked")
            return []
        self._transition(State.EXIT_MENU_OPEN, "dead_wait_done")
        return []

    # ==================================================================
    # EXIT_MENU_OPEN / EXIT_CONFIRMING / EXITING
    # ==================================================================
    def _enter_exit_menu_open(self, now: float, reason: str) -> None:
        exit_cfg = self.profile.exit
        key = self.deps.config.keymap.resolve(exit_cfg.menu_key)
        self._exit_retried = False
        self._log_action("exit_step", {"state": "EXIT_MENU_OPEN", "key": key})
        self._pending_exit_actions = [Action.key_down(key, label="exit_menu"), Action.key_up(key, label="exit_menu")]

    def _handle_exit_menu_open(self, observation: Observation, now: float) -> list[Action]:
        actions = self._pending_exit_actions
        self._pending_exit_actions = []
        exit_cfg = self.profile.exit
        # 部分客户端死亡后可直接离开战斗
        direct = self._match_feature(exit_cfg.direct_exit_feature)
        if direct is not None and direct.found:
            self._exit_method = "direct"
            self._mark_exit_requested()
            self._transition(State.EXITING, "direct_exit")
            return actions + [_click_action(direct, observation, "exit_direct")]
        menu = self._match_feature(exit_cfg.menu_open_feature)
        if menu is not None and menu.found:
            target = self._match_feature(exit_cfg.return_port_feature)
            if target is not None and target.found:
                self._exit_method = "menu"
                self._mark_exit_requested()
                self._transition(State.EXIT_CONFIRMING, "return_port_click")
                return actions + [_click_action(target, observation, "exit_return_port")]
            if now - self._entered_at > 1.0:
                self._log_action("exit_step", {"step": "return_port_missing"})
                self._transition(State.ERROR_RECOVERY, "return_port_missing")
            return actions
        elapsed = now - self._entered_at
        if not self._exit_retried and elapsed > 1.2:
            self._exit_retried = True
            key = self.deps.config.keymap.resolve(exit_cfg.menu_key)
            self._log_action("exit_step", {"step": "retry_menu_key", "elapsed_s": round(elapsed, 2)})
            return actions + [Action.key_down(key, label="exit_menu_retry"), Action.key_up(key, label="exit_menu_retry")]
        timeout = float(self.config.global_config.timeouts.exit_menu_s)
        if elapsed > timeout:
            self._transition(State.ERROR_RECOVERY, "exit_menu_timeout")
        return actions

    def _enter_exit_confirming(self, now: float, reason: str) -> None:
        self._confirm_seen = False
        if not self.profile.exit.confirm_feature:
            self._transition(State.EXITING, "no_confirm_dialog")
            return
        exit_cfg = self.profile.exit
        self._pending_confirm_key = self.deps.config.keymap.resolve(exit_cfg.menu_key)

    def _handle_exit_confirming(self, observation: Observation, now: float) -> list[Action]:
        exit_cfg = self.profile.exit
        confirm = self._match_feature(exit_cfg.confirm_feature)
        if confirm is not None and confirm.found:
            self._confirm_seen = True
            self._exit_method = "confirm"
            self._transition(State.EXITING, "confirm_click")
            return [_click_action(confirm, observation, "exit_confirm")]
        timeout = float(self.config.global_config.timeouts.exit_confirm_s)
        if now - self._entered_at > timeout:
            # 没有确认框：按“点击返回港口后直接退出”处理（详细设计 6.6）
            self._transition(State.EXITING, "no_confirm_dialog")
        return []

    def _handle_exiting(self, observation: Observation, now: float) -> list[Action]:
        # 结算页“继续战斗！”：游戏会直接回到排队界面（不再经过港口）
        if self._result_continue_clicked and self._observed_feature(observation, "queue_indicator"):
            self._result_continue_clicked = False
            self._mark_returned_to_port()
            self._log_action("result_step", {"step": "continue_battle_queued"})
            self._finish_battle(
                now, next_state=State.START_COOP, reason="battle_finished_continue"
            )
            return []
        if self._port_visible(observation, now):
            self._mark_returned_to_port()
            self._transition(State.POST_EXIT_INTERFACE, "port_detected")
            return []
        for feature_id in self.profile.popup_features:
            if observation.found(feature_id):
                self._mark_returned_to_port()
                self._transition(State.POST_EXIT_INTERFACE, "popup_detected")
                return []
        timeout = float(self.config.global_config.timeouts.exit_s)
        if now - self._entered_at > timeout:
            self._save_diagnostic("exit_timeout", now)
            self._transition(State.ERROR_RECOVERY, "exit_timeout")
        return []

    # ==================================================================
    # POST_EXIT_INTERFACE
    # ==================================================================
    def _enter_post_exit_interface(self, now: float, reason: str) -> None:
        self._stop_subflows()
        self._popup_rounds = 0
        self._popup_wait_until = 0.0

    def _handle_post_exit_interface(self, observation: Observation, now: float) -> list[Action]:
        if now < self._popup_wait_until:
            return []
        popup = self._popup_visible(observation)
        if popup:
            if self._popup_rounds >= 5:
                self._transition(State.ERROR_RECOVERY, "popup_limit")
                return []
            self._popup_rounds += 1
            sequence = self.config.profile.popup_sequence
            if sequence:
                try:
                    self.deps.steps.start(sequence, label=f"popup_{self._popup_rounds}")
                    self._popup_wait_until = now + 0.3
                    return []
                except Exception as exc:
                    self._logger.warning("弹窗清理序列启动失败: %s", exc)
            self._popup_wait_until = now + 0.5
            return []
        if self._port_visible(observation, now):
            self._finish_battle(now)
            return []
        timeout = float(self.config.global_config.timeouts.popup_s)
        if now - self._entered_at > timeout:
            self._transition(State.ERROR_RECOVERY, "popup_timeout")
        return []

    def _popup_visible(self, observation: Observation) -> bool:
        if any(observation.found(feature_id) for feature_id in self.profile.popup_features):
            return True
        for feature_id in self.profile.popup_features:
            try:
                if self.deps.perception.match_feature(feature_id).found:
                    return True
            except Exception:
                continue
        return False

    def _finish_battle(
        self,
        now: float,
        *,
        next_state: State = State.PORT,
        reason: str = "battle_finished_cleanup",
    ) -> None:
        battle_id = self._battle_id
        result = self._battle_result or "finished"
        if battle_id is not None:
            try:
                self.deps.events.end_battle(battle_id, result, self._exit_method, self._exit_error)
                self.deps.screenshots.end_battle(battle_id)
            except Exception as exc:
                self._logger.debug("结束战斗记录失败: %s", exc)
        self._battles_completed += 1
        try:
            self.deps.session.end_battle(result)
        except Exception:
            pass
        self._battle_id = None
        self._battle = None
        self._notify_battle_context(None)
        # 下一局直接排队（继续战斗）时也要遵守会话时长/场次/轮换限制（2026-10-31）
        stop = self.deps.session.check_stop()
        if stop is not None:
            self.request_stop(str(stop.reason), stop.detail)
            return
        if self._rotation_limit is not None and self._battles_completed >= self._rotation_limit:
            self.request_stop("account_rotation", f"本账号已完成 {self._battles_completed} 场")
            return
        self._transition(next_state, reason)

    # ==================================================================
    # ERROR_RECOVERY
    # ==================================================================
    def _enter_error_recovery(self, now: float, reason: str) -> None:
        self._stop_subflows()
        self._recovery_step = 0
        self._recovery_times.append(now)
        self._log_action("recovery", {"kind": "enter", "reason": reason, "attempts_10min": len(self._recovery_times)})

    def _handle_error_recovery(self, observation: Observation, now: float) -> list[Action]:
        max_recoveries = int(self.config.global_config.session.recovery_max_per_10min)
        recent = [moment for moment in self._recovery_times if now - moment <= 600.0]
        self._recovery_times = deque(recent, maxlen=16)
        if max_recoveries > 0 and len(recent) >= max_recoveries:
            self.request_pause("recovery_frequency", f"{len(recent)} 次/10 分钟")
            return []
        detected = self._safe_detect_state(now)
        if detected is State.PORT:
            self._log_action("recovery", {"kind": "success", "state": "PORT"})
            self._transition(State.PORT, "recovery_port")
            return []
        if detected is State.POST_EXIT_INTERFACE:
            self._transition(State.POST_EXIT_INTERFACE, "recovery_popup")
            return []
        if detected in (State.BATTLE_ACTIVE, State.BATTLE_FINISHED, State.DEAD_PENDING_EXIT):
            self.request_pause("recovery_mid_battle", f"detected={detected.value}")
            return []
        actions: list[Action] = []
        if self._recovery_step == 0:
            self._recovery_step = 1
            return actions
        if self._recovery_step == 1:
            # 第二步：尝试关闭已知弹窗（详细设计 6.4）
            close_feature = self._popup_close_feature()
            close = self._match_feature(close_feature)
            if close is not None and close.found and observation.window_rect:
                actions.append(_click_action(close, observation, "recovery_popup_close"))
                self._recovery_wait_until = now + 0.5
                self._recovery_step = 2
                return actions
            self._recovery_step = 2
            return actions
        if self._recovery_step == 2:
            if now < self._recovery_wait_until:
                return []
            # ISS-002：战斗内未知画面不自动发送 ESC（可能是战斗 HUD 被遮挡/加载抖动），
            # 只重试识别或暂停，避免误开退出菜单。
            if self._battle_id is not None:
                self._logger.warning("战斗内未知画面，不发送 ESC，转入暂停等待人工确认")
                self._log_action("recovery", {"kind": "battle_unknown_interface", "action": "pause"})
                self.request_pause("battle_unknown_interface", "战斗内未知画面，未自动发送 ESC")
                return []
            # 第三步：按一次 ESC，等待菜单或状态变化
            key = self.deps.config.keymap.resolve(self.profile.exit.menu_key)
            actions.extend([Action.key_down(key, label="recovery_escape"), Action.key_up(key, label="recovery_escape")])
            self._recovery_step = 3
            self._recovery_wait_until = now + 1.0
            return actions
        # 第四步：等待并重新识别；超时后重新进入 INIT，连续 3 次失败则暂停
        if now < self._recovery_wait_until:
            return []
        timeout = float(self.config.global_config.timeouts.recovery_s)
        if now - self._entered_at > timeout:
            self._recovery_failures += 1
            if self._recovery_failures >= 3:
                self.request_pause("recovery_failed", "连续 3 次恢复失败")
            else:
                self._transition(State.INIT, "recovery_retry")
        return actions

    def _popup_close_feature(self) -> str | None:
        sequence = self.config.profile.popup_sequence
        for step in sequence:
            if step.feature:
                return step.feature
        return "popup_close_button"

    # ==================================================================
    # PAUSED / STOPPING
    # ==================================================================
    def _enter_paused(self, now: float, reason: str) -> None:
        self._stop_subflows()
        # ISS-004：暂停时关闭输入门禁并等待输入线程确认释放，之后不再执行排队动作
        executor = self.deps.executor
        close_fn = getattr(executor, "close_gate_and_release", None)
        try:
            if callable(close_fn):
                close_fn(f"pause:{reason}", wait=True, timeout=2.0)
            else:  # 兼容测试 fake
                executor.release_all()
        except Exception as exc:
            self._logger.warning("暂停时释放输入失败: %s", exc)
        self._save_diagnostic(f"pause_{_slug(reason)}", now)
        self._log_action("state_change", {"to": "PAUSED", "reason": reason})

    def _open_input_gate(self, reason: str) -> None:
        open_fn = getattr(self.deps.executor, "open_gate", None)
        if callable(open_fn):
            try:
                open_fn(reason)
            except Exception as exc:
                self._logger.debug("打开输入门禁失败: %s", exc)

    def _handle_paused(self, observation: Observation, now: float) -> list[Action]:
        if self._resume_requested:
            self._resume_requested = False
            self._logger.info("收到恢复指令，重新识别界面")
            self._resume_from_pause(observation, now, source="user")
            return []
        # 因窗口失去焦点/暂不可用而暂停时，窗口恢复后自动回到原流程（不需要再按 F11）
        if self._pause_reason in ("window_lost", "window_unfocused", "window_unavailable"):
            info = self._window_info
            if info is not None and info.available and info.foreground and not observation.window_missing:
                self._logger.info("游戏窗口已回到前台，自动恢复")
                self._resume_from_pause(observation, now, source="window")
        return []

    def _resume_from_pause(self, observation: Observation, now: float, *, source: str) -> None:
        """ISS-006 恢复分流：当前会话战斗保留场次上下文；新进程不接管已有战斗。"""
        detected = None if observation.window_missing else self._safe_detect_state(now)
        battle_id = self._battle_id
        battle_active = battle_id is not None and self._battle is not None and self._battle_result != "finished"
        if battle_active:
            if detected in (State.BATTLE_ACTIVE, State.QUEUEING, State.LOADING):
                self._log_action("resume", {"source": source, "mode": "battle", "detected": detected.value})
                self._transition(State.BATTLE_ACTIVE, f"resume_battle:{source}")
                self._open_input_gate("resume_battle")
                return
            if detected is State.BATTLE_FINISHED:
                self._battle_result = "finished"
                self._transition(State.BATTLE_FINISHED, f"resume_result:{source}")
                self._open_input_gate("resume_result")
                return
            if detected in (
                State.EXIT_MENU_OPEN,
                State.EXIT_CONFIRMING,
                State.EXITING,
                State.POST_EXIT_INTERFACE,
                State.DEAD_PENDING_EXIT,
            ):
                self._transition(detected, f"resume_exit:{source}")
                self._open_input_gate("resume_exit")
                return
            if detected is State.PORT:
                self._abort_battle(now, f"resume_port_interrupt:{source}")
                self._transition(State.PORT, f"resume_port:{source}")
                self._open_input_gate("resume_port")
                return
            self._stay_paused("resume_unknown", f"无法识别当前战斗画面（detected={detected}）", source)
            return
        # 无本进程战斗上下文：新进程不接管已开始的战斗
        if detected is State.PORT:
            self._transition(State.PORT, f"resume_port:{source}")
            self._open_input_gate("resume_port")
            return
        if detected is State.POST_EXIT_INTERFACE:
            self._transition(State.POST_EXIT_INTERFACE, f"resume_popup:{source}")
            self._open_input_gate("resume_popup")
            return
        if detected is State.BATTLE_FINISHED:
            self._battle_result = "finished"
            self._transition(State.BATTLE_FINISHED, f"resume_result:{source}")
            self._open_input_gate("resume_result")
            return
        if detected in (
            State.LOADING,
            State.BATTLE_ACTIVE,
            State.DEAD_PENDING_EXIT,
            State.EXIT_MENU_OPEN,
            State.EXIT_CONFIRMING,
            State.EXITING,
        ):
            self._stay_paused(
                "restart_mid_battle",
                "不接管本进程启动前已存在的战斗，等待自然结算或回港后再恢复",
                source,
            )
            return
        # 其余情况按全新流程从 INIT 重新识别（港口/排队/未知界面）
        self._transition(State.INIT, f"resume_{source}")
        self._open_input_gate("resume_init")

    def _stay_paused(self, reason: str, detail: str, source: str) -> None:
        """恢复尝试无法离开 PAUSED 时保持暂停；用户手动恢复需重新同步控制器暂停状态。"""
        self._pause_reason = reason
        if source == "user" and self._controller is not None:
            # 主循环已清除控制器暂停，若不写回会在本状态“无人暂停”却无法自动恢复
            try:
                self._controller.request_pause(reason, detail, hook="state_machine")
            except Exception:
                pass
        self._logger.warning("%s：%s", reason, detail)
        self._log_action("resume_refused", {"reason": reason, "detail": detail, "source": source})

    def _enter_stopping(self, now: float, reason: str) -> None:
        self._stop_subflows()
        stop = self._stop_request
        reason_text = str(getattr(stop, "reason", reason))
        detail = str(getattr(stop, "detail", ""))
        try:
            self.deps.executor.stop(drain=True)
        except Exception as exc:
            self._logger.warning("停止输入线程失败: %s", exc)
        self._abort_battle(now, f"stop:{reason_text}")
        try:
            if self._session_id is not None:
                self.deps.events.end_session(self._session_id, reason_text, self._battles_completed)
                self.deps.session.stop(reason_text, detail)
        except Exception as exc:
            self._logger.debug("写会话结束记录失败: %s", exc)
        self._finished = True
        self._log_action("session_stop", {"reason": reason_text, "detail": detail})

    def _handle_stopping(self, observation: Observation, now: float) -> list[Action]:
        return []

    # ==================================================================
    # 辅助
    # ==================================================================
    def _mark_exit_requested(self) -> None:
        if self._battle_id is None:
            return
        try:
            self.deps.events.mark_exit_requested(self._battle_id)
        except Exception:
            pass

    def _mark_returned_to_port(self) -> None:
        if self._battle_id is None:
            return
        try:
            self.deps.events.mark_returned_to_port(self._battle_id)
        except Exception:
            pass

    def _begin_battle_record(self, now: float, map_id: str | None) -> int | None:
        if self._battle_id is not None:
            return self._battle_id
        if self._session_id is None:
            return None
        try:
            self._battle_id = self.deps.events.begin_battle(self._session_id, map_id)
            self.deps.screenshots.begin_battle(self._battle_id)
            self.deps.session.begin_battle()
            self._notify_battle_context(self._battle_id)
            self._log_action("state_change", {"to": "battle_begin", "map_id": map_id})
        except Exception as exc:
            self._logger.warning("创建战斗记录失败: %s", exc)
            self._battle_id = None
        return self._battle_id

    def _abort_battle(self, now: float, reason: str) -> None:
        """ISS-006：离开战斗上下文但未走完结算时显式结束场次记录（幂等）。"""
        battle_id = self._battle_id
        if battle_id is not None:
            try:
                self.deps.events.end_battle(battle_id, "aborted", "interrupted", reason)
                self.deps.screenshots.end_battle(battle_id)
                self._log_action("battle_aborted", {"reason": reason})
            except Exception as exc:
                self._logger.debug("写中断战斗记录失败: %s", exc)
        self._battle_id = None
        self._battle = None
        self._battle_result = ""
        self._notify_battle_context(None)

    def _notify_battle_context(self, battle_id: int | None) -> None:
        callback = self.deps.on_battle_context
        if callback is None:
            return
        try:
            callback(battle_id)
        except Exception as exc:
            self._logger.debug("更新战斗上下文失败: %s", exc)

    def _exit_authorized(self, observation: Observation, now: float) -> bool:
        """ISS-002：判断是否已满足“血条确认归零”的提前退出门禁。

        只认感知层给出的 ``exit_authorized``（基于颜色无关的空条证据 + 连续帧 + TTL）；
        死亡提示、血条名称模板消失、小地图标记丢失都不能单独授权。
        """
        death = observation.death
        if death is None:
            return False
        if not bool(getattr(death, "exit_authorized", False)):
            return False
        # evidence_at 是单调时钟秒；0.0 也是合法证据时间（FakeClock 从 0 起步的测试），
        # “从未确认”由 exit_authorized=False 表达，这里只校验新鲜度。
        evidence_at = float(getattr(death, "evidence_at", 0.0))
        if evidence_at < 0:
            return False
        ttl = float(getattr(self.profile.battle_hud, "hp_bar_evidence_ttl_s", 1.0) or 0.0)
        return now - evidence_at <= max(ttl, 0.0) + 0.5

    def _port_visible(self, observation: Observation, now: float) -> bool:
        feature = self.profile.exit.port_feature or "port_indicator"
        if observation.found(feature):
            return True
        try:
            return self.deps.perception.match_feature(feature).found
        except Exception:
            return False

    def _observed_feature(self, observation: Observation, feature_id: str) -> bool:
        if observation.found(feature_id):
            return True
        try:
            return self.deps.perception.match_feature(feature_id).found
        except Exception:
            return False

    def _match_feature(self, feature_id: str | None, now: float | None = None) -> MatchResult | None:
        if not feature_id:
            return None
        if not self.deps.perception.has_feature(feature_id):
            return None
        try:
            return self.deps.perception.match_feature(feature_id, now=now)
        except Exception:
            return None

    def _save_diagnostic(self, kind: str, now: float | None = None) -> None:
        frame = getattr(self.deps.perception, "last_frame", None)
        if frame is None or frame.image is None:
            return
        try:
            self.deps.screenshots.save_diagnostic(f"{kind}", frame.image)
        except Exception as exc:
            self._logger.debug("保存诊断截图失败: %s", exc)

    def _tick_clock(self, now: float | None) -> float:
        self._now = self.deps.clock.now() if now is None else float(now)
        return self._now


def _click_action(match: MatchResult, observation: Observation, label: str) -> Action:
    if match.center is None:
        raise ValueError("匹配结果没有中心坐标")
    window = Rect(*observation.window_rect)
    return Action.click_abs(window.x + int(match.center[0]), window.y + int(match.center[1]), label=label)


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)[:40] or "unknown"
