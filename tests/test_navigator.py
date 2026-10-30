"""自动导航单元测试（详细设计 7.3~7.6；评审 P0-5 像素域阈值）。

所有用例使用合成 Observation 与直接传入的单调时间，不 sleep、不截屏、不发输入。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from src.common.coords import (
    BattleCalibration,
    CalibrationMode,
    WorldImageTransform,
)
from src.common.types import Observation, Rect, ShipPosition
from src.config.models import NavigationConfig, Route
from src.navigation.navigator import (
    NAV_EVENT_ABANDON,
    NAV_EVENT_CLICK_WAYPOINT,
    NAV_EVENT_RETRY,
    NAV_EVENT_ROUTE_END,
    NAV_EVENT_SELECT_ROUTE,
    NAV_EVENT_SKIP,
    NAV_EVENT_STUCK,
    NAV_EVENT_WAYPOINT_REACHED,
    Navigator,
)

WORLD_BOUNDS = (-6000.0, -6000.0, 6000.0, 6000.0)
IMAGE_BOUNDS = (0.0, 0.0, 1200.0, 1200.0)
WINDOW_RECT = Rect(1000, 500, 2560, 1600)
MINIMAP_RECT = Rect(100, 200, 300, 300)

CALIBRATION = BattleCalibration(
    world_image=WorldImageTransform(WORLD_BOUNDS, IMAGE_BOUNDS),
    minimap_rect=MINIMAP_RECT,
    mode=CalibrationMode.FULL_MAP,
    window_rect=WINDOW_RECT,
)


def make_route(waypoints: tuple[tuple[float, float], ...] | None = None) -> Route:
    return Route(
        route_id="route_a",
        spawn_id="east_1",
        waypoints=waypoints or ((0.0, 0.0), (0.0, -2000.0), (0.0, -4000.0)),
    )


def make_ship(
    world_xy: tuple[float, float] | None,
    *,
    minimap_px: tuple[float, float] | None = None,
    speed_mps: float = 0.0,
    confidence: float = 0.95,
    source: str = "marker",
) -> ShipPosition:
    if minimap_px is None:
        if world_xy is None:
            raise ValueError("需要 world_xy 或 minimap_px")
        px = CALIBRATION.world_to_minimap(*world_xy)
    else:
        px = minimap_px
    return ShipPosition(
        minimap_px=(float(px[0]), float(px[1])),
        world_xy=world_xy,
        heading_deg=0.0,
        speed_mps=speed_mps,
        confidence=confidence,
        source=source,
    )


def make_observation(
    ship: ShipPosition | None,
    now: float,
    *,
    window_missing: bool = False,
    capture_error: str | None = None,
) -> Observation:
    return Observation(
        timestamp=now,
        window_rect=WINDOW_RECT.to_tuple(),
        ship=ship,
        frame_timestamp=now,
        window_missing=window_missing,
        capture_error=capture_error,
    )


class NavHarness:
    """导航测试夹具：收集事件并按时间推进。"""

    def __init__(self, config: NavigationConfig | None = None) -> None:
        self.events: list[tuple[str, dict]] = []
        self.navigator = Navigator(
            config or NavigationConfig(),
            on_event=lambda event, detail: self.events.append((event, detail)),
        )

    def start(self, route: Route, now: float = 0.0) -> None:
        self.navigator.start(route, CALIBRATION, now)

    def tick(self, world_xy: tuple[float, float], now: float, **ship_kwargs) -> list:
        return self.navigator.tick(make_observation(make_ship(world_xy, **ship_kwargs), now), now)

    def tick_ship(self, ship: ShipPosition | None, now: float, **obs_kwargs) -> list:
        return self.navigator.tick(make_observation(ship, now, **obs_kwargs), now)

    def event_names(self) -> list[str]:
        return [event for event, _ in self.events]

    def last_event(self, name: str) -> dict:
        for event, detail in reversed(self.events):
            if event == name:
                return detail
        raise AssertionError(f"未产生事件 {name}: {self.event_names()}")


# ---------------------------------------------------------------------------
# 启动与点击
# ---------------------------------------------------------------------------


def test_start_emits_select_route() -> None:
    harness = NavHarness()
    harness.start(make_route(), now=3.0)
    assert harness.navigator.route_id == "route_a"
    assert harness.navigator.active
    detail = harness.last_event(NAV_EVENT_SELECT_ROUTE)
    assert detail["route_id"] == "route_a"
    assert detail["spawn_id"] == "east_1"
    assert detail["waypoints"] == 3


def test_first_waypoint_click_targets_minimap_screen_coordinates() -> None:
    harness = NavHarness()
    harness.start(make_route())
    actions = harness.tick((0.0, 3000.0), 0.0)
    assert len(actions) == 1
    action = actions[0]
    assert action.kind.value == "click_abs"
    # 世界 (0,0) -> 小地图客户区 (250,350) -> 屏幕 (1250,850)
    assert (action.x, action.y) == (1250, 850)
    assert action.priority == 10
    assert action.label.startswith("nav_click")
    detail = harness.last_event(NAV_EVENT_CLICK_WAYPOINT)
    assert detail["index"] == 0
    assert detail["retry"] == 0
    assert detail["reason"] == "navigate"


def test_no_repeat_click_before_confirm_window() -> None:
    harness = NavHarness()
    harness.start(make_route())
    harness.tick((0.0, 3000.0), 0.0)
    for now in (0.5, 2.0, 4.0, 7.9):
        assert harness.tick((0.0, 3000.0), now) == []


def test_confirm_first_window_is_eight_seconds() -> None:
    harness = NavHarness()
    harness.start(make_route())
    harness.tick((0.0, 3000.0), 0.0)
    # 位移 4px（160 米）足够确认；窗口未到前不判定
    assert harness.tick((0.0, 2840.0), 7.9) == []
    assert harness.tick((0.0, 2840.0), 8.0) == []
    assert harness.navigator.phase == "await_arrival"
    assert NAV_EVENT_RETRY not in harness.event_names()


def test_click_confirmed_waits_for_arrival_without_reclick() -> None:
    harness = NavHarness()
    harness.start(make_route())
    harness.tick((0.0, 3000.0), 0.0)
    harness.tick((0.0, 2840.0), 8.0)
    for now in (8.1, 9.0, 12.0, 15.0):
        assert harness.tick((0.0, 2840.0), now) == []


def test_arrival_advances_to_next_waypoint_and_clicks_it() -> None:
    harness = NavHarness()
    harness.start(make_route())
    harness.tick((0.0, 3000.0), 0.0)
    harness.tick((0.0, 2840.0), 8.0)
    assert harness.tick((0.0, 300.0), 9.0) == []  # 距一航点 300m <= 400m
    reached = harness.last_event(NAV_EVENT_WAYPOINT_REACHED)
    assert reached["index"] == 0
    assert reached["reason"] == "arrival"
    assert harness.navigator.index == 1
    # 下一 tick 点击第二个航点（受点击最小间隔限制，t=9.1 距上次点击 9.1s 已满足）
    actions = harness.tick((0.0, 300.0), 9.1)
    assert len(actions) == 1
    # 世界 (0,-2000) -> 小地图 (250,400) -> 屏幕 (1250,900)
    assert (actions[0].x, actions[0].y) == (1250, 900)


def test_click_min_interval_after_immediate_arrival() -> None:
    harness = NavHarness()
    route = make_route(((0.0, 0.0), (0.0, -2000.0)))
    harness.start(route)
    harness.tick((0.0, 3000.0), 0.0)  # 点击第一航点
    harness.tick((0.0, 300.0), 1.0)  # 直接到达
    assert harness.navigator.index == 1
    # 距上次点击不足 4s，不点击；边界 4.0s 之后才允许下一次点击
    assert harness.tick((0.0, 300.0), 3.9) == []
    assert len(harness.tick((0.0, 300.0), 4.0)) == 1


def test_overshoot_arrival() -> None:
    harness = NavHarness()
    route = make_route(((0.0, 0.0), (0.0, -2000.0), (0.0, -4000.0)))
    harness.start(route)
    harness.tick((0.0, 300.0), 0.0)  # 立即到达第一个航点
    harness.tick((0.0, 300.0), 4.1)  # 点击第二航点
    harness.tick((0.0, 140.0), 12.1)  # 确认移动
    # 越过第二航点 500 米（> arrival_m=400，< overshoot_m=800），方向与航段一致
    harness.tick((0.0, -2500.0), 13.0)
    reached = harness.last_event(NAV_EVENT_WAYPOINT_REACHED)
    assert reached["index"] == 1
    assert reached["reason"] == "overshoot"


# ---------------------------------------------------------------------------
# 点击未确认 -> 重试 -> 跳过
# ---------------------------------------------------------------------------


def test_click_not_confirmed_retries_then_skips() -> None:
    harness = NavHarness(replace(NavigationConfig(), click_retry=1))
    harness.start(make_route())
    harness.tick((0.0, 3000.0), 0.0)
    # 第一次确认窗口到期仍未移动 -> 重试
    assert harness.tick((0.0, 3000.0), 8.0) == []
    retry = harness.last_event(NAV_EVENT_RETRY)
    assert retry["reason"] == "click_not_confirmed"
    assert retry["retry"] == 1
    assert harness.navigator.phase == "retry"
    # 重试点击（距上次点击已超过 retry_interval_s=4）
    actions = harness.tick((0.0, 3000.0), 8.1)
    assert len(actions) == 1
    assert (actions[0].x, actions[0].y) == (1250, 850)
    # 第二次确认窗口（后续 5s）到期仍未移动 -> 跳过当前航点
    assert harness.tick((0.0, 3000.0), 13.2) == []
    skip = harness.last_event(NAV_EVENT_SKIP)
    assert skip["index"] == 0
    assert skip["skipped"] == 1
    assert harness.navigator.index == 1
    assert harness.navigator.phase == "navigate"


def test_retry_interval_controls_reclick_timing() -> None:
    config = replace(NavigationConfig(), click_retry=2, retry_interval_s=10.0)
    harness = NavHarness(config)
    harness.start(make_route())
    harness.tick((0.0, 3000.0), 0.0)
    harness.tick((0.0, 3000.0), 8.0)  # 第一次失败 -> retry
    # 距上次点击 8s < retry_interval_s=10，不点击
    assert harness.tick((0.0, 3000.0), 9.0) == []
    assert len(harness.tick((0.0, 3000.0), 10.1)) == 1


# ---------------------------------------------------------------------------
# 卡住判定与三级恢复
# ---------------------------------------------------------------------------


def test_stuck_detection_utf8_text() -> None:
    harness = NavHarness(replace(NavigationConfig(), click_retry=1))
    harness.start(make_route())
    harness.tick((0.0, 3000.0), 0.0)
    harness.tick((0.0, 2840.0), 8.0)  # 确认移动，进入等待到达
    for now in (24.0, 25.0, 26.0, 27.0):
        assert harness.tick((0.0, 2840.0), now) == []
    # 观察窗口 20s 到期且位移 < 3px -> 卡住
    assert harness.tick((0.0, 2840.0), 28.0) == []
    stuck = harness.last_event(NAV_EVENT_STUCK)
    assert stuck["index"] == 0
    assert stuck["displacement_px"] < 3.0
    assert stuck["window_s"] == 20.0
    assert stuck["speed_px_s"] == pytest.approx(0.0)
    assert stuck["speed_samples"] >= 2
    retry = harness.last_event(NAV_EVENT_RETRY)
    assert retry["reason"] == "stuck"
    assert retry["retry"] == 1


def test_stuck_second_time_skips_waypoint() -> None:
    harness = NavHarness(replace(NavigationConfig(), click_retry=1))
    harness.start(make_route())
    harness.tick((0.0, 3000.0), 0.0)
    harness.tick((0.0, 2840.0), 8.0)
    harness.tick((0.0, 2840.0), 28.1)  # 第一次卡住 -> 重试
    harness.tick((0.0, 2840.0), 28.2)  # 重新点击
    harness.tick((0.0, 2680.0), 33.2)  # 重新确认移动
    harness.tick((0.0, 2680.0), 53.3)  # 再次卡住 -> 跳过
    assert NAV_EVENT_SKIP in harness.event_names()
    assert harness.navigator.index == 1
    assert harness.navigator.skipped == 1


def test_continuous_skips_abandon_navigation() -> None:
    config = replace(NavigationConfig(), click_retry=0, max_skipped=2)
    harness = NavHarness(config)
    route = make_route(((0.0, 0.0), (0.0, -2000.0), (0.0, -4000.0), (0.0, -5000.0)))
    harness.start(route)
    harness.tick((0.0, 3000.0), 0.0)
    harness.tick((0.0, 3000.0), 8.0)  # 失败 -> 跳过 1
    harness.tick((0.0, 3000.0), 8.1)  # 点击第二航点
    harness.tick((0.0, 3000.0), 16.1)  # 失败 -> 跳过 2 -> 放弃
    skips = [detail for event, detail in harness.events if event == NAV_EVENT_SKIP]
    assert len(skips) == 2
    abandon = harness.last_event(NAV_EVENT_ABANDON)
    assert abandon["reason"] == "max_skipped"
    assert harness.navigator.abandoned
    assert harness.navigator.phase == "abandoned"
    assert harness.tick((0.0, 3000.0), 20.0) == []


# ---------------------------------------------------------------------------
# 位置丢失降级
# ---------------------------------------------------------------------------


def test_position_lost_degrades_then_abandons() -> None:
    harness = NavHarness()
    harness.start(make_route())
    harness.tick((0.0, 3000.0), 0.0)
    assert harness.tick_ship(None, 1.0) == []
    assert harness.tick_ship(None, 3.9) == []
    assert harness.tick_ship(None, 10.9) == []
    assert harness.tick_ship(None, 11.0) == []
    abandon = harness.last_event(NAV_EVENT_ABANDON)
    assert abandon["reason"] == "ship_lost"
    assert harness.navigator.abandoned


def test_window_missing_counts_as_position_lost() -> None:
    harness = NavHarness(replace(NavigationConfig(), ship_lost_abandon_s=2.0, ship_lost_degrade_s=1.0))
    harness.start(make_route())
    harness.tick((0.0, 3000.0), 0.0)
    harness.tick_ship(None, 1.0, window_missing=True)
    harness.tick_ship(None, 3.1, window_missing=True)
    assert harness.navigator.abandoned


def test_position_recovery_rebuilds_confirm_baseline() -> None:
    harness = NavHarness()
    harness.start(make_route())
    harness.tick((0.0, 3000.0), 0.0)
    # 丢失 3.5s（超过降级阈值）后恢复：不把丢失期间的跳变当失败
    harness.tick_ship(None, 1.0)
    harness.tick_ship(None, 4.5)
    harness.tick((0.0, 2000.0), 5.0)  # 恢复，位置大幅跳变
    assert NAV_EVENT_RETRY not in harness.event_names()
    # 基线重建后需要再等满一次确认窗口（首次点击之后的重试窗口 5s）
    assert harness.tick((0.0, 2000.0), 9.9) == []
    assert NAV_EVENT_RETRY not in harness.event_names()
    harness.tick((0.0, 2000.0), 10.0)
    assert NAV_EVENT_RETRY in harness.event_names()


def test_low_confidence_position_is_ignored() -> None:
    harness = NavHarness()
    harness.start(make_route())
    harness.tick((0.0, 3000.0), 0.0)
    harness.tick_ship(make_ship((0.0, 3000.0), confidence=0.05), 1.0)
    assert harness.navigator.index == 0


# ---------------------------------------------------------------------------
# 点击目标 clamp 与到达基准
# ---------------------------------------------------------------------------


def test_click_target_is_clamped_into_minimap() -> None:
    harness = NavHarness()
    route = make_route(((20000.0, 0.0), (0.0, -2000.0)))
    harness.start(route)
    actions = harness.tick((5900.0, 0.0), 0.0)
    assert len(actions) == 1
    # 世界 x=20000 超出小地图右边界，clamp 到内容区最右像素 399 -> 屏幕 x=1399
    assert actions[0].x == 1399
    detail = harness.last_event(NAV_EVENT_CLICK_WAYPOINT)
    assert detail["minimap_px"][0] <= MINIMAP_RECT.right - 1


def test_arrival_uses_clicked_target_not_configured_waypoint() -> None:
    harness = NavHarness()
    route = make_route(((20000.0, 0.0), (0.0, -2000.0)))
    harness.start(route)
    harness.tick((5600.0, 0.0), 0.0)  # 点击被 clamp 到世界约 (5960, 0)
    # 与配置航点 (20000,0) 距离很远，但与被点击目标只差 360 米 -> 到达
    harness.tick((5600.0, 0.0), 1.0)
    reached = harness.last_event(NAV_EVENT_WAYPOINT_REACHED)
    assert reached["index"] == 0
    assert reached["reason"] == "arrival"


def test_position_without_world_coordinates_is_convertible() -> None:
    harness = NavHarness()
    route = make_route(((0.0, 0.0), (0.0, -2000.0)))
    harness.start(route)
    # ShipPosition.minimap_px 为内容区相对坐标（原点内容区左上）
    client_px = CALIBRATION.world_to_minimap(0.0, 3000.0)
    content_px = (client_px[0] - MINIMAP_RECT.x, client_px[1] - MINIMAP_RECT.y)
    actions = harness.tick_ship(make_ship(None, minimap_px=content_px), 0.0)
    assert len(actions) == 1
    moved_px = (content_px[0], content_px[1] - 4.0)
    harness.tick_ship(make_ship(None, minimap_px=moved_px), 8.0)
    assert harness.navigator.phase == "await_arrival"


# ---------------------------------------------------------------------------
# 航线结束/停止/配置保护
# ---------------------------------------------------------------------------


def test_route_end_stops_navigation() -> None:
    harness = NavHarness()
    harness.start(make_route(((0.0, 0.0),)))
    harness.tick((0.0, 300.0), 0.0)
    ended = harness.last_event(NAV_EVENT_ROUTE_END)
    assert ended["waypoints"] == 1
    assert ended["action"] == "wait"
    assert harness.navigator.finished
    assert harness.navigator.current_waypoint is None
    assert harness.tick((0.0, 300.0), 5.0) == []


def test_stop_disables_further_actions() -> None:
    harness = NavHarness()
    harness.start(make_route())
    harness.navigator.stop()
    assert harness.navigator.tick(make_observation(make_ship((0.0, 3000.0)), 0.0), 0.0) == []
    assert not harness.navigator.active


def test_full_map_surface_is_rejected() -> None:
    harness = NavHarness(replace(NavigationConfig(), surface="full_map"))
    with pytest.raises(ValueError, match="minimap"):
        harness.start(make_route())


def test_empty_waypoints_rejected() -> None:
    harness = NavHarness()
    with pytest.raises(ValueError, match="没有航点"):
        harness.start(Route("empty", "s", ()))


def test_move_confirm_px_threshold_is_configurable() -> None:
    harness = NavHarness(replace(NavigationConfig(), move_confirm_px=10.0))
    harness.start(make_route())
    harness.tick((0.0, 3000.0), 0.0)
    # 位移 4px 小于配置阈值 10px -> 判定失败并重试
    harness.tick((0.0, 2840.0), 8.0)
    retry = harness.last_event(NAV_EVENT_RETRY)
    assert retry["reason"] == "click_not_confirmed"
    assert retry["displacement_px"] == pytest.approx(4.0)


def test_stuck_distance_px_threshold_is_configurable() -> None:
    config = replace(NavigationConfig(), click_retry=1, stuck_distance_px=100.0)
    harness = NavHarness(config)
    harness.start(make_route())
    harness.tick((0.0, 3000.0), 0.0)
    harness.tick((0.0, 2840.0), 8.0)
    # 20s 内位移 4px，小于配置阈值 100px -> 卡住
    harness.tick((0.0, 2840.0), 28.1)
    assert NAV_EVENT_STUCK in harness.event_names()


def test_events_use_expected_nav_names() -> None:
    allowed = {
        NAV_EVENT_SELECT_ROUTE,
        NAV_EVENT_CLICK_WAYPOINT,
        NAV_EVENT_WAYPOINT_REACHED,
        NAV_EVENT_RETRY,
        NAV_EVENT_STUCK,
        NAV_EVENT_SKIP,
        NAV_EVENT_ABANDON,
        NAV_EVENT_ROUTE_END,
    }
    harness = NavHarness(replace(NavigationConfig(), click_retry=1))
    harness.start(make_route())
    harness.tick((0.0, 3000.0), 0.0)
    harness.tick((0.0, 3000.0), 8.0)
    harness.tick((0.0, 3000.0), 8.1)
    harness.tick((0.0, 3000.0), 13.2)
    assert set(harness.event_names()) <= allowed
