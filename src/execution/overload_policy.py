"""过载保护纯状态机（设计 §6；2026-10-31 确认阈值）。

只做判定，不采样、不操作控制器、不写日志文件，因此可以离线单步测试：

- 输入 :class:`~src.common.sysmetrics.SystemSample` 序列 + ``controlling`` 上下文；
- 输出不可变 :class:`OverloadDecision`（新告警、请求的 pause/stop/resume、状态、阈值快照）。

阈值口径（全部来自 :class:`~src.config.models.OverloadConfig`，代码不写死）：

- **窗口**：1 Hz 采样，30/60/120s 滚动；有效覆盖率 <90% 不判定；样本缺失、首拍、时间戳不递增
  （越界）一律丢弃；睡眠/挂起（``GetTickCount64 - QueryUnbiasedInterruptTime`` 增量覆盖迟到）
  清窗并进入宽限、不计迟到；系统时间跳变清窗。
- **W 告警（只记录）**：CPU≥90% 60s；物理可用<max(2500MB,8%) 60s；Commit 余量
  <max(6000MB,12%) 60s；脚本私有>max(1024MB,2×预热基线) 60s；清醒迟到 1–2 次/120s。
- **T1 暂停（可恢复）**：Commit 余量<max(2000MB,5%) 占 60s ≥80%；或清醒迟到>2.5s 事件
  ≥3 次/120s。仅在“脚本控制中”生效。暂停请求统一用 ``reason="overload_pause"``（detail
  记录具体规则，便于运行服务/轮换白名单识别），可自动恢复。
- **T2 停止（终态）**：快速通道 Commit 余量<max(400MB,1%) 连续 10s；或已暂停后
  Commit 余量<max(1200MB,2.5%) 占 30s ≥90%；或暂停后 180s 内清醒迟到事件仍 ≥3。
  CPU/物理可用/心跳/帧龄/脚本内存**不得**单独触发停止。停止 reason 为
  ``overload_commit`` / ``overload_starvation``。
- **恢复**：暂停 ≥60s、Commit 余量 >1.5×暂停阈值占 60s ≥90%、120s 无迟到；最多自动恢复
  2 次/会话；10min 内反复暂停后不再自动恢复（交给人工）。
- **告警滞回**：退出需恢复幅度（上界指标 <阈值×0.8、下界指标 >阈值/0.8）且持续 60s；
  同类告警 5min 最多一条。
- **mode**：off 不采样判定；monitor 只记录“本应暂停/停止”，绝不请求控制器；pause_stop 先暂停、
  必要时停止；stop 达到 T1/T2 条件直接停止（不再经过暂停）。
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from ..common.sysmetrics import SystemSample
from ..config.models import OVERLOAD_MODES, OverloadConfig

logger = logging.getLogger(__name__)

#: 告警种类（与 5 个 W 条件一一对应）
WARNING_KINDS: tuple[str, ...] = ("cpu", "physical", "commit", "script_memory", "starvation")
WARNING_LABELS: Mapping[str, str] = {
    "cpu": "全机 CPU 过高",
    "physical": "物理可用内存偏低",
    "commit": "Commit 余量偏低",
    "script_memory": "脚本进程内存偏高",
    "starvation": "采样线程清醒迟到",
}

#: 每类状态字符串（PerformanceView.overload_state / GUI 展示）
STATE_NORMAL = "normal"
STATE_WARNING = "warning"
STATE_PAUSE_PENDING = "pause_pending"
STATE_PAUSED = "paused"
STATE_STOP_PENDING = "stop_pending"
STATE_STOPPED = "stopped"


class OverloadMode(StrEnum):
    """过载保护模式。"""

    OFF = "off"
    MONITOR = "monitor"
    PAUSE_STOP = "pause_stop"
    STOP = "stop"


class OverloadAction(StrEnum):
    """本次判定要求的动作（monitor 模式下由监控器只记录、不执行）。"""

    NONE = "none"
    PAUSE = "pause"
    STOP = "stop"
    RESUME = "resume"


def _copy_mapping(value: Mapping[str, object] | None) -> dict[str, object]:
    return dict(value) if value else {}


@dataclass(frozen=True, slots=True)
class WarningTrigger:
    """一条新产生的 W 告警（由监控器转成 SafetyEvent / 日志）。"""

    kind: str
    reason: str
    detail: str
    metrics: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", _copy_mapping(self.metrics))

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "detail": self.detail,
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True, slots=True)
class EffectiveThresholds:
    """按当前机器总内存/Commit 上限换算后的实际生效阈值（MB）。"""

    cpu_warn_pct: float
    physical_warn_mb: float
    commit_warn_mb: float
    commit_pause_mb: float
    commit_stop_mb: float
    commit_fast_stop_mb: float
    script_private_warn_mb: float


@dataclass(frozen=True, slots=True)
class OverloadDecision:
    """一次判定的不可变结果（监控器据此请求控制器并生成安全事件）。"""

    mode: str
    state: str = STATE_NORMAL
    action: OverloadAction = OverloadAction.NONE
    warnings: tuple[WarningTrigger, ...] = ()
    active_warnings: tuple[str, ...] = ()
    reason: str | None = None
    detail: str | None = None
    latency_s: float | None = None
    lateness_events: int = 0
    would_pause: bool = False
    would_stop: bool = False
    unavailable_reason: str | None = None
    metrics: Mapping[str, object] = field(default_factory=dict)
    thresholds: EffectiveThresholds | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", _copy_mapping(self.metrics))

    @property
    def paused(self) -> bool:
        return self.state == STATE_PAUSED

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "state": self.state,
            "action": self.action.value,
            "reason": self.reason,
            "detail": self.detail,
            "would_pause": self.would_pause,
            "would_stop": self.would_stop,
            "active_warnings": list(self.active_warnings),
        }


@dataclass(frozen=True, slots=True)
class _Record:
    """窗口内的一条样本（valid=False 表示指标不可用，只占用时间轴、不计覆盖率）。"""

    now: float
    sample: SystemSample
    valid: bool


class OverloadPolicy:
    """过载保护状态机；所有判定由 :meth:`update` 单步推进。"""

    def __init__(self, config: OverloadConfig) -> None:
        mode = _parse_mode(config.mode)
        if config.sample_hz <= 0:
            raise ValueError(f"采样频率必须为正数: {config.sample_hz!r}")
        self._config = config
        self._mode = mode
        self._acting = mode in (OverloadMode.PAUSE_STOP, OverloadMode.STOP)
        self.reset()

    # -- 只读属性 ------------------------------------------------------------
    @property
    def config(self) -> OverloadConfig:
        return self._config

    @property
    def mode(self) -> str:
        return self._mode.value

    @property
    def acting(self) -> bool:
        return self._acting

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    @property
    def auto_resume_used(self) -> int:
        return self._auto_resume_used

    @property
    def auto_resume_disabled(self) -> bool:
        return self._auto_resume_disabled

    @property
    def last_lateness_s(self) -> float | None:
        return self._last_lateness_s

    @property
    def active_warnings(self) -> tuple[str, ...]:
        return tuple(kind for kind in WARNING_KINDS if self._warnings.get(kind))

    @property
    def state(self) -> str:
        return self._state_name(bool(self.active_warnings))

    # -- 状态重置/外部通知 ---------------------------------------------------
    def reset(self) -> None:
        """清空全部判定状态（新会话或测试重放使用）。"""
        self._records: deque[_Record] = deque()
        self._history_start: float | None = None
        self._started_at: float | None = None
        self._last_record: _Record | None = None
        self._latest_sample: SystemSample | None = None
        self._lateness_events: deque[float] = deque()
        self._last_lateness_s: float | None = None
        self._grace_until: float = float("-inf")
        self._warnings: dict[str, bool] = {kind: False for kind in WARNING_KINDS}
        self._warning_emit: dict[str, float] = {}
        self._paused = False
        self._paused_at: float | None = None
        self._pause_reason: str | None = None
        self._pause_detail: str | None = None
        self._pause_times: deque[float] = deque()
        self._auto_resume_used = 0
        self._auto_resume_disabled = False
        self._stop_requested = False
        self._controlling = False
        self._unavailable_reason: str | None = None
        self._warmup_private: list[float] = []
        self._script_baseline_mb: float | None = None
        self._last_trigger: dict[str, object] | None = None

    def set_mode(self, mode: str) -> None:
        """运行中切换模式（off/monitor/pause_stop/stop）；不重置窗口与告警。"""
        parsed = _parse_mode(mode)
        if parsed is self._mode:
            return
        logger.info("过载保护模式切换: %s -> %s", self._mode.value, parsed.value)
        self._mode = parsed
        self._acting = parsed in (OverloadMode.PAUSE_STOP, OverloadMode.STOP)
        if parsed is OverloadMode.OFF:
            self._stop_requested = False

    def notify_resumed(self, now: float) -> None:
        """外部（F11/人工）已恢复：撤销本状态机的暂停记录，不消耗自动恢复次数。"""
        if not self._paused:
            return
        logger.info("过载保护：外部恢复暂停（不消耗自动恢复次数）")
        self._paused = False
        self._paused_at = None
        self._reset_windows(now)

    def notify_stopped(self, now: float | None = None) -> None:
        """外部已停止：进入终态，不再产生判定动作。"""
        self._stop_requested = True
        self._paused = False
        self._paused_at = None
        logger.info("过载保护：已停止，后续不再判定动作（at=%s）", now)

    def mark_unavailable(self, reason: str) -> None:
        """采样失败：清空窗口并记录不可用原因，下次恢复采样后重新积累。"""
        self._unavailable_reason = str(reason or "系统指标不可用")
        now = self._last_record.now if self._last_record is not None else 0.0
        self._reset_windows(now)

    # -- 核心：单步更新 ------------------------------------------------------
    def update(self, sample: SystemSample, *, controlling: bool = False) -> OverloadDecision:
        """喂入一条样本并返回判定结果（纯计算，不抛异常；非法样本降级为不可用）。"""
        self._controlling = bool(controlling)
        now = float(sample.mono)
        triggered: list[WarningTrigger] = []
        action = OverloadAction.NONE
        reason: str | None = None
        detail: str | None = None
        unavailable: str | None = None
        if not sample.available:
            unavailable = sample.unavailable_reason
            self._unavailable_reason = unavailable
        else:
            self._unavailable_reason = None

        # --- 首拍丢弃：只建立时间基线（CPU 无时间差） ---
        if self._last_record is None:
            self._history_start = now
            self._started_at = now
            self._last_record = _Record(now, sample, valid=False)
            self._last_lateness_s = None
            if sample.available and sample.script_private_mb is not None:
                self._warmup_private.append(sample.script_private_mb)
                self._latest_sample = sample
            return self._decision(
                state=STATE_NORMAL,
                action=OverloadAction.NONE,
                warnings=(),
                now=now,
                latency=None,
                unavailable=unavailable,
            )

        previous = self._last_record
        delta_s = now - previous.now
        if not math.isfinite(now) or delta_s <= 0:
            # 时间戳重复/倒退（越界）：丢弃本拍，不更新基线、不判定
            logger.debug("过载保护丢弃越界样本: mono=%r（上一拍 %r）", now, previous.now)
            return self._current_decision(now, unavailable=unavailable, note="样本时间戳越界，已丢弃")

        lateness: float | None = None
        interval = 1.0 / self._config.sample_hz
        raw_lateness = max(0.0, delta_s - interval)
        sleep_s = self._sleep_between(previous.sample, sample, delta_s)
        sleep_covered = (
            sleep_s is not None
            and sleep_s > 0.0
            and sleep_s
            >= max(self._config.sleep_detect_s, self._config.sleep_cover_ratio * raw_lateness)
        )
        wall_jump = abs((sample.wall - previous.sample.wall) - delta_s) > self._config.clock_jump_s
        if sleep_covered:
            # 睡眠/挂起：``GTC64-QUT`` 偏移增量覆盖了这次迟到 → 清窗 + 宽限，不计迟到
            logger.info(
                "检测到系统睡眠/挂起约 %.1fs（当拍迟到 %.1fs）：清空判定窗口并宽限 %.0fs",
                sleep_s,
                raw_lateness,
                self._config.sleep_grace_s,
            )
            self._reset_windows(now)
            self._grace_until = now + self._config.sleep_grace_s
            self._lateness_events.clear()
        elif wall_jump:
            logger.warning("检测到系统时间跳变（墙上与单调时间不一致）：清空判定窗口")
            self._reset_windows(now)
            self._lateness_events.clear()
        else:
            lateness = raw_lateness
            self._last_lateness_s = lateness
            if now >= self._grace_until and lateness > self._config.starvation_lateness_s:
                self._lateness_events.append(now)

        # --- 记录本拍并更新基线 ---------------
        valid = self._sample_valid(sample)
        record = _Record(now, sample, valid)
        self._records.append(record)
        self._last_record = record
        self._prune(now)
        if valid:
            self._latest_sample = sample
            if self._started_at is not None and now - self._started_at <= self._config.script_warmup_s:
                if sample.script_private_mb is not None:
                    self._warmup_private.append(sample.script_private_mb)
            elif self._script_baseline_mb is None and self._warmup_private:
                self._script_baseline_mb = max(self._warmup_private)
                logger.info("脚本进程内存预热基线约 %.0f MB", self._script_baseline_mb)

        if self._mode is OverloadMode.OFF:
            return self._decision(
                state=STATE_NORMAL,
                action=OverloadAction.NONE,
                warnings=(),
                now=now,
                latency=lateness,
                unavailable=unavailable,
            )

        # --- W 告警（不动作，任何上下文都记录） ---
        triggered = self._evaluate_warnings(now)

        if self._stop_requested:
            return self._current_decision(now, warnings=triggered, unavailable=unavailable)

        if not self._controlling:
            return self._decision(
                state=self._state_name(bool(self.active_warnings)),
                action=OverloadAction.NONE,
                warnings=tuple(triggered),
                now=now,
                latency=lateness,
                unavailable=unavailable,
                detail="非脚本控制上下文：只记录告警，不请求暂停/停止",
            )

        # --- T2 快速通道：不受睡眠宽限限制（严重 Commit 耗尽需要立即停止） ---
        fast = self._fast_stop_condition(now)
        if fast is not None:
            self._stop_requested = True
            action = OverloadAction.STOP
            reason, detail = fast
            logger.error("过载保护：请求停止（快速通道）—— %s", detail)

        # --- 睡眠/挂起宽限期内不做暂停/恢复判定 ---
        elif now < self._grace_until:
            return self._decision(
                state=self._state_name(bool(self.active_warnings)),
                action=OverloadAction.NONE,
                warnings=tuple(triggered),
                now=now,
                latency=lateness,
                unavailable=unavailable,
                detail="睡眠/挂起宽限期内，不判定暂停/恢复",
            )

        # --- 暂停中的判定：停止 → 恢复 ---
        elif self._paused:
            stop = self._stop_after_pause(now)
            if stop is not None:
                self._stop_requested = True
                action = OverloadAction.STOP
                reason, detail = stop
                logger.error("过载保护：暂停后请求停止—— %s", detail)
            elif self._can_resume(now):
                self._paused = False
                self._paused_at = None
                self._auto_resume_used += 1
                if self._auto_resume_used >= self._config.auto_resume_max:
                    logger.warning(
                        "过载保护：自动恢复次数已达上限（%d/%d），此后不再自动恢复",
                        self._auto_resume_used,
                        self._config.auto_resume_max,
                    )
                action = OverloadAction.RESUME
                reason = "overload_resume"
                detail = (
                    f"Commit 余量恢复且 {self._config.resume_quiet_s:.0f}s 无迟到；"
                    f"第 {self._auto_resume_used}/{self._config.auto_resume_max} 次自动恢复"
                )
                logger.info("过载保护：自动恢复运行—— %s", detail)
        else:
            # --- 未暂停：T1 暂停（mode=stop 时直接停止） ---
            pause = self._pause_condition(now)
            if pause is not None:
                detail = pause[1]
                if self._mode is OverloadMode.STOP:
                    reason = pause[0]
                    self._stop_requested = True
                    action = OverloadAction.STOP
                    logger.error("过载保护：mode=stop 直接请求停止—— %s", detail)
                else:
                    # 暂停统一使用 overload_pause（自动恢复/轮换白名单按该原因识别），
                    # 具体触发规则写在 detail 里（Commit 余量 / 清醒迟到）。
                    reason = "overload_pause"
                    self._enter_pause(now, reason, detail)
                    action = OverloadAction.PAUSE
                    logger.warning("过载保护：请求暂停—— %s", detail)
            elif self._mode is OverloadMode.STOP:
                slow_stop = self._slow_stop_condition(now)
                if slow_stop is not None:
                    self._stop_requested = True
                    action = OverloadAction.STOP
                    reason, detail = slow_stop
                    logger.error("过载保护：mode=stop 请求停止—— %s", detail)

        return self._decision(
            state=self._state_name(bool(self.active_warnings)),
            action=action,
            warnings=tuple(triggered),
            now=now,
            latency=lateness,
            reason=reason,
            detail=detail,
            unavailable=unavailable,
            stop_requested=self._stop_requested,
            paused=self._paused,
        )

    # -- 阈值 ----------------------------------------------------------------
    def effective_thresholds(self) -> EffectiveThresholds:
        """按最近可用样本的总内存/Commit 上限换算实际阈值（无样本时用配置下限）。"""
        cfg = self._config
        sample = self._latest_sample
        mem_total = sample.mem_total_mb if sample is not None else None
        commit_limit = sample.commit_limit_mb if sample is not None else None
        physical = cfg.physical_warn_floor_mb
        if mem_total:
            physical = max(physical, cfg.physical_warn_limit_ratio * mem_total)
        commit_warn = cfg.commit_warn_floor_mb
        commit_pause = cfg.commit_pause_floor_mb
        commit_stop = cfg.commit_stop_floor_mb
        commit_fast = cfg.commit_fast_stop_floor_mb
        if commit_limit:
            commit_warn = max(commit_warn, cfg.commit_warn_limit_ratio * commit_limit)
            commit_pause = max(commit_pause, cfg.commit_pause_limit_ratio * commit_limit)
            commit_stop = max(commit_stop, cfg.commit_stop_limit_ratio * commit_limit)
            commit_fast = max(commit_fast, cfg.commit_fast_stop_limit_ratio * commit_limit)
        script = cfg.script_private_warn_floor_mb
        if self._script_baseline_mb:
            script = max(script, cfg.script_private_baseline_ratio * self._script_baseline_mb)
        return EffectiveThresholds(
            cpu_warn_pct=cfg.cpu_warn_pct,
            physical_warn_mb=physical,
            commit_warn_mb=commit_warn,
            commit_pause_mb=commit_pause,
            commit_stop_mb=commit_stop,
            commit_fast_stop_mb=commit_fast,
            script_private_warn_mb=script,
        )

    # -- 告警 ----------------------------------------------------------------
    def _evaluate_warnings(self, now: float) -> list[WarningTrigger]:
        cfg = self._config
        eff = self.effective_thresholds()
        triggered: list[WarningTrigger] = []
        warmup = self._started_at is not None and now - self._started_at < cfg.script_warmup_s

        cpu_enter = self._sustained(
            now, cfg.window_warn_s, lambda s: s.cpu_pct is not None and s.cpu_pct >= eff.cpu_warn_pct
        )
        cpu_clear = self._sustained(
            now,
            cfg.warn_clear_s,
            lambda s: s.cpu_pct is not None and s.cpu_pct < eff.cpu_warn_pct * cfg.warn_clear_ratio,
        )
        physical_enter = self._sustained(
            now,
            cfg.window_warn_s,
            lambda s: s.mem_available_mb is not None and s.mem_available_mb < eff.physical_warn_mb,
        )
        physical_clear = self._sustained(
            now,
            cfg.warn_clear_s,
            lambda s: s.mem_available_mb is not None
            and s.mem_available_mb > eff.physical_warn_mb / cfg.warn_clear_ratio,
        )
        commit_enter = self._sustained(
            now,
            cfg.window_warn_s,
            lambda s: s.commit_available_mb is not None and s.commit_available_mb < eff.commit_warn_mb,
        )
        commit_clear = self._sustained(
            now,
            cfg.warn_clear_s,
            lambda s: s.commit_available_mb is not None
            and s.commit_available_mb > eff.commit_warn_mb / cfg.warn_clear_ratio,
        )
        script_enter = (
            False
            if warmup
            else self._sustained(
                now,
                cfg.window_warn_s,
                lambda s: s.script_private_mb is not None
                and s.script_private_mb > eff.script_private_warn_mb,
            )
        )
        script_clear = self._sustained(
            now,
            cfg.warn_clear_s,
            lambda s: s.script_private_mb is not None
            and s.script_private_mb < eff.script_private_warn_mb * cfg.warn_clear_ratio,
        )
        lateness_count = self._lateness_count(now, cfg.starvation_window_s)
        starvation_enter = lateness_count >= 1
        starvation_clear = self._lateness_count(now, cfg.warn_clear_s) == 0 and not (
            self._last_lateness_s is not None and self._last_lateness_s > cfg.starvation_lateness_s
        )

        entries = {
            "cpu": (
                cpu_enter,
                cpu_clear,
                f"全机 CPU ≥ {eff.cpu_warn_pct:.0f}% 持续 {cfg.window_warn_s:.0f}s",
            ),
            "physical": (
                physical_enter,
                physical_clear,
                f"物理可用内存 < {eff.physical_warn_mb:.0f}MB 持续 {cfg.window_warn_s:.0f}s",
            ),
            "commit": (
                commit_enter,
                commit_clear,
                f"Commit 余量 < {eff.commit_warn_mb:.0f}MB 持续 {cfg.window_warn_s:.0f}s",
            ),
            "script_memory": (
                script_enter,
                script_clear,
                f"脚本进程私有内存 > {eff.script_private_warn_mb:.0f}MB 持续 {cfg.window_warn_s:.0f}s",
            ),
            "starvation": (
                starvation_enter,
                starvation_clear,
                f"{cfg.starvation_window_s:.0f}s 内清醒采样迟到 >{cfg.starvation_lateness_s:.1f}s 事件 "
                f"{lateness_count} 次",
            ),
        }
        metrics = self._metrics_snapshot(eff)
        for kind in WARNING_KINDS:
            enter, clear, text = entries[kind]
            trigger = self._advance_warning(kind, enter, clear, now, text, metrics)
            if trigger is not None:
                triggered.append(trigger)
        return triggered

    def _advance_warning(
        self,
        kind: str,
        enter: bool,
        clear: bool,
        now: float,
        text: str,
        metrics: Mapping[str, object],
    ) -> WarningTrigger | None:
        active = self._warnings.get(kind, False)
        if active:
            if clear:
                self._warnings[kind] = False
                logger.info("过载保护告警解除: %s", WARNING_LABELS.get(kind, kind))
            else:
                last_emit = self._warning_emit.get(kind)
                if last_emit is None or now - last_emit >= self._config.warn_repeat_interval_s:
                    self._warning_emit[kind] = now
                    return WarningTrigger(kind, f"overload_{kind}", text, metrics)
            return None
        if not enter:
            return None
        self._warnings[kind] = True
        last_emit = self._warning_emit.get(kind)
        if last_emit is not None and now - last_emit < self._config.warn_repeat_interval_s:
            return None
        self._warning_emit[kind] = now
        logger.warning("过载保护告警: %s —— %s", WARNING_LABELS.get(kind, kind), text)
        return WarningTrigger(kind, f"overload_{kind}", text, metrics)

    # -- 暂停/停止/恢复条件 --------------------------------------------------
    def _pause_condition(self, now: float) -> tuple[str, str] | None:
        cfg = self._config
        eff = self.effective_thresholds()
        if self._sustained(
            now,
            cfg.window_warn_s,
            lambda s: s.commit_available_mb is not None
            and s.commit_available_mb < eff.commit_pause_mb,
            cfg.commit_pause_occupancy,
        ):
            current = self._metric_text("commit_available_mb")
            return (
                "overload_commit",
                f"Commit 余量 {current} 持续低于暂停阈值 {eff.commit_pause_mb:.0f}MB"
                f"（{cfg.window_warn_s:.0f}s 窗口占比 ≥{cfg.commit_pause_occupancy:.0%}）",
            )
        count = self._lateness_count(now, cfg.starvation_window_s)
        if count >= cfg.starvation_pause_events:
            return (
                "overload_starvation",
                f"{cfg.starvation_window_s:.0f}s 内清醒采样迟到 >{cfg.starvation_lateness_s:.1f}s "
                f"事件 {count} 次（≥{cfg.starvation_pause_events}）",
            )
        return None

    def _fast_stop_condition(self, now: float) -> tuple[str, str] | None:
        cfg = self._config
        eff = self.effective_thresholds()
        sustained = self._sustained(
            now,
            cfg.commit_fast_stop_sustained_s,
            lambda s: s.commit_available_mb is not None
            and s.commit_available_mb < eff.commit_fast_stop_mb,
        )
        if not sustained:
            return None
        return (
            "overload_commit",
            f"Commit 余量 {self._metric_text('commit_available_mb')} 连续 "
            f"{cfg.commit_fast_stop_sustained_s:.0f}s 低于快速停止阈值 {eff.commit_fast_stop_mb:.0f}MB",
        )

    def _slow_stop_condition(self, now: float) -> tuple[str, str] | None:
        cfg = self._config
        eff = self.effective_thresholds()
        sustained = self._sustained(
            now,
            cfg.commit_stop_window_s,
            lambda s: s.commit_available_mb is not None
            and s.commit_available_mb < eff.commit_stop_mb,
            cfg.commit_stop_occupancy,
        )
        if not sustained:
            return None
        return (
            "overload_commit",
            f"Commit 余量 {self._metric_text('commit_available_mb')} 持续低于停止阈值 "
            f"{eff.commit_stop_mb:.0f}MB（{cfg.commit_stop_window_s:.0f}s 窗口占比 "
            f"≥{cfg.commit_stop_occupancy:.0%}）",
        )

    def _stop_after_pause(self, now: float) -> tuple[str, str] | None:
        slow = self._slow_stop_condition(now)
        if slow is not None:
            return slow
        cfg = self._config
        points = [
            moment
            for moment in self._lateness_events
            if moment >= now - cfg.starvation_stop_window_s
            and (self._paused_at is None or moment > self._paused_at + 1e-9)
        ]
        if len(points) >= cfg.starvation_stop_events:
            return (
                "overload_starvation",
                f"暂停后 {cfg.starvation_stop_window_s:.0f}s 内清醒采样迟到事件仍 "
                f"{len(points)} 次（≥{cfg.starvation_stop_events}）",
            )
        return None

    def _enter_pause(self, now: float, reason: str, detail: str) -> None:
        cfg = self._config
        recent = [moment for moment in self._pause_times if now - moment <= cfg.auto_resume_repeat_window_s]
        if recent and not self._auto_resume_disabled:
            self._auto_resume_disabled = True
            logger.warning(
                "过载保护：%.0fs 内反复暂停，停止自动恢复（交给人工）",
                cfg.auto_resume_repeat_window_s,
            )
        self._pause_times.append(now)
        while self._pause_times and now - self._pause_times[0] > cfg.auto_resume_repeat_window_s:
            self._pause_times.popleft()
        self._paused = True
        self._paused_at = now
        self._pause_reason = reason
        self._pause_detail = detail

    def _can_resume(self, now: float) -> bool:
        cfg = self._config
        if self._auto_resume_disabled or self._auto_resume_used >= cfg.auto_resume_max:
            return False
        if self._paused_at is None or now - self._paused_at < cfg.pause_min_s:
            return False
        eff = self.effective_thresholds()
        recovered = eff.commit_pause_mb * cfg.resume_commit_ratio
        if not self._sustained(
            now,
            cfg.window_resume_s,
            lambda s: s.commit_available_mb is not None and s.commit_available_mb > recovered,
            cfg.resume_occupancy,
        ):
            return False
        return self._lateness_count(now, cfg.resume_quiet_s) == 0

    # -- 窗口/覆盖率工具 -----------------------------------------------------
    def _window_records(self, now: float, window_s: float) -> list[_Record]:
        cutoff = now - window_s
        return [record for record in self._records if record.now >= cutoff - 1e-9]

    def _window_ready(self, now: float, window_s: float) -> bool:
        if self._history_start is None:
            return False
        slack = 0.5 / self._config.sample_hz
        return now - self._history_start >= window_s - slack

    def _coverage(self, now: float, window_s: float) -> float:
        expected = max(1, round(window_s * self._config.sample_hz))
        records = self._window_records(now, window_s)
        valid = sum(1 for record in records if record.valid)
        return valid / expected

    def _sustained(
        self,
        now: float,
        window_s: float,
        predicate,
        occupancy: float = 1.0,
    ) -> bool:
        """窗口内所有有效样本满足条件（或满足比例 ≥ occupancy）且覆盖率达标。"""
        if not self._window_ready(now, window_s):
            return False
        records = [record for record in self._window_records(now, window_s) if record.valid]
        if not records or self._coverage(now, window_s) < self._config.min_coverage:
            return False
        hits = sum(1 for record in records if predicate(record.sample))
        return hits / len(records) >= occupancy - 1e-9

    def _lateness_count(self, now: float, window_s: float, *, since: float | None = None) -> int:
        cutoff = now - window_s
        floor = cutoff if since is None else max(cutoff, since)
        count = 0
        for moment in self._lateness_events:
            if moment >= floor - 1e-9:
                count += 1
        return count

    def _prune(self, now: float) -> None:
        cfg = self._config
        keep = max(
            cfg.window_long_s,
            cfg.window_stop_after_pause_s,
            cfg.starvation_stop_window_s,
            cfg.window_warn_s,
        ) + 10.0
        while self._records and self._records[0].now < now - keep:
            self._records.popleft()
        lateness_keep = max(cfg.starvation_window_s, cfg.starvation_stop_window_s) + 10.0
        while self._lateness_events and self._lateness_events[0] < now - lateness_keep:
            self._lateness_events.popleft()

    def _reset_windows(self, now: float) -> None:
        """清空判定窗口（睡眠/挂起/时间跳变/采样不可用时）；预热基线是会话级状态，保留。"""
        self._records.clear()
        self._history_start = now
        self._latest_sample = None
        self._last_lateness_s = None

    def _sleep_between(self, previous: SystemSample, current: SystemSample, delta_s: float) -> float | None:
        """相邻样本的 ``GTC64-QUT`` 偏移增量 ≈ 期间待机/休眠秒数（QUT 不含待机）。

        本机 ``time.monotonic()`` 含待机，因此“墙钟间隔与清醒时间之差”就是睡眠量；
        直接用偏移增量不依赖 monotonic 与 GTC64 的微小刻度差。
        """
        if (
            previous.gtc64_ms is None
            or previous.unbiased_100ns is None
            or current.gtc64_ms is None
            or current.unbiased_100ns is None
        ):
            return None
        previous_offset = previous.gtc64_ms / 1000.0 - previous.unbiased_100ns / 1e7
        current_offset = current.gtc64_ms / 1000.0 - current.unbiased_100ns / 1e7
        return current_offset - previous_offset

    def _sample_valid(self, sample: SystemSample) -> bool:
        if not sample.available:
            return False
        checks = (
            (sample.cpu_pct, 0.0, 100.0),
            (sample.mem_available_mb, 0.0, None),
            (sample.mem_total_mb, 0.0, None),
            (sample.commit_available_mb, 0.0, None),
            (sample.commit_limit_mb, 0.0, None),
            (sample.script_private_mb, 0.0, None),
            (sample.script_cpu_pct, 0.0, None),
        )
        for value, low, high in checks:
            if value is None:
                continue
            if not math.isfinite(value) or value < low or (high is not None and value > high):
                return False
        return True

    # -- 决策构造 ------------------------------------------------------------
    def _metrics_snapshot(self, eff: EffectiveThresholds) -> dict[str, object]:
        sample = self._latest_sample
        data: dict[str, object] = {
            "cpu_pct": sample.cpu_pct if sample is not None else None,
            "mem_available_mb": sample.mem_available_mb if sample is not None else None,
            "mem_total_mb": sample.mem_total_mb if sample is not None else None,
            "commit_available_mb": sample.commit_available_mb if sample is not None else None,
            "commit_limit_mb": sample.commit_limit_mb if sample is not None else None,
            "script_private_mb": sample.script_private_mb if sample is not None else None,
            "physical_warn_mb": eff.physical_warn_mb,
            "commit_warn_mb": eff.commit_warn_mb,
            "commit_pause_mb": eff.commit_pause_mb,
            "commit_stop_mb": eff.commit_stop_mb,
            "commit_fast_stop_mb": eff.commit_fast_stop_mb,
            "script_private_warn_mb": eff.script_private_warn_mb,
            "lateness_events": self._lateness_count(
                self._last_record.now if self._last_record else 0.0, self._config.starvation_window_s
            ),
        }
        return data

    def _metric_text(self, name: str) -> str:
        sample = self._latest_sample
        value = getattr(sample, name, None) if sample is not None else None
        if value is None:
            return "不可用"
        if name == "cpu_pct" or name.endswith("_pct"):
            return f"{float(value):.1f}%"
        return f"{float(value):.0f}MB"

    def _state_name(self, warning_active: bool) -> str:
        if self._stop_requested:
            return STATE_STOPPED if self._acting else STATE_STOP_PENDING
        if self._paused:
            return STATE_PAUSED if self._acting else STATE_PAUSE_PENDING
        if warning_active:
            return STATE_WARNING
        return STATE_NORMAL

    def _decision(
        self,
        *,
        state: str,
        action: OverloadAction,
        warnings: tuple[WarningTrigger, ...],
        now: float,
        latency: float | None,
        reason: str | None = None,
        detail: str | None = None,
        unavailable: str | None = None,
        stop_requested: bool | None = None,
        paused: bool | None = None,
    ) -> OverloadDecision:
        eff = self.effective_thresholds()
        decision = OverloadDecision(
            mode=self._mode.value,
            state=state,
            action=action,
            warnings=warnings,
            active_warnings=self.active_warnings,
            reason=reason,
            detail=detail,
            latency_s=latency,
            lateness_events=self._lateness_count(now, self._config.starvation_window_s),
            would_pause=self._paused if paused is None else paused,
            would_stop=self._stop_requested if stop_requested is None else stop_requested,
            unavailable_reason=unavailable,
            metrics=self._metrics_snapshot(eff),
            thresholds=eff,
        )
        if action is not OverloadAction.NONE:
            self._last_trigger = {
                "kind": action.value,
                "reason": reason,
                "detail": detail,
                "state": state,
                "at": now,
                "mode": self._mode.value,
            }
        elif warnings:
            self._last_trigger = {
                "kind": "warning",
                "reason": warnings[0].reason,
                "detail": warnings[0].detail,
                "state": state,
                "at": now,
                "mode": self._mode.value,
            }
        return decision

    def _current_decision(
        self,
        now: float,
        *,
        warnings: tuple[WarningTrigger, ...] = (),
        unavailable: str | None = None,
        note: str | None = None,
    ) -> OverloadDecision:
        return self._decision(
            state=self._state_name(bool(self.active_warnings)),
            action=OverloadAction.NONE,
            warnings=warnings,
            now=now,
            latency=self._last_lateness_s,
            detail=note,
            unavailable=unavailable,
            stop_requested=self._stop_requested,
            paused=self._paused,
        )

    @property
    def last_trigger(self) -> dict[str, object] | None:
        return dict(self._last_trigger) if self._last_trigger is not None else None

    @property
    def pause_reason(self) -> str | None:
        return self._pause_reason

    @property
    def pause_detail(self) -> str | None:
        return self._pause_detail


def _parse_mode(mode: str) -> OverloadMode:
    try:
        return OverloadMode(str(mode))
    except ValueError as exc:
        raise ValueError(
            f"未知过载保护模式 {mode!r}（允许 {sorted(OVERLOAD_MODES)}）"
        ) from exc


__all__ = [
    "EffectiveThresholds",
    "OVERLOAD_MODES",
    "OverloadAction",
    "OverloadDecision",
    "OverloadMode",
    "OverloadPolicy",
    "STATE_NORMAL",
    "STATE_PAUSED",
    "STATE_PAUSE_PENDING",
    "STATE_STOPPED",
    "STATE_STOP_PENDING",
    "STATE_WARNING",
    "WARNING_KINDS",
    "WARNING_LABELS",
    "WarningTrigger",
]
