"""死亡判定：多信号加权投票（详细设计 5.9，评审 P0-7 修正版）。

输入是上游感知给出的 5 个布尔信号（``hp_bar_absent`` / ``hp_zero`` / ``banner`` /
``marker_lost`` / ``spectator_ui``），输出带得分、稳定时间与逐信号投票的
``DeathSignals``。判定规则：

- **开局静默**：战斗开始后 ``ignore_start_s`` 秒内不判定（默认 5 秒），避免加载残留
  画面误判；静默期内不累积任何稳定计时。
- **强信号直判**：``hp_bar_absent`` 或 ``hp_zero`` 连续稳定 ``strong_stable_s``
  （默认 1.5 秒）后直接判定死亡。
- **死亡提示锁存**：``banner`` 命中后锁存 ``banner_latch_s`` 秒（提示文字只显示数秒，
  消失后投票不应立刻回落）。
- **观战模式**：``spectator_ui`` 命中后进入观战模式，此后己方血条信号
  （``hp_bar_absent`` / ``hp_zero``）不再参与投票，也不再触发强信号直判。
- **加权投票（辅助）**：各信号先满足自己的稳定时间（普通信号 ``stable_s``，
  ``marker_lost`` 用 ``marker_lost_stable_s``）后按 ``cfg.weights`` 累加；
  得分 >= ``score_threshold`` 且该条件连续保持 ``stable_s`` 秒时判定死亡。
- **矛盾只清对应信号**：某个信号由真变假时只重置它自己的稳定计时，不清空其他信号
  的计时（评审 P0-7；详细设计 5.9 原文“清零投票”按评审修订为逐信号处理）。

与公共类型 / 状态机的约定
--------------------------
- 引擎与状态机目前通过 ``DeathSignals.dead`` 这一便捷属性消费死亡结果（
  ``score >= 0.75 and stable_ms >= 1000``，见 ``common.types`` 与
  ``state_machine.machine``）。为兼容该消费方式：**判定成立后**输出的
  ``DeathSignals.score`` 取下限 ``DEAD_SIGNAL_SCORE_FLOOR``（0.75），
  ``stable_ms`` 为实际稳定时间（强信号 1500ms、投票 1000ms，默认配置下均 >= 1000），
  因此 ``DeathSignals.dead`` 在判定后为 True；原始加权得分通过
  ``DeathDetector.decision_raw_score`` / ``last_raw_score`` 保留，便于日志分析。
- 权威判定仍是 ``DeathDetector.decided``（判定后锁存到 reset）；若把配置里的
  ``stable_s`` 调到 1.0 秒以下，``DeathSignals.dead`` 的 1000ms 阈值可能不满足，
  此时调用方应以 ``decided`` 为准。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping

from ..common.types import DeathSignals
from ..config.models import DeathConfig

logger = logging.getLogger(__name__)

#: 判定成立后 DeathSignals.score 的下限，用于兼容 ``DeathSignals.dead``（0.75 阈值）。
DEAD_SIGNAL_SCORE_FLOOR = 0.75

#: 参与投票的全部信号名（与 DeathConfig.weights 的键一致）。
SIGNAL_NAMES: tuple[str, ...] = (
    "hp_bar_absent",
    "hp_zero",
    "banner",
    "marker_lost",
    "spectator_ui",
)
#: 强信号（稳定 strong_stable_s 后可直判死亡）。
STRONG_SIGNALS: tuple[str, ...] = ("hp_bar_absent", "hp_zero")
#: 观战模式下不再参与投票的信号（己方血条）。
HP_SIGNALS: tuple[str, ...] = ("hp_bar_absent", "hp_zero")


class DeathDetector:
    """死亡投票器（详细设计 5.9）。

    用法::

        detector = DeathDetector(config.perception.death, battle_started_at=now)
        signals = detector.update(votes, now)
        if detector.decided:
            ...  # 进入退出流程

    ``update`` 可以每帧（约 2 Hz）调用；同一实例内计时连续，``reset`` 用于复用实例。
    """

    def __init__(self, cfg: DeathConfig, battle_started_at: float) -> None:
        self._cfg = cfg
        self._battle_started_at = float(battle_started_at)
        self._weights: dict[str, float] = {
            name: float(cfg.weights.get(name, 0.0)) for name in SIGNAL_NAMES
        }
        self._signal_since: dict[str, float | None] = {name: None for name in SIGNAL_NAMES}
        self._banner_latched_until: float | None = None
        self._spectator_mode = False
        self._vote_since: float | None = None
        self._decided = False
        self._decided_at: float | None = None
        self._decision_mode: str | None = None
        self._decision_score = 0.0
        self._decision_stable_ms = 0
        self._last_signals: DeathSignals | None = None
        self._last_raw_score = 0.0

    # ------------------------------------------------------------------
    # 公开状态
    # ------------------------------------------------------------------
    @property
    def decided(self) -> bool:
        """死亡判定是否已成立（权威结果，判定后保持锁存直到 reset）。"""
        return self._decided

    @property
    def decided_at(self) -> float | None:
        return self._decided_at

    @property
    def decision_mode(self) -> str | None:
        """判定依据：``strong:<signal>`` 或 ``vote``；未判定时为 None。"""
        return self._decision_mode

    @property
    def spectator_mode(self) -> bool:
        """是否已进入观战模式（观战后己方血条信号不再参与投票）。"""
        return self._spectator_mode

    @property
    def last_signals(self) -> DeathSignals | None:
        return self._last_signals

    @property
    def decision_raw_score(self) -> float:
        """判定成立时的原始加权得分（未做 0.75 下限兼容处理，供日志分析）。"""
        return self._decision_score

    @property
    def last_raw_score(self) -> float:
        """最近一帧的原始加权得分（未做 0.75 下限兼容处理）。"""
        return self._last_raw_score

    @property
    def battle_started_at(self) -> float:
        return self._battle_started_at

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """清空全部计时与判定状态（新战斗/重新开始监测时调用）。"""
        for name in SIGNAL_NAMES:
            self._signal_since[name] = None
        self._banner_latched_until = None
        self._spectator_mode = False
        self._vote_since = None
        self._decided = False
        self._decided_at = None
        self._decision_mode = None
        self._decision_score = 0.0
        self._decision_stable_ms = 0
        self._last_signals = None
        self._last_raw_score = 0.0

    def update(self, votes: Mapping[str, bool], timestamp: float) -> DeathSignals:
        """输入一帧投票并返回当前死亡信号。

        参数：
            votes: 信号名 -> 是否命中；缺失的键按 False 处理，多余键忽略。
            timestamp: 该帧时间戳（单调时钟秒，与 ``battle_started_at`` 同一时钟）。
        """
        timestamp = float(timestamp)
        raw = {name: bool(votes.get(name, False)) for name in SIGNAL_NAMES}
        if self._in_ignore_window(timestamp):
            self._clear_evidence()
            signals = self._compose(
                effective=raw, score=0.0, stable_ms=0, timestamp=timestamp
            )
            self._last_signals = signals
            self._last_raw_score = 0.0
            return signals
        effective = self._apply_latches(raw, timestamp)
        self._update_signal_timers(effective, timestamp)
        score = self._weighted_score(timestamp)
        self._last_raw_score = score
        if self._decided:
            stable_ms = self._decision_stable_ms + int(
                max(0.0, timestamp - (self._decided_at or timestamp)) * 1000
            )
            signals = self._compose(
                effective=effective, score=score, stable_ms=stable_ms, timestamp=timestamp
            )
            self._last_signals = signals
            return signals

        strong_mode = self._strong_signal_mode(timestamp)
        if strong_mode is not None:
            self._decide(
                timestamp=timestamp,
                mode=f"strong:{strong_mode}",
                score=score,
                stable_ms=int(float(self._cfg.strong_stable_s) * 1000),
            )
        else:
            self._evaluate_weighted_vote(timestamp, score)
        stable_ms = (
            self._decision_stable_ms if self._decided else self._evidence_stable_ms(timestamp)
        )
        signals = self._compose(
            effective=effective, score=score, stable_ms=stable_ms, timestamp=timestamp
        )
        self._last_signals = signals
        return signals

    # ------------------------------------------------------------------
    # 内部：时间与锁存
    # ------------------------------------------------------------------
    def _ignore_until(self) -> float:
        return self._battle_started_at + float(self._cfg.ignore_start_s)

    def _in_ignore_window(self, timestamp: float) -> bool:
        return timestamp < self._ignore_until()

    def _clear_evidence(self) -> None:
        for name in SIGNAL_NAMES:
            self._signal_since[name] = None
        self._banner_latched_until = None
        self._spectator_mode = False
        self._vote_since = None
        self._decided = False
        self._decided_at = None
        self._decision_mode = None
        self._decision_score = 0.0
        self._decision_stable_ms = 0

    def _apply_latches(
        self, raw: dict[str, bool], timestamp: float
    ) -> dict[str, bool]:
        effective = dict(raw)
        latch_s = float(self._cfg.banner_latch_s)
        if raw["banner"] and latch_s > 0:
            self._banner_latched_until = timestamp + latch_s
        effective["banner"] = bool(
            raw["banner"]
            or (
                self._banner_latched_until is not None
                and timestamp <= self._banner_latched_until
            )
        )
        if raw["spectator_ui"]:
            self._spectator_mode = True
        return effective

    def _update_signal_timers(self, effective: dict[str, bool], timestamp: float) -> None:
        for name in SIGNAL_NAMES:
            if effective[name]:
                if self._signal_since[name] is None:
                    self._signal_since[name] = timestamp
            else:
                # 矛盾只清零对应信号（评审 P0-7）。
                self._signal_since[name] = None

    # ------------------------------------------------------------------
    # 内部：稳定性与得分
    # ------------------------------------------------------------------
    def _signal_stable_s(self, name: str) -> float:
        if name == "marker_lost":
            return float(self._cfg.marker_lost_stable_s)
        return float(self._cfg.stable_s)

    def _signal_active(self, name: str, timestamp: float) -> bool:
        since = self._signal_since[name]
        if since is None:
            return False
        return timestamp - since >= self._signal_stable_s(name)

    def _weighted_score(self, timestamp: float) -> float:
        score = 0.0
        for name in SIGNAL_NAMES:
            if self._spectator_mode and name in HP_SIGNALS:
                continue
            if self._signal_active(name, timestamp):
                score += self._weights.get(name, 0.0)
        return float(score)

    def _strong_signal_mode(self, timestamp: float) -> str | None:
        if self._spectator_mode:
            return None
        for name in STRONG_SIGNALS:
            since = self._signal_since[name]
            if since is not None and timestamp - since >= float(self._cfg.strong_stable_s):
                return name
        return None

    def _evaluate_weighted_vote(self, timestamp: float, score: float) -> None:
        threshold = float(self._cfg.score_threshold)
        if score < threshold:
            self._vote_since = None
            return
        if self._vote_since is None:
            self._vote_since = timestamp
        if timestamp - self._vote_since >= float(self._cfg.stable_s):
            self._decide(
                timestamp=timestamp,
                mode="vote",
                score=score,
                stable_ms=int(float(self._cfg.stable_s) * 1000),
            )

    def _evidence_stable_ms(self, timestamp: float) -> int:
        if self._vote_since is not None:
            return int(max(0.0, timestamp - self._vote_since) * 1000)
        stable_since = [
            since
            for name, since in self._signal_since.items()
            if since is not None
        ]
        if not stable_since:
            return 0
        return int(max(0.0, timestamp - min(stable_since)) * 1000)

    def _decide(self, *, timestamp: float, mode: str, score: float, stable_ms: int) -> None:
        if self._decided:
            return
        self._decided = True
        self._decided_at = timestamp
        self._decision_mode = mode
        self._decision_score = float(score)
        self._decision_stable_ms = int(stable_ms)
        logger.info(
            "死亡判定成立：依据=%s 得分=%.2f 稳定=%dms 时间戳=%.3f",
            mode,
            score,
            stable_ms,
            timestamp,
        )

    # ------------------------------------------------------------------
    # 内部：输出
    # ------------------------------------------------------------------
    def _compose(
        self,
        *,
        effective: Mapping[str, bool],
        score: float,
        stable_ms: int,
        timestamp: float,
    ) -> DeathSignals:
        votes = {name: bool(effective.get(name, False)) for name in SIGNAL_NAMES}
        reported_score = float(max(0.0, score))
        if self._decided:
            # 兼容 DeathSignals.dead 的 0.75 阈值（见模块 docstring）；原始分见 last_raw_score。
            reported_score = max(reported_score, DEAD_SIGNAL_SCORE_FLOOR)
        return DeathSignals(
            hp_bar_absent=votes["hp_bar_absent"],
            hp_zero=votes["hp_zero"],
            banner=votes["banner"],
            marker_lost=votes["marker_lost"],
            spectator_ui=votes["spectator_ui"],
            score=reported_score,
            stable_ms=int(max(0, stable_ms)),
            timestamp=float(timestamp),
            votes=votes,
        )


def death_score_weights_valid(cfg: DeathConfig) -> bool:
    """辅助校验：权重非负且总和为正（供配置校验工具调用）。"""
    weights = [float(cfg.weights.get(name, 0.0)) for name in SIGNAL_NAMES]
    if any(weight < 0 for weight in weights):
        return False
    return math.fsum(weights) > 0
