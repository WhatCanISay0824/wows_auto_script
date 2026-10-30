"""简化导航：小地图点击自动驾驶（用户简化需求）。

不再识别地图、不标定航线。开局在小地图上点一次目标，让游戏自带自动驾驶把船开过去；
之后不碰 WASD，也不做世界坐标换算，只在必要时（到达/卡住/长时间无反馈）周期重发航点。

目标点（默认 ``navigation.autopilot_target=symmetric``，用户确认的口径）：
- 取开局时自身舰船标记在小地图上的位置 ``(own_x, own_y)``（归一化到小地图内容区）；
- 敌方区域 = 我方出生点关于地图中心的对称点；
- 横坐标固定到中线，因此目标 = ``(0.5, 1 - own_y)``（再叠加配置微调与安全内缩）；
- 若自身标记识别不到（模板缺失/掉帧），回退到小地图中心 ``(0.5, 0.5)`` 并记录 warning。

点击坐标来源：客户端 profile.minimap 归一化内容区 -> 窗口客户区像素 -> 屏幕像素
（``Observation.window_rect`` 是客户区屏幕矩形）。不涉及地图包与世界坐标。
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from ..common.types import Action, Observation, Rect
from ..config.models import ClientProfile, NavigationConfig

logger = logging.getLogger(__name__)

# 目标点离小地图内容区边缘的安全内缩（归一化）
_EDGE_SAFE = 0.02


@dataclass(frozen=True, slots=True)
class AutoPilotTarget:
    """一次航点目标（归一化到小地图内容区 + 屏幕像素）。"""

    normalized: tuple[float, float]
    content_px: tuple[int, int]
    screen_px: tuple[int, int]
    from_own_marker: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "normalized": [round(self.normalized[0], 4), round(self.normalized[1], 4)],
            "screen": list(self.screen_px),
            "from_own_marker": self.from_own_marker,
        }


class AutoPilotNavigator:
    """把小地图点击转成“自动驾驶目标点”的简化导航器。

    与 :class:`src.navigation.navigator.Navigator` 的接口保持一致的部分：
    ``start(...)`` / ``tick(observation, now)`` / ``stop()``；区别是不需要地图包与标定，
    只依赖客户端小地图区域配置。
    """

    def __init__(
        self,
        config: NavigationConfig,
        profile: ClientProfile,
        *,
        rng: random.Random | None = None,
        on_event=None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.profile = profile
        self._rng = rng or random.Random()
        self._on_event = on_event
        self._logger = logger_ or logger
        self._active = False
        self._own_norm: tuple[float, float] | None = None
        self._next_click_at = 0.0
        self._started_at = 0.0
        self._last_target: AutoPilotTarget | None = None
        self._clicks = 0
        self._last_click_at = 0.0
        self._missing_marker_warned = False

    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._active

    @property
    def last_target(self) -> AutoPilotTarget | None:
        return self._last_target

    @property
    def clicks(self) -> int:
        return self._clicks

    @property
    def own_position_norm(self) -> tuple[float, float] | None:
        return self._own_norm

    @property
    def next_click_at(self) -> float:
        return self._next_click_at

    # ------------------------------------------------------------------
    def start(self, now: float) -> None:
        """进入战斗时启动；首次 tick 立即点一次（symmetric 模式最多等 10s 识别出生点）。"""
        self._active = True
        self._own_norm = None
        self._last_target = None
        self._clicks = 0
        self._last_click_at = float(now)
        self._next_click_at = float(now)
        self._started_at = float(now)
        self._missing_marker_warned = False
        self._emit("autopilot_start", now)
        self._logger.info("autopilot 导航启动：开局点小地图目标后交给游戏自动驾驶，不碰 WASD")

    def stop(self) -> None:
        if self._active:
            self._active = False
            self._emit("autopilot_stop", self._last_click_at)

    def reset(self) -> None:
        self._active = False
        self._own_norm = None
        self._last_target = None
        self._clicks = 0
        self._next_click_at = 0.0

    # ------------------------------------------------------------------
    def tick(self, observation: Observation | None, now: float) -> list[Action]:
        if not self._active:
            return []
        if observation is None or observation.window_missing:
            return []
        self._remember_own_position(observation)
        if now < self._next_click_at:
            return []
        # symmetric 模式先等自身标记（最多 10s）再点，避免开局用中心兜底导致方向错误
        if (
            self._clicks == 0
            and self.config.autopilot_target == "symmetric"
            and self._own_norm is None
            and now - self._started_at < 10.0
        ):
            return []
        target = self.compute_target(observation)
        if target is None:
            return []
        self._last_target = target
        self._last_click_at = float(now)
        self._clicks += 1
        interval = self._sample_interval()
        self._next_click_at = now + interval
        self._emit("autopilot_click", now, **target.to_dict())
        return [Action.click_abs(target.screen_px[0], target.screen_px[1], label="autopilot_click")]

    # ------------------------------------------------------------------
    def _remember_own_position(self, observation: Observation) -> None:
        ship = observation.ship
        if ship is None or ship.source == "lost":
            return
        content = self._content_rect(observation)
        if content is None or content.w <= 0 or content.h <= 0:
            return
        if self._own_norm is None:
            px, py = ship.minimap_px
            # ShipPosition.minimap_px 是「小地图内容区相对坐标」
            x = min(max(float(px) / content.w, 0.0), 1.0)
            y = min(max(float(py) / content.h, 0.0), 1.0)
            self._own_norm = (x, y)
            self._emit("autopilot_spawn", self._last_click_at, x=round(x, 4), y=round(y, 4))
            self._logger.info("autopilot 记录我方出生点（小地图归一化）: (%.3f, %.3f)", x, y)

    def compute_target(self, observation: Observation) -> AutoPilotTarget | None:
        content_rel = self._content_rect(observation)
        if content_rel is None:
            return None
        window = Rect(*observation.window_rect)
        if window.w <= 0 or window.h <= 0:
            return None
        target_x, target_y, from_marker = self._normalized_target()
        target_x = min(max(target_x, _EDGE_SAFE), 1.0 - _EDGE_SAFE)
        target_y = min(max(target_y, _EDGE_SAFE), 1.0 - _EDGE_SAFE)
        local_x = content_rel.x + int(round(target_x * content_rel.w))
        local_y = content_rel.y + int(round(target_y * content_rel.h))
        return AutoPilotTarget(
            normalized=(target_x, target_y),
            content_px=(local_x, local_y),
            screen_px=(window.x + local_x, window.y + local_y),
            from_own_marker=from_marker,
        )

    def _normalized_target(self) -> tuple[float, float, bool]:
        offset_x, offset_y = self.config.autopilot_offset
        if self.config.autopilot_target == "center":
            return 0.5 + offset_x, 0.5 + offset_y, False
        if self._own_norm is not None:
            # 对称点：x 固定中线，y 取我方出生点关于中心的对称位置
            own_x, own_y = self._own_norm
            del own_x
            target_y = 1.0 - own_y
            return 0.5 + offset_x, target_y + offset_y, True
        if not self._missing_marker_warned:
            self._missing_marker_warned = True
            self._logger.warning("autopilot 未识别到自身标记，敌区目标回退小地图中心")
            self._emit("autopilot_fallback_center", self._last_click_at)
        return 0.5 + offset_x, 0.5 + offset_y, False

    def _content_rect(self, observation: Observation) -> Rect | None:
        minimap = self.profile.minimap
        if minimap is None:
            return None
        window = Rect(*observation.window_rect)
        rect = minimap.rect.to_pixels(window.w, window.h)
        border = max(0, int(minimap.border_px))
        width = rect.w - border * 2
        height = rect.h - border * 2
        if width <= 8 or height <= 8:
            return None
        return Rect(rect.x + border, rect.y + border, width, height)

    def _sample_interval(self) -> float:
        low, high = self.config.autopilot_reclick_s
        low = max(1.0, float(low))
        high = max(low, float(high))
        return self._rng.uniform(low, high)

    def _emit(self, event: str, now: float, **detail) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, {"event": event, "at": round(float(now), 3), **detail})
        except Exception:  # 事件回调失败不影响导航
            self._logger.debug("autopilot 事件回调失败: %s", event)
