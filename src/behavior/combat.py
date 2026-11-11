"""有新鲜敌方证据时小步转向、原地开火；不修改随机视角的方向变量。"""

from __future__ import annotations

import math
import random

from ..common.types import Action, CombatObservation
from ..config.models import BehaviorConfig


class CombatController:
    def __init__(self, config: BehaviorConfig, fire_key: str = "mouse_left") -> None:
        self.config = config
        self.fire_key = fire_key
        self.reset()

    def reset(self, seed: int = 0) -> None:
        self._rng = random.Random(seed)
        self._target: tuple[float, float] | None = None
        self._next_fire = 0.0
        self._last_turn_at: float | None = None
        self._last_turn_frame: tuple[int, float] | None = None
        self._last_turn_error: float | None = None
        self._unchanged_turns = 0

    def tick(self, frame: CombatObservation | None, now: float) -> tuple[list[Action], dict]:
        if frame is None or not 0 <= now-frame.timestamp <= self.config.max_input_frame_age_s:
            self._target = None
            return [], {"reason": "combat_frame_unavailable"}
        own, camera, enemies = frame.own_px, frame.camera_bearing_deg, frame.enemies
        if own is None or camera is None or not enemies:
            self._target = None
            return [], {"reason": "no_confirmed_target_or_camera", "enemies": len(enemies)}
        if not all(math.isfinite(v) for v in (*own, camera)):
            return [], {"reason": "invalid_geometry"}

        def angle(point: tuple[float, float]) -> float:
            return math.degrees(math.atan2(point[0]-own[0], own[1]-point[1])) % 360

        def delta(point: tuple[float, float]) -> float:
            return (angle(point) - camera + 180) % 360 - 180

        candidates = [enemy.minimap_px for enemy in enemies if math.dist(enemy.minimap_px, own) > 8]
        if not candidates:
            return [], {"reason": "target_too_close"}
        nearby = [p for p in candidates if self._target is not None and math.dist(p, self._target) <= 20]
        target = min(nearby, key=lambda p: math.dist(p, self._target)) if nearby else min(
            candidates, key=lambda p: (abs(delta(p)), math.dist(p, own)),
        )
        if not nearby:
            self._last_turn_error = None
            self._unchanged_turns = 0
        self._target = target
        error = delta(target)
        detail = {"target_px": target, "camera_deg": round(camera, 1), "bearing_error_deg": round(error, 1)}
        if self._last_turn_at is not None and (
            now-self._last_turn_at < self.config.aim_feedback_s
            or frame.timestamp <= self._last_turn_at
            or (frame.frame_id, frame.timestamp) == self._last_turn_frame
        ):
            return [], {**detail, "reason": "awaiting_turn_feedback"}
        expiry = frame.timestamp + self.config.max_input_frame_age_s
        if abs(error) > self.config.aim_tolerance_deg:
            if self._last_turn_error is not None:
                self._unchanged_turns = self._unchanged_turns + 1 if abs(error) >= self._last_turn_error - 1 else 0
            if self._unchanged_turns >= 3:
                return [], {**detail, "reason": "aim_not_responding"}
            maximum = self.config.aim_step_max_px
            dx = round(max(-maximum, min(maximum, error * self.config.aim_counts_per_degree)))
            self._last_turn_at = now
            self._last_turn_frame = frame.frame_id, frame.timestamp
            self._last_turn_error = abs(error)
            return [Action.move_rel(dx, 0, label="combat_aim").with_expiry(expiry)], {**detail, "reason": "turn"}
        self._unchanged_turns = 0
        self._last_turn_error = None
        if now < self._next_fire:
            return [], {**detail, "reason": "fire_interval"}
        self._next_fire = now + self._rng.uniform(*self.config.combat_fire_interval_s)
        return [Action.tap_key(self.fire_key, label="combat_fire").with_expiry(expiry)], {**detail, "reason": "fire"}
