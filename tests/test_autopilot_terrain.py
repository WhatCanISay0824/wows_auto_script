"""AutoPilotNavigator 自动水面目标集成测试（合成 BGR 小地图提供器）。"""

from __future__ import annotations

import math
import random
from dataclasses import replace

import cv2
import numpy as np
import pytest
from src.common.types import ActionKind
from src.navigation.autopilot import AutoPilotNavigator
from src.navigation.water_target import FAILED_TARGET_RADIUS

from tests.test_autopilot import (
    _Events,
    key_actions,
    make_config,
    make_keymap,
    make_observation,
    make_profile,
)

SEA_HSV = (95, 180, 90)
LAND_HSV = (35, 80, 90)
ICON_HSV = (0, 0, 245)


def make_hsv(size: int = 200, *, land: bool = False) -> np.ndarray:
    hsv = np.empty((size, size, 3), dtype=np.uint8)
    hsv[:] = LAND_HSV if land else SEA_HSV
    return hsv


def to_bgr(hsv: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def water_image(size: int = 200) -> np.ndarray:
    return to_bgr(make_hsv(size))


def island_image(size: int = 200, *, center: tuple[int, int] = (100, 40), radius: int = 20) -> np.ndarray:
    hsv = make_hsv(size)
    cv2.circle(hsv, center, radius, LAND_HSV, thickness=-1)
    return to_bgr(hsv)


def icon_image(size: int = 200, *, center: tuple[int, int] = (100, 40), half: int = 6) -> np.ndarray:
    hsv = make_hsv(size)
    x, y = center
    cv2.rectangle(hsv, (x - half, y - half), (x + half, y + half), ICON_HSV, thickness=-1)
    return to_bgr(hsv)


def water_mask(image: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, (85, 130, 20), (110, 255, 160)) > 0


def clearance_at(image: np.ndarray, x: float, y: float) -> float:
    mask = water_mask(image).astype(np.uint8)
    padded = cv2.copyMakeBorder(mask, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    distance = cv2.distanceTransform(padded, cv2.DIST_L2, 3)[1:-1, 1:-1]
    height, width = image.shape[:2]
    px = min(width - 1, max(0, int(round(x * width))))
    py = min(height - 1, max(0, int(round(y * height))))
    return float(distance[py, px])


def safety_distance(image: np.ndarray) -> float:
    height, width = image.shape[:2]
    return max(3.0, float(math.ceil(0.035 * min(height, width))))


class _Provider:
    """按调用次数返回预设帧；用于验证一个周期只取一次当前小地图。"""

    def __init__(self, *frames: np.ndarray | None) -> None:
        self.frames = list(frames)
        self.calls = 0

    def __call__(self, observation) -> np.ndarray | None:  # noqa: ANN001
        self.calls += 1
        if not self.frames:
            return None
        return self.frames[min(self.calls - 1, len(self.frames) - 1)]


def make_terrain_nav(
    provider: _Provider,
    *,
    config=None,  # noqa: ANN001
    events: _Events | None = None,
) -> AutoPilotNavigator:
    return AutoPilotNavigator(
        config or make_config(),
        make_profile(),
        rng=random.Random(1),
        on_event=events,
        keymap=make_keymap(),
        minimap_provider=provider,
    )


def run_full_cycle(nav: AutoPilotNavigator, observation, start: float):  # noqa: ANN001
    """推进一个完整 M 开图 -> 点击 -> M 关图周期，返回点击动作。"""
    assert nav.tick(observation, start) == []
    assert nav.phase == "open_map"
    assert key_actions(nav.tick(observation, start + 0.1)) == [("key_down", "m"), ("key_up", "m")]
    assert nav.tick(observation, start + 1.2) == []
    assert nav.phase == "click"
    click_actions = nav.tick(observation, start + 1.3)
    assert len(click_actions) == 1
    assert click_actions[0].kind is ActionKind.CLICK_ABS
    assert key_actions(nav.tick(observation, start + 1.8)) == [("key_down", "m"), ("key_up", "m")]
    assert nav.phase == "verify"
    return click_actions[0]


def test_destination_inside_island_moves_to_water_with_clearance() -> None:
    image = island_image()
    provider = _Provider(image)
    events = _Events()
    nav = make_terrain_nav(provider, events=events)
    nav.start(0.0)
    observation = make_observation((0.5, 0.8))  # 默认对称目标 (0.5, 0.2) 正好在岛上

    run_full_cycle(nav, observation, 0.0)

    target = nav.last_target
    assert target is not None
    assert math.dist(target.normalized, (0.5, 0.2)) > 0.0
    assert math.dist(target.normalized, (0.5, 0.2)) <= 0.25
    assert bool(water_mask(image)[int(round(target.normalized[1] * image.shape[0])),
                                  int(round(target.normalized[0] * image.shape[1]))])
    assert clearance_at(image, *target.normalized) >= safety_distance(image)
    assert events.last("autopilot_target_adjusted")["reason"] == "sea_clearance"
    assert provider.calls == 1


def test_already_safe_desired_is_retained_within_pixel_rounding() -> None:
    image = water_image()
    events = _Events()
    nav = make_terrain_nav(_Provider(image), events=events)
    nav.start(0.0)
    observation = make_observation((0.5, 0.8))

    run_full_cycle(nav, observation, 0.0)

    target = nav.last_target
    assert target is not None
    assert target.normalized == pytest.approx((0.5, 0.2))
    assert abs(target.normalized[0] - 0.5) <= 1.0 / image.shape[1]
    assert abs(target.normalized[1] - 0.2) <= 1.0 / image.shape[0]
    assert "autopilot_target_adjusted" not in events.names()


@pytest.mark.parametrize(
    ("image", "reason"),
    [
        (to_bgr(make_hsv(land=True)), "no_water_candidate"),
        (np.zeros((200, 200, 3), dtype=np.uint8), "no_water_candidate"),
        (np.zeros((10, 10, 3), dtype=np.uint8), "invalid_image"),
    ],
)
def test_unusable_frames_produce_no_map_or_click(image: np.ndarray, reason: str) -> None:
    provider = _Provider(image)
    events = _Events()
    nav = make_terrain_nav(provider, events=events)
    nav.start(0.0)
    observation = make_observation((0.5, 0.8))

    assert nav.tick(observation, 0.0) == []

    assert nav.phase == "reclick"
    assert nav.clicks == 0
    assert nav.map_open is False
    assert nav.next_click_at == pytest.approx(10.0)
    assert events.last("autopilot_target_unavailable")["reason"] == reason
    assert provider.calls == 1

    assert nav.tick(observation, 10.0) == []
    assert nav.clicks == 0
    assert nav.map_open is False


def test_provider_returning_none_produces_no_map_or_click() -> None:
    provider = _Provider(None)
    events = _Events()
    nav = make_terrain_nav(provider, events=events)
    nav.start(0.0)

    assert nav.tick(make_observation((0.5, 0.8)), 0.0) == []

    assert nav.phase == "reclick"
    assert nav.next_click_at == pytest.approx(10.0)
    assert nav.clicks == 0
    assert events.last("autopilot_target_unavailable")["reason"] == "image_unavailable"
    assert provider.calls == 1


def test_narrow_shore_near_desired_produces_no_click() -> None:
    hsv = make_hsv(200, land=True)
    hsv[0:20, :] = SEA_HSV
    hsv[180:200, :] = SEA_HSV
    hsv[20:180, 96:104] = SEA_HSV
    provider = _Provider(to_bgr(hsv))
    events = _Events()
    nav = make_terrain_nav(provider, config=make_config(target="center"), events=events)
    nav.start(0.0)

    assert nav.tick(make_observation((0.5, 0.8)), 0.0) == []

    assert nav.clicks == 0
    assert nav.phase == "reclick"
    assert events.last("autopilot_target_unavailable")["reason"] == "no_water_candidate"


def test_water_unconnected_to_current_ship_is_excluded() -> None:
    hsv = make_hsv(300, land=True)
    hsv[:, 0:140] = SEA_HSV
    hsv[:, 160:300] = SEA_HSV
    provider = _Provider(to_bgr(hsv))
    events = _Events()
    # target=center + offset 0.4 -> desired (0.9, 0.5)，位于与自身不连通的右侧水域。
    nav = make_terrain_nav(provider, config=make_config(target="center", offset=(0.4, 0.0)), events=events)
    nav.start(0.0)

    assert nav.tick(make_observation((0.15, 0.5)), 0.0) == []

    assert nav.clicks == 0
    assert nav.phase == "reclick"
    assert events.last("autopilot_target_unavailable")["reason"] == "no_water_candidate"


def test_land_icon_overlay_moves_candidate_away() -> None:
    image = icon_image()
    nav = make_terrain_nav(_Provider(image))
    nav.start(0.0)
    observation = make_observation((0.5, 0.8))

    run_full_cycle(nav, observation, 0.0)

    target = nav.last_target
    assert target is not None
    assert math.dist(target.normalized, (0.5, 0.2)) > 0.0
    px = int(round(target.normalized[0] * image.shape[1]))
    py = int(round(target.normalized[1] * image.shape[0]))
    assert not (94 <= px <= 106 and 34 <= py <= 46)
    assert clearance_at(image, *target.normalized) >= safety_distance(image)


def test_retry_after_no_movement_selects_different_safe_candidate() -> None:
    image = water_image()
    provider = _Provider(image)
    events = _Events()
    nav = make_terrain_nav(provider, events=events)
    nav.start(0.0)
    observation = make_observation((0.5, 0.8))

    run_full_cycle(nav, observation, 0.0)
    first = nav.last_target
    assert first is not None
    assert first.normalized == pytest.approx((0.5, 0.2))

    # 验证窗口结束仍无位移/船速 -> 自动目标进入失败邻域，1s 后开始下一周期。
    for index in range(16):
        moment = 2.0 + index * 0.5
        nav.tick(replace(observation, timestamp=moment, frame_timestamp=moment), moment)
    assert nav.tick(replace(observation, timestamp=10.0, frame_timestamp=10.0), 10.0) == []
    assert nav.phase == "reclick"
    assert events.last("autopilot_target_rejected")["reason"] == "no_movement"
    assert nav.next_click_at == pytest.approx(11.0)

    run_full_cycle(nav, observation, 11.0)
    second = nav.last_target
    assert second is not None
    assert second.normalized != first.normalized
    assert math.dist(second.normalized, first.normalized) > FAILED_TARGET_RADIUS - 1e-9
    assert provider.calls == 2


def test_user_override_keeps_exact_requested_target() -> None:
    image = island_image()
    provider = _Provider(image)
    events = _Events()
    nav = make_terrain_nav(provider, events=events)
    nav.start(0.0)
    observation = make_observation((0.5, 0.8))

    # 先产生一个失败自动目标，再确认用户显式目标不受水域筛选和失败邻域影响。
    run_full_cycle(nav, observation, 0.0)
    for index in range(16):
        moment = 2.0 + index * 0.5
        nav.tick(replace(observation, timestamp=moment, frame_timestamp=moment), moment)
    assert nav.tick(replace(observation, timestamp=10.0, frame_timestamp=10.0), 10.0) == []
    assert nav.phase == "reclick"
    calls_before_override = provider.calls
    adjustments_before_override = events.names().count("autopilot_target_adjusted")

    nav.replace_target((0.3, 0.6), 10.0)
    run_full_cycle(nav, observation, 10.5)

    target = nav.last_target
    assert target is not None
    assert target.normalized == pytest.approx((0.3, 0.6))
    assert target.from_own_marker is False
    assert provider.calls == calls_before_override
    assert events.last("autopilot_target_replaced")["target"] == [0.3, 0.6]
    assert events.names().count("autopilot_target_adjusted") == adjustments_before_override


def test_selected_target_is_frozen_for_whole_map_sequence() -> None:
    # 第一次调用返回有岛帧，后续调用返回全水面帧；若周期中重新取图，目标会变成 (0.5, 0.2)。
    provider = _Provider(island_image(), water_image())
    nav = make_terrain_nav(provider)
    nav.start(0.0)
    observation = make_observation((0.5, 0.8))

    assert nav.tick(observation, 0.0) == []
    assert nav.phase == "open_map"
    assert provider.calls == 1
    frozen = nav.compute_full_map_target(observation)
    assert frozen is not None
    assert math.dist(frozen.normalized, (0.5, 0.2)) > 0.0

    assert key_actions(nav.tick(observation, 0.1)) == [("key_down", "m"), ("key_up", "m")]
    assert nav.compute_full_map_target(observation).normalized == pytest.approx(frozen.normalized)
    assert nav.tick(observation, 1.2) == []
    assert nav.compute_full_map_target(observation).normalized == pytest.approx(frozen.normalized)
    click_actions = nav.tick(observation, 1.3)
    assert len(click_actions) == 1
    assert provider.calls == 1
    assert nav.last_target is not None
    assert nav.last_target.normalized == pytest.approx(frozen.normalized)
