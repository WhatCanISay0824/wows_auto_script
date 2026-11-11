"""无目标时短的平滑水平视角动作；不读取或修改交战随机流。"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from ..common.types import Action, CombatObservation
from ..config.models import BehaviorConfig

#: 输入线程默认最低间隔 150ms 且允许 20% 抖动；idle 至少 200ms 发一步避免积压过期。
_MIN_STEP_INTERVAL_S = 0.2

#: 相邻 tick 超过该间隔说明主循环停顿，丢弃余量而不是补发大步。
_MAX_TICK_GAP_S = 0.3

#: 单个 MOVE 的水平计数上限，避免停顿后的下一 tick 补足整段运动。
_MAX_STEP_COUNTS = 24

_EPS = 1e-9


@dataclass(slots=True)
class _Motion:
    movement_id: int
    started_at: float
    duration_s: float
    signed_distance_px: int
    sent_px: int = 0
    last_step_at: float | None = None


class IdleCameraController:
    """每次只生成当前一小步的 MOVE_REL，不生成 WAIT 或未来动作队列。"""

    def __init__(self, config: BehaviorConfig) -> None:
        self.config = config
        self._rng = random.Random(0)
        self._movement_seq = 0
        self._next_start: float | None = None
        self._motion: _Motion | None = None
        self._last_tick: float | None = None

    def reset(self, seed: int = 0) -> None:
        """重置独立随机流、计时和当前运动。"""
        self._rng = random.Random(seed)
        self._movement_seq = 0
        self._next_start = None
        self._motion = None
        self._last_tick = None

    def cancel(self) -> None:
        """清除计时和剩余运动；保留随机流当前位置。"""
        self._next_start = None
        self._motion = None
        self._last_tick = None

    def eligible(self, frame: CombatObservation | None, now: float) -> bool:
        """可靠、新鲜、无确认敌人和候选，且有有限己方位置。"""
        if frame is None:
            return False
        age = now - frame.timestamp
        if age < 0.0 or age > self.config.max_input_frame_age_s:
            return False
        own = frame.own_px
        if own is None or len(own) < 2 or not all(math.isfinite(float(value)) for value in own[:2]):
            return False
        return not frame.enemies and int(frame.enemy_candidates) == 0

    def tick(
        self, frame: CombatObservation | None, now: float
    ) -> tuple[list[Action], dict]:
        """推进一次；最多返回一个非零水平 MOVE_REL。"""
        if not self.eligible(frame, now):
            self._next_start = None
            self._motion = None
            self._last_tick = now
            return [], {"reason": "idle_ineligible" if frame is not None else "idle_frame_unavailable"}

        gap = self._last_tick is not None and now - self._last_tick > _MAX_TICK_GAP_S
        if gap and self._motion is not None:
            self._motion = None
            self._last_tick = now
            return [], {"reason": "idle_motion_cancelled_gap", "next_start_at": self._next_start}

        self._last_tick = now
        if self._motion is None:
            if self._next_start is None:
                self._next_start = now + self._sample_interval()
                return [], {"reason": "idle_wait", "next_start_at": self._next_start}
            if now + _EPS < self._next_start:
                return [], {"reason": "idle_wait", "next_start_at": self._next_start}
            return self._start_motion(frame, now)

        return self._advance_motion(frame, now)

    def _start_motion(
        self, frame: CombatObservation, now: float
    ) -> tuple[list[Action], dict]:
        interval = self._sample_interval()
        self._movement_seq += 1
        direction = 1 if self._rng.random() < 0.5 else -1
        low, high = self.config.idle_camera_move_px
        amplitude = int(round(self._rng.uniform(float(low), float(high))))
        amplitude = max(1, amplitude)
        duration = self._rng.uniform(*self.config.idle_camera_duration_s)
        self._next_start = now + interval
        self._motion = _Motion(
            movement_id=self._movement_seq,
            started_at=now,
            duration_s=float(duration),
            signed_distance_px=direction * amplitude,
            sent_px=direction,
            last_step_at=now,
        )
        expiry = min(frame.timestamp + self.config.max_input_frame_age_s, now + 0.15)
        action = Action.move_rel(direction, 0, label="idle_camera").with_expiry(expiry)
        return [action], {
            "reason": "idle_motion_started",
            "movement_id": self._movement_seq,
            "amplitude_px": direction * amplitude,
            "duration_s": float(duration),
            "started_at": now,
            "elapsed_s": 0.0,
            "step_px": direction,
            "expires_at": expiry,
            "next_start_at": self._next_start,
        }

    def _advance_motion(
        self, frame: CombatObservation, now: float
    ) -> tuple[list[Action], dict]:
        motion = self._motion
        assert motion is not None
        anchor = motion.started_at if motion.last_step_at is None else motion.last_step_at
        if now + _EPS < anchor + _MIN_STEP_INTERVAL_S:
            return [], {
                "reason": "idle_step_wait",
                "movement_id": motion.movement_id,
                "next_step_at": anchor + _MIN_STEP_INTERVAL_S,
            }

        elapsed = max(0.0, now - motion.started_at)
        progress = min(1.0, elapsed / max(motion.duration_s, _EPS))
        target = round(motion.signed_distance_px * (progress * progress * (3.0 - 2.0 * progress)))
        complete = elapsed + _EPS >= motion.duration_s
        if complete:
            target = motion.signed_distance_px
        delta = int(target - motion.sent_px)
        if abs(delta) > _MAX_STEP_COUNTS:
            delta = _MAX_STEP_COUNTS if delta > 0 else -_MAX_STEP_COUNTS
        if delta == 0:
            if complete:
                movement_id = motion.movement_id
                self._motion = None
                return [], {"reason": "idle_motion_complete", "movement_id": movement_id}
            return [], {"reason": "idle_step_rounded", "movement_id": motion.movement_id}

        motion.sent_px += delta
        motion.last_step_at = now
        movement_id = motion.movement_id
        if complete:
            self._motion = None
        expiry = min(
            frame.timestamp + self.config.max_input_frame_age_s,
            now + 0.15,
        )
        action = Action.move_rel(delta, 0, label="idle_camera").with_expiry(expiry)
        return [action], {
            "reason": "idle_motion_complete" if complete else "idle_motion_step",
            "movement_id": movement_id,
            "elapsed_s": elapsed,
            "step_px": delta,
            "expires_at": expiry,
        }

    def _sample_interval(self) -> float:
        low, high = self.config.idle_camera_interval_s
        if float(high) < float(low):
            raise ValueError(f"idle_camera_interval_s 区间非法: {self.config.idle_camera_interval_s!r}")
        return float(self._rng.uniform(float(low), float(high)))


__all__ = ["IdleCameraController"]
