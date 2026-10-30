"""死亡投票测试（详细设计 5.9；评审 P0-7 修正口径）。

覆盖：
- 开局静默窗口（ignore_start_s）内不判定、不计时；
- 强信号直判（hp_bar_absent / hp_zero 稳定 strong_stable_s）；
- 加权投票（分数阈值 + 稳定窗口，各信号自身稳定时间）；
- banner 命中后锁存，提示消失后投票不回落；
- 观战模式后己方血条信号不参与投票、不触发直判；
- 矛盾只清对应信号，不清空其他信号计时；
- marker_lost 使用独立的 marker_lost_stable_s；
- 判定后锁存、reset 清空、votes 字段与额外键容忍。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from src.common.types import DeathSignals
from src.config.models import DeathConfig
from src.perception.death import SIGNAL_NAMES, DeathDetector, death_score_weights_valid

BATTLE_START = 100.0


def _config(**overrides: object) -> DeathConfig:
    return replace(DeathConfig(), **overrides)


def _detector(config: DeathConfig | None = None, battle_started_at: float = BATTLE_START) -> DeathDetector:
    return DeathDetector(config or DeathConfig(), battle_started_at=battle_started_at)


def _all_true() -> dict[str, bool]:
    return dict.fromkeys(SIGNAL_NAMES, True)


def _only(name: str) -> dict[str, bool]:
    votes = dict.fromkeys(SIGNAL_NAMES, False)
    votes[name] = True
    return votes


# ---------------------------------------------------------------------------
# 开局静默
# ---------------------------------------------------------------------------


def test_ignore_start_window_does_not_decide() -> None:
    """战斗开始后的 ignore_start_s 内即使全部信号命中也不判定、不计时。"""
    detector = _detector()
    votes = {"hp_bar_absent": True, "hp_zero": True, "banner": True}
    signals = detector.update(votes, BATTLE_START + 4.9)
    assert detector.decided is False
    assert signals.score == 0.0
    assert signals.stable_ms == 0
    assert signals.hp_bar_absent is True  # 原始信号仍如实上报
    # 静默结束后重新计时：105.0 开始，hp 强信号需要 1.5s。
    detector.update(votes, BATTLE_START + 5.0)
    detector.update(votes, BATTLE_START + 6.4)
    assert detector.decided is False
    detector.update(votes, BATTLE_START + 6.5)
    assert detector.decided is True


def test_ignore_start_zero_starts_immediately() -> None:
    detector = _detector(_config(ignore_start_s=0.0), battle_started_at=0.0)
    detector.update(_only("hp_bar_absent"), 0.0)
    detector.update(_only("hp_bar_absent"), 1.5)
    assert detector.decided is True
    assert detector.decision_mode == "strong:hp_bar_absent"


# ---------------------------------------------------------------------------
# 强信号直判
# ---------------------------------------------------------------------------


def test_hp_bar_absent_strong_signal_direct_decision() -> None:
    """hp_bar_absent 连续稳定 strong_stable_s 后直接判定死亡。"""
    detector = _detector()
    detector.update(_only("hp_bar_absent"), BATTLE_START + 5.0)
    detector.update(_only("hp_bar_absent"), BATTLE_START + 6.0)
    assert detector.decided is False
    assert detector.decision_mode is None
    signals = detector.update(_only("hp_bar_absent"), BATTLE_START + 6.5)
    assert detector.decided is True
    assert detector.decision_mode == "strong:hp_bar_absent"
    assert detector.decided_at == BATTLE_START + 6.5
    assert signals.stable_ms == 1500
    # 判定成立后 score 取下限 0.75，兼容状态机消费的 DeathSignals.dead；
    # 原始加权得分保留在 detector.decision_raw_score，便于日志分析。
    assert detector.decision_raw_score == 0.35
    assert detector.last_raw_score == 0.35
    assert signals.score == 0.75
    assert signals.dead is True


def test_hp_zero_strong_signal_direct_decision() -> None:
    """hp_zero 连续稳定 strong_stable_s 后直接判定死亡。"""
    detector = _detector()
    for timestamp in (5.0, 6.0):
        detector.update(_only("hp_zero"), BATTLE_START + timestamp)
        assert detector.decided is False
    detector.update(_only("hp_zero"), BATTLE_START + 6.5)
    assert detector.decided is True
    assert detector.decision_mode == "strong:hp_zero"


def test_banner_alone_does_not_decide() -> None:
    """banner 权重 0.25 单独不足阈值，且不是强信号。"""
    detector = _detector()
    for timestamp in (5.0, 6.0, 7.0, 10.0):
        detector.update(_only("banner"), BATTLE_START + timestamp)
    assert detector.decided is False


# ---------------------------------------------------------------------------
# 加权投票
# ---------------------------------------------------------------------------


def test_weighted_vote_requires_sum_and_stable_window() -> None:
    """加权投票：得分达到阈值后还需连续稳定 stable_s 秒。"""
    config = _config(
        weights={"banner": 0.4, "marker_lost": 0.3, "spectator_ui": 0.3},
        score_threshold=0.6,
        stable_s=1.0,
        strong_stable_s=1.5,
        marker_lost_stable_s=0.5,
        ignore_start_s=0.0,
    )
    detector = _detector(config, battle_started_at=0.0)
    votes = {"banner": True, "marker_lost": True, "spectator_ui": False}
    detector.update(votes, 0.0)
    # marker_lost 0.5s 后进入计分，banner 需要 1.0s。
    detector.update(votes, 0.5)
    assert detector.decided is False
    detector.update(votes, 1.0)
    assert detector.decided is False  # 得分刚达标，稳定窗口从 1.0 开始
    detector.update(votes, 1.9)
    assert detector.decided is False
    detector.update(votes, 2.0)
    assert detector.decided is True
    assert detector.decision_mode == "vote"
    decided_signals = detector.last_signals
    assert decided_signals is not None
    assert decided_signals.stable_ms == 1000
    assert decided_signals.score >= 0.75
    assert decided_signals.dead is True  # 状态机通过 death.dead 消费


def test_weighted_vote_early_strong_signal_wins() -> None:
    """强信号先满足条件时优先走直判（与投票结果一致但不等待投票稳定窗口）。"""
    detector = _detector()
    votes = {"hp_bar_absent": True, "hp_zero": True, "banner": True}
    for timestamp in (5.0, 6.0):
        detector.update(votes, BATTLE_START + timestamp)
        assert detector.decided is False
    detector.update(votes, BATTLE_START + 6.5)
    assert detector.decided is True
    assert detector.decision_mode == "strong:hp_bar_absent"


# ---------------------------------------------------------------------------
# banner 锁存
# ---------------------------------------------------------------------------


def test_banner_latch_keeps_vote_after_banner_disappears() -> None:
    """banner 提示消失后锁存 banner_latch_s 秒，投票与 votes 字段保持命中。"""
    config = _config(banner_latch_s=3.0, ignore_start_s=0.0, strong_stable_s=10.0)
    detector = _detector(config, battle_started_at=0.0)
    first = detector.update({"banner": True}, 0.0)
    assert first.banner is True
    second = detector.update({"banner": False}, 1.0)
    assert second.banner is True
    assert second.votes["banner"] is True
    assert detector.update({"banner": False}, 2.9).banner is True
    # 锁存到 3.0 秒为止。
    assert detector.update({"banner": False}, 3.0).banner is True
    assert detector.update({"banner": False}, 3.1).banner is False


# ---------------------------------------------------------------------------
# 观战模式
# ---------------------------------------------------------------------------


def test_spectator_mode_disables_hp_signal_voting() -> None:
    """spectator_ui 命中后，己方血条信号不再参与投票、也不触发强信号直判。"""
    detector = _detector()
    detector.update(_only("spectator_ui"), BATTLE_START + 5.0)
    assert detector.spectator_mode is True
    votes = {"hp_bar_absent": True, "hp_zero": True, "spectator_ui": True}
    for timestamp in (5.5, 6.0, 7.0, 8.0, 9.0, 10.0):
        signals = detector.update(votes, BATTLE_START + timestamp)
        assert detector.decided is False
        assert signals.hp_bar_absent is True  # 原始信号仍上报，但不计分
    # 观战模式下只剩 spectator_ui 自身权重 0.05，远低于阈值。
    assert signals.score == pytest.approx(0.05)


def test_spectator_mode_allows_banner_and_marker_votes() -> None:
    """观战模式下 banner / marker_lost 仍计入加权分（只是排除己方血条）。"""
    config = _config(
        weights={"banner": 0.4, "marker_lost": 0.3, "spectator_ui": 0.3},
        score_threshold=0.6,
        stable_s=1.0,
        marker_lost_stable_s=0.5,
        strong_stable_s=1.5,
        ignore_start_s=0.0,
    )
    detector = _detector(config, battle_started_at=0.0)
    votes = {"spectator_ui": True, "banner": True, "marker_lost": True, "hp_bar_absent": True}
    detector.update(votes, 0.0)
    detector.update(votes, 0.5)
    detector.update(votes, 1.0)
    detector.update(votes, 2.0)
    assert detector.decided is True
    assert detector.decision_mode == "vote"


# ---------------------------------------------------------------------------
# 矛盾信号只清对应信号
# ---------------------------------------------------------------------------


def test_contradiction_only_resets_affected_signal() -> None:
    """某信号由真变假只重置它自己的稳定计时，其他信号计时继续。"""
    config = _config(
        weights={"hp_bar_absent": 1.0, "hp_zero": 1.0, "banner": 0.5},
        score_threshold=1.2,
        stable_s=1.0,
        strong_stable_s=3.0,
        marker_lost_stable_s=3.0,
        ignore_start_s=0.0,
        banner_latch_s=0.0,
    )
    detector = _detector(config, battle_started_at=0.0)
    both = {"hp_bar_absent": True, "hp_zero": True, "banner": True}
    without_hp_bar_absent = {"hp_bar_absent": False, "hp_zero": True, "banner": True}
    detector.update(both, 0.0)
    detector.update(both, 1.0)
    contradiction = detector.update(without_hp_bar_absent, 1.5)
    # hp_zero 与 banner 的稳定计时未被清零：得分仍达标（1.0 + 0.5）。
    assert contradiction.score == 1.5
    assert detector.decided is False
    detector.update(both, 1.6)
    # 投票条件从 1.0s 起持续满足，2.0s 到达 stable_s 判定。
    detector.update(both, 2.0)
    assert detector.decided is True
    assert detector.decision_mode == "vote"


def test_contradiction_resets_vote_condition() -> None:
    """得分跌破阈值时投票稳定窗口重新计时（对应信号恢复后需重新累计）。"""
    config = _config(
        weights={"banner": 1.0, "marker_lost": 1.0},
        score_threshold=1.0,
        stable_s=1.0,
        strong_stable_s=10.0,
        marker_lost_stable_s=0.2,
        ignore_start_s=0.0,
        banner_latch_s=0.0,
    )
    detector = _detector(config, battle_started_at=0.0)
    on = {"banner": True, "marker_lost": True}
    off = {"banner": False, "marker_lost": False}
    detector.update(on, 0.0)
    detector.update(on, 1.0)
    detector.update(on, 1.5)
    detector.update(off, 1.6)  # 得分归零，投票窗口重新计时
    detector.update(on, 2.0)
    detector.update(on, 2.9)
    assert detector.decided is False
    detector.update(on, 3.0)
    assert detector.decided is False  # 投票窗口从 2.9s 重新计时
    detector.update(on, 4.0)
    assert detector.decided is True


# ---------------------------------------------------------------------------
# marker_lost 独立稳定时间
# ---------------------------------------------------------------------------


def test_marker_lost_uses_own_stable_time() -> None:
    """marker_lost 信号需连续稳定 marker_lost_stable_s 才计分。"""
    config = _config(
        weights={"marker_lost": 1.0},
        score_threshold=0.5,
        stable_s=0.2,
        marker_lost_stable_s=1.0,
        strong_stable_s=10.0,
        ignore_start_s=0.0,
    )
    detector = _detector(config, battle_started_at=0.0)
    detector.update(_only("marker_lost"), 0.0)
    detector.update(_only("marker_lost"), 0.9)
    assert detector.decided is False
    detector.update(_only("marker_lost"), 1.0)
    assert detector.decided is False  # 刚计分，投票稳定窗口 0.2s 未满足
    detector.update(_only("marker_lost"), 1.5)
    assert detector.decided is True


# ---------------------------------------------------------------------------
# 锁存、复位与输出结构
# ---------------------------------------------------------------------------


def test_decision_is_latched_until_reset() -> None:
    """判定成立后信号消失也不回退，直到 reset。"""
    detector = _detector()
    for timestamp in (5.0, 6.0, 6.5):
        detector.update(_only("hp_bar_absent"), BATTLE_START + timestamp)
    assert detector.decided is True
    signals = detector.update(_only("banner"), BATTLE_START + 8.0)
    assert detector.decided is True
    assert signals.hp_bar_absent is False
    assert signals.stable_ms >= 1500
    detector.reset()
    assert detector.decided is False
    assert detector.spectator_mode is False
    assert detector.decision_mode is None
    assert detector.last_signals is None
    cleared = detector.update({}, BATTLE_START + 9.0)
    assert cleared.score == 0.0


def test_death_signals_fields_and_votes() -> None:
    """输出 DeathSignals 五个信号字段与 votes 字典一致。"""
    detector = _detector(_config(ignore_start_s=0.0), battle_started_at=0.0)
    signals = detector.update(_all_true(), 0.0)
    assert isinstance(signals, DeathSignals)
    assert signals.votes == dict.fromkeys(SIGNAL_NAMES, True)
    assert signals.hp_bar_absent and signals.hp_zero and signals.banner
    assert signals.marker_lost and signals.spectator_ui
    assert signals.timestamp == 0.0
    payload = signals.to_dict()
    assert payload["votes"]["banner"] is True
    assert set(payload["votes"]) == set(SIGNAL_NAMES)


def test_unknown_vote_keys_are_ignored() -> None:
    """未知信号键不影响判定。"""
    detector = _detector(_config(ignore_start_s=0.0), battle_started_at=0.0)
    signals = detector.update({"unknown_signal": True}, 0.0)
    assert signals.votes == dict.fromkeys(SIGNAL_NAMES, False)
    assert detector.decided is False


def test_weights_helper() -> None:
    assert death_score_weights_valid(DeathConfig()) is True
    assert death_score_weights_valid(_config(weights={"banner": -1.0})) is False
    assert death_score_weights_valid(_config(weights={})) is False
