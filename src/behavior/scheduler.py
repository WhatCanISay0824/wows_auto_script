"""防挂机随机行为调度器（详细设计 8.2~8.8；评审 P1-6 / 算法层补充第 9 条）。

实现口径：
- 两个独立计时器（视角 ``camera``、攻击 ``attack``）+ 一个活动看门狗；
- 采样使用**截断正态**（``camera_sd_s`` / ``attack_sd_s``，有限次拒绝采样，失败钳位），
  间隔均值 = 基准均值 / 本场频率倍率（评审 P1-6：倍率越大越频繁）；
- RNG 分流：camera / attack / watchdog 各自 ``random.Random(seed ^ 常量)``，本场可复现；
- 攻击事件在开火与消耗品之间二选一（本场 ``consumable_ratio``），消耗品达到单场上限后
  一律改为开火；
- 长时间凝视（``long_stare_bias``）命中时本次视角动作不移动，下一次视角动作时间 =
  ``now + U(long_stare_s)``；
- 每次输入前检查感知帧新鲜度（``max_input_frame_age_s``）与上下文（in_battle /
  modal_visible / foreground），非战斗或画面过期时跳过本次动作并顺延；
- 每分钟输入事件上限（视角拆分步、点射发数都计数），单次 tick 超出剩余额度时截断；
- 开火落点在 ``fire_click_region`` 归一化区域内随机，换算屏幕坐标需要窗口矩形：
  优先用 ``BehaviorContext.window_rect``，否则回退 ``Observation.window_rect``；
  两者都不可用时用参考分辨率换算并标注 ``window_rel``（仅离线兜底）。
"""

from __future__ import annotations

import math
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..common.types import Action, ActionKind, Observation, Rect
from ..config.models import BehaviorConfig, KeyMap
from .random_profile import RandomProfile

BehaviorEventCallback = Callable[[str, dict[str, Any]], None]

#: RNG 分流掩码（评审要求 camera/attack/watchdog 各自独立随机流）
_CAMERA_RNG_MASK = 0x9E3779B9
_ATTACK_RNG_MASK = 0x85EBCA6B
_WATCHDOG_RNG_MASK = 0xC2B2AE35

#: 视角方向在上一方向基础上的随机偏转角（度，实现假设，设计未给具体值）
_CAMERA_DEFLECT_DEG = 45.0

#: 截断正态拒绝采样最大次数，超过后退回钳位
_MAX_TRUNCATED_ATTEMPTS = 16

#: 每分钟事件统计窗口（秒）
_ACTION_WINDOW_S = 60.0

#: 无窗口矩形时开火落点的参考分辨率（仅离线兜底，实机集成必须提供窗口矩形）
_FALLBACK_WINDOW_SIZE = (2560, 1600)


@dataclass(frozen=True, slots=True)
class BehaviorContext:
    """行为调度上下文（由状态机每 tick 提供）。

    - ``in_battle``：是否处于 BATTLE_ACTIVE；False 时行为立即停止发输入；
    - ``modal_visible``：当前是否有弹窗/退出菜单等遮挡输入；
    - ``foreground``：游戏窗口是否前台；
    - ``window_rect``：窗口客户区屏幕矩形（点击坐标换算用），None 时回退 Observation；
    - ``to_screen``：窗口客户区像素 -> 屏幕物理像素的换算回调（多显示器/DPI 场景）。
    """

    in_battle: bool = True
    modal_visible: bool = False
    foreground: bool = True
    window_rect: Rect | None = None
    to_screen: Callable[[int, int], tuple[int, int]] | None = None


class BehaviorScheduler:
    """随机行为调度器（每场战斗创建一个实例）。"""

    def __init__(
        self,
        config: BehaviorConfig,
        keymap: KeyMap,
        *,
        on_event: BehaviorEventCallback | None = None,
    ) -> None:
        self._config = config
        self._keymap = keymap
        self._on_event = on_event
        self._profile: RandomProfile | None = None
        self._running = False
        self._stopped = False
        self._camera_rng = random.Random(0)
        self._attack_rng = random.Random(0)
        self._watchdog_rng = random.Random(0)
        self._heading_deg: float | None = None
        self._quiet_until = 0.0
        self._next_camera = 0.0
        self._next_attack = 0.0
        self._watchdog_interval = 0.0
        self._watchdog_deferred_until = 0.0
        self._last_activity = 0.0
        self._consumables_used = 0
        self._action_times: deque[float] = deque()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self, profile: RandomProfile, now: float) -> None:
        """开始本场行为调度（进入 BATTLE_ACTIVE 且战斗 HUD 稳定后调用）。"""
        self._profile = profile
        self._running = True
        self._stopped = False
        self._camera_rng = random.Random(profile.seed ^ _CAMERA_RNG_MASK)
        self._attack_rng = random.Random(profile.seed ^ _ATTACK_RNG_MASK)
        self._watchdog_rng = random.Random(profile.seed ^ _WATCHDOG_RNG_MASK)
        self._heading_deg = None
        self._quiet_until = now + max(0.0, self._config.battle_start_quiet_s)
        self._next_camera = self._quiet_until + self._sample_camera_interval()
        self._next_attack = self._quiet_until + self._sample_attack_interval()
        self._watchdog_interval = self._sample_watchdog_interval()
        self._watchdog_deferred_until = 0.0
        self._last_activity = self._quiet_until
        self._consumables_used = 0
        self._action_times.clear()
        self._emit(
            "behavior_start",
            now,
            profile=profile.to_dict(),
            quiet_until=round(self._quiet_until, 2),
            next_camera_at=round(self._next_camera, 2),
            next_attack_at=round(self._next_attack, 2),
        )

    def stop(self) -> None:
        """停止行为调度：之后 tick 不再返回任何动作。"""
        self._running = False
        self._stopped = True

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def tick(
        self,
        observation: Observation | None,
        now: float,
        context: BehaviorContext | None = None,
    ) -> list[Action]:
        """推进一次行为调度，返回本次需要提交的输入动作列表。"""
        if not self._running or self._stopped or self._profile is None:
            return []
        ctx = context if context is not None else BehaviorContext()
        if not ctx.in_battle:
            return []
        self._prune_action_window(now)
        if now < self._quiet_until:
            return []
        if self._heading_deg is None:
            heading = observation.ship.heading_deg if observation is not None and observation.ship else None
            self._heading_deg = float(heading) if heading is not None else 0.0

        skip_reason = self._input_gate(observation, ctx, now)
        actions: list[Action] = []

        # 1) 视角计时器
        if now >= self._next_camera:
            if skip_reason is not None:
                self._next_camera = now + self._sample_camera_interval()
                self._emit("behavior_skipped", now, timer="camera", reason=skip_reason)
            elif self._remaining_quota(now) <= 0:
                self._next_camera = now + self._sample_camera_interval()
                self._emit("behavior_rate_limited", now, timer="camera")
            else:
                actions.extend(self._advance_camera(now))

        # 2) 攻击计时器
        if now >= self._next_attack:
            if skip_reason is not None:
                self._next_attack = now + self._sample_attack_interval()
                self._emit("behavior_skipped", now, timer="attack", reason=skip_reason)
            elif self._remaining_quota(now) <= 0:
                self._next_attack = now + self._sample_attack_interval()
                self._emit("behavior_rate_limited", now, timer="attack")
            else:
                actions.extend(self._advance_attack(now, observation, ctx))

        # 3) 活动看门狗
        if (
            skip_reason is None
            and now >= self._watchdog_deferred_until
            and now - self._last_activity >= self._watchdog_interval
        ):
            if self._remaining_quota(now) <= 0:
                self._watchdog_deferred_until = now + 5.0
                self._emit("behavior_rate_limited", now, timer="watchdog")
            else:
                generated, detail = self._make_relative_move(self._camera_rng, "watchdog")
                accepted = self._limit_actions(generated, now)
                if accepted:
                    actions.extend(accepted)
                    self._touch(now)
                self._watchdog_interval = self._sample_watchdog_interval()
                self._emit("watchdog", now, **detail, emitted=len(accepted))

        return actions

    def notify_activity(self, now: float) -> None:
        """外部活动（例如导航点击）刷新看门狗计时。"""
        self._touch(now)

    @property
    def running(self) -> bool:
        return self._running and not self._stopped

    @property
    def profile(self) -> RandomProfile | None:
        return self._profile

    @property
    def consumables_used(self) -> int:
        return self._consumables_used

    @property
    def next_camera_at(self) -> float:
        return self._next_camera

    @property
    def next_attack_at(self) -> float:
        return self._next_attack

    @property
    def last_activity_at(self) -> float:
        return self._last_activity

    @property
    def heading_deg(self) -> float:
        return self._heading_deg or 0.0

    # ------------------------------------------------------------------
    # 事件实现
    # ------------------------------------------------------------------
    def _advance_camera(self, now: float) -> list[Action]:
        self._next_camera = now + self._sample_camera_interval()
        if self._camera_rng.random() < self._long_stare_probability():
            duration = float(self._camera_rng.uniform(*self._config.long_stare_s))
            self._next_camera = now + duration
            self._emit("camera_stare", now, duration_s=round(duration, 2))
            return []
        generated, detail = self._make_relative_move(self._camera_rng, "camera")
        accepted = self._limit_actions(generated, now)
        self._touch(now)
        self._emit("camera_move", now, **detail, emitted=len(accepted))
        return accepted

    def _advance_attack(self, now: float, observation: Observation | None, ctx: BehaviorContext) -> list[Action]:
        profile = self._profile
        assert profile is not None
        self._next_attack = now + self._sample_attack_interval()
        roll = self._attack_rng.random()
        use_consumable = (
            roll < profile.consumable_ratio
            and self._consumables_used < self._config.consumable_max_per_battle
            and len(self._keymap.consumables) > 0
        )
        if use_consumable:
            slots = list(self._keymap.consumables)
            slot = slots[self._attack_rng.randrange(len(slots))]
            generated = [
                Action.key_down(slot.key, label=f"consumable:{slot.name}"),
                Action.key_up(slot.key, label=f"consumable:{slot.name}"),
            ]
            accepted = self._limit_actions(generated, now)
            self._consumables_used += 1
            self._touch(now)
            self._emit(
                "consumable",
                now,
                roll=round(roll, 4),
                choice="consumable",
                slot=slot.name,
                key=slot.key,
                consumables_used=self._consumables_used,
                emitted=len(accepted),
            )
            return accepted
        fire_actions, fire_detail = self._make_fire_actions(ctx, observation)
        accepted = self._limit_actions(fire_actions, now)
        self._touch(now)
        self._emit(
            "fire_burst",
            now,
            roll=round(roll, 4),
            choice="fire",
            consumables_used=self._consumables_used,
            emitted=len(accepted),
            **fire_detail,
        )
        return accepted

    def _make_fire_actions(
        self,
        ctx: BehaviorContext,
        observation: Observation | None,
    ) -> tuple[list[Action], dict[str, Any]]:
        rng = self._attack_rng
        config = self._config
        heading = self.heading_deg
        target_heading = heading + rng.uniform(-config.fire_sector_deg, config.fire_sector_deg)
        actions: list[Action] = []
        turned = False
        if rng.random() < config.fire_turn_probability:
            turn_actions, _ = self._make_relative_move(rng, "fire_turn", heading=target_heading)
            actions.extend(turn_actions)
            self._heading_deg = _normalize_heading(target_heading)
            turned = True
        burst = max(1, int(rng.randint(*config.fire_burst)))
        window_rect = self._resolve_window_rect(ctx, observation)
        for shot in range(burst):
            nx = rng.uniform(config.fire_click_region.x1, config.fire_click_region.x2)
            ny = rng.uniform(config.fire_click_region.y1, config.fire_click_region.y2)
            actions.append(self._make_click(window_rect, nx, ny, ctx, label=f"fire:{shot}"))
            if shot < burst - 1:
                actions.append(
                    Action.wait(max(1, int(rng.randint(*config.fire_interval_ms))), label="fire_interval")
                )
        detail = {
            "direction_deg": round(_normalize_heading(target_heading), 1),
            "turned": turned,
            "burst": burst,
            "window_known": window_rect is not None,
        }
        return actions, detail

    def _make_relative_move(
        self,
        rng: random.Random,
        label: str,
        *,
        heading: float | None = None,
    ) -> tuple[list[Action], dict[str, Any]]:
        """生成一次“拆步缓动”的相对视角移动，返回 (动作列表, 日志参数)。"""
        config = self._config
        if heading is None:
            deflection = rng.uniform(-_CAMERA_DEFLECT_DEG, _CAMERA_DEFLECT_DEG)
            heading = self.heading_deg + deflection
        heading = _normalize_heading(heading)
        self._heading_deg = heading
        total_px = max(1, int(rng.randint(*config.move_px)))
        steps = max(1, int(rng.randint(*config.move_steps)))
        duration_ms = max(1, int(rng.randint(*config.move_duration_ms)))
        sensitivity = max(0.01, float(self._keymap.camera_sensitivity))
        total_dx = round(math.cos(math.radians(heading)) * total_px * sensitivity)
        total_dy = round(math.sin(math.radians(heading)) * total_px * sensitivity)
        weights = _ease_weights(steps)
        actions: list[Action] = []
        allocated_x = 0
        allocated_y = 0
        for index, weight in enumerate(weights):
            if index == steps - 1:
                dx = total_dx - allocated_x
                dy = total_dy - allocated_y
            else:
                dx = round(total_dx * weight)
                dy = round(total_dy * weight)
            allocated_x += dx
            allocated_y += dy
            actions.append(Action.move_rel(dx, dy, label=label))
            if index < steps - 1:
                step_ms = max(1, round(duration_ms * weight))
                actions.append(Action.wait(step_ms, label=f"{label}_step"))
        detail = {
            "heading_deg": round(heading, 1),
            "total_px": total_px,
            "steps": steps,
            "duration_ms": duration_ms,
        }
        return actions, detail

    def _make_click(
        self,
        window_rect: Rect | None,
        nx: float,
        ny: float,
        ctx: BehaviorContext,
        *,
        label: str,
    ) -> Action:
        if window_rect is not None:
            wx = int(round(nx * window_rect.w))
            wy = int(round(ny * window_rect.h))
            if ctx.to_screen is not None:
                sx, sy = ctx.to_screen(wx, wy)
            else:
                sx, sy = window_rect.x + wx, window_rect.y + wy
            return Action.click_abs(sx, sy, label=label)
        # 离线兜底：没有窗口矩形时输出参考分辨率下的“相对窗口像素”并标注
        fallback_w, fallback_h = _FALLBACK_WINDOW_SIZE
        return Action.click_abs(
            int(round(nx * fallback_w)),
            int(round(ny * fallback_h)),
            label=f"{label}:window_rel",
        )

    # ------------------------------------------------------------------
    # 采样
    # ------------------------------------------------------------------
    def _sample_camera_interval(self) -> float:
        assert self._profile is not None
        activity = self._profile.camera_activity
        mean = self._config.camera_mean_s / (activity if activity > 0 else 1.0)
        return _sample_truncated_normal(
            self._camera_rng,
            mean,
            self._config.camera_sd_s,
            self._config.camera_interval_s,
        )

    def _sample_attack_interval(self) -> float:
        assert self._profile is not None
        frequency = self._profile.attack_frequency
        mean = self._config.attack_mean_s / (frequency if frequency > 0 else 1.0)
        return _sample_truncated_normal(
            self._attack_rng,
            mean,
            self._config.attack_sd_s,
            self._config.attack_interval_s,
        )

    def _sample_watchdog_interval(self) -> float:
        low, high = self._config.activity_watchdog_s
        if float(high) < float(low):
            raise ValueError(f"activity_watchdog_s 区间非法: {self._config.activity_watchdog_s!r}")
        return float(self._watchdog_rng.uniform(float(low), float(high)))

    def _sample_long_stare(self) -> float:
        low, high = self._config.long_stare_s
        if float(high) < float(low):
            raise ValueError(f"long_stare_s 区间非法: {self._config.long_stare_s!r}")
        return float(self._camera_rng.uniform(float(low), float(high)))

    def _long_stare_probability(self) -> float:
        profile = self._profile
        bias = profile.long_stare_bias if profile is not None else 0.0
        value = self._config.long_stare_probability * (0.5 + bias)
        return min(max(value, 0.0), 1.0)

    # ------------------------------------------------------------------
    # 输入节流
    # ------------------------------------------------------------------
    def _prune_action_window(self, now: float) -> None:
        cutoff = now - _ACTION_WINDOW_S
        while self._action_times and self._action_times[0] < cutoff:
            self._action_times.popleft()

    def _remaining_quota(self, now: float) -> int:
        self._prune_action_window(now)
        return max(0, self._config.actions_per_minute_max - len(self._action_times))

    def _limit_actions(self, actions: list[Action], now: float) -> list[Action]:
        """按每分钟剩余额度截断动作列表（WAIT 不计入输入事件）。"""
        accepted: list[Action] = []
        remaining = self._remaining_quota(now)
        for action in actions:
            if action.kind is ActionKind.WAIT:
                accepted.append(action)
                continue
            if remaining <= 0:
                break
            accepted.append(action)
            self._action_times.append(now)
            remaining -= 1
        return accepted

    # ------------------------------------------------------------------
    # 上下文/辅助
    # ------------------------------------------------------------------
    def _input_gate(self, observation: Observation | None, ctx: BehaviorContext, now: float) -> str | None:
        if not ctx.foreground:
            return "foreground"
        if ctx.modal_visible:
            return "modal_visible"
        if observation is None:
            return "no_observation"
        if observation.window_missing:
            return "window_missing"
        if observation.capture_error:
            return "capture_error"
        frame_time = observation.frame_timestamp if observation.frame_timestamp > 0 else observation.timestamp
        if now - frame_time > self._config.max_input_frame_age_s:
            return "stale_frame"
        return None

    @staticmethod
    def _resolve_window_rect(ctx: BehaviorContext, observation: Observation | None) -> Rect | None:
        if ctx.window_rect is not None and ctx.window_rect.w > 0 and ctx.window_rect.h > 0:
            return ctx.window_rect
        if observation is not None:
            x, y, w, h = observation.window_rect
            if w > 0 and h > 0:
                return Rect(int(x), int(y), int(w), int(h))
        return None

    def _touch(self, now: float) -> None:
        self._last_activity = now

    def _emit(self, event: str, now: float, **detail: Any) -> None:
        if self._on_event is None:
            return
        payload: dict[str, Any] = {"event": event, "at": round(now, 3)}
        payload.update(detail)
        self._on_event(event, payload)


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def _sample_truncated_normal(
    rng: random.Random,
    mean: float,
    sd: float,
    bounds: tuple[float, float],
) -> float:
    """截断正态采样：有限次拒绝采样，失败退回钳位（评审算法层第 9 条）。"""
    low, high = float(bounds[0]), float(bounds[1])
    if high < low:
        raise ValueError(f"采样区间非法: {bounds!r}")
    clamped_mean = min(max(mean, low), high)
    if sd <= 0:
        return clamped_mean
    value = clamped_mean
    for _ in range(_MAX_TRUNCATED_ATTEMPTS):
        value = rng.gauss(mean, sd)
        if low <= value <= high:
            return value
    return min(max(value, low), high)


def _ease_weights(steps: int) -> list[float]:
    """缓入缓出权重（smoothstep 差分），总和为 1。"""
    if steps <= 1:
        return [1.0]
    weights: list[float] = []
    previous = 0.0
    for index in range(1, steps + 1):
        t = index / steps
        eased = t * t * (3.0 - 2.0 * t)
        weights.append(eased - previous)
        previous = eased
    return weights


def _normalize_heading(degrees: float) -> float:
    return degrees % 360.0
