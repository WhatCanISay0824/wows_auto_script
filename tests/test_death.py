"""死亡投票测试（详细设计 5.9；评审 P0-7 修正口径 / 评审 10.2 退出授权）。

覆盖：
- 开局静默窗口（ignore_start_s）内不判定、不计时；
- 强信号直判（hp_bar_absent / hp_zero 稳定 strong_stable_s）；
- 加权投票（分数阈值 + 稳定窗口，各信号自身稳定时间）；
- banner 命中后锁存，提示消失后投票不回落；
- 观战模式后己方血条信号不参与投票、不触发直判；
- 矛盾只清对应信号，不清空其他信号计时；
- marker_lost 使用独立的 marker_lost_stable_s；
- 判定后锁存、reset 清空、votes 字段与额外键容忍；
- ISS-002 / 评审 10.2：血条归零证据（hp_fill_ratio/hp_evidence）、连续帧确认、
  证据 TTL、矛盾证据撤销，以及 banner/hp_bar_absent/marker_lost 不能单独授权退出。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from src.common.types import DeathSignals
from src.config.models import BattleHudConfig, DeathConfig
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


# ---------------------------------------------------------------------------
# ISS-002 / 评审 10.2：血条归零证据（hp_fill_ratio / hp_evidence）与退出授权
# ---------------------------------------------------------------------------


def _hud_detector(
    *,
    confirm_frames: int = 2,
    ttl_s: float = 1.0,
    config: DeathConfig | None = None,
    battle_started_at: float = 0.0,
) -> DeathDetector:
    return DeathDetector(
        config or _config(ignore_start_s=0.0),
        battle_started_at=battle_started_at,
        hud=BattleHudConfig(
            hp_bar_confirm_frames=confirm_frames,
            hp_bar_evidence_ttl_s=ttl_s,
        ),
    )


def _hp_update(
    detector: DeathDetector,
    timestamp: float,
    *,
    evidence: str = "zero",
    ratio: float | None = 0.0,
    frame_id: int | None = None,
    votes: dict[str, bool] | None = None,
):
    return detector.update(
        votes or {},
        timestamp,
        hp_fill_ratio=ratio,
        hp_evidence=evidence,
        frame_id=frame_id,
    )


def test_hp_evidence_requires_consecutive_frames_and_ttl() -> None:
    """连续 2 个新帧 zero 才授权；同一帧重复消费不刷新证据，TTL 到期失效。"""
    detector = _hud_detector(confirm_frames=2, ttl_s=1.0)
    first = _hp_update(detector, 1.0, frame_id=1)
    assert first.hp_evidence == "zero"
    assert first.hp_fill_ratio == 0.0
    assert first.exit_authorized is False
    assert first.evidence_at == 0.0
    second = _hp_update(detector, 1.5, frame_id=2)
    assert second.exit_authorized is True
    assert second.evidence_at == 1.5
    # 同一帧（frame_id=2）反复调用：不刷新证据时间戳，1.0s TTL 内仍有效、超时失效。
    assert _hp_update(detector, 2.4, frame_id=2).exit_authorized is True
    assert _hp_update(detector, 2.6, frame_id=2).exit_authorized is False


def test_banner_and_hp_bar_absent_do_not_authorize_exit() -> None:
    """banner / hp_bar_absent / marker_lost 不能单独授权提前退出（只能参与投票/日志）。"""
    detector = _hud_detector()
    votes = {"hp_bar_absent": True, "banner": True, "marker_lost": True}
    for timestamp in (1.0, 1.5, 2.0, 3.0, 4.0):
        signals = detector.update(
            votes,
            timestamp,
            hp_fill_ratio=None,
            hp_evidence="unavailable",
            frame_id=int(timestamp * 2),
        )
        assert signals.exit_authorized is False
        assert signals.hp_evidence == "unavailable"


def test_unknown_evidence_does_not_authorize_exit() -> None:
    """空条上限与存活下限之间的 unknown 死区不授权（保守等待自然结算）。"""
    detector = _hud_detector()
    for frame_id, timestamp in enumerate((1.0, 1.5, 2.0, 2.5), start=1):
        signals = _hp_update(detector, timestamp, evidence="unknown", ratio=0.05, frame_id=frame_id)
        assert signals.exit_authorized is False
        assert signals.hp_evidence == "unknown"


def test_alive_evidence_revokes_exit_authorization() -> None:
    """授权后出现存活帧：立即撤销授权，evidence_at 清零。"""
    detector = _hud_detector()
    _hp_update(detector, 1.0, frame_id=1)
    authorized = _hp_update(detector, 1.5, frame_id=2)
    assert authorized.exit_authorized is True
    revoked = _hp_update(detector, 2.0, evidence="alive", ratio=0.6, frame_id=3)
    assert revoked.exit_authorized is False
    assert revoked.hp_evidence == "alive"
    assert revoked.evidence_at == 0.0


def test_spectator_mode_disables_hp_exit_authorization() -> None:
    """观战模式下看到的不是己方血条：零证据失效、不授权。"""
    detector = _hud_detector()
    _hp_update(detector, 1.0, frame_id=1)
    _hp_update(detector, 1.5, frame_id=2)
    signals = detector.update(
        {"spectator_ui": True},
        2.0,
        hp_fill_ratio=0.0,
        hp_evidence="zero",
        frame_id=3,
    )
    assert detector.spectator_mode is True
    assert signals.hp_evidence == "unavailable"
    assert signals.hp_fill_ratio is None
    assert signals.exit_authorized is False


def test_ignore_start_window_does_not_authorize_exit() -> None:
    """战斗开始后 ignore_start_s 内不累计归零证据（如实上报但证据时间戳为 0）。"""
    detector = _hud_detector(config=_config(ignore_start_s=5.0), battle_started_at=10.0)
    for frame_id, timestamp in enumerate((10.0, 10.5, 11.0, 12.0), start=1):
        signals = _hp_update(detector, timestamp, frame_id=frame_id)
        assert signals.hp_evidence == "zero"
        assert signals.exit_authorized is False
        assert signals.evidence_at == 0.0
    # 静默结束后重新累计连续帧。
    _hp_update(detector, 15.0, frame_id=10)
    assert _hp_update(detector, 15.5, frame_id=11).exit_authorized is True


def test_hp_evidence_values_are_sanitized() -> None:
    """非法证据值降为 unknown；zero 没有填充比例也降为 unknown（不凭空授权）。"""
    detector = _hud_detector()
    garbage = _hp_update(detector, 1.0, evidence="not-a-level", ratio=0.0, frame_id=1)
    assert garbage.hp_evidence == "unknown"
    no_ratio = _hp_update(detector, 1.5, evidence="zero", ratio=None, frame_id=2)
    assert no_ratio.hp_evidence == "unknown"
    assert no_ratio.exit_authorized is False


def test_legacy_votes_without_hp_evidence_never_authorize() -> None:
    """未传血条证据的旧调用（只传 votes）可继续投票，但绝不授权提前退出。"""
    detector = _hud_detector()
    for timestamp in (1.0, 2.0, 3.0):
        signals = detector.update(_only("hp_zero"), timestamp)
        assert signals.exit_authorized is False
        assert signals.hp_evidence == "unavailable"
        assert signals.evidence_at == 0.0


def test_death_signals_to_dict_contains_hp_evidence_fields() -> None:
    """DeathSignals 序列化包含 4 个新字段（诊断日志/复盘用）。"""
    detector = _hud_detector()
    _hp_update(detector, 1.0, frame_id=1)
    signals = _hp_update(detector, 1.5, frame_id=2)
    payload = signals.to_dict()
    assert payload["hp_evidence"] == "zero"
    assert payload["hp_fill_ratio"] == 0.0
    assert payload["exit_authorized"] is True
    assert payload["evidence_at"] == 1.5
    assert signals.to_dict()["votes"]["hp_zero"] is True
