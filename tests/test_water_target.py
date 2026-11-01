"""自动水面目标筛选纯函数测试（合成 BGR 小地图，不发送输入）。"""

from __future__ import annotations

import math

import cv2
import numpy as np
import pytest
from src.navigation.water_target import FAILED_TARGET_RADIUS, choose_water_target

SEA_HSV = (95, 180, 90)
LAND_HSV = (35, 80, 90)
ICON_HSV = (0, 0, 245)


def make_hsv(width: int = 200, height: int = 200, *, land: bool = False) -> np.ndarray:
    hsv = np.empty((height, width, 3), dtype=np.uint8)
    hsv[:] = LAND_HSV if land else SEA_HSV
    return hsv


def to_bgr(hsv: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def water_mask(image: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, (85, 130, 20), (110, 255, 160)) > 0


def safety_distance(image: np.ndarray) -> float:
    height, width = image.shape[:2]
    return max(3.0, float(math.ceil(0.035 * min(height, width))))


def clearance_at(image: np.ndarray, x: float, y: float) -> float:
    mask = water_mask(image).astype(np.uint8)
    padded = cv2.copyMakeBorder(mask, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    distance = cv2.distanceTransform(padded, cv2.DIST_L2, 3)[1:-1, 1:-1]
    height, width = image.shape[:2]
    px = min(width - 1, max(0, int(round(x * width))))
    py = min(height - 1, max(0, int(round(y * height))))
    return float(distance[py, px])


def test_known_sea_hsv_is_recognized() -> None:
    image = to_bgr(make_hsv(64, 64))
    assert bool(water_mask(image).all())


def test_destination_inside_island_moves_to_water_with_clearance() -> None:
    hsv = make_hsv(200, 200)
    cv2.circle(hsv, (100, 40), 20, LAND_HSV, thickness=-1)
    image = to_bgr(hsv)

    result = choose_water_target(image, (0.5, 0.2), own_position=(0.5, 0.8))

    assert result is not None
    x, y = result
    assert math.dist(result, (0.5, 0.2)) <= 0.25
    height, width = image.shape[:2]
    px = min(width - 1, max(0, int(round(x * width))))
    py = min(height - 1, max(0, int(round(y * height))))
    assert bool(water_mask(image)[py, px])
    assert clearance_at(image, x, y) >= safety_distance(image)
    assert math.dist(result, (0.5, 0.2)) > 0.0


def test_already_safe_desired_is_retained_within_pixel_rounding() -> None:
    image = to_bgr(make_hsv(200, 200))
    result = choose_water_target(image, (0.5, 0.2), own_position=(0.5, 0.8))
    assert result == pytest.approx((0.5, 0.2))
    assert abs(result[0] - 0.5) <= 1.0 / 200
    assert abs(result[1] - 0.2) <= 1.0 / 200


@pytest.mark.parametrize(
    "image",
    [
        np.zeros((20, 200, 3), dtype=np.uint8),
        np.zeros((200, 20, 3), dtype=np.uint8),
        np.zeros((200, 200), dtype=np.uint8),
        np.zeros((200, 200, 4), dtype=np.uint8),
        np.zeros((200, 200, 3), dtype=np.float32),
        None,
    ],
)
def test_invalid_images_return_none(image: object) -> None:
    assert choose_water_target(image, (0.5, 0.5), own_position=(0.1, 0.1)) is None


def test_all_land_blank_or_tiny_water_returns_none() -> None:
    blank = np.zeros((200, 200, 3), dtype=np.uint8)
    all_land = to_bgr(make_hsv(200, 200, land=True))
    hsv = make_hsv(200, 200, land=True)
    hsv[0:20, 0:20] = SEA_HSV
    tiny_water = to_bgr(hsv)

    for image in (blank, all_land, tiny_water):
        assert choose_water_target(image, (0.5, 0.5), own_position=(0.1, 0.1)) is None


def test_narrow_water_channel_near_desired_is_rejected() -> None:
    hsv = make_hsv(200, 200, land=True)
    hsv[0:20, :] = SEA_HSV
    hsv[180:200, :] = SEA_HSV
    hsv[20:180, 96:104] = SEA_HSV
    image = to_bgr(hsv)

    # 水面总量足够，但目标附近的通道宽度小于两侧安全距离，远端水面又在 0.25 半径外。
    assert choose_water_target(image, (0.5, 0.5)) is None


def test_water_unconnected_to_own_position_is_excluded() -> None:
    hsv = make_hsv(300, 300, land=True)
    hsv[:, 0:140] = SEA_HSV
    hsv[:, 160:300] = SEA_HSV
    image = to_bgr(hsv)

    result = choose_water_target(image, (0.9, 0.5), own_position=(0.15, 0.5))
    assert result is None


def test_own_position_without_nearby_water_returns_none() -> None:
    hsv = make_hsv(300, 300, land=True)
    hsv[:, 150:300] = SEA_HSV
    image = to_bgr(hsv)

    assert choose_water_target(image, (0.9, 0.5), own_position=(0.05, 0.5)) is None


def test_own_unavailable_uses_largest_water_component() -> None:
    hsv = make_hsv(300, 300, land=True)
    hsv[:, 0:80] = SEA_HSV
    hsv[:, 100:300] = SEA_HSV
    image = to_bgr(hsv)

    result = choose_water_target(image, (0.9, 0.5))

    assert result == pytest.approx((0.9, 0.5))


def test_land_icon_overlay_is_conservatively_avoided() -> None:
    hsv = make_hsv(200, 200)
    cv2.rectangle(hsv, (94, 34), (106, 46), ICON_HSV, thickness=-1)
    image = to_bgr(hsv)

    result = choose_water_target(image, (0.5, 0.2), own_position=(0.5, 0.8))

    assert result is not None
    assert math.dist(result, (0.5, 0.2)) > 0.0
    height, width = image.shape[:2]
    px = min(width - 1, max(0, int(round(result[0] * width))))
    py = min(height - 1, max(0, int(round(result[1] * height))))
    assert not (94 <= px <= 106 and 34 <= py <= 46)
    assert clearance_at(image, result[0], result[1]) >= safety_distance(image)


def test_thin_overlay_line_is_filled_by_close_before_clearance() -> None:
    hsv = make_hsv(200, 200)
    hsv[40, :] = ICON_HSV
    image = to_bgr(hsv)

    result = choose_water_target(image, (0.5, 0.2), own_position=(0.5, 0.8))

    assert result == pytest.approx((0.5, 0.2))


def test_choice_is_deterministic_and_excluded_neighborhood_is_rejected() -> None:
    image = to_bgr(make_hsv(240, 240))
    first = choose_water_target(image, (0.5, 0.5), own_position=(0.2, 0.2))
    second = choose_water_target(image, (0.5, 0.5), own_position=(0.2, 0.2))

    assert first == second == pytest.approx((0.5, 0.5))

    fallback = choose_water_target(image, (0.5, 0.5), own_position=(0.2, 0.2), excluded=[first])
    assert fallback is not None
    assert math.dist(fallback, first) > FAILED_TARGET_RADIUS - 1e-9
    assert math.dist(fallback, (0.5, 0.5)) <= 0.25
