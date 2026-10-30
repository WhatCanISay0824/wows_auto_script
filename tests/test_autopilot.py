"""autopilot 简化导航单元测试（用户简化需求：不识别地图/航线，点小地图后交给游戏自动驾驶）。"""

from __future__ import annotations

import random
from dataclasses import replace

from src.common.types import NormalizedRect, Observation, Rect, ShipPosition
from src.config.models import ClientProfile, MinimapConfig, NavigationConfig, WindowConfig
from src.navigation.autopilot import AutoPilotNavigator

WINDOW = Rect(100, 50, 2000, 1200)  # 客户区屏幕矩形
MINIMAP_NORM = NormalizedRect(0.50, 0.50, 0.90, 0.90)  # 便于手算的测试区域


def make_profile() -> ClientProfile:
    return ClientProfile(
        client_id="test",
        display_name="test",
        window=WindowConfig(process_names=("game.exe",)),
        language="zh_cn",
        reference_resolution=(2000, 1200),
        features={},
        minimap=MinimapConfig(rect=MINIMAP_NORM, border_px=0),
    )


def make_config(target: str = "symmetric", reclick: tuple[float, float] = (60.0, 120.0)) -> NavigationConfig:
    return NavigationConfig(
        mode="autopilot",
        autopilot_target=target,
        autopilot_reclick_s=reclick,
        autopilot_offset=(0.0, 0.0),
    )


def make_observation(ship_norm: tuple[float, float] | None = None, now: float = 0.0) -> Observation:
    window = Rect(*WINDOW.to_tuple())
    content = MINIMAP_NORM.to_pixels(window.w, window.h)
    ship = None
    if ship_norm is not None:
        ship = ShipPosition(
            minimap_px=(ship_norm[0] * content.w, ship_norm[1] * content.h),
            world_xy=None,
            heading_deg=0.0,
            speed_mps=10.0,
            confidence=0.9,
            source="marker",
        )
    return Observation(timestamp=now, window_rect=window.to_tuple(), ship=ship)


def test_symmetric_target_uses_centerline_and_mirrors_own_y() -> None:
    nav = AutoPilotNavigator(make_config(), make_profile(), rng=random.Random(1))
    nav.start(0.0)
    # 我方出生点在小地图下方偏右：own=(0.8, 0.9)
    actions = nav.tick(make_observation((0.8, 0.9)), 0.0)
    assert len(actions) == 1
    target = nav.last_target
    assert target is not None
    assert target.from_own_marker is True
    assert target.normalized[0] == 0.5  # 横坐标固定中线
    assert abs(target.normalized[1] - 0.1) < 1e-6  # 对称到上方
    # 屏幕坐标 = 客户区原点 + 小地图内容区内坐标
    content = MINIMAP_NORM.to_pixels(WINDOW.w, WINDOW.h)
    expected_x = WINDOW.x + content.x + int(round(0.5 * content.w))
    expected_y = WINDOW.y + content.y + int(round(0.1 * content.h))
    assert target.screen_px == (expected_x, expected_y)
    action = actions[0]
    assert action.x == expected_x and action.y == expected_y


def test_center_target_when_configured() -> None:
    nav = AutoPilotNavigator(make_config(target="center"), make_profile(), rng=random.Random(2))
    nav.start(0.0)
    nav.tick(make_observation((0.2, 0.2)), 0.0)
    assert nav.last_target is not None
    assert nav.last_target.normalized == (0.5, 0.5)
    assert nav.last_target.from_own_marker is False


def test_no_click_before_marker_available() -> None:
    nav = AutoPilotNavigator(make_config(), make_profile(), rng=random.Random(3))
    nav.start(0.0)
    # 前 10 秒没有自身标记时先等待，避免错误方向
    assert nav.tick(make_observation(None), 0.0) == []
    assert nav.tick(make_observation(None), 5.0) == []
    # 超过 10 秒仍无标记 -> 回退中心
    actions = nav.tick(make_observation(None), 10.1)
    assert len(actions) == 1
    assert nav.last_target is not None and nav.last_target.from_own_marker is False


def test_reclick_interval_and_no_duplicate_within_window() -> None:
    nav = AutoPilotNavigator(make_config(reclick=(60.0, 60.0)), make_profile(), rng=random.Random(4))
    nav.start(0.0)
    assert len(nav.tick(make_observation((0.5, 0.8)), 0.0)) == 1
    assert nav.tick(make_observation((0.5, 0.8)), 30.0) == []
    assert len(nav.tick(make_observation((0.5, 0.8)), 60.0)) == 1
    assert nav.clicks == 2


def test_own_position_recorded_once() -> None:
    nav = AutoPilotNavigator(make_config(), make_profile(), rng=random.Random(5))
    nav.start(0.0)
    nav.tick(make_observation((0.9, 0.9)), 0.0)
    first = nav.own_position_norm
    nav.tick(make_observation((0.1, 0.1)), 61.0)  # 后续位置变化不覆盖出生点
    assert nav.own_position_norm == first


def test_stop_disables_navigation() -> None:
    nav = AutoPilotNavigator(make_config(), make_profile(), rng=random.Random(6))
    nav.start(0.0)
    nav.stop()
    assert nav.tick(make_observation((0.5, 0.8)), 0.0) == []


def test_missing_minimap_config_returns_no_action() -> None:
    profile = replace(make_profile(), minimap=None)
    nav = AutoPilotNavigator(make_config(), profile, rng=random.Random(7))
    nav.start(0.0)
    assert nav.tick(make_observation((0.5, 0.8)), 0.0) == []
