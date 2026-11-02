"""自身舰船定位：颜色候选 + 多角度标记模板 + 时序跟踪（详细设计 5.7，评审 P0-6）。

算法分两阶段（评审 P0-6：24 角度 × 全区域多尺度无法在 5~15ms 内完成）：

1. **候选筛选**：配置了 ``marker_hsv_lower/upper`` 时，先在小地图内容区做 HSV 颜色
   阈值 + 连通域分析，按模板标记面积/长宽比筛出 1~8 个候选中心；没有颜色配置时退化
   为 Otsu 亮度分割（自身标记通常是最亮元素），且只在上一帧预测位置附近搜索。
2. **局部模板匹配**：只对候选中心 ±``search_radius_px``（候选中心已较准时更小）的窗口
   做多角度形状模板匹配：旋转后的标记掩码与画面候选掩码计算 Dice 系数。取最高分
   模板的角度作为 ``heading_deg``。（角度模板数量来自 ``profile.minimap.marker_angles``，
   默认 24 个 = 每 15° 一个。）

时序约束（详细设计 5.7）：

- 用最近 3~5 秒的位置样本估计速度，并预测当前位置；候选按「模板得分 - 距预测位置
  的惩罚」排序，优先选择靠近预测位置的候选，避免把友军/敌军相似标记认成自身；
- 连续 3 帧位置跳变超过 ``marker_jump_px`` 时清空跟踪状态重新搜索；
- 连续 ``marker_lost_s`` 秒无法定位时 ``source="lost"``，其间返回 ``predicted``。

与 ``ShipPosition`` 的字段约定
------------------------------
- ``minimap_px``：小地图内容区像素坐标（原点内容区左上）；
- ``world_xy``：本类不做世界坐标换算，固定为 None，由调用方按 ``BattleCalibration``
  换算；
- ``heading_deg``：最佳模板角度（顺时针为正，0 度为模板原始朝向）。模板由标定工具
  从实机截图截取，因此 0 度对应「模板被截取时标记的朝向」；若需要绝对航向，需在
  标定阶段让标记朝向正北截图，或在调用方叠加固定偏移（设计未明确，实机标定确认）；
- ``speed_mps``：默认是「小地图像素/秒」。调用方通过 ``set_meters_per_pixel``
  （或构造参数 ``meters_per_pixel``）给出世界米/小地图像素后，自动换算为米/秒；
- ``source``：``marker`` / ``predicted`` / ``lost``；
- ``confidence``：marker 由模板得分换算，predicted 随时间衰减，lost 约 0.05。

实现假设（设计未明确处）
------------------------
- 假设游戏窗口固定 2560×1600（项目已确定前提），标记模板不做多尺度匹配；
- 颜色候选数量上限、搜索半径、形状匹配阈值等是性能/鲁棒性折中参数，通过构造参数
  暴露，暂不写入全局配置（配置契约中无对应字段）。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ..common.images import read_color_image
from ..common.types import ShipPosition
from ..config.models import ClientProfile, PerceptionConfig

logger = logging.getLogger(__name__)

#: 模板匹配得分阈值（形状 Dice 或灰度 NCC，均在 0~1）。
DEFAULT_MARKER_MATCH_THRESHOLD = 0.75
#: 候选中心/预测位置周围的局部搜索半径（像素）。
DEFAULT_SEARCH_RADIUS_PX = 40.0
#: 每次最多验证的颜色候选数量（候选中心按面积/距预测位置排序后截断）。
DEFAULT_MAX_CANDIDATES = 16
#: 颜色候选连通域面积相对模板标记面积的允许范围。
MIN_CANDIDATE_AREA_RATIO = 0.3
MAX_CANDIDATE_AREA_RATIO = 4.0
#: 颜色候选包围盒长宽比相对模板长宽比的允许范围。
CANDIDATE_ASPECT_TOLERANCE = 3.0
#: 速度估计窗口（秒）与最小样本间隔。
SPEED_WINDOW_S = 5.0
SPEED_MIN_INTERVAL_S = 0.15
#: 外推预测的最长时间（秒），超过后只返回最后已知位置。
PREDICTION_MAX_DT_S = 2.0
#: 位置历史最大样本数。
HISTORY_MAX_SAMPLES = 64
#: 颜色候选已由连通域给出较准的中心，形状匹配只需在其附近小幅搜索（像素）。
COLOR_CANDIDATE_RADIUS_PX = 12.0
#: 形态学开运算核大小（过滤零散颜色噪点）。
MORPH_KERNEL_SIZE = 3


# ---------------------------------------------------------------------------
# 内部数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MarkerSample:
    """一次成功定位的位置样本（小地图内容区像素）。"""

    timestamp: float
    x: float
    y: float


@dataclass(frozen=True)
class _Detection:
    """一次标记检测结果。"""

    score: float
    heading_deg: float
    x: float
    y: float


@dataclass(frozen=True)
class _HeadingTemplate:
    """一个角度版本的标记形状模板（旋转后的二值掩码）。"""

    heading_deg: float
    shape: np.ndarray
    shape_area: float


# ---------------------------------------------------------------------------
# 纯函数工具
# ---------------------------------------------------------------------------


def _to_gray(image: np.ndarray | None) -> np.ndarray | None:
    """BGR/BGRA/灰度 -> uint8 灰度。"""
    if image is None:
        return None
    array = np.asarray(image)
    if array.size == 0:
        return None
    if array.dtype != np.uint8:
        array = cv2.convertScaleAbs(array)
    if array.ndim == 2:
        return np.ascontiguousarray(array)
    if array.ndim != 3:
        return None
    channels = array.shape[2]
    if channels == 1:
        return np.ascontiguousarray(array[:, :, 0])
    if channels == 3:
        return cv2.cvtColor(array, cv2.COLOR_BGR2GRAY)
    if channels == 4:
        return cv2.cvtColor(array, cv2.COLOR_BGRA2GRAY)
    return None


def _to_bgr(image: np.ndarray | None) -> np.ndarray | None:
    """BGR/BGRA/灰度 -> uint8 BGR。"""
    if image is None:
        return None
    array = np.asarray(image)
    if array.size == 0:
        return None
    if array.dtype != np.uint8:
        array = cv2.convertScaleAbs(array)
    if array.ndim == 2:
        return cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
    if array.ndim != 3:
        return None
    if array.shape[2] == 4:
        return cv2.cvtColor(array, cv2.COLOR_BGRA2BGR)
    if array.shape[2] == 1:
        return cv2.cvtColor(array[:, :, 0], cv2.COLOR_GRAY2BGR)
    return np.ascontiguousarray(array)


def _rotate_expanded(
    image: np.ndarray,
    angle_cv_deg: float,
    *,
    interpolation: int = cv2.INTER_LINEAR,
) -> np.ndarray:
    """围绕中心旋转并把内容放进扩大的方形画布（避免裁掉角点）。

    ``angle_cv_deg`` 使用 OpenCV 口径：正值表示画面逆时针旋转。航向角（顺时针为正）
    与它的关系是 ``angle_cv_deg = -heading``。
    """
    height, width = image.shape[:2]
    diagonal = int(math.ceil(math.hypot(height, width))) + 2
    rotation = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), angle_cv_deg, 1.0)
    rotation[0, 2] += diagonal / 2.0 - width / 2.0
    rotation[1, 2] += diagonal / 2.0 - height / 2.0
    return cv2.warpAffine(
        image,
        rotation,
        (diagonal, diagonal),
        flags=interpolation,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )


def _clamp_slice(shape: tuple[int, int], center_x: float, center_y: float, radius: float) -> tuple[int, int, int, int]:
    """返回以 (center_x, center_y) 为中心、边长约 2*radius 的裁剪框。"""
    height, width = shape
    left = max(0, int(math.floor(center_x - radius)))
    top = max(0, int(math.floor(center_y - radius)))
    right = min(width, int(math.ceil(center_x + radius)) + 1)
    bottom = min(height, int(math.ceil(center_y + radius)) + 1)
    if right <= left or bottom <= top:
        return 0, 0, 0, 0
    return left, top, right, bottom


def _shape_dice(
    patch_binary: np.ndarray, template_binary: np.ndarray, template_area: float
) -> tuple[float, float, float]:
    """二值形状在滑动窗口内的 Dice 系数，返回 (最高 Dice, 中心 x, 中心 y)。

    ``patch_binary`` 与 ``template_binary`` 均为 {0,1} float32，窗口内交集用
    ``TM_CCORR`` 计算，窗口内画面掩码面积用全 1 模板的 ``TM_CCORR`` 计算。
    """
    template_height, template_width = template_binary.shape[:2]
    patch_height, patch_width = patch_binary.shape[:2]
    if patch_height < template_height or patch_width < template_width:
        return 0.0, 0.0, 0.0
    inter = cv2.matchTemplate(patch_binary, template_binary, cv2.TM_CCORR)
    ones = np.ones_like(template_binary)
    patch_sum = cv2.matchTemplate(patch_binary, ones, cv2.TM_CCORR)
    denominator = np.maximum(1e-6, template_area + patch_sum)
    dice = 2.0 * inter / denominator
    _, max_value, _, max_location = cv2.minMaxLoc(dice)
    center_x = max_location[0] + template_width / 2.0
    center_y = max_location[1] + template_height / 2.0
    return float(max_value), float(center_x), float(center_y)


# ---------------------------------------------------------------------------
# ShipLocator
# ---------------------------------------------------------------------------


class ShipLocator:
    """自身舰船在小地图上的位置与朝向识别（详细设计 5.7）。

    用法::

        locator = ShipLocator(profile, config.perception)
        position = locator.locate(minimap_image, timestamp)
        locator.reset()

    ``minimap_image`` 是已经按 ``profile.minimap.rect`` 裁好的小地图内容区图像；
    本类不访问窗口、不发送输入、不做世界坐标换算。
    """

    def __init__(
        self,
        profile: ClientProfile,
        cfg: PerceptionConfig,
        *,
        project_root: Path | None = None,
        match_threshold: float = DEFAULT_MARKER_MATCH_THRESHOLD,
        search_radius_px: float = DEFAULT_SEARCH_RADIUS_PX,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        meters_per_pixel: float | None = None,
    ) -> None:
        minimap = profile.minimap
        if minimap is None:
            raise ValueError("ShipLocator 需要 profile.minimap 配置（小地图标定）")
        if minimap.marker_template is None:
            raise ValueError("ShipLocator 需要 profile.minimap.marker_template")
        if minimap.marker_jump_px <= 0:
            raise ValueError("marker_jump_px 必须大于 0")
        if minimap.marker_lost_s < 0:
            raise ValueError("marker_lost_s 不能为负")
        if not 0.0 <= match_threshold <= 1.0:
            raise ValueError("match_threshold 必须在 0~1 之间")
        if search_radius_px <= 0:
            raise ValueError("search_radius_px 必须大于 0")
        if max_candidates < 1:
            raise ValueError("max_candidates 必须大于等于 1")
        self._cfg = cfg
        self._minimap = minimap
        self._threshold = float(match_threshold)
        self._search_radius = float(search_radius_px)
        self._max_candidates = int(max_candidates)
        self._jump_px = float(minimap.marker_jump_px)
        self._lost_s = float(minimap.marker_lost_s)
        self._meters_per_pixel = (
            float(meters_per_pixel) if meters_per_pixel and meters_per_pixel > 0 else None
        )
        self._hsv_lower, self._hsv_upper = self._parse_hsv_range(
            minimap.marker_hsv_lower, minimap.marker_hsv_upper
        )
        template_path = self._resolve_template_path(profile, minimap.marker_template, project_root)
        template_bgr = self._load_template(template_path)
        self._template_mask = self._build_shape_mask(template_bgr, self._hsv_lower, self._hsv_upper)
        self._template_area = float(self._template_mask.sum())
        self._template_aspect = self._mask_aspect(self._template_mask)
        self._fallback_brightness = self._fallback_threshold(template_bgr, self._template_mask)
        angles = int(minimap.marker_angles)
        if angles <= 0:
            angles = 24
        self._heading_step = 360.0 / angles
        self._templates = self._build_heading_templates(self._template_mask, angles)
        # 时序跟踪状态
        self._history: list[_MarkerSample] = []
        self._last_heading: float | None = None
        self._velocity: tuple[float, float] = (0.0, 0.0)
        self._jump_count = 0
        self._missing_since: float | None = None
        self._last_source: str | None = None
        self.last_elapsed_s: float = 0.0
        self.last_score: float = 0.0

    # ------------------------------------------------------------------
    # 构造期辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_hsv_range(
        lower: tuple[int, int, int] | None,
        upper: tuple[int, int, int] | None,
    ) -> tuple[np.ndarray | None, np.ndarray | None]:
        if lower is None and upper is None:
            return None, None
        if lower is None or upper is None:
            raise ValueError("marker_hsv_lower / marker_hsv_upper 必须同时配置")
        if len(lower) != 3 or len(upper) != 3:
            raise ValueError("marker HSV 范围必须是 3 个整数")
        lower_array = np.asarray(lower, dtype=np.int32).reshape(1, 3)
        upper_array = np.asarray(upper, dtype=np.int32).reshape(1, 3)
        if np.any(lower_array < 0) or np.any(upper_array > 255):
            raise ValueError("marker HSV 分量必须在 0~255")
        if np.any(lower_array > upper_array):
            raise ValueError("marker HSV 下界不能大于上界")
        return lower_array, upper_array

    @staticmethod
    def _resolve_template_path(
        profile: ClientProfile, template: str, project_root: Path | None = None
    ) -> Path:
        """解析标记模板路径：绝对路径直用；相对路径先试 profile 目录，再试项目根。"""
        raw = Path(template)
        if raw.is_absolute():
            return raw
        candidates: list[Path] = []
        if profile.profile_dir is not None:
            candidates.append(Path(profile.profile_dir) / raw)
        if project_root is not None:
            candidates.append(Path(project_root) / raw)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return candidates[0] if candidates else raw

    @staticmethod
    def _load_template(path: Path) -> np.ndarray:
        if not path.is_file():
            raise FileNotFoundError(f"自身标记模板不存在: {path}")
        image = read_color_image(path)
        if image is None or image.size == 0:
            raise ValueError(f"自身标记模板无法读取: {path}")
        height, width = image.shape[:2]
        if height < 8 or width < 8:
            raise ValueError(f"自身标记模板尺寸过小（需要 >=8x8）: {width}x{height}")
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if float(gray.std()) < 1.0:
            raise ValueError("自身标记模板是纯色图，无法用于匹配")
        return image

    @staticmethod
    def _build_shape_mask(
        template_bgr: np.ndarray,
        hsv_lower: np.ndarray | None,
        hsv_upper: np.ndarray | None,
    ) -> np.ndarray:
        """提取标记形状掩码：优先 HSV 颜色范围，其次 Otsu 亮度分割（取较小面积侧）。"""
        mask: np.ndarray | None = None
        if hsv_lower is not None and hsv_upper is not None:
            hsv = cv2.cvtColor(template_bgr, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, hsv_lower, hsv_upper)
            if int((mask > 0).sum()) < 16:
                mask = None
        if mask is None:
            gray = cv2.cvtColor(template_bgr, cv2.COLOR_BGR2GRAY)
            _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            bright = otsu > 0
            bright_count = int(bright.sum())
            if bright_count == 0:
                return np.ones(gray.shape, dtype=np.uint8)
            if bright_count * 2 > gray.size:
                otsu = cv2.bitwise_not(otsu)
            mask = otsu
        mask = (mask > 0).astype(np.uint8)
        if int(mask.sum()) < 16:
            return np.ones(mask.shape, dtype=np.uint8)
        return mask

    @staticmethod
    def _fallback_threshold(template_bgr: np.ndarray, template_mask: np.ndarray) -> float:
        """未配置 HSV 时的亮度阈值：取模板标记像素与背景像素亮度中位数的中间值。"""
        gray = cv2.cvtColor(template_bgr, cv2.COLOR_BGR2GRAY)
        marker_values = gray[template_mask > 0]
        background_values = gray[template_mask == 0]
        if marker_values.size == 0:
            return 200.0
        marker_median = float(np.median(marker_values))
        background_median = (
            float(np.median(background_values)) if background_values.size else 0.0
        )
        threshold = background_median + 0.5 * (marker_median - background_median)
        return float(min(254.0, max(1.0, threshold)))

    @staticmethod
    def _mask_aspect(mask: np.ndarray) -> float:
        ys, xs = np.nonzero(mask)
        if xs.size == 0:
            return 1.0
        width = float(xs.max() - xs.min() + 1)
        height = float(ys.max() - ys.min() + 1)
        if height <= 0:
            return 1.0
        return width / height

    @staticmethod
    def _build_heading_templates(
        template_mask: np.ndarray, angles: int
    ) -> list[_HeadingTemplate]:
        mask_u8 = (template_mask > 0).astype(np.uint8) * 255
        templates: list[_HeadingTemplate] = []
        for index in range(angles):
            heading = index * (360.0 / angles)
            angle_cv = -heading  # 航向顺时针为正，OpenCV 旋转逆时针为正
            rotated_mask = _rotate_expanded(mask_u8, angle_cv, interpolation=cv2.INTER_NEAREST)
            shape = (rotated_mask > 127).astype(np.float32)
            templates.append(
                _HeadingTemplate(
                    heading_deg=float(heading),
                    shape=shape,
                    shape_area=float(shape.sum()),
                )
            )
        return templates

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------
    def set_meters_per_pixel(self, meters_per_pixel: float | None) -> None:
        """设置世界米/小地图像素比例，使 ``speed_mps`` 输出真实速度（米/秒）。"""
        if meters_per_pixel is not None and meters_per_pixel > 0:
            self._meters_per_pixel = float(meters_per_pixel)
        else:
            self._meters_per_pixel = None

    def reset(self) -> None:
        """清空时序跟踪状态（切换战斗/标定变化时调用）。"""
        self._history.clear()
        self._last_heading = None
        self._velocity = (0.0, 0.0)
        self._jump_count = 0
        self._missing_since = None
        self._last_source = None
        self.last_elapsed_s = 0.0
        self.last_score = 0.0

    def locate(self, minimap_image: np.ndarray, timestamp: float) -> ShipPosition | None:
        """定位自身舰船标记（详细设计 5.7）。

        参数：
            minimap_image: 小地图内容区图像（BGR/BGRA/灰度）。
            timestamp: 该帧时间戳（单调时钟秒）。

        返回：
            ``ShipPosition``；尚未建立跟踪且当前帧无检测时返回 None。
        """
        started = time.perf_counter()
        self.last_elapsed_s = 0.0
        bgr = _to_bgr(minimap_image)
        gray = _to_gray(minimap_image)
        if bgr is None or gray is None or gray.size == 0 or float(gray.std()) < 1.0:
            self.last_elapsed_s = time.perf_counter() - started
            return self._missing_position(timestamp)
        detection = self._detect(bgr, gray, timestamp)
        self.last_elapsed_s = time.perf_counter() - started
        if detection is None:
            self.last_score = 0.0
            return self._missing_position(timestamp)
        self.last_score = detection.score
        predicted = self._predict(timestamp)
        if predicted is not None:
            jump = math.hypot(detection.x - predicted[0], detection.y - predicted[1])
            if jump > self._jump_px:
                self._jump_count += 1
                if self._jump_count < 3:
                    logger.debug("舰船标记跳变 %.1fpx（%d/3），暂不采信", jump, self._jump_count)
                    return self._missing_position(timestamp)
                logger.warning("舰船标记连续跳变，重置跟踪状态：%.1fpx", jump)
                self._clear_tracking()
        self._jump_count = 0
        self._missing_since = None
        self._last_heading = detection.heading_deg
        self._history.append(
            _MarkerSample(timestamp=float(timestamp), x=detection.x, y=detection.y)
        )
        if len(self._history) > HISTORY_MAX_SAMPLES:
            del self._history[: len(self._history) - HISTORY_MAX_SAMPLES]
        self._velocity = self._estimate_velocity()
        confidence = min(1.0, 0.5 + 0.5 * max(0.0, min(1.0, detection.score)))
        self._last_source = "marker"
        return ShipPosition(
            minimap_px=(float(detection.x), float(detection.y)),
            world_xy=None,
            heading_deg=float(detection.heading_deg),
            speed_mps=self._speed_mps(),
            confidence=float(confidence),
            source="marker",
        )

    # ------------------------------------------------------------------
    # 检测
    # ------------------------------------------------------------------
    def _detect(
        self, image_bgr: np.ndarray, gray: np.ndarray, timestamp: float
    ) -> _Detection | None:
        predicted = self._predict(timestamp)
        if self._hsv_lower is not None and self._hsv_upper is not None:
            mask = self._color_mask_bgr(image_bgr)
        else:
            # 未配置颜色范围时退化为「亮度阈值 + 上一帧附近搜索」（评审 P0-6 降级路径）。
            mask = self._brightness_mask(gray)
        candidates = self._color_candidates(mask, predicted)
        if self._hsv_lower is None and predicted is not None:
            gate = max(self._search_radius * 2.0, 32.0)
            candidates = [
                (center_x, center_y)
                for center_x, center_y in candidates
                if math.hypot(center_x - predicted[0], center_y - predicted[1]) <= gate
            ]
        best: _Detection | None = None
        for center_x, center_y in candidates:
            detection = self._match_shape_near(mask, center_x, center_y)
            if detection is None or detection.score < self._threshold:
                continue
            best = self._pick_better(best, detection, predicted)
        return best

    def _color_mask_bgr(self, image_bgr: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self._hsv_lower, self._hsv_upper)  # type: ignore[arg-type]
        kernel = np.ones((MORPH_KERNEL_SIZE, MORPH_KERNEL_SIZE), dtype=np.uint8)
        return cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    def _brightness_mask(self, gray: np.ndarray) -> np.ndarray:
        """未配置 HSV 时的候选掩码：按模板亮度中值分割（自身标记通常是最亮元素）。"""
        mask = np.where(gray >= self._fallback_brightness, 255, 0).astype(np.uint8)
        kernel = np.ones((MORPH_KERNEL_SIZE, MORPH_KERNEL_SIZE), dtype=np.uint8)
        return cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    def _color_candidates(
        self, mask: np.ndarray, predicted: tuple[float, float] | None
    ) -> list[tuple[float, float]]:
        count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        minimum_area = MIN_CANDIDATE_AREA_RATIO * self._template_area
        maximum_area = MAX_CANDIDATE_AREA_RATIO * self._template_area
        candidates: list[tuple[float, float, float]] = []
        for index in range(1, count):
            area = float(stats[index, cv2.CC_STAT_AREA])
            if not minimum_area <= area <= maximum_area:
                continue
            box_width = float(stats[index, cv2.CC_STAT_WIDTH])
            box_height = float(stats[index, cv2.CC_STAT_HEIGHT])
            if box_height <= 0:
                continue
            aspect = box_width / box_height
            if not (
                self._template_aspect / CANDIDATE_ASPECT_TOLERANCE
                <= aspect
                <= self._template_aspect * CANDIDATE_ASPECT_TOLERANCE
            ):
                continue
            center_x = float(centroids[index][0])
            center_y = float(centroids[index][1])
            if predicted is not None:
                distance = math.hypot(center_x - predicted[0], center_y - predicted[1])
                sort_key = distance
            else:
                sort_key = -area
            candidates.append((sort_key, center_x, center_y))
        candidates.sort(key=lambda item: item[0])
        return [(item[1], item[2]) for item in candidates[: self._max_candidates]]

    def _match_shape_near(
        self, mask: np.ndarray, center_x: float, center_y: float
    ) -> _Detection | None:
        patch_radius = COLOR_CANDIDATE_RADIUS_PX + max(
            self._templates[0].shape.shape[0], self._templates[0].shape.shape[1]
        ) / 2.0
        left, top, right, bottom = _clamp_slice(mask.shape, center_x, center_y, patch_radius)
        if right <= left or bottom <= top:
            return None
        patch = (mask[top:bottom, left:right] > 0).astype(np.float32)
        best: _Detection | None = None
        for template in self._templates:
            score, offset_x, offset_y = _shape_dice(patch, template.shape, template.shape_area)
            if best is None or score > best.score:
                best = _Detection(
                    score=score,
                    heading_deg=template.heading_deg,
                    x=left + offset_x,
                    y=top + offset_y,
                )
        return best

    def _pick_better(
        self,
        current: _Detection | None,
        candidate: _Detection,
        predicted: tuple[float, float] | None,
    ) -> _Detection:
        if current is None:
            return candidate
        return candidate if self._rank(candidate, predicted) > self._rank(current, predicted) else current

    def _rank(self, detection: _Detection, predicted: tuple[float, float] | None) -> float:
        """候选排序分：模板得分减去距预测位置的惩罚（时序约束）。"""
        score = detection.score
        if predicted is None:
            return score
        distance = math.hypot(detection.x - predicted[0], detection.y - predicted[1])
        return score - 0.25 * min(1.0, distance / self._jump_px)

    # ------------------------------------------------------------------
    # 时序跟踪
    # ------------------------------------------------------------------
    def _predict(self, timestamp: float) -> tuple[float, float] | None:
        if not self._history:
            return None
        last = self._history[-1]
        delta = timestamp - last.timestamp
        if delta <= 0:
            return (last.x, last.y)
        delta = min(delta, PREDICTION_MAX_DT_S)
        return (
            last.x + self._velocity[0] * delta,
            last.y + self._velocity[1] * delta,
        )

    def _estimate_velocity(self) -> tuple[float, float]:
        if len(self._history) < 2:
            return (0.0, 0.0)
        latest = self._history[-1].timestamp
        samples = [
            sample
            for sample in self._history
            if latest - sample.timestamp <= SPEED_WINDOW_S
        ]
        velocities: list[tuple[float, float]] = []
        for previous, current in zip(samples, samples[1:], strict=False):
            delta = current.timestamp - previous.timestamp
            if delta < SPEED_MIN_INTERVAL_S:
                continue
            velocities.append(
                ((current.x - previous.x) / delta, (current.y - previous.y) / delta)
            )
        if not velocities:
            return (0.0, 0.0)
        velocity_x = float(np.median([item[0] for item in velocities]))
        velocity_y = float(np.median([item[1] for item in velocities]))
        return (velocity_x, velocity_y)

    def _speed_px_per_s(self) -> float:
        return math.hypot(self._velocity[0], self._velocity[1])

    def _speed_mps(self) -> float:
        speed = self._speed_px_per_s()
        if self._meters_per_pixel is not None:
            return speed * self._meters_per_pixel
        return speed

    def _missing_position(self, timestamp: float) -> ShipPosition | None:
        if not self._history:
            return None
        if self._missing_since is None:
            self._missing_since = timestamp
        elapsed = max(0.0, timestamp - self._missing_since)
        predicted = self._predict(timestamp)
        if predicted is None:  # pragma: no cover - history 非空时不会发生
            predicted = (self._history[-1].x, self._history[-1].y)
        if elapsed < self._lost_s:
            source = "predicted"
            decay = 1.0 - elapsed / self._lost_s if self._lost_s > 0 else 0.0
            confidence = max(0.15, 0.6 * decay)
        else:
            source = "lost"
            confidence = 0.05
        self._last_source = source
        return ShipPosition(
            minimap_px=(float(predicted[0]), float(predicted[1])),
            world_xy=None,
            heading_deg=self._last_heading,
            speed_mps=self._speed_mps(),
            confidence=float(confidence),
            source=source,
        )

    def _clear_tracking(self) -> None:
        self._history.clear()
        self._last_heading = None
        self._velocity = (0.0, 0.0)
        self._jump_count = 0
        self._missing_since = None
