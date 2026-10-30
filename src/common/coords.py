"""坐标系统与变换（详细设计 1.4 / 4.5 / 5.6 / 7.2）。

四层坐标：
- 世界坐标 world：米，x 向东，y 向北；
- 底图像素坐标 image：background.png 的像素，原点左上，v 向下；
- 小地图像素坐标 minimap：小地图内容区像素，原点内容区左上；
- 屏幕坐标 screen：桌面物理像素，鼠标绝对点击使用。

本模块只处理数学与数据，不访问游戏窗口。窗口矩形由调用方（捕获/输入模块）提供。

关于 ``image_orientation`` 的口径（详细设计 4.2 未写明组合顺序，这里是实现口径，
已记录在 design/详细设计评审与实现补充.md）：
它表示「底图相对世界坐标的旋转/镜像量」，即底图内容相对正北正东画面已经转过的角度。
换算时先得到正北画面的归一化坐标 (a, b)（a 向右、b 向下），再经方向变换得到底图内的
归一化坐标 (a', b')，最后映射到 ``image_bounds`` 矩形内。旋转均按顺时针计算。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from .types import NormalizedRect, Rect

# 2x3 仿射矩阵类型别名（按行展开：m00, m01, tx, m10, m11, ty）
Affine2x3 = tuple[float, float, float, float, float, float]


class ImageOrientation(StrEnum):
    """底图相对世界坐标的方向（详细设计 4.2 / 4.5）。"""

    NORMAL = "normal"
    ROT90 = "rot90"
    ROT180 = "rot180"
    ROT270 = "rot270"
    MIRROR_H = "mirror_h"
    MIRROR_V = "mirror_v"
    MIRROR_ROT90 = "mirror_rot90"
    MIRROR_ROT270 = "mirror_rot270"

    @property
    def swaps_axes(self) -> bool:
        return self in (
            ImageOrientation.ROT90,
            ImageOrientation.ROT270,
            ImageOrientation.MIRROR_ROT90,
            ImageOrientation.MIRROR_ROT270,
        )

    @property
    def is_mirrored(self) -> bool:
        return self in (
            ImageOrientation.MIRROR_H,
            ImageOrientation.MIRROR_V,
            ImageOrientation.MIRROR_ROT90,
            ImageOrientation.MIRROR_ROT270,
        )


# ---------------------------------------------------------------------------
# 仿射矩阵工具
# ---------------------------------------------------------------------------


def affine_to_matrix(affine: Affine2x3) -> np.ndarray:
    """2x3 -> 3x3 齐次矩阵。"""
    if len(affine) != 6:
        raise ValueError(f"仿射矩阵需要 6 个数: {affine!r}")
    m = np.eye(3, dtype=np.float64)
    m[0, 0], m[0, 1], m[0, 2] = affine[0], affine[1], affine[2]
    m[1, 0], m[1, 1], m[1, 2] = affine[3], affine[4], affine[5]
    return m


def matrix_to_affine(matrix: np.ndarray) -> Affine2x3:
    """3x3 -> 2x3（丢弃最后一行）。"""
    m = np.asarray(matrix, dtype=np.float64)
    return (
        float(m[0, 0]),
        float(m[0, 1]),
        float(m[0, 2]),
        float(m[1, 0]),
        float(m[1, 1]),
        float(m[1, 2]),
    )


def compose_affine(*matrices: np.ndarray) -> np.ndarray:
    """按传入顺序合成仿射矩阵：compose(A, B) 表示先 B 后 A。"""
    result = np.eye(3, dtype=np.float64)
    for matrix in matrices:
        result = result @ np.asarray(matrix, dtype=np.float64)
    return result


def apply_affine(matrix: np.ndarray, x: float, y: float) -> tuple[float, float]:
    m = np.asarray(matrix, dtype=np.float64)
    px = m[0, 0] * x + m[0, 1] * y + m[0, 2]
    py = m[1, 0] * x + m[1, 1] * y + m[1, 2]
    return float(px), float(py)


def invert_affine(matrix: np.ndarray) -> np.ndarray:
    m = np.asarray(matrix, dtype=np.float64)
    if not np.isfinite(m).all():
        raise ValueError("仿射矩阵包含非有限值，无法求逆")
    if abs(np.linalg.det(m)) < 1e-12:
        raise ValueError("仿射矩阵不可逆")
    return np.linalg.inv(m)


def affine_rotation_deg(matrix: np.ndarray) -> float:
    """提取 2x3 仿射矩阵的旋转角（顺时针为正，单位度）。"""
    m = np.asarray(matrix, dtype=np.float64)
    return math.degrees(math.atan2(m[1, 0], m[0, 0]))


def affine_scale(matrix: np.ndarray) -> float:
    """提取 2x3 仿射矩阵的缩放比例（相似变换假设）。"""
    m = np.asarray(matrix, dtype=np.float64)
    return float(math.hypot(m[0, 0], m[1, 0]))


def affine_is_mirrored(matrix: np.ndarray) -> bool:
    m = np.asarray(matrix, dtype=np.float64)
    return float(np.linalg.det(m[:2, :2])) < 0


def mirror_x_matrix(width: float) -> np.ndarray:
    """水平镜像：u' = (width - 1) - u，用于底图镜像特征匹配的坐标还原。"""
    return np.array([[-1.0, 0.0, width - 1.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)


def mirror_y_matrix(height: float) -> np.ndarray:
    return np.array([[1.0, 0.0, 0.0], [0.0, -1.0, height - 1.0], [0.0, 0.0, 1.0]], dtype=np.float64)


# ---------------------------------------------------------------------------
# 底图方向变换
# ---------------------------------------------------------------------------

# 把正北归一化坐标 (a, b) 变换到底图归一化坐标 (a', b') 的 3x3 矩阵。
# 旋转按顺时针计算，镜像按「先 mirror_h 再旋转」组合（已写入实现补充文档）。
_ORIENTATION_MATRICES: dict[ImageOrientation, tuple[tuple[float, float, float], ...]] = {
    ImageOrientation.NORMAL: ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    ImageOrientation.ROT90: ((0.0, -1.0, 1.0), (1.0, 0.0, 0.0)),  # (1-b, a)
    ImageOrientation.ROT180: ((-1.0, 0.0, 1.0), (0.0, -1.0, 1.0)),  # (1-a, 1-b)
    ImageOrientation.ROT270: ((0.0, 1.0, 0.0), (-1.0, 0.0, 1.0)),  # (b, 1-a)
    ImageOrientation.MIRROR_H: ((-1.0, 0.0, 1.0), (0.0, 1.0, 0.0)),  # (1-a, b)
    ImageOrientation.MIRROR_V: ((1.0, 0.0, 0.0), (0.0, -1.0, 1.0)),  # (a, 1-b)
    ImageOrientation.MIRROR_ROT90: ((0.0, -1.0, 1.0), (-1.0, 0.0, 1.0)),  # (1-b, 1-a)
    ImageOrientation.MIRROR_ROT270: ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0)),  # (b, a)
}


def orientation_matrix(orientation: ImageOrientation) -> np.ndarray:
    """返回正北归一化坐标 -> 底图归一化坐标的 3x3 矩阵。"""
    try:
        rows = _ORIENTATION_MATRICES[orientation]
    except KeyError as exc:  # pragma: no cover - 枚举已穷举
        raise ValueError(f"未知底图方向: {orientation!r}") from exc
    return np.array(
        [
            [rows[0][0], rows[0][1], rows[0][2]],
            [rows[1][0], rows[1][1], rows[1][2]],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )


def orientation_canonical_size(
    image_bounds: tuple[float, float, float, float], orientation: ImageOrientation
) -> tuple[float, float]:
    """底图可游玩区域在正北画面中的宽高（像素）。"""
    u_min, v_min, u_max, v_max = image_bounds
    width = u_max - u_min
    height = v_max - v_min
    if orientation.swaps_axes:
        return height, width
    return width, height


@dataclass(frozen=True)
class WorldImageTransform:
    """世界坐标 <-> 底图像素坐标（详细设计 4.5）。

    ``world_to_image_matrix`` 提供时（2x3，按行展开）优先于 bounds + orientation。
    """

    world_bounds: tuple[float, float, float, float]
    image_bounds: tuple[float, float, float, float]
    orientation: ImageOrientation = ImageOrientation.NORMAL
    world_to_image_matrix: Affine2x3 | None = None

    def __post_init__(self) -> None:
        x_min, y_min, x_max, y_max = (float(v) for v in self.world_bounds)
        if not x_max > x_min or not y_max > y_min:
            raise ValueError(f"world_bounds 必须满足 x_max>x_min 且 y_max>y_min: {self.world_bounds!r}")
        u_min, v_min, u_max, v_max = (float(v) for v in self.image_bounds)
        if not u_max > u_min or not v_max > v_min:
            raise ValueError(f"image_bounds 必须满足 u_max>u_min 且 v_max>v_min: {self.image_bounds!r}")
        if self.world_to_image_matrix is not None and len(self.world_to_image_matrix) != 6:
            raise ValueError("world_to_image 需要 6 个数字")

    # --- 矩阵 -------------------------------------------------------
    def canonical_size(self) -> tuple[float, float]:
        """可游玩区域在正北画面中的宽高（像素）。"""
        return orientation_canonical_size(self.image_bounds, self.orientation)

    @property
    def world_to_image_affine(self) -> Affine2x3:
        if self.world_to_image_matrix is not None:
            return tuple(float(v) for v in self.world_to_image_matrix)  # type: ignore[return-value]
        return matrix_to_affine(self._computed_world_to_image())

    def _computed_world_to_image(self) -> np.ndarray:
        x_min, y_min, x_max, y_max = (float(v) for v in self.world_bounds)
        u_min, v_min, u_max, v_max = (float(v) for v in self.image_bounds)
        width = u_max - u_min
        height = v_max - v_min

        # 世界 -> 正北归一化坐标 a（向右）、b（向下）
        to_canonical = np.array(
            [
                [1.0 / (x_max - x_min), 0.0, -x_min / (x_max - x_min)],
                [0.0, -1.0 / (y_max - y_min), y_max / (y_max - y_min)],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        to_image_px = np.array(
            [[width, 0.0, u_min], [0.0, height, v_min], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )
        return compose_affine(to_image_px, orientation_matrix(self.orientation), to_canonical)

    @property
    def image_to_world_affine(self) -> Affine2x3:
        return matrix_to_affine(invert_affine(affine_to_matrix(self.world_to_image_affine)))

    # --- 正反变换 ---------------------------------------------------
    def world_to_image(self, x: float, y: float) -> tuple[float, float]:
        return apply_affine(affine_to_matrix(self.world_to_image_affine), x, y)

    def image_to_world(self, u: float, v: float) -> tuple[float, float]:
        return apply_affine(affine_to_matrix(self.image_to_world_affine), u, v)

    def world_normalized(self, x: float, y: float) -> tuple[float, float]:
        """世界坐标 -> 正北归一化坐标（0～1，b 向下）。"""
        x_min, y_min, x_max, y_max = (float(v) for v in self.world_bounds)
        return (x - x_min) / (x_max - x_min), (y_max - y) / (y_max - y_min)


# ---------------------------------------------------------------------------
# 战斗内标定：世界坐标 -> 小地图 / 屏幕
# ---------------------------------------------------------------------------


class CalibrationMode(StrEnum):
    FULL_MAP = "full_map"
    MATCH = "match"


@dataclass(frozen=True)
class BattleCalibration:
    """一场战斗内的坐标标定（详细设计 4.5 / 5.6 / 7.2）。

    ``minimap_rect`` 为小地图内容区，坐标相对于窗口客户区左上角。
    ``window_rect`` 为窗口客户区在桌面上的屏幕矩形（x/y 为屏幕物理像素）。
    ``image_to_minimap_matrix`` 为匹配模式下的底图像素 -> 小地图像素仿射矩阵。
    """

    world_image: WorldImageTransform
    minimap_rect: Rect
    mode: CalibrationMode
    window_rect: Rect
    image_to_minimap_matrix: Affine2x3 | None = None

    def __post_init__(self) -> None:
        if self.mode is CalibrationMode.MATCH and self.image_to_minimap_matrix is None:
            raise ValueError("匹配模式必须提供 image_to_minimap_matrix")
        if self.minimap_rect.w <= 0 or self.minimap_rect.h <= 0:
            raise ValueError("小地图内容区尺寸必须大于 0")

    # --- 世界 <-> 小地图 --------------------------------------------
    def world_to_minimap_matrix(self) -> np.ndarray:
        if self.mode is CalibrationMode.MATCH:
            return compose_affine(
                affine_to_matrix(self.image_to_minimap_matrix),  # type: ignore[arg-type]
                affine_to_matrix(self.world_image.world_to_image_affine),
            )
        x_min, y_min, x_max, y_max = (float(v) for v in self.world_image.world_bounds)
        mx, my = self.minimap_rect.x, self.minimap_rect.y
        mw, mh = self.minimap_rect.w, self.minimap_rect.h
        return np.array(
            [
                [mw / (x_max - x_min), 0.0, mx - x_min * mw / (x_max - x_min)],
                [0.0, -mh / (y_max - y_min), my + y_max * mh / (y_max - y_min)],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )

    def minimap_to_world_matrix(self) -> np.ndarray:
        return invert_affine(self.world_to_minimap_matrix())

    def world_to_minimap(self, x: float, y: float) -> tuple[float, float]:
        return apply_affine(self.world_to_minimap_matrix(), x, y)

    def minimap_to_world(self, mx: float, my: float) -> tuple[float, float]:
        return apply_affine(self.minimap_to_world_matrix(), mx, my)

    # --- 小地图 / 客户区 -> 屏幕 ------------------------------------
    def window_to_screen(self, wx: float, wy: float) -> tuple[int, int]:
        return int(round(self.window_rect.x + wx)), int(round(self.window_rect.y + wy))

    def minimap_to_screen(self, mx: float, my: float) -> tuple[int, int]:
        return self.window_to_screen(mx, my)

    def world_to_screen(self, x: float, y: float) -> tuple[int, int]:
        mx, my = self.world_to_minimap(x, y)
        return self.minimap_to_screen(mx, my)

    def screen_to_world(self, sx: float, sy: float) -> tuple[float, float]:
        return self.minimap_to_world(sx - self.window_rect.x, sy - self.window_rect.y)


# ---------------------------------------------------------------------------
# 其余换算辅助
# ---------------------------------------------------------------------------


def client_to_screen(window_rect: Rect, wx: float, wy: float) -> tuple[int, int]:
    """窗口客户区坐标 -> 屏幕坐标。"""
    return int(round(window_rect.x + wx)), int(round(window_rect.y + wy))


def normalized_rect_pixels(nrect: NormalizedRect, frame_w: int, frame_h: int) -> Rect:
    return nrect.to_pixels(frame_w, frame_h)


def expected_minimap_scale(
    minimap_rect: Rect, world_image: WorldImageTransform
) -> float:
    """整图模式下小地图相对底图可游玩区域的期望缩放比例。

    用于地图识别接受条件（详细设计 5.5）：小地图内容区宽高与底图可游玩区域
    （正北画面）宽高的比值。返回宽、高比值的几何平均。
    """
    canonical_w, canonical_h = world_image.canonical_size()
    if canonical_w <= 0 or canonical_h <= 0:
        raise ValueError("底图可游玩区域尺寸必须大于 0")
    return math.sqrt((minimap_rect.w / canonical_w) * (minimap_rect.h / canonical_h))


def rotation_is_axis_aligned(rotation_deg: float, tolerance_deg: float = 10.0) -> bool:
    """旋转角是否接近 90 度的整数倍（详细设计 5.5）。"""
    remainder = rotation_deg % 90.0
    return min(remainder, 90.0 - remainder) <= tolerance_deg


def nearest_axis_rotation(rotation_deg: float) -> int:
    """把任意旋转角归到最近的 90 度整数倍（返回 0/90/180/270，允许负值回绕）。"""
    quarter = round(rotation_deg / 90.0) % 4
    return quarter * 90
