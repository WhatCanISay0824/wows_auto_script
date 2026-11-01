"""过载保护纯状态机测试（src/execution/overload_policy.py）。

全部用假样本序列 + 假时钟口径的 ``mono`` 驱动，不真正等待；覆盖 CPU 满载/单峰值、
睡眠/挂起、缺样本、加载宽限、暂停上下文、Commit 慢降/快速停止、恢复与自动恢复上限、
告警滞回与限频、monitor 模式只记录不动手等场景。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import FrozenInstanceError, replace

import pytest
from src.common.sysmetrics import SystemSample
from src.config.models import OverloadConfig
from src.execution.overload_policy import (
    OVERLOAD_MODES,
    STATE_PAUSE_PENDING,
    STATE_PAUSED,
    STATE_STOP_PENDING,
    STATE_WARNING,
    OverloadAction,
    OverloadDecision,
    OverloadPolicy,
    WarningTrigger,
)

TOTAL_MB = 32_768.0
COMMIT_LIMIT_MB = 51_200.0
BASE_GTC_MS = 1_000_000
BASE_QUT_100NS = 1_000_000_000
WALL_BASE = 1_700_000_000.0


def make_sample(
    t: float,
    *,
    cpu: float | None = 10.0,
    mem_available: float | None = 16_000.0,
    commit: float | None = 30_000.0,
    script: float | None = 300.0,
    mem_total: float | None = TOTAL_MB,
    commit_limit: float | None = COMMIT_LIMIT_MB,
    sleep_total_s: float = 0.0,
    wall_jump_s: float = 0.0,
    available: bool = True,
) -> SystemSample:
    """构造一条 1 Hz 样本；``sleep_total_s`` 使 ``GTC64-QUT`` 偏移增加（模拟待机）。"""
    if not available:
        return SystemSample(
            mono=float(t),
            wall=WALL_BASE + float(t),
            available=False,
            errors=("测试样本不可用",),
        )
    return SystemSample(
        mono=float(t),
        wall=WALL_BASE + float(t) + wall_jump_s,
        cpu_pct=cpu,
        mem_total_mb=mem_total,
        mem_available_mb=mem_available,
        commit_limit_mb=commit_limit,
        commit_available_mb=commit,
        script_private_mb=script,
        script_cpu_pct=1.0,
        gtc64_ms=BASE_GTC_MS + int(round(t * 1000)),
        unbiased_100ns=BASE_QUT_100NS + int(round((t - sleep_total_s) * 10_000_000)),
    )


class Feed:
    """按 1 Hz 推进的样本驱动器（首拍 t=0 会被状态机丢弃）。"""

    def __init__(self, policy: OverloadPolicy, *, controlling: bool = True, start: float = 0.0) -> None:
        self.policy = policy
        self.controlling = controlling
        self.t = float(start)
        self.decisions: list[OverloadDecision] = []

    def step(self, *, dt: float = 1.0, **kwargs) -> OverloadDecision:
        sample = make_sample(self.t, **kwargs)
        decision = self.policy.update(sample, controlling=self.controlling)
        self.decisions.append(decision)
        self.t += dt
        return decision

    def steps(self, count: int, **kwargs) -> list[OverloadDecision]:
        return [self.step(**kwargs) for _ in range(count)]

    def advance(self, seconds: float) -> None:
        """只推进时间（不采样），用于构造采样迟到/睡眠的间隔。"""
        self.t += float(seconds)


def make_policy(mode: str = "pause_stop", **overrides) -> OverloadPolicy:
    return OverloadPolicy(replace(OverloadConfig(), mode=mode, **overrides))


def all_warnings(decisions: Iterable[OverloadDecision], kind: str | None = None) -> list[WarningTrigger]:
    result: list[WarningTrigger] = []
    for decision in decisions:
        for warning in decision.warnings:
            if kind is None or warning.kind == kind:
                result.append(warning)
    return result


def first_action(decisions: Iterable[OverloadDecision], action: OverloadAction) -> int | None:
    for index, decision in enumerate(decisions):
        if decision.action is action:
            return index
    return None


def action_kinds(decisions: Iterable[OverloadDecision]) -> list[str]:
    return [decision.action.value for decision in decisions if decision.action is not OverloadAction.NONE]


# ---------------------------------------------------------------------------
# 构造/参数
# ---------------------------------------------------------------------------


def test_default_mode_is_monitor_and_config_is_immutable() -> None:
    config = OverloadConfig()
    assert config.mode == "monitor"
    assert config.commit_pause_floor_mb == 2000.0
    policy = OverloadPolicy(config)
    assert policy.mode == "monitor"
    assert policy.acting is False
    with pytest.raises(FrozenInstanceError):
        config.mode = "stop"  # type: ignore[misc]


def test_invalid_mode_and_sample_hz_rejected() -> None:
    with pytest.raises(ValueError, match="未知过载保护模式"):
        OverloadPolicy(replace(OverloadConfig(), mode="bogus"))
    with pytest.raises(ValueError, match="采样频率必须为正数"):
        OverloadPolicy(replace(OverloadConfig(), sample_hz=0.0))
    assert "monitor" in OVERLOAD_MODES


def test_effective_thresholds_scale_with_machine_size() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.step(mem_total=131_072.0, commit_limit=200_000.0)
    eff = policy.effective_thresholds()
    assert eff.physical_warn_mb == pytest.approx(max(2500.0, 0.08 * 131_072.0))
    assert eff.commit_warn_mb == pytest.approx(max(6000.0, 0.12 * 200_000.0))
    assert eff.commit_pause_mb == pytest.approx(max(2000.0, 0.05 * 200_000.0))
    assert eff.commit_stop_mb == pytest.approx(max(1200.0, 0.025 * 200_000.0))
    assert eff.commit_fast_stop_mb == pytest.approx(max(400.0, 0.01 * 200_000.0))


# ---------------------------------------------------------------------------
# W 告警：只记录，不动作
# ---------------------------------------------------------------------------


def test_cpu_full_load_warns_after_60s_but_never_pauses() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.steps(61, cpu=99.0)
    warnings = all_warnings(feed.decisions, "cpu")
    assert len(warnings) == 1
    assert policy.paused is False
    assert policy.stop_requested is False
    assert "cpu" in policy.active_warnings
    assert action_kinds(feed.decisions) == []


def test_single_cpu_peak_is_not_overload() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.step(cpu=100.0)
    feed.steps(120, cpu=10.0)
    assert all_warnings(feed.decisions) == []
    assert action_kinds(feed.decisions) == []


def test_physical_low_warns_and_hysteresis_clears() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.steps(61, mem_available=500.0)
    assert all_warnings(feed.decisions, "physical")
    assert policy.paused is False and policy.stop_requested is False
    # 进入滞回区间（阈值 2500×0.8=2000 与 2500 之间）：保持告警但不重新计数
    feed.steps(61, mem_available=2200.0)
    assert "physical" in policy.active_warnings
    # 恢复到阈值/0.8=3125 以上并持续 60s：告警解除
    feed.steps(62, mem_available=5000.0)
    assert "physical" not in policy.active_warnings
    assert action_kinds(feed.decisions) == []


def test_commit_low_warns_but_never_pauses_alone() -> None:
    """Commit 低于告警阈值但高于暂停阈值时只告警，不暂停。"""
    policy = make_policy()
    feed = Feed(policy)
    feed.steps(62, commit=4000.0)
    feed.steps(1, commit=30_000.0)
    assert all_warnings(feed.decisions, "commit")
    assert policy.paused is False
    assert action_kinds(feed.decisions) == []


def test_script_memory_only_warns_after_warmup() -> None:
    policy = make_policy()
    feed = Feed(policy)
    # 预热阶段的大内存只作为基线，不告警
    feed.steps(125, script=300.0)
    feed.steps(61, script=5000.0)
    assert all_warnings(feed.decisions, "script_memory")
    assert policy.paused is False and policy.stop_requested is False


def test_warning_repeat_limited_to_once_per_5min() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.steps(61, cpu=99.0)  # t=60 告警
    assert len(all_warnings(feed.decisions, "cpu")) == 1
    feed.steps(61, cpu=99.0)  # 持续保持：5 分钟内不重复
    assert len(all_warnings(feed.decisions, "cpu")) == 1
    feed.steps(240, cpu=99.0)  # 超过 300s 后允许再提醒一次
    assert len(all_warnings(feed.decisions, "cpu")) == 2


# ---------------------------------------------------------------------------
# T1 暂停
# ---------------------------------------------------------------------------


def test_commit_slow_decline_pauses_after_occupancy() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.steps(61)
    low_start = len(feed.decisions)
    feed.steps(80, commit=1500.0)
    pause_index = first_action(feed.decisions, OverloadAction.PAUSE)
    assert pause_index is not None
    low_offset = pause_index - low_start
    # 60s 窗口内 48s 低于暂停阈值（≈80%）后触发
    assert 44 <= low_offset <= 54
    decision = feed.decisions[pause_index]
    assert decision.reason == "overload_pause"
    assert "Commit" in (decision.detail or "")
    assert policy.paused is True
    assert policy.state == STATE_PAUSED
    assert policy.stop_requested is False


def test_pause_requires_controlling_context() -> None:
    policy = make_policy()
    feed = Feed(policy, controlling=False)
    feed.steps(61)
    feed.steps(120, commit=1500.0)
    assert action_kinds(feed.decisions) == []
    assert policy.paused is False
    assert all(decision.would_pause is False for decision in feed.decisions)


def test_pause_not_triggered_before_window_covers_60s() -> None:
    """启动加载宽限：窗口未覆盖 60s 前，即使 Commit 极低也不暂停（快速通道除外）。"""
    policy = make_policy()
    feed = Feed(policy)
    feed.steps(50, commit=1500.0)  # 低 Commit，但历史不足 60s
    assert action_kinds(feed.decisions) == []
    assert policy.paused is False


def test_missing_samples_low_coverage_does_not_judge() -> None:
    policy = make_policy()
    feed = Feed(policy)
    # 每 2s 一个样本：覆盖率 ≈50%（<90%），迟到 1s <2.5s 不算事件
    feed.steps(130, dt=2.0, commit=1500.0)
    assert action_kinds(feed.decisions) == []
    assert policy.paused is False
    assert all_warnings(feed.decisions) == []


def test_first_sample_and_out_of_order_sample_are_dropped() -> None:
    policy = make_policy()
    decision = policy.update(make_sample(0.0, commit=300.0), controlling=True)
    assert decision.action is OverloadAction.NONE
    assert action_kinds([decision]) == []
    normal = make_sample(100.0)
    policy.update(normal, controlling=True)
    decision = policy.update(make_sample(50.0, commit=300.0), controlling=True)
    assert decision.action is OverloadAction.NONE
    assert decision.detail and "越界" in decision.detail


# ---------------------------------------------------------------------------
# T2 停止
# ---------------------------------------------------------------------------


def test_fast_commit_stop_after_10s() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.steps(20, commit=300.0)
    stop_index = first_action(feed.decisions, OverloadAction.STOP)
    assert stop_index is not None
    assert stop_index <= 11
    decision = feed.decisions[stop_index]
    assert decision.reason == "overload_commit"
    assert policy.stop_requested is True
    assert action_kinds(feed.decisions[:stop_index]).count("pause") == 0


def test_cpu_physical_script_never_stop() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.steps(130)  # 建立脚本内存预热基线
    feed.steps(180, cpu=100.0, mem_available=200.0, script=5000.0)
    assert "stop" not in action_kinds(feed.decisions)
    assert "pause" not in action_kinds(feed.decisions)
    assert policy.stop_requested is False
    assert policy.paused is False
    # CPU / 物理可用 / 脚本内存都只告警
    assert {"cpu", "physical", "script_memory"} <= set(policy.active_warnings)


def test_post_pause_commit_stop_30s_occupancy() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.steps(61)
    feed.steps(80, commit=1500.0)
    assert policy.paused is True
    pause_index = first_action(feed.decisions, OverloadAction.PAUSE)
    assert pause_index is not None
    low_start = len(feed.decisions)
    feed.steps(60, commit=1000.0)  # 低于停止阈值 max(1200, 2.5%)=1280
    stop_index = first_action(feed.decisions, OverloadAction.STOP)
    assert stop_index is not None
    assert stop_index - low_start <= 60
    decision = feed.decisions[stop_index]
    assert decision.reason == "overload_commit"
    assert decision.state in (STATE_STOP_PENDING, "stopped")


def test_awake_lateness_pauses_then_stops() -> None:
    policy = make_policy()
    feed = Feed(policy)
    # 每 4s 才采到一个样本（清醒迟到 3s > 2.5s）
    feed.steps(4, dt=4.0)
    pause_index = first_action(feed.decisions, OverloadAction.PAUSE)
    assert pause_index is not None
    decision = feed.decisions[pause_index]
    assert decision.reason == "overload_pause"
    assert "迟到" in (decision.detail or "")
    assert policy.paused is True
    # 暂停后 180s 内迟到事件仍 ≥3 → 停止
    feed.steps(4, dt=4.0)
    stop_index = first_action(feed.decisions, OverloadAction.STOP)
    assert stop_index is not None
    assert feed.decisions[stop_index].reason == "overload_starvation"
    assert policy.stop_requested is True


def test_single_lateness_event_only_warns() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.step()  # t=0 首拍丢弃
    feed.advance(3.0)  # 下一拍在 t=4，迟到 3s
    decision = feed.step()
    assert decision.warnings and decision.warnings[0].kind == "starvation"
    assert decision.action is OverloadAction.NONE
    assert policy.paused is False


def test_mode_stop_skips_pause_and_stops_directly() -> None:
    policy = make_policy(mode="stop")
    feed = Feed(policy)
    feed.steps(61)
    feed.steps(80, commit=1500.0)
    stop_index = first_action(feed.decisions, OverloadAction.STOP)
    pause_index = first_action(feed.decisions, OverloadAction.PAUSE)
    assert stop_index is not None
    assert pause_index is None
    assert policy.paused is False
    assert policy.stop_requested is True


# ---------------------------------------------------------------------------
# 睡眠/挂起、时间跳变、不可用
# ---------------------------------------------------------------------------


def test_sleep_clears_windows_and_grace_blocks_pause() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.steps(61)
    feed.steps(20, commit=1500.0)  # 低 Commit 但不足 80%
    assert policy.paused is False
    sleep_at = feed.t - 1.0
    feed.advance(3600.0 - 1.0)  # 下一拍距离上一拍 3600s（其中 3600s 在睡眠中）
    decision = feed.step(commit=1500.0, sleep_total_s=3600.0)
    assert decision.action is OverloadAction.NONE
    assert decision.lateness_events == 0, "睡眠造成的迟到不计入清醒迟到"
    assert feed.t - sleep_at >= 3600.0
    # 宽限期内继续低 Commit：历史刚清空，不应立即暂停
    feed.steps(20, commit=1500.0)
    assert policy.paused is False


def test_system_time_jump_clears_windows_without_lateness() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.steps(61)
    decision = feed.step(commit=1500.0, wall_jump_s=500.0)
    assert decision.action is OverloadAction.NONE
    assert decision.lateness_events == 0
    feed.steps(20, commit=1500.0)
    assert policy.paused is False


def test_unavailable_samples_degrade_without_decision() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.steps(130, available=False)
    assert action_kinds(feed.decisions) == []
    assert feed.decisions[-1].unavailable_reason
    assert policy.paused is False


def test_unavailable_then_recovered_still_respects_coverage() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.steps(70, available=False)
    feed.steps(80, commit=1500.0)  # 恢复后从 0 覆盖率开始积累，60s 后才会判定
    pause_index = first_action(feed.decisions, OverloadAction.PAUSE)
    assert pause_index is not None


# ---------------------------------------------------------------------------
# 恢复与自动恢复上限
# ---------------------------------------------------------------------------


def _drive_to_pause(feed: Feed) -> int:
    """喂入正常 61s + Commit 偏低 80s，返回暂停发生时的决策下标。"""
    feed.steps(61)
    feed.steps(80, commit=1500.0)
    index = first_action(feed.decisions, OverloadAction.PAUSE)
    assert index is not None
    return index


def _drive_until_action(feed: Feed, action: OverloadAction, limit: int = 300, **kwargs) -> int | None:
    for _ in range(limit):
        decision = feed.step(**kwargs)
        if decision.action is action:
            return len(feed.decisions) - 1
    return None


def test_auto_resume_after_recovery() -> None:
    policy = make_policy()
    feed = Feed(policy)
    pause_index = _drive_to_pause(feed)
    assert pause_index is not None
    resume_index = _drive_until_action(feed, OverloadAction.RESUME, commit=30_000.0)
    assert resume_index is not None
    decision = feed.decisions[resume_index]
    assert decision.reason == "overload_resume"
    assert policy.paused is False
    assert policy.auto_resume_used == 1


def test_repeated_pause_within_10min_disables_auto_resume() -> None:
    policy = make_policy()
    feed = Feed(policy)
    _drive_to_pause(feed)
    assert _drive_until_action(feed, OverloadAction.RESUME, commit=30_000.0) is not None
    # 10 分钟内再次暂停 → 禁用自动恢复
    feed.steps(80, commit=1500.0)
    assert policy.paused is True
    assert policy.auto_resume_disabled is True
    assert _drive_until_action(feed, OverloadAction.RESUME, limit=200, commit=30_000.0) is None
    assert policy.paused is True


def test_auto_resume_at_most_twice_per_session() -> None:
    policy = make_policy(auto_resume_repeat_window_s=0.0)
    feed = Feed(policy)
    resumes = 0
    for cycle in range(3):
        feed.steps(80, commit=1500.0)
        assert policy.paused is True, f"第 {cycle + 1} 次暂停未发生"
        index = _drive_until_action(feed, OverloadAction.RESUME, limit=300, commit=30_000.0)
        if index is not None:
            resumes += 1
    assert resumes == 2
    assert policy.auto_resume_used == 2
    assert policy.paused is True, "第三次暂停后不再自动恢复，交给人工"


def test_notify_resumed_does_not_consume_auto_resume() -> None:
    policy = make_policy()
    feed = Feed(policy)
    pause_index = _drive_to_pause(feed)
    policy.notify_resumed(feed.t)
    assert policy.paused is False
    assert policy.auto_resume_used == 0
    feed.steps(200, commit=30_000.0)
    assert action_kinds(feed.decisions[pause_index:]).count("resume") == 0


def test_notify_stopped_is_terminal() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.steps(61)
    policy.notify_stopped(feed.t)
    feed.steps(80, commit=1500.0)
    assert policy.stop_requested is True
    assert action_kinds(feed.decisions) == []


# ---------------------------------------------------------------------------
# mode 语义
# ---------------------------------------------------------------------------


def test_monitor_mode_records_would_pause_without_acting() -> None:
    policy = make_policy(mode="monitor")
    feed = Feed(policy)
    feed.steps(61)
    feed.steps(80, commit=1500.0)
    decision = feed.decisions[-1]
    assert policy.acting is False
    assert policy.paused is True  # 演练状态
    assert policy.stop_requested is False
    assert decision.would_pause is True
    assert decision.state == STATE_PAUSE_PENDING
    assert action_kinds(feed.decisions)[-1] == "pause"


def test_monitor_mode_records_would_stop() -> None:
    policy = make_policy(mode="monitor")
    feed = Feed(policy)
    feed.steps(20, commit=300.0)
    decision = feed.decisions[-1]
    assert decision.would_stop is True
    assert decision.state == STATE_STOP_PENDING
    assert policy.stop_requested is True
    assert policy.acting is False


def test_off_mode_does_not_judge() -> None:
    policy = make_policy(mode="off")
    feed = Feed(policy)
    feed.steps(61)
    feed.steps(120, commit=300.0)
    assert action_kinds(feed.decisions) == []
    assert all_warnings(feed.decisions) == []
    assert policy.paused is False
    assert policy.state == "normal"


def test_set_mode_off_stops_judging() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.steps(61)
    policy.set_mode("off")
    feed.steps(80, commit=300.0)
    assert action_kinds(feed.decisions[61:]) == []


def test_warning_state_visible_when_active() -> None:
    policy = make_policy()
    feed = Feed(policy)
    feed.steps(61, cpu=99.0)
    assert policy.state == STATE_WARNING
