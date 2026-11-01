"""过载监控器：把采样、纯状态机与 ``StopController``/安全事件接起来。

职责：

- 由 :class:`~src.execution.safety.SafetyWatchdog` 在 20 Hz 循环内按 1 Hz 调用
  :meth:`OverloadMonitor.poll`（也可由主循环内联驱动）；
- 采样 :class:`~src.common.sysmetrics.SystemMetricsSource`，交给
  :class:`~src.execution.overload_policy.OverloadPolicy` 判定；
- 按判定结果请求共享 :class:`~src.execution.safety.StopController` 的 pause/stop/resume，
  并返回 ``SafetyEvent`` 供看门狗 dispatch（F12/心跳路径完全不变）；
- 生成只读 :class:`~src.runtime.messages.PerformanceView` 供 GUI 性能面板展示；
- 任何异常只降级为“不可用/清窗”，不让看门狗线程静默死掉。

``mode="monitor"`` 时状态机照常“演练”（记录本应暂停/停止），监控器**不**调用控制器的
pause/stop/resume，也不产生 pause/stop 事件——只更新快照与日志。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ..common.clock import Clock, SystemClock
from ..common.sysmetrics import MetricsCheck, MetricsSampler, SystemSample, create_metrics_source
from ..config.models import OverloadConfig
from .overload_policy import (
    STATE_NORMAL,
    EffectiveThresholds,
    OverloadAction,
    OverloadDecision,
    OverloadPolicy,
)
from .safety import SafetyEvent, SafetyEventKind, StopController

if TYPE_CHECKING:  # 只读契约；运行时延迟导入，避免与 src.runtime 包初始化互相依赖
    from ..runtime.messages import PerformanceView

logger = logging.getLogger(__name__)

#: 过载采样/判定频率（秒），与 PerformanceThresholds.sample_hz 对应
OVERLOAD_POLL_INTERVAL_S = 1.0


@dataclass(frozen=True, slots=True)
class OverloadContext:
    """运行上下文：是否“脚本控制中”决定 T1/T2 是否生效。"""

    state: str = "UNKNOWN"
    controlling: bool = False
    transition_at: float = 0.0
    foreground: bool = False


class OverloadMonitor:
    """1 Hz 过载监控器（线程安全：poll 与 snapshot 可从不同线程调用）。"""

    def __init__(
        self,
        config: OverloadConfig,
        controller: StopController,
        *,
        clock: Clock | None = None,
        metrics: MetricsSampler | None = None,
        capture_ms_provider: Callable[[], float | None] | None = None,
        loop_tick_p95_provider: Callable[[], float | None] | None = None,
    ) -> None:
        self._config = config
        self._controller = controller
        self._clock = clock or SystemClock()
        self._metrics = metrics or create_metrics_source()
        self._capture_ms_provider = capture_ms_provider
        self._loop_tick_provider = loop_tick_p95_provider
        self._policy = OverloadPolicy(config)
        self._context = OverloadContext()
        self._lock = threading.RLock()
        self._last_poll_at: float | None = None
        self._last_sample: SystemSample | None = None
        self._last_decision: OverloadDecision | None = None
        self._pause_request = None
        self._unavailable_reason: str | None = None
        self._startup_issues: tuple[str, ...] | None = None
        self._startup_detail = ""
        self._started_at = float("-inf")

    # -- 构造/只读属性 -------------------------------------------------------
    @property
    def config(self) -> OverloadConfig:
        return self._config

    @property
    def controller(self) -> StopController:
        return self._controller

    @property
    def policy(self) -> OverloadPolicy:
        return self._policy

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def poll_interval_s(self) -> float:
        hz = self._config.sample_hz if self._config.sample_hz > 0 else 1.0
        return 1.0 / hz

    @property
    def mode(self) -> str:
        return self._policy.mode

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def context(self) -> OverloadContext:
        return self._context

    @property
    def last_decision(self) -> OverloadDecision | None:
        return self._last_decision

    @property
    def last_sample(self) -> SystemSample | None:
        return self._last_sample

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    @property
    def startup_issues(self) -> tuple[str, ...]:
        return self._startup_issues or ()

    @property
    def startup_ok(self) -> bool:
        return self._startup_issues is None or not self._startup_issues

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self._policy.set_mode(mode)
            logger.info("过载监控模式切换为 %s", self._policy.mode)

    def set_context(
        self,
        state: str,
        controlling: bool,
        transition_at: float = 0.0,
        foreground: bool = False,
    ) -> None:
        """更新运行上下文；``controlling``=脚本是否处于控制中（T1/T2 生效前提）。"""
        with self._lock:
            previous = self._context
            self._context = OverloadContext(
                state=str(state),
                controlling=bool(controlling),
                transition_at=float(transition_at),
                foreground=bool(foreground),
            )
            if previous.controlling != self._context.controlling:
                logger.info(
                    "过载监控上下文: state=%s controlling=%s foreground=%s",
                    self._context.state,
                    self._context.controlling,
                    self._context.foreground,
                )

    # -- 外部状态通知 --------------------------------------------------------
    def notify_resumed(self, now: float | None = None) -> None:
        """外部（F11/人工/GUI）已恢复：撤销暂停记录，不消耗自动恢复次数。"""
        with self._lock:
            moment = self._clock.now() if now is None else float(now)
            self._policy.notify_resumed(moment)
            self._pause_request = None

    def notify_stopped(self, now: float | None = None) -> None:
        """外部已停止：进入终态，不再请求动作。"""
        with self._lock:
            moment = self._clock.now() if now is None else float(now)
            self._policy.notify_stopped(moment)
            self._pause_request = None

    def reset(self, now: float | None = None) -> None:
        """新会话/重新启用时重置监控状态。"""
        with self._lock:
            self._policy.reset()
            self._last_poll_at = now
            self._last_sample = None
            self._last_decision = None
            self._pause_request = None
            self._unavailable_reason = None
            self._startup_issues = None
            self._startup_detail = ""
            self._started_at = self._clock.now()

    # -- 轮询 ----------------------------------------------------------------
    def poll(self, now: float | None = None) -> list[SafetyEvent]:
        """按 1 Hz 采样并判定，返回需要 dispatch 的过载安全事件（异常时返回空列表）。"""
        events: list[SafetyEvent] = []
        try:
            with self._lock:
                moment = self._clock.now() if now is None else float(now)
                if not self._due(moment):
                    return events
                self._last_poll_at = moment
                if self._startup_issues is None:
                    self._run_startup_check(moment)
                sample = self._sample(moment)
                self._last_sample = sample
                if sample.available:
                    self._unavailable_reason = None
                else:
                    self._unavailable_reason = sample.unavailable_reason
                decision = self._policy.update(sample, controlling=self._context.controlling)
                self._last_decision = decision
                events = self._apply_decision(decision, moment)
        except Exception:
            logger.exception("过载监控 poll 异常（本轮清窗并标记不可用，不终止线程）")
            self._unavailable_reason = "过载监控内部异常（详见日志）"
            try:
                self._policy.mark_unavailable(self._unavailable_reason)
            except Exception:  # pragma: no cover - 状态机清理不应抛异常
                logger.exception("过载状态机清窗失败")
        return events

    def _due(self, moment: float) -> bool:
        if self._last_poll_at is None:
            return True
        return moment - self._last_poll_at >= self.poll_interval_s - 1e-9

    def _sample(self, moment: float) -> SystemSample:
        try:
            return self._metrics.sample(moment)
        except Exception as exc:  # 采样器异常降级为“不可用”样本
            logger.exception("系统指标采样异常，本轮标记不可用")
            reason = f"采样异常: {exc}"
            self._unavailable_reason = reason
            return SystemSample(mono=moment, wall=time.time(), available=False, errors=(reason,))

    def _run_startup_check(self, moment: float) -> None:
        self._started_at = moment
        checker = getattr(self._metrics, "check", None)
        if checker is None:
            self._startup_issues = ()
            return
        try:
            check: MetricsCheck = checker()
        except Exception as exc:  # pragma: no cover - 自检失败不阻断监控
            logger.warning("性能采样启动自检异常: %s", exc)
            self._startup_issues = (f"启动自检异常: {exc}",)
            return
        self._startup_detail = check.detail
        self._startup_issues = tuple(check.issues)
        if check.ok:
            logger.info("性能采样启动自检通过: %s", check.detail)
        else:
            logger.warning("性能采样启动自检问题: %s", "；".join(check.issues))

    # -- 判定 → 事件/控制器 --------------------------------------------------
    def _apply_decision(self, decision: OverloadDecision, moment: float) -> list[SafetyEvent]:
        events: list[SafetyEvent] = []
        for warning in decision.warnings:
            events.append(
                SafetyEvent(
                    SafetyEventKind.OVERLOAD_WARNING,
                    moment,
                    None,
                    detail={
                        "kind": warning.kind,
                        "reason": warning.reason,
                        "message": warning.detail,
                        "metrics": dict(warning.metrics),
                        "mode": decision.mode,
                    },
                )
            )
            logger.warning("过载告警（%s）: %s", decision.mode, warning.detail)

        acting = self._policy.acting
        if decision.action is OverloadAction.PAUSE:
            if not acting:
                logger.warning("monitor 模式：本应暂停但未动作 —— %s", decision.detail)
                return events
            if self._controller.stop_requested:
                self._policy.notify_stopped(moment)
                return events
            request = self._controller.request_pause(
                decision.reason or "overload_pause",
                decision.detail or "",
                hook="overload",
            )
            if request is not None and request is self._controller.pause_request:
                self._pause_request = request
                events.append(
                    SafetyEvent(
                        SafetyEventKind.OVERLOAD_PAUSE_REQUESTED,
                        moment,
                        request,
                        detail=self._decision_detail(decision),
                    )
                )
                logger.warning("过载保护：已请求暂停（%s）", request.format())
        elif decision.action is OverloadAction.STOP:
            if not acting:
                logger.error("monitor 模式：本应停止但未动作 —— %s", decision.detail)
                return events
            if self._controller.stop_requested:
                self._policy.notify_stopped(moment)
                return events
            request = self._controller.request_stop(
                decision.reason or "overload_stop",
                decision.detail or "",
                hook="overload",
            )
            events.append(
                SafetyEvent(
                    SafetyEventKind.OVERLOAD_STOP_REQUESTED,
                    moment,
                    request,
                    detail=self._decision_detail(decision),
                )
            )
            logger.error("过载保护：已请求停止（%s）", request.format())
        elif decision.action is OverloadAction.RESUME:
            if not acting:
                logger.info("monitor 模式：本应恢复但未动作 —— %s", decision.detail)
                return events
            pause_request = self._pause_request
            is_ours = pause_request is not None and self._controller.pause_request is pause_request
            if is_ours and self._controller.resume():
                self._pause_request = None
                events.append(
                    SafetyEvent(
                        SafetyEventKind.OVERLOAD_RESUME_REQUESTED,
                        moment,
                        None,
                        detail=self._decision_detail(decision),
                    )
                )
                logger.info("过载保护：已请求恢复运行（%s）", decision.detail)
                return events
            # 暂停已被别处解除（F11/人工/其它暂停原因）：只同步状态机，不抢控制权
            self._policy.notify_resumed(moment)
            self._pause_request = None
        return events

    @staticmethod
    def _decision_detail(decision: OverloadDecision) -> dict[str, object]:
        return {
            "reason": decision.reason,
            "detail": decision.detail,
            "state": decision.state,
            "mode": decision.mode,
            "metrics": dict(decision.metrics),
            "lateness_events": decision.lateness_events,
            "would_pause": decision.would_pause,
            "would_stop": decision.would_stop,
        }

    # -- GUI 快照 ------------------------------------------------------------
    def snapshot(self, now: float | None = None) -> PerformanceView:
        """生成性能面板快照（只读；任何异常都降级为“不可用”，不向 GUI 抛异常）。"""
        try:
            with self._lock:
                moment = self._clock.now() if now is None else float(now)
                return self._build_snapshot(moment)
        except Exception:
            logger.exception("生成性能快照失败，返回不可用视图")
            return self._degraded_snapshot("性能快照生成失败（详见日志）")

    def performance_view(self) -> PerformanceView:
        """``RuntimeService``/runner 快照入口：等价于 ``snapshot()``（无参、只读）。"""
        return self.snapshot()

    def view(self) -> PerformanceView:
        """``performance_view()`` 的兼容别名（runner 会依次探测 performance_view / view）。"""
        return self.snapshot()

    def _build_snapshot(self, moment: float) -> PerformanceView:
        from ..runtime.messages import PerformanceThresholds, PerformanceView

        cfg = self._config
        sample = self._last_sample
        decision = self._last_decision
        eff: EffectiveThresholds = self._policy.effective_thresholds()
        thresholds = PerformanceThresholds(
            mode=self._policy.mode,
            sample_hz=cfg.sample_hz,
            cpu_warn_pct=eff.cpu_warn_pct,
            physical_warn_mb=eff.physical_warn_mb,
            commit_warn_mb=eff.commit_warn_mb,
            commit_pause_mb=eff.commit_pause_mb,
            commit_stop_mb=eff.commit_stop_mb,
            commit_fast_stop_mb=eff.commit_fast_stop_mb,
            starvation_lateness_s=cfg.starvation_lateness_s,
            starvation_pause_events=cfg.starvation_pause_events,
            starvation_stop_events=cfg.starvation_stop_events,
            script_private_warn_mb=eff.script_private_warn_mb,
        )
        unavailable = self._unavailable_reason
        if unavailable is None and sample is not None and not sample.available:
            unavailable = sample.unavailable_reason
        state = decision.state if decision is not None else STATE_NORMAL
        reason = decision.reason if decision is not None else None
        detail = decision.detail if decision is not None else None
        if state == STATE_NORMAL and self._policy.paused:
            state = "paused"
            reason = reason or self._policy.pause_reason
            detail = detail or self._policy.pause_detail
        last_trigger = self._policy.last_trigger
        return PerformanceView(
            enabled=self.enabled,
            mode=self._policy.mode,
            sampled_at=sample.mono if sample is not None else None,
            sampled_wall=_wall_iso(sample.wall) if sample is not None else None,
            sample_age_s=max(0.0, moment - sample.mono) if sample is not None else None,
            unavailable_reason=unavailable,
            cpu_pct=sample.cpu_pct if sample is not None else None,
            mem_total_mb=sample.mem_total_mb if sample is not None else None,
            mem_available_mb=sample.mem_available_mb if sample is not None else None,
            commit_limit_mb=sample.commit_limit_mb if sample is not None else None,
            commit_available_mb=sample.commit_available_mb if sample is not None else None,
            script_private_mb=sample.script_private_mb if sample is not None else None,
            script_cpu_pct=sample.script_cpu_pct if sample is not None else None,
            capture_ms_avg=self._capture_ms(),
            loop_tick_p95_ms=self._loop_tick_p95(),
            sampler_lateness_ms=(
                self._policy.last_lateness_s * 1000.0
                if self._policy.last_lateness_s is not None
                else None
            ),
            overload_state=state,
            overload_reason=reason,
            overload_detail=detail,
            would_pause=self._policy.paused or (decision.would_pause if decision is not None else False),
            would_stop=self._policy.stop_requested,
            thresholds=thresholds,
            last_trigger=last_trigger,
        )

    def _capture_ms(self) -> float | None:
        if self._capture_ms_provider is None:
            return None
        try:
            value = self._capture_ms_provider()
        except Exception:  # pragma: no cover - 提供方异常不能拖垮快照
            logger.debug("读取截屏耗时失败", exc_info=True)
            return None
        return None if value is None else float(value)

    def _loop_tick_p95(self) -> float | None:
        if self._loop_tick_provider is None:
            return None
        try:
            value = self._loop_tick_provider()
        except Exception:  # pragma: no cover
            logger.debug("读取主循环耗时失败", exc_info=True)
            return None
        return None if value is None else float(value)

    def _degraded_snapshot(self, reason: str) -> PerformanceView:
        from ..runtime.messages import PerformanceThresholds, PerformanceView

        cfg = self._config
        return PerformanceView(
            enabled=self.enabled,
            mode=self._policy.mode,
            unavailable_reason=reason,
            overload_state=STATE_NORMAL,
            thresholds=PerformanceThresholds(
                mode=self._policy.mode,
                sample_hz=cfg.sample_hz,
                cpu_warn_pct=cfg.cpu_warn_pct,
                physical_warn_mb=cfg.physical_warn_floor_mb,
                commit_warn_mb=cfg.commit_warn_floor_mb,
                commit_pause_mb=cfg.commit_pause_floor_mb,
                commit_stop_mb=cfg.commit_stop_floor_mb,
                commit_fast_stop_mb=cfg.commit_fast_stop_floor_mb,
                starvation_lateness_s=cfg.starvation_lateness_s,
                starvation_pause_events=cfg.starvation_pause_events,
                starvation_stop_events=cfg.starvation_stop_events,
                script_private_warn_mb=cfg.script_private_warn_floor_mb,
            ),
        )


def _wall_iso(wall: float) -> str:
    return datetime.fromtimestamp(wall).astimezone().isoformat(timespec="seconds")


def build_overload_monitor(
    *,
    controller: StopController,
    config: object,
    clock: Clock | None = None,
    safety: object | None = None,
    app: object | None = None,
    metrics: MetricsSampler | None = None,
    **kwargs: object,
) -> OverloadMonitor:
    """runner 注入点工厂：按 ``overload_factory(safety=, controller=, clock=, config=, app=)`` 契约构造。

    ``config`` 接受 ``GlobalConfig``（取 ``.overload``）或直接 ``OverloadConfig``；``safety`` 与
    ``app`` 仅为兼容 runner 的关键字而接收（监控器只依赖 ``StopController`` 与配置）。
    """
    overload_config = getattr(config, "overload", config)
    if not isinstance(overload_config, OverloadConfig):
        raise TypeError(f"无法从 {type(config).__name__} 获取 OverloadConfig")
    logger.info("构造过载监控: mode=%s", overload_config.mode)
    return OverloadMonitor(
        overload_config,
        controller,
        clock=clock,
        metrics=metrics,
        capture_ms_provider=kwargs.get("capture_ms_provider"),  # type: ignore[arg-type]
        loop_tick_p95_provider=kwargs.get("loop_tick_p95_provider"),  # type: ignore[arg-type]
    )


__all__ = [
    "OVERLOAD_POLL_INTERVAL_S",
    "OverloadContext",
    "OverloadMonitor",
    "build_overload_monitor",
]
