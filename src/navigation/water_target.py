"""自动水面目标筛选（纯函数，不发送任何输入）。

只从当前战斗帧的小地图裁剪图中识别已知的蓝色海面 HSV 范围，排除陆地、图标和
未知像素；不会把“不是陆地”当成水面。返回值是归一化小地图坐标，仍需由现有
全屏地图换算和游戏自动驾驶完成完整航线规划。

本模块不实现绕岛路径规划，也不声称选中的点一定可到达；它只负责给自动巡航
提供一个远离当前可识别海岸线的合理终点。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Final

import cv2
import numpy as np

# 失败候选邻域半径（归一化）；导航器重试时排除该范围内已确认未移动的目标。
FAILED_TARGET_RADIUS: Final = 0.06

_MIN_IMAGE_SIDE: Final = 32
_MIN_WATER_RATIO: Final = 0.15
_SAFE_RADIUS_RATIO: Final = 0.035
_OWN_WATER_SEARCH_RADIUS: Final = 0.06
_MAX_DESIRED_DISTANCE: Final = 0.25
_EDGE_SAFE_X: Final = (0.04, 0.96)
_EDGE_SAFE_Y: Final = (0.02, 0.98)

# 实机已知海面：OpenCV HSV 中 H 为 0~179。其余像素一律视为未知/不安全。
_SEA_HSV_LOWER = np.array([85, 130, 20], dtype=np.uint8)
_SEA_HSV_UPPER = np.array([110, 255, 160], dtype=np.uint8)


def choose_water_target(
    image_bgr: np.ndarray,
    desired: tuple[float, float],
    *,
    own_position: tuple[float, float] | None = None,
    excluded: Sequence[tuple[float, float]] = (),
) -> tuple[float, float] | None:
    """在小地图中返回一个安全、靠近 ``desired`` 的水面归一化坐标。

    参数：
    - ``image_bgr``：当前战斗帧的小地图内容区，必须为 uint8 BGR，短边至少 32px；
    - ``desired``：默认策略给出的归一化目标；
    - ``own_position``：当前自身标记的归一化位置；``None`` 表示不可用；
    - ``excluded``：已确认未移动的失败候选，排除其 0.06 归一化邻域。

    无有效水面、图像无效或所有候选都被排除时返回 ``None``，调用方不得回退到
    地图中心或其他未验证终点。
    """
    if not _is_valid_bgr(image_bgr):
        return None
    desired_point = _normalize_point(desired)
    if desired_point is None:
        return None
    desired_x, desired_y = desired_point

    height, width = image_bgr.shape[:2]
    water = _sea_mask(image_bgr)
    total = height * width
    if int(np.count_nonzero(water)) < _MIN_WATER_RATIO * total:
        return None

    safety = max(3.0, float(math.ceil(_SAFE_RADIUS_RATIO * min(height, width))))
    safe_water = _safe_water_mask(water, safety)
    component_count, labels = cv2.connectedComponents(water.astype(np.uint8), connectivity=8)
    if component_count <= 1:
        return None

    own_point = _normalize_point(own_position)
    allowed_label = _allowed_component(water, labels, own_point, total)
    if allowed_label is None:
        return None

    failed = _normalize_points(excluded)
    if _point_is_candidate(
        desired_x,
        desired_y,
        width,
        height,
        safe_water,
        labels,
        allowed_label,
        failed,
    ):
        return desired_point

    candidates = _safe_candidates(
        safe_water,
        labels,
        allowed_label,
        width,
        height,
        desired_x,
        desired_y,
        failed,
    )
    if candidates is None:
        return None
    x, y = candidates
    return float(x), float(y)


def _is_valid_bgr(image: object) -> bool:
    return (
        isinstance(image, np.ndarray)
        and image.dtype == np.uint8
        and image.ndim == 3
        and image.shape[2] == 3
        and image.shape[0] >= _MIN_IMAGE_SIDE
        and image.shape[1] >= _MIN_IMAGE_SIDE
    )


def _normalize_point(point: object) -> tuple[float, float] | None:
    if point is None:
        return None
    try:
        x = float(point[0])
        y = float(point[1])
    except (TypeError, ValueError, IndexError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return x, y


def _normalize_points(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for point in points:
        normalized = _normalize_point(point)
        if normalized is not None:
            result.append(normalized)
    return result


def _sea_mask(image_bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, _SEA_HSV_LOWER, _SEA_HSV_UPPER)
    # 3x3 闭运算可填补细小的覆盖线/图标描边，不改变大块陆地的边界。
    kernel = np.ones((3, 3), dtype=np.uint8)
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel) > 0


def _safe_water_mask(water: np.ndarray, safety: float) -> np.ndarray:
    """离任何非水面像素至少 ``safety`` 像素的水面；图像边缘按非水面处理。"""
    mask = water.astype(np.uint8)
    padded = cv2.copyMakeBorder(mask, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    distances = cv2.distanceTransform(padded, cv2.DIST_L2, 3)
    distances = distances[1:-1, 1:-1]
    return water & (distances >= safety - 1e-6)


def _allowed_component(
    water: np.ndarray,
    labels: np.ndarray,
    own_point: tuple[float, float] | None,
    total: int,
) -> int | None:
    height, width = water.shape[:2]
    if own_point is not None:
        water_y, water_x = np.nonzero(water)
        if water_x.size == 0:
            return None
        distances = np.hypot(water_x / float(width) - own_point[0], water_y / float(height) - own_point[1])
        nearest = int(np.argmin(distances))
        if float(distances[nearest]) > _OWN_WATER_SEARCH_RADIUS + 1e-9:
            return None
        label = int(labels[water_y[nearest], water_x[nearest]])
        return label if label > 0 else None

    areas = np.bincount(labels.ravel())
    if areas.size <= 1:
        return None
    areas[0] = 0
    label = int(np.argmax(areas))
    if int(areas[label]) < _MIN_WATER_RATIO * total:
        return None
    return label


def _point_is_candidate(
    x: float,
    y: float,
    width: int,
    height: int,
    safe_water: np.ndarray,
    labels: np.ndarray,
    allowed_label: int,
    failed: list[tuple[float, float]],
) -> bool:
    if not _within_bounds(x, y):
        return False
    if _is_failed(x, y, failed):
        return False
    px = min(width - 1, max(0, int(math.floor(x * width))))
    py = min(height - 1, max(0, int(math.floor(y * height))))
    return bool(safe_water[py, px] and int(labels[py, px]) == allowed_label)


def _safe_candidates(
    safe_water: np.ndarray,
    labels: np.ndarray,
    allowed_label: int,
    width: int,
    height: int,
    desired_x: float,
    desired_y: float,
    failed: list[tuple[float, float]],
) -> tuple[float, float] | None:
    y_index, x_index = np.nonzero(safe_water)
    if x_index.size == 0:
        return None
    same_component = labels[y_index, x_index] == allowed_label
    if not np.any(same_component):
        return None
    x_index = x_index[same_component]
    y_index = y_index[same_component]

    normalized_x = x_index / float(width)
    normalized_y = y_index / float(height)
    desired_distance = np.hypot(normalized_x - desired_x, normalized_y - desired_y)

    valid = desired_distance <= _MAX_DESIRED_DISTANCE + 1e-12
    valid &= (normalized_x >= _EDGE_SAFE_X[0]) & (normalized_x <= _EDGE_SAFE_X[1])
    valid &= (normalized_y >= _EDGE_SAFE_Y[0]) & (normalized_y <= _EDGE_SAFE_Y[1])
    for failed_x, failed_y in failed:
        valid &= np.hypot(normalized_x - failed_x, normalized_y - failed_y) > FAILED_TARGET_RADIUS
    if not np.any(valid):
        return None

    indexes = np.nonzero(valid)[0]
    # 按到 desired 的距离排序；同距离时用 y、x 打破平局，保证确定性。
    ordered = indexes[np.lexsort((x_index[indexes], y_index[indexes], desired_distance[indexes]))]
    best = int(ordered[0])
    return float(normalized_x[best]), float(normalized_y[best])


def _within_bounds(x: float, y: float) -> bool:
    return _EDGE_SAFE_X[0] <= x <= _EDGE_SAFE_X[1] and _EDGE_SAFE_Y[0] <= y <= _EDGE_SAFE_Y[1]


def _is_failed(x: float, y: float, failed: list[tuple[float, float]]) -> bool:
    return any(math.hypot(x - failed_x, y - failed_y) <= FAILED_TARGET_RADIUS for failed_x, failed_y in failed)
