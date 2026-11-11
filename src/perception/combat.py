"""从小地图读取实心敌舰标记和镜头扇区；不推测射程或弹道。"""

from __future__ import annotations

import math

import cv2
import numpy as np

from ..common.types import CombatObservation, EnemyContact, ShipPosition
from ..config.models import MinimapConfig


def enemy_markers(image: np.ndarray, config: MinimapConfig) -> tuple[tuple[float, float], ...]:
    """保留实心、近似凸形的红橙标记；排除细线、空心符号和矩形旗帜。"""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(config.enemy_hsv_lower), np.array(config.enemy_hsv_upper))
    count, labels, stats, centers = cv2.connectedComponentsWithStats(mask)
    points: list[tuple[float, float]] = []
    height, width = mask.shape
    for index in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[index])
        if not 35 <= area <= 350 or not 5 <= w <= 30 or not 5 <= h <= 30:
            continue
        if x < 3 or y < 3 or x+w > width-3 or y+h > height-3:
            continue
        if not .36 <= area / (w*h) <= .78 or not .4 <= w/h <= 2.5:
            continue
        component = (labels[y:y+h, x:x+w] == index).astype(np.uint8) * 255
        contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contour = max(contours, key=cv2.contourArea)
        hull_area = cv2.contourArea(cv2.convexHull(contour))
        if hull_area <= 0 or cv2.contourArea(contour) / hull_area < .82:
            continue
        padded = np.pad(component, 1)
        if cv2.distanceTransform(padded, cv2.DIST_L2, 3).max() < 1.8:
            continue
        point = (float(centers[index][0]), float(centers[index][1]))
        if any(math.dist(point, previous) < 12 for previous in points):
            continue
        points.append(point)
    return tuple(points)


def camera_bearing(
    image: np.ndarray, own: tuple[float, float], config: MinimapConfig,
) -> float | None:
    """以己方标记为原点，估计青色扇区方向，北为 0 度、顺时针为正。"""
    height, width = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(config.view_cone_hsv_lower), np.array(config.view_cone_hsv_upper)) > 0
    angles = np.arange(0, 360, 2)
    radians = np.deg2rad(angles)[:, None]
    radii = np.linspace(25, min(width, height) * .4, 45)
    xs = np.rint(own[0] + np.sin(radians) * radii).astype(int)
    ys = np.rint(own[1] - np.cos(radians) * radii).astype(int)
    valid = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
    samples = mask[np.clip(ys, 0, height-1), np.clip(xs, 0, width-1)] & valid
    scores = samples.sum(axis=1) / np.maximum(valid.sum(axis=1), 1)
    scores[valid.sum(axis=1) < 20] = 0
    active_degrees = int(np.count_nonzero(scores > .5)) * 2
    if not 24 <= active_degrees <= 120:
        return None
    weights = np.maximum(scores - .35, 0)
    total = float(weights.sum())
    vector = np.sum(weights * np.exp(1j * np.deg2rad(angles)))
    if total <= 0 or abs(vector) / total < .85:
        return None
    return float(np.rad2deg(np.angle(vector)) % 360)


class MinimapCombatObserver:
    """按连续两张新帧确认敌舰；断帧或标记消失立即撤销。"""

    def __init__(self, config: MinimapConfig) -> None:
        self.config = config
        self.reset()

    def reset(self) -> None:
        self._previous: tuple[tuple[float, float], ...] = ()
        self._last: CombatObservation | None = None

    def observe(
        self, image: np.ndarray, ship: ShipPosition | None, frame_id: int, timestamp: float,
    ) -> CombatObservation:
        if self._last is not None and (frame_id, timestamp) == (self._last.frame_id, self._last.timestamp):
            return self._last
        valid_ship = ship is not None and ship.source == "marker" and ship.confidence >= .6
        own = ship.minimap_px if valid_ship else None
        points = enemy_markers(image, self.config) if own is not None else ()
        camera = camera_bearing(image, own, self.config) if own is not None else None
        consecutive = (
            self._last is not None and frame_id > self._last.frame_id
            and 0 < timestamp - self._last.timestamp <= 1.5
        )
        confirmed = tuple(
            EnemyContact(point) for point in points
            if consecutive and any(math.dist(point, previous) <= 12 for previous in self._previous)
        )
        self._previous = points
        self._last = CombatObservation(timestamp, frame_id, own, camera, confirmed, len(points))
        return self._last
