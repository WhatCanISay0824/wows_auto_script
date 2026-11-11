"""死亡判定：多信号加权投票 + 血条归零证据与提前退出授权（详细设计 5.9，
评审 P0-7 / 10.2 修正版）。

输入是上游感知给出的 5 个布尔信号（``hp_bar_absent`` / ``hp_zero`` / ``banner`` /
``marker_lost`` / ``spectator_ui``），输出带得分、稳定时间与逐信号投票的
``DeathSignals``。判定规则：

- **开局静默**：战斗开始后 ``ignore_start_s`` 秒内不判定（默认 5 秒），避免加载残留
  画面误判；静默期内不累积任何稳定计时，也不授权提前退出。
- **强信号直判**：``hp_bar_absent`` 或 ``hp_zero`` 连续稳定 ``strong_stable_s``
  （默认 1.5 秒）后直接判定死亡。注意：判定/投票结果只用于日志与统计，
  **不能单独作为提前退出授权**（见下一条）。
- **死亡提示锁存**：``banner`` 命中后锁存 ``banner_latch_s`` 秒（提示文字只显示数秒，
  消失后投票不应立刻回落）。
- **观战模式**：``spectator_ui`` 命中后进入观战模式，此后己方血条信号
  （``hp_bar_absent`` / ``hp_zero``）不再参与投票、不触发强信号直判，
  且血条归零证据直接失效（观战界面看到的不是己方血条）。
- **加权投票（辅助）**：各信号先满足自己的稳定时间（普通信号 ``stable_s``，
  ``marker_lost`` 用 ``marker_lost_stable_s``）后按 ``cfg.weights`` 累加；
  得分 >= ``score_threshold`` 且该条件连续保持 ``stable_s`` 秒时判定死亡。
- **矛盾只清对应信号**：某个信号由真变假时只重置它自己的稳定计时，不清空其他信号
  的计时（评审 P0-7；详细设计 5.9 原文“清零投票”按评审修订为逐信号处理）。

血条归零证据与退出授权（ISS-002 / 评审 10.2）
---------------------------------------------
状态机的 ``BATTLE_ACTIVE -> DEAD_PENDING_EXIT`` 与“按 ESC/离开战斗”前的二次校验
**只认** ``DeathSignals.exit_authorized``。它由感知上游（``PerceptionEngine``）给出的
数字判零或兼容配置的血条填充分析驱动：

- 配置 ``hp_zero_feature`` 时，必须从己方血量数字确认 ``hp_current == 0``；
  未配置时使用血条填充兼容路径，低于 ``hp_bar_empty_max_ratio`` 才给出零血证据；
- 连续 ``hp_bar_confirm_frames`` 个**新帧**（同一帧被 10Hz 主循环重复消费只计一次）
  都是 ``zero``；
- 最近一次归零证据在 ``hp_bar_evidence_ttl_s`` 内（超时或没有新帧即撤销）；
- 出现 ``alive`` / ``unknown`` / ``unavailable`` 矛盾证据立即撤销。

``banner`` / ``hp_bar_absent`` / ``marker_lost`` / ``spectator_ui`` 仍参与投票与日志，
但**不能单独授权提前退出**。无法证明归零时状态机等待自然结算（BATTLE_FINISHED）。

与公共类型 / 状态机的约定
--------------------------
- ``DeathSignals.dead`` 是历史便捷属性（``score >= 0.75 and stable_ms >= 1000``），
  只为兼容日志/旧消费方保留；**退出授权必须以 ``exit_authorized`` 为准**。
  判定成立后输出 ``score`` 取下限 ``DEAD_SIGNAL_SCORE_FLOOR``（0.75），原始加权得分
  通过 ``DeathDetector.decision_raw_score`` / ``last_raw_score`` 保留。
- 权威判定仍是 ``DeathDetector.decided``（判定后锁存到 reset）；若把配置里的
  ``stable_s`` 调到 1.0 秒以下，``DeathSignals.dead`` 的 1000ms 阈值可能不满足，
  此时调用方应以 ``decided`` 为准。
- ``update()`` 的 ``hp_fill_ratio`` / ``hp_evidence`` / ``frame_id`` 是关键字参数：
  引擎每帧传入；不传时沿用 ``votes`` 里的 ``hp_zero``（兼容手工调用/单元测试），
  但此时 ``exit_authorized`` 恒为 False。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping

from ..common.types import HP_EVIDENCE_LEVELS, DeathSignals
from ..config.models import BattleHudConfig, DeathConfig

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
#: 可以确认“归零”的证据等级（其余等级都会撤销退出授权）。
_ZERO_EVIDENCE = "zero"


def _normalize_hp_evidence(value: str | None) -> str:
    """把外部传入的血条证据等级规范化为 ``HP_EVIDENCE_LEVELS`` 之一（未知值保守降为 unknown）。"""
    if value is None:
        return "unavailable"
    text_value = str(value)
    if text_value in HP_EVIDENCE_LEVELS:
        return text_value
    logger.warning("未知血条证据等级 %r，按 unknown 处理（不授权退出）", value)
    return "unknown"


def _normalize_fill_ratio(value: float | None) -> float | None:
    """规范化填充列比例：非法/NaN/越界按“无数据”处理，避免脏数据触发误授权。"""
    if value is None:
        return None
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ratio):
        return None
    return min(1.0, max(0.0, ratio))


def _normalize_hp_current(value: int | None) -> int | None:
    """目前只确认数字零值；其他值不冒称可读，统一返回 None。"""
    return 0 if isinstance(value, int) and not isinstance(value, bool) and value == 0 else None


class DeathDetector:
    """死亡投票器（详细设计 5.9）。

    用法::

        detector = DeathDetector(config.perception.death, battle_started_at=now)
        signals = detector.update(votes, now)
        if detector.decided:
            ...  # 进入退出流程

    ``update`` 可以每帧（约 2 Hz）调用；同一实例内计时连续，``reset`` 用于复用实例。
    """

    def __init__(
        self,
        cfg: DeathConfig,
        battle_started_at: float,
        *,
        hud: BattleHudConfig | None = None,
    ) -> None:
        self._cfg = cfg
        # 血条归零证据的连续帧/TTL 配置属于客户端 battle_hud 标定（评审 10.2）。
        self._hud = hud if hud is not None else BattleHudConfig()
        self._hp_confirm_frames = max(1, int(self._hud.hp_bar_confirm_frames))
        self._hp_evidence_ttl_s = max(0.0, float(self._hud.hp_bar_evidence_ttl_s))
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
        # --- ISS-002 血条归零证据状态 -----------------------------------
        self._hp_evidence: str = "unavailable"
        self._hp_fill_ratio: float | None = None
        self._hp_zero_frames = 0
        self._evidence_at = 0.0
        self._hp_frame_id: int | None = None
        self._hp_current: int | None = None

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

    @property
    def hp_evidence(self) -> str:
        """最近一帧的血条证据等级（alive / zero / unknown / unavailable）。"""
        return self._hp_evidence

    @property
    def evidence_at(self) -> float:
        """最近一次“确认归零”证据的时间戳（单调时钟秒；0 表示从未确认）。"""
        return self._evidence_at

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
        self._reset_hp_evidence()

    def _reset_hp_evidence(self) -> None:
        self._hp_evidence = "unavailable"
        self._hp_fill_ratio = None
        self._hp_zero_frames = 0
        self._evidence_at = 0.0
        self._hp_frame_id = None
        self._hp_current = None

    def update(
        self,
        votes: Mapping[str, bool],
        timestamp: float,
        *,
        hp_fill_ratio: float | None = None,
        hp_evidence: str | None = None,
        frame_id: int | None = None,
        hp_current: int | None = None,
    ) -> DeathSignals:
        """输入一帧投票与血条归零证据并返回当前死亡信号。

        参数：
            votes: 信号名 -> 是否命中；缺失的键按 False 处理，多余键忽略。
            timestamp: 该帧时间戳（单调时钟秒，与 ``battle_started_at`` 同一时钟）。
            hp_fill_ratio: 血条填充列比例（0~1，引擎按颜色无关算法给出）；None 表示无数据。
            hp_evidence: 血条证据等级（``alive``/``zero``/``unknown``/``unavailable``）。
                None 表示调用方未提供血条证据：此时沿用 ``votes["hp_zero"]`` 的旧语义，
                但 ``exit_authorized`` 恒为 False（兼容手工调用，绝不放宽退出门禁）。
            frame_id: 帧序号；10Hz 主循环复用同一张 2Hz 帧时只计一次“连续帧”。
            hp_current: 数字判读的当前血量；目前只有确认零值时传 0，其余为 None。
                数字零证据允许 ``hp_fill_ratio`` 缺失，但绝不伪造填充比例。
        """
        timestamp = float(timestamp)
        raw = {name: bool(votes.get(name, False)) for name in SIGNAL_NAMES}
        hp_supplied = hp_evidence is not None
        hp_level = _normalize_hp_evidence(hp_evidence)
        fill_ratio = _normalize_fill_ratio(hp_fill_ratio)
        current_hp = _normalize_hp_current(hp_current)
        numeric_zero = current_hp == 0 and hp_level == _ZERO_EVIDENCE
        if hp_level in ("alive", "zero") and fill_ratio is None and not numeric_zero:
            # 没有填充比例时不能声称已判读血条，保守降为 unknown（不授权）。
            hp_level = "unknown"
        if current_hp is not None and hp_level != _ZERO_EVIDENCE:
            current_hp = None
        if self._in_ignore_window(timestamp):
            self._clear_evidence()
            signals = self._compose(
                effective=raw,
                score=0.0,
                stable_ms=0,
                timestamp=timestamp,
                hp_fill_ratio=fill_ratio if hp_supplied else None,
                hp_evidence=hp_level if hp_supplied else "unavailable",
                exit_authorized=False,
                evidence_at=0.0,
                hp_current=None,
            )
            self._last_signals = signals
            self._last_raw_score = 0.0
            return signals
        effective = self._apply_latches(raw, timestamp)
        applied_level, _applied_ratio, _applied_current = self._update_hp_evidence(
            hp_level if hp_supplied else "unavailable",
            fill_ratio if hp_supplied else None,
            timestamp,
            frame_id if hp_supplied else None,
            current_hp if hp_supplied else None,
        )
        if hp_supplied:
            # 引擎传入血条证据时，hp_zero 投票严格由证据推导（alive/unknown 均为 False）。
            effective["hp_zero"] = applied_level == _ZERO_EVIDENCE
        self._update_signal_timers(effective, timestamp)
        score = self._weighted_score(timestamp)
        self._last_raw_score = score
        exit_authorized = self._authorized_at(timestamp)
        if self._decided:
            stable_ms = self._decision_stable_ms + int(
                max(0.0, timestamp - (self._decided_at or timestamp)) * 1000
            )
            signals = self._compose(
                effective=effective,
                score=score,
                stable_ms=stable_ms,
                timestamp=timestamp,
                hp_fill_ratio=self._hp_fill_ratio,
                hp_evidence=self._hp_evidence,
                exit_authorized=exit_authorized,
                evidence_at=self._reported_evidence_at(),
                hp_current=self._hp_current,
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
            effective=effective,
            score=score,
            stable_ms=stable_ms,
            timestamp=timestamp,
            hp_fill_ratio=self._hp_fill_ratio,
            hp_evidence=self._hp_evidence,
            exit_authorized=exit_authorized,
            evidence_at=self._reported_evidence_at(),
            hp_current=self._hp_current,
        )
        self._last_signals = signals
        return signals

    # ------------------------------------------------------------------
    # 内部：血条归零证据与提前退出授权（ISS-002 / 评审 10.2）
    # ------------------------------------------------------------------
    def _update_hp_evidence(
        self,
        evidence: str,
        fill_ratio: float | None,
        timestamp: float,
        frame_id: int | None,
        hp_current: int | None,
    ) -> tuple[str, float | None, int | None]:
        """累计连续“确认归零”帧，返回本帧实际生效的 (证据等级, 填充比例, 数字血量)。"""
        if self._spectator_mode:
            # 观战界面显示的不是己方血条：己方血条证据一律不可用。
            evidence = "unavailable"
            fill_ratio = None
            hp_current = None
        if frame_id is not None and self._hp_frame_id is not None and frame_id == self._hp_frame_id:
            # 同一张帧被 10Hz 主循环重复消费：不重复累计，也不刷新证据时间戳。
            return self._hp_evidence, self._hp_fill_ratio, self._hp_current
        self._hp_frame_id = frame_id
        self._hp_evidence = evidence
        self._hp_fill_ratio = fill_ratio
        self._hp_current = hp_current if evidence == _ZERO_EVIDENCE else None
        if evidence == _ZERO_EVIDENCE:
            self._hp_zero_frames += 1
            self._evidence_at = timestamp
        else:
            # 存活/未知/不可用都属于矛盾证据：立即撤销尚未执行的退出授权。
            self._hp_zero_frames = 0
            self._evidence_at = 0.0
        return self._hp_evidence, self._hp_fill_ratio, self._hp_current

    def _authorized_at(self, timestamp: float) -> bool:
        """当前时刻是否满足提前退出授权（连续帧归零 + 证据在 TTL 内 + 无矛盾证据）。"""
        if self._hp_evidence != _ZERO_EVIDENCE:
            return False
        if self._hp_zero_frames < self._hp_confirm_frames:
            return False
        return (timestamp - self._evidence_at) <= self._hp_evidence_ttl_s

    def _reported_evidence_at(self) -> float:
        """输出“最近一次确认归零”的时间戳；未达到连续帧要求时输出 0。"""
        if self._hp_zero_frames < self._hp_confirm_frames:
            return 0.0
        return self._evidence_at

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
        # 开局静默期内不累积任何血条归零证据。
        self._reset_hp_evidence()

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
        hp_fill_ratio: float | None,
        hp_evidence: str,
        exit_authorized: bool,
        evidence_at: float,
        hp_current: int | None,
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
            hp_fill_ratio=hp_fill_ratio,
            hp_evidence=hp_evidence,
            exit_authorized=bool(exit_authorized),
            evidence_at=float(evidence_at),
            hp_current=hp_current,
        )


def death_score_weights_valid(cfg: DeathConfig) -> bool:
    """辅助校验：权重非负且总和为正（供配置校验工具调用）。"""
    weights = [float(cfg.weights.get(name, 0.0)) for name in SIGNAL_NAMES]
    if any(weight < 0 for weight in weights):
        return False
    return math.fsum(weights) > 0
