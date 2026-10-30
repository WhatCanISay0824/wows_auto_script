"""坐标变换单元测试（详细设计 4.5 / 5.6 / 7.2）。"""

from __future__ import annotations

import math

import numpy as np
import pytest
from src.common.coords import (
    BattleCalibration,
    CalibrationMode,
    ImageOrientation,
    WorldImageTransform,
    apply_affine,
    compose_affine,
    expected_minimap_scale,
    invert_affine,
    orientation_matrix,
    rotation_is_axis_aligned,
)
from src.common.types import Rect

WORLD_BOUNDS = (-6000.0, -6000.0, 6000.0, 6000.0)
IMAGE_BOUNDS = (120.0, 80.0, 1880.0, 1840.0)


def test_normal_orientation_roundtrip() -> None:
    transform = WorldImageTransform(WORLD_BOUNDS, IMAGE_BOUNDS)
    for point in [(-6000.0, -6000.0), (0.0, 0.0), (4200.0, -3500.0), (6000.0, 6000.0)]:
        u, v = transform.world_to_image(*point)
        x, y = transform.image_to_world(u, v)
        assert x == pytest.approx(point[0], abs=1e-6)
        assert y == pytest.approx(point[1], abs=1e-6)


def test_north_maps_to_top() -> None:
    transform = WorldImageTransform(WORLD_BOUNDS, IMAGE_BOUNDS)
    u, v = transform.world_to_image(0.0, 6000.0)
    assert v == pytest.approx(80.0)  # 北 -> 底图顶部 v_min
    _, south_v = transform.world_to_image(0.0, -6000.0)
    assert south_v == pytest.approx(1840.0)  # 南 -> 底部 v_max


@pytest.mark.parametrize("orientation", list(ImageOrientation))
def test_orientation_roundtrip_all(orientation: ImageOrientation) -> None:
    transform = WorldImageTransform(WORLD_BOUNDS, IMAGE_BOUNDS, orientation)
    for point in [(-6000.0, -6000.0), (0.0, 0.0), (6000.0, 6000.0), (3000.0, -2500.0)]:
        u, v = transform.world_to_image(*point)
        # 结果应落在 image_bounds 内（允许 1e-6 浮点误差）
        assert IMAGE_BOUNDS[0] - 1e-6 <= u <= IMAGE_BOUNDS[2] + 1e-6
        assert IMAGE_BOUNDS[1] - 1e-6 <= v <= IMAGE_BOUNDS[3] + 1e-6
        x, y = transform.image_to_world(u, v)
        assert x == pytest.approx(point[0], abs=1e-6)
        assert y == pytest.approx(point[1], abs=1e-6)


def test_orientation_matrix_maps_unit_square_into_unit_square() -> None:
    for orientation in ImageOrientation:
        matrix = orientation_matrix(orientation)
        for a, b in [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0), (0.3, 0.7)]:
            pa, pb = apply_affine(matrix, a, b)
            assert -1e-9 <= pa <= 1 + 1e-9
            assert -1e-9 <= pb <= 1 + 1e-9


def test_orientation_swaps_axes() -> None:
    rot90 = WorldImageTransform(WORLD_BOUNDS, IMAGE_BOUNDS, ImageOrientation.ROT90)
    normal = WorldImageTransform(WORLD_BOUNDS, IMAGE_BOUNDS)
    # 同一世界点，旋转 90 度后 u/v 的角色互换
    u0, v0 = normal.world_to_image(3000.0, 0.0)
    u1, v1 = rot90.world_to_image(3000.0, 0.0)
    assert (u1, v1) != pytest.approx((u0, v0))
    assert rot90.canonical_size() == (1760.0, 1760.0)


def test_world_to_image_matrix_override() -> None:
    # 直接给出矩阵时优先使用
    matrix = (0.5, 0.0, 10.0, 0.0, 0.5, 20.0)
    transform = WorldImageTransform(WORLD_BOUNDS, IMAGE_BOUNDS, world_to_image_matrix=matrix)
    assert transform.world_to_image(0.0, 0.0) == (10.0, 20.0)
    assert transform.world_to_image_affine == pytest.approx(matrix)
    x, y = transform.image_to_world(10.0, 20.0)
    assert (x, y) == pytest.approx((0.0, 0.0))


def test_invert_affine_rejects_singular() -> None:
    singular = np.array([[1.0, 2.0, 0.0], [2.0, 4.0, 0.0], [0.0, 0.0, 1.0]])
    with pytest.raises(ValueError):
        invert_affine(singular)


def test_full_map_calibration() -> None:
    transform = WorldImageTransform(WORLD_BOUNDS, IMAGE_BOUNDS)
    calibration = BattleCalibration(
        world_image=transform,
        minimap_rect=Rect(100, 200, 300, 300),
        mode=CalibrationMode.FULL_MAP,
        window_rect=Rect(1000, 500, 2560, 1600),
    )
    # 世界中心 -> 小地图内容区中心
    mx, my = calibration.world_to_minimap(0.0, 0.0)
    assert (mx, my) == pytest.approx((250.0, 350.0))
    # 世界西北角（x_min, y_max）-> 小地图左上角
    mx, my = calibration.world_to_minimap(-6000.0, 6000.0)
    assert (mx, my) == pytest.approx((100.0, 200.0))
    # 世界东南角 -> 小地图右下角
    mx, my = calibration.world_to_minimap(6000.0, -6000.0)
    assert (mx, my) == pytest.approx((400.0, 500.0))
    # 反变换
    assert calibration.minimap_to_world(250.0, 350.0) == pytest.approx((0.0, 0.0))
    # 屏幕坐标 = 窗口客户区原点 + 小地图坐标
    assert calibration.world_to_screen(0.0, 0.0) == (1250, 850)
    assert calibration.screen_to_world(1250, 850) == pytest.approx((0.0, 0.0))


def test_match_mode_calibration() -> None:
    transform = WorldImageTransform(WORLD_BOUNDS, IMAGE_BOUNDS)
    # 底图 1760x1760 -> 小地图 300x300，缩放约 0.17，再平移到内容区
    scale = 300.0 / 1760.0
    affine = (scale, 0.0, 100.0 - IMAGE_BOUNDS[0] * scale, 0.0, scale, 200.0 - IMAGE_BOUNDS[1] * scale)
    calibration = BattleCalibration(
        world_image=transform,
        minimap_rect=Rect(100, 200, 300, 300),
        mode=CalibrationMode.MATCH,
        window_rect=Rect(1000, 500, 2560, 1600),
        image_to_minimap_matrix=affine,
    )
    # 世界中心 -> 底图中心 -> 小地图中心附近
    mx, my = calibration.world_to_minimap(0.0, 0.0)
    assert (mx, my) == pytest.approx((250.0, 350.0), abs=1e-6)
    # 与整图模式在同一世界点上的结果应一致（本构造下）
    full = BattleCalibration(
        world_image=transform,
        minimap_rect=Rect(100, 200, 300, 300),
        mode=CalibrationMode.FULL_MAP,
        window_rect=Rect(1000, 500, 2560, 1600),
    )
    for point in [(-6000.0, 6000.0), (4200.0, -3500.0), (6000.0, -6000.0)]:
        assert calibration.world_to_minimap(*point) == pytest.approx(full.world_to_minimap(*point), abs=1e-6)


def test_match_mode_requires_affine() -> None:
    transform = WorldImageTransform(WORLD_BOUNDS, IMAGE_BOUNDS)
    with pytest.raises(ValueError):
        BattleCalibration(
            world_image=transform,
            minimap_rect=Rect(0, 0, 300, 300),
            mode=CalibrationMode.MATCH,
            window_rect=Rect(0, 0, 2560, 1600),
        )


def test_compose_affine_order() -> None:
    translate = np.array([[1.0, 0.0, 10.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    scale = np.array([[2.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 1.0]])
    # 先平移再缩放：(x + 10) * 2
    combined = compose_affine(scale, translate)
    assert apply_affine(combined, 5.0, 0.0) == (30.0, 0.0)


def test_expected_minimap_scale() -> None:
    transform = WorldImageTransform(WORLD_BOUNDS, IMAGE_BOUNDS)
    scale = expected_minimap_scale(Rect(0, 0, 300, 300), transform)
    assert scale == pytest.approx((300.0 / 1760.0) * math.sqrt(1.0))


def test_rotation_helpers() -> None:
    assert rotation_is_axis_aligned(0.0)
    assert rotation_is_axis_aligned(89.0)
    assert rotation_is_axis_aligned(181.0)
    assert not rotation_is_axis_aligned(45.0)
