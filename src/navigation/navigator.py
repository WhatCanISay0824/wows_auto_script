"""自动导航（详细设计 7.3~7.6；评审 P0-5：判定阈值改到像素域）。

与设计文档的差异（实现补充第 3 节算法层第 6 条）：
- 移动确认与卡住判定使用**小地图像素**：``move_confirm_px``（默认 3px，约 120 米）、
  ``stuck_distance_px``（默认 3px）、``stuck_window_s``（默认 20s）。米制阈值
  ``arrival_m`` / ``overshoot_m`` 只作为到达上限与越点条件。
- 到达判定以**点击时实际点击的小地图目标**（``world_to_minimap`` 后 clamp 到小地图
  内容区，再反算回世界坐标）为基准，而不是配置里的航点世界坐标；这样小地图点击
  精度、边框裁剪造成的偏差不会被误判为“未到达”。
- 卡住恢复使用一张显式状态表（``_Phase``），只维护“本航点重试计数”与“整条航线
  跳过计数”两个计数器，避免设计文档伪代码里的三套计数互相竞争：

  ==================  ==================================  =============================
  当前阶段            事件                                 下一阶段
  ==================  ==================================  =============================
  NAVIGATE            到达 / 越点                          航点 +1（或航线结束）
  NAVIGATE            满足点击最小间隔                       CONFIRM
  CONFIRM             确认窗口内位移 >= move_confirm_px      AWAIT_ARRIVAL
  CONFIRM             窗口到期仍未移动 & 重试未满            RETRY
  CONFIRM             窗口到期仍未移动 & 重试用尽            SKIP（跳过航点）
  AWAIT_ARRIVAL       卡住（窗口内位移 < stuck_distance_px） RETRY / SKIP
  RETRY               距上次点击 >= retry_interval_s        CONFIRM（重新点击）
  SKIP                累计跳过 >= max_skipped               ABANDON
  ==================  ==================================  =============================

导航只返回 :class:`~src.common.types.Action`，不直接发送输入；事件通过 ``on_event``
回调交给状态机/日志层（导航本身不写数据库）。
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..common.coords import BattleCalibration
from ..common.types import Action, Observation
from ..config.models import NavigationConfig, Route

NavigationEventCallback = Callable[[str, dict[str, Any]], None]

#: 导航事件名（详细设计 7.9 / 任务 T5；状态机据此写 battle_actions）
NAV_EVENT_SELECT_ROUTE = "nav_select_route"
NAV_EVENT_CLICK_WAYPOINT = "nav_click_waypoint"
NAV_EVENT_WAYPOINT_REACHED = "nav_waypoint_reached"
NAV_EVENT_RETRY = "nav_retry"
NAV_EVENT_STUCK = "nav_stuck"
NAV_EVENT_SKIP = "nav_skip"
NAV_EVENT_ABANDON = "nav_abandon"
NAV_EVENT_ROUTE_END = "nav_route_end"

#: 小地图点击时不贴边的最小内缩（像素），避免点在小地图边框上
_MINIMAP_CLICK_MARGIN_PX = 1.0

#: 历史位置样本上限（10Hz 主循环下足够覆盖 20s 卡住窗口）
_HISTORY_MAX = 300


class _Phase(StrEnum):
    """导航阶段（显式恢复状态表，见模块 docstring）。"""

    NAVIGATE = "navigate"
    CONFIRM = "confirm"
    AWAIT_ARRIVAL = "await_arrival"
    RETRY = "retry"
    ABANDONED = "abandoned"
    FINISHED = "finished"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class _TrackedPosition:
    minimap_px: tuple[float, float]
    world_xy: tuple[float, float]


@dataclass(frozen=True, slots=True)
class _ArrivalCheck:
    arrived: bool
    reason: str
    distance_m: float
    distance_px: float


class Navigator:
    """单场战斗内的自动导航器（进入战斗创建，退出战斗销毁，不跨场复用）。"""

    def __init__(
        self,
        config: NavigationConfig,
        *,
        on_event: NavigationEventCallback | None = None,
        min_confidence: float = 0.2,
    ) -> None:
        self._config = config
        self._on_event = on_event
        self._min_confidence = min_confidence
        self._route: Route | None = None
        self._calibration: BattleCalibration | None = None
        self._index = 0
        self._phase = _Phase.STOPPED
        self._started = False
        self._last_click_at = float("-inf")
        self._click_px: tuple[float, float] | None = None
        self._target_world: tuple[float, float] | None = None
        self._first_click = True
        self._retries = 0
        self._skipped = 0
        self._route_origin_world: tuple[float, float] | None = None
        self._lost_since: float | None = None
        self._degraded = False
        self._history: deque[tuple[float, float, float]] = deque(maxlen=_HISTORY_MAX)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self, route: Route, calibration: BattleCalibration, now: float) -> None:
        """开始导航（详细设计 7.1：地图/出生点/航线/小地图/自身位置均可用后调用）。"""
        if self._config.surface != "minimap":
            raise ValueError(
                f"v1 只实现 minimap 导航，当前 navigation.surface={self._config.surface!r}（full_map 未实现）"
            )
        if not route.waypoints:
            raise ValueError(f"航线 {route.route_id!r} 没有航点")
        self._route = route
        self._calibration = calibration
        self._index = 0
        self._phase = _Phase.NAVIGATE
        self._started = True
        self._last_click_at = float("-inf")
        self._click_px = None
        self._target_world = None
        self._first_click = True
        self._retries = 0
        self._skipped = 0
        self._route_origin_world = None
        self._lost_since = None
        self._degraded = False
        self._history.clear()
        self._emit(
            NAV_EVENT_SELECT_ROUTE,
            now,
            route_id=route.route_id,
            spawn_id=route.spawn_id,
            waypoints=len(route.waypoints),
            weight=route.weight,
        )

    def stop(self) -> None:
        """停止导航：之后 tick 不再返回任何点击（详细设计 6.3）。"""
        if self._phase in (_Phase.ABANDONED, _Phase.FINISHED):
            return
        self._phase = _Phase.STOPPED
        self._started = False

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def tick(self, observation: Observation | None, now: float) -> list[Action]:
        """每轮主循环推进一次导航，返回本轮需要提交的绝对点击动作列表。"""
        route = self._route
        calibration = self._calibration
        if not self._started or route is None or calibration is None:
            return []
        if self._phase in (_Phase.ABANDONED, _Phase.FINISHED, _Phase.STOPPED):
            return []

        position = self._read_position(observation, now, calibration)
        if position is None:
            self._handle_position_missing(now)
            return []
        self._handle_position_available(position, now)

        arrival = self._arrival_check(position, calibration)
        if arrival.arrived:
            self._handle_arrival(now, position, arrival)
            return []

        if self._phase is _Phase.CONFIRM:
            return self._advance_confirm(now, position)
        if self._phase is _Phase.AWAIT_ARRIVAL:
            return self._advance_await_arrival(now, position)
        if self._phase is _Phase.RETRY:
            if now - self._last_click_at >= self._config.retry_interval_s:
                return self._issue_click(now, position, calibration, reason="retry")
            return []
        # NAVIGATE：到达当前航点前只在满足最小点击间隔时发一次点击
        if now - self._last_click_at >= self._config.click_min_interval_s:
            return self._issue_click(now, position, calibration, reason="navigate")
        return []

    # ------------------------------------------------------------------
    # 位置读取与丢失降级
    # ------------------------------------------------------------------
    def _read_position(
        self,
        observation: Observation | None,
        now: float,
        calibration: BattleCalibration,
    ) -> _TrackedPosition | None:
        if observation is None or observation.window_missing or observation.capture_error:
            return None
        ship = observation.ship
        if ship is None or ship.source == "lost" or ship.confidence < self._min_confidence:
            return None
        if ship.world_xy is not None:
            world = (float(ship.world_xy[0]), float(ship.world_xy[1]))
            px = calibration.world_to_minimap(*world)
        else:
            # ShipPosition.minimap_px 是“小地图内容区像素（原点内容区左上）”
            # （感知模块口径）；加上内容区在窗口客户区中的偏移后，才是
            # BattleCalibration 的 minimap 坐标，再换算回世界坐标。
            rect = calibration.minimap_rect
            content_px = (rect.x + float(ship.minimap_px[0]), rect.y + float(ship.minimap_px[1]))
            world = calibration.minimap_to_world(*content_px)
            px = calibration.world_to_minimap(*world)
        if self._route_origin_world is None:
            self._route_origin_world = world
        self._history.append((now, px[0], px[1]))
        return _TrackedPosition(minimap_px=(float(px[0]), float(px[1])), world_xy=world)

    def _handle_position_missing(self, now: float) -> None:
        if self._lost_since is None:
            self._lost_since = now
        lost_for = now - self._lost_since
        if lost_for >= self._config.ship_lost_abandon_s:
            self._abandon(now, "ship_lost")
        elif lost_for >= self._config.ship_lost_degrade_s:
            self._degraded = True

    def _handle_position_available(self, position: _TrackedPosition, now: float) -> None:
        if self._lost_since is None:
            return
        self._lost_since = None
        if not self._degraded:
            return
        self._degraded = False
        # 位置恢复后重建基线：不把丢失期间的跳变当成点击失败或卡住
        self._history.clear()
        self._history.append((now, position.minimap_px[0], position.minimap_px[1]))
        if self._phase is _Phase.CONFIRM:
            self._last_click_at = now
            self._click_px = position.minimap_px

    # ------------------------------------------------------------------
    # 到达/越点
    # ------------------------------------------------------------------
    def _arrival_check(self, position: _TrackedPosition, calibration: BattleCalibration) -> _ArrivalCheck:
        route = self._route
        assert route is not None
        target = self._target_world if self._target_world is not None else route.waypoints[self._index]
        distance_m = math.dist(position.world_xy, target)
        target_px = calibration.world_to_minimap(*target)
        distance_px = math.dist(position.minimap_px, (float(target_px[0]), float(target_px[1])))
        if distance_m <= self._config.arrival_m:
            return _ArrivalCheck(True, "arrival", distance_m, distance_px)
        previous = self._previous_waypoint_world()
        if previous is not None:
            direction = _unit((target[0] - previous[0], target[1] - previous[1]))
            to_target = (target[0] - position.world_xy[0], target[1] - position.world_xy[1])
            to_target_length = math.hypot(*to_target)
            if direction is not None and to_target_length > 1e-9:
                cos = (to_target[0] * direction[0] + to_target[1] * direction[1]) / to_target_length
                if cos <= self._config.overshoot_cos_threshold and distance_m <= self._config.overshoot_m:
                    return _ArrivalCheck(True, "overshoot", distance_m, distance_px)
        return _ArrivalCheck(False, "", distance_m, distance_px)

    def _previous_waypoint_world(self) -> tuple[float, float] | None:
        route = self._route
        assert route is not None
        if self._index > 0:
            return route.waypoints[self._index - 1]
        return self._route_origin_world

    def _handle_arrival(self, now: float, position: _TrackedPosition, arrival: _ArrivalCheck) -> None:
        route = self._route
        assert route is not None
        waypoint = route.waypoints[self._index]
        self._emit(
            NAV_EVENT_WAYPOINT_REACHED,
            now,
            index=self._index,
            waypoint=_round_point(waypoint),
            target=_round_point(self._target_world) if self._target_world else None,
            reason=arrival.reason,
            distance_m=round(arrival.distance_m, 1),
            distance_px=round(arrival.distance_px, 2),
            position_px=_round_point(position.minimap_px),
        )
        self._index += 1
        self._retries = 0
        self._first_click = True
        self._click_px = None
        self._target_world = None
        self._history.clear()
        self._history.append((now, position.minimap_px[0], position.minimap_px[1]))
        if self._index >= len(route.waypoints):
            self._finish_route(now, position)
        else:
            self._phase = _Phase.NAVIGATE

    # ------------------------------------------------------------------
    # 点击与确认
    # ------------------------------------------------------------------
    def _issue_click(
        self,
        now: float,
        position: _TrackedPosition,
        calibration: BattleCalibration,
        *,
        reason: str,
    ) -> list[Action]:
        route = self._route
        assert route is not None
        waypoint = route.waypoints[self._index]
        minimap_px = calibration.world_to_minimap(*waypoint)
        clamped = self._clamp_to_minimap(float(minimap_px[0]), float(minimap_px[1]), calibration)
        screen = calibration.window_to_screen(*clamped)
        target_world = calibration.minimap_to_world(*clamped)
        action = Action.click_abs(screen[0], screen[1], label=f"nav_click:{self._index}")
        self._last_click_at = now
        # 移动确认以“点击时刻的舰船位置”为基准，不是点击目标点
        self._click_px = position.minimap_px
        # 首次点击后的确认窗口 8s，之后（含重试）5s
        self._first_click = False
        self._target_world = (float(target_world[0]), float(target_world[1]))
        self._phase = _Phase.CONFIRM
        self._emit(
            NAV_EVENT_CLICK_WAYPOINT,
            now,
            index=self._index,
            waypoint=_round_point(waypoint),
            target_world=_round_point(self._target_world),
            minimap_px=_round_point(clamped),
            screen=(screen[0], screen[1]),
            distance_m=round(math.dist(position.world_xy, waypoint), 1),
            distance_px=round(math.dist(position.minimap_px, clamped), 2),
            retry=self._retries,
            reason=reason,
            **self._speed_stats(now),
        )
        return [action]

    def _advance_confirm(self, now: float, position: _TrackedPosition) -> list[Action]:
        window = self._config.click_confirm_first_s if self._first_click else self._config.click_confirm_s
        if now - self._last_click_at < window:
            return []
        click_px = self._click_px
        displacement_px = math.inf if click_px is None else math.dist(position.minimap_px, click_px)
        if displacement_px >= self._config.move_confirm_px:
            self._first_click = False
            self._phase = _Phase.AWAIT_ARRIVAL
            self._history.clear()
            self._history.append((now, position.minimap_px[0], position.minimap_px[1]))
            return []
        return self._handle_failure(
            now,
            position,
            "click_not_confirmed",
            displacement_px=round(displacement_px, 2),
            confirm_window_s=window,
        )

    def _advance_await_arrival(self, now: float, position: _TrackedPosition) -> list[Action]:
        history = self._history
        if not history:
            return []
        oldest = history[0]
        window_s = self._config.stuck_window_s
        if now - oldest[0] < window_s:
            return []
        displacement_px = math.dist(position.minimap_px, (oldest[1], oldest[2]))
        if displacement_px >= self._config.stuck_distance_px:
            return []
        self._emit(
            NAV_EVENT_STUCK,
            now,
            index=self._index,
            position_px=_round_point(position.minimap_px),
            displacement_px=round(displacement_px, 2),
            window_s=window_s,
            **self._speed_stats(now),
        )
        return self._handle_failure(now, position, "stuck", displacement_px=round(displacement_px, 2))

    # ------------------------------------------------------------------
    # 失败恢复（显式状态表，见模块 docstring）
    # ------------------------------------------------------------------
    def _handle_failure(
        self,
        now: float,
        position: _TrackedPosition,
        reason: str,
        **detail: Any,
    ) -> list[Action]:
        self._retries += 1
        if self._retries <= self._config.click_retry:
            self._emit(
                NAV_EVENT_RETRY,
                now,
                index=self._index,
                reason=reason,
                retry=self._retries,
                max_retries=self._config.click_retry,
                retry_interval_s=self._config.retry_interval_s,
                **detail,
            )
            self._phase = _Phase.RETRY
            self._history.clear()
            self._history.append((now, position.minimap_px[0], position.minimap_px[1]))
            return []
        self._retries = 0
        return self._skip_waypoint(now, position, reason, **detail)

    def _skip_waypoint(
        self,
        now: float,
        position: _TrackedPosition,
        reason: str,
        **detail: Any,
    ) -> list[Action]:
        route = self._route
        assert route is not None
        self._skipped += 1
        self._emit(
            NAV_EVENT_SKIP,
            now,
            index=self._index,
            reason=reason,
            skipped=self._skipped,
            max_skipped=self._config.max_skipped,
            **detail,
        )
        self._index += 1
        self._first_click = True
        self._click_px = None
        self._target_world = None
        self._history.clear()
        self._history.append((now, position.minimap_px[0], position.minimap_px[1]))
        if self._skipped >= self._config.max_skipped:
            self._abandon(now, "max_skipped")
            return []
        if self._index >= len(route.waypoints):
            self._finish_route(now, position)
            return []
        self._phase = _Phase.NAVIGATE
        return []

    def _abandon(self, now: float, reason: str) -> None:
        if self._phase is _Phase.ABANDONED:
            return
        self._phase = _Phase.ABANDONED
        self._emit(
            NAV_EVENT_ABANDON,
            now,
            index=self._index,
            reason=reason,
            skipped=self._skipped,
            retries=self._retries,
        )

    def _finish_route(self, now: float, position: _TrackedPosition) -> None:
        route = self._route
        assert route is not None
        self._phase = _Phase.FINISHED
        self._emit(
            NAV_EVENT_ROUTE_END,
            now,
            route_id=route.route_id,
            waypoints=len(route.waypoints),
            action=self._config.route_end_action,
            position_px=_round_point(position.minimap_px),
        )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _clamp_to_minimap(
        self,
        mx: float,
        my: float,
        calibration: BattleCalibration,
    ) -> tuple[float, float]:
        rect = calibration.minimap_rect
        margin = _MINIMAP_CLICK_MARGIN_PX
        low_x = float(rect.x) + margin
        high_x = max(low_x, float(rect.x + rect.w) - margin)
        low_y = float(rect.y) + margin
        high_y = max(low_y, float(rect.y + rect.h) - margin)
        return min(max(mx, low_x), high_x), min(max(my, low_y), high_y)

    def _speed_stats(self, now: float) -> dict[str, Any]:
        """用最近 ``ship_speed_window_s`` 内的位置样本估计速度（像素/秒）。

        ``ship_speed_samples`` 为判定“速度估计可靠”所需的样本数；样本不足时仍给出
        中位速度但标记 ``speed_reliable=False``，避免把少量抖动当航速。
        """
        config = self._config
        window_start = now - config.ship_speed_window_s
        samples = [sample for sample in self._history if sample[0] >= window_start]
        speeds: list[float] = []
        for first, second in zip(samples, samples[1:], strict=False):
            dt = second[0] - first[0]
            if dt <= 1e-9:
                continue
            speeds.append(math.dist((first[1], first[2]), (second[1], second[2])) / dt)
        reliable = len(samples) >= max(2, int(config.ship_speed_samples)) and bool(speeds)
        median = _median(speeds) if speeds else None
        return {
            "speed_px_s": None if median is None else round(median, 3),
            "speed_samples": len(samples),
            "speed_reliable": reliable,
        }

    def _emit(self, event: str, now: float, **detail: Any) -> None:
        if self._on_event is None:
            return
        payload: dict[str, Any] = {"event": event, "at": round(now, 3)}
        payload.update(detail)
        self._on_event(event, payload)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def route_id(self) -> str | None:
        return self._route.route_id if self._route is not None else None

    @property
    def index(self) -> int:
        """当前航点序号（结束后等于航点总数）。"""
        return self._index

    @property
    def phase(self) -> str:
        return self._phase.value

    @property
    def skipped(self) -> int:
        return self._skipped

    @property
    def retries(self) -> int:
        return self._retries

    @property
    def abandoned(self) -> bool:
        return self._phase is _Phase.ABANDONED

    @property
    def finished(self) -> bool:
        return self._phase is _Phase.FINISHED

    @property
    def active(self) -> bool:
        return self._started and self._phase in (
            _Phase.NAVIGATE,
            _Phase.CONFIRM,
            _Phase.AWAIT_ARRIVAL,
            _Phase.RETRY,
        )

    @property
    def current_waypoint(self) -> tuple[float, float] | None:
        route = self._route
        if route is None or self._index >= len(route.waypoints):
            return None
        return route.waypoints[self._index]


def _unit(vector: tuple[float, float]) -> tuple[float, float] | None:
    length = math.hypot(vector[0], vector[1])
    if length <= 1e-12:
        return None
    return vector[0] / length, vector[1] / length


def _round_point(point: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(round(value, 2) for value in point)


def _median(values: list[float]) -> float:
    """中位数（减少单帧位置抖动的干扰，详细设计 7.4）。"""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 0:
        return (ordered[middle - 1] + ordered[middle]) / 2.0
    return ordered[middle]
