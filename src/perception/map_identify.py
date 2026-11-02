"""地图识别：ORB 特征匹配 + 局部仿射估计（详细设计 5.5，评审纠偏 P0-2/P1-6）。

职责与口径
----------
本模块只回答两个问题：当前小地图是哪张地图；底图像素与小地图内容区像素之间的
几何关系。它不访问游戏窗口，输入是调用方裁剪好的小地图内容区图像（BGR/gray）。

``MapMatch.affine`` 的语义（与 ``WorldImageTransform.world_to_image`` 可直接合成）：

    原始底图像素（background.png 的像素坐标）-> 小地图内容区像素（原点内容区左上）

即 ``affine`` 是 2x3 按行展开（m00, m01, m02, m10, m11, m12），不含任何裁剪或缩放偏移。

尺度归一（评审 P0-2）
--------------------
底图宽度（约 1760~2000 px）与小地图内容区（约 300 px）相差约 6 倍，超过 ORB 金字塔
的尺度覆盖范围（1.2^7≈3.6 倍），直接匹配会内点不足。因此在提取底图特征前先：

1. 按 ``MapPack.image_bounds`` 裁剪出可游玩区域（offset=(u_min, v_min)）；
2. 把裁剪图缩放到宽度约为小地图内容区宽度的 ``BG_TARGET_OVERSAMPLE`` 倍
   （默认 1.5，落在 1~2 倍的安全区间），缩放系数记为 ``bg_scale``；
3. 在缩放后的裁剪图上提取 ORB（原图 + 水平镜像各一份），缓存到
   ``<pack_dir>/features.npz``，记录 crop、bg_scale、cv2 版本、ORB 参数、底图
   mtime/size，任一变化自动重建；
4. 匹配得到 ``A_est``（缩放裁剪图像素 -> 小地图内容区像素）后换算回原始底图坐标：

       S      = [[bg_scale, 0, -bg_scale * u_min],
                 [0, bg_scale, -bg_scale * v_min], [0, 0, 1]]
       A_full = A_est @ S                                # 未镜像
       A_full = A_est @ mirror_x_matrix(W_scaled) @ S    # 使用镜像特征时

其中 ``A_est`` 由 ``estimateAffinePartial2D(src=底图关键点, dst=小地图关键点)`` 得到
（评审要求先底图后小地图），``W_scaled`` 为缩放后裁剪图宽度。

识别结果字段
------------
- ``mirrored``：是否使用了水平镜像底图特征；
- ``rotation_deg``：从 ``A_est`` 提取的旋转角（顺时针为正，单位度），吸附到最近
  的 90 度整数倍；镜像情况下它是「镜像帧内」的旋转角，不包含镜像本身；
- ``scale``：``A_full`` 的缩放比例（原始底图像素 -> 小地图像素），可用来与
  ``common.coords.expected_minimap_scale`` 比较；
- ``score``：内点数 / 有效匹配数（0~1，越大越好）；
- ``candidates``：通过全部接受条件的地图编号，按内点数降序。

补偿后的几何语义可写成：``p_mini = Rot(rotation_deg) @ Mirror(mirrored) @ (scale * p_bg + t)``，
即小地图上的底图内容先按 ``mirrored`` 水平镜像、再顺时针旋转 ``rotation_deg``。

接受条件（评审 P1-6）
--------------------
内点 >= ``min_inliers``；内点重投影误差中位数 <= ``max_reprojection_error_px``；
内点凸包面积 / 小地图内容区面积 >= ``map_min_inlier_hull_ratio``；内点覆盖至少
3 个象限；旋转角吸附误差 <= ``map_rotation_tolerance_deg``；传入 ``expected_scale``
时缩放相对偏差 <= ``map_scale_tolerance``；warp 校验 NCC >= ``map_verify_ncc``。
多候选内点数领先不足 ``map_candidate_margin`` 时返回 None（避免在相似地图间误判）。

实现假设（设计未明确处）
------------------------
- 识别假设小地图显示整图且北向固定（详细设计 4.5 的运行前提）；不对「小地图随
  镜头旋转」做补偿，旋转角明显不是 90 度整数倍时直接判为不接受。
- ``features.npz`` 首次 ``identify``/``prepare`` 时惰性生成（启动时无法得知窗口
  尺寸，需要小地图宽度反推 ``bg_scale``）；之后命中缓存，除非底图或参数变化。
- 缓存写入失败不影响识别（退化为内存特征），只在日志中告警。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import math
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ..common.coords import (
    ImageOrientation,
    affine_rotation_deg,
    affine_scale,
    matrix_to_affine,
    mirror_x_matrix,
    nearest_axis_rotation,
)
from ..common.images import read_color_image
from ..common.types import MapMatch
from ..config.models import MapPack, PerceptionConfig

logger = logging.getLogger(__name__)

#: 缩放后底图宽度相对小地图内容区宽度的目标倍数（1~2 倍之间，默认 1.5）。
BG_TARGET_OVERSAMPLE = 1.5
#: bg_scale 相对变化在此范围内时复用缓存，避免窗口轻微变化导致重建。
CACHE_SCALE_REUSE_TOL = 0.10
#: features.npz 缓存格式版本（v3：增加底图 sha256 失效条件）。
CACHE_VERSION = 3
#: ORB 描述子固定 32 字节。
_DESCRIPTOR_COLS = 32
#: 小地图内容区最小边长（像素），更小的输入直接视为无效。
MIN_MINIMAP_SIZE = 32
#: 小地图灰度标准差低于该值视为纯色/黑屏，不做识别。
MIN_MINIMAP_STD = 1.0
#: 缩放后底图宽度范围（像素）。
_MIN_SCALED_WIDTH = 96
_MAX_SCALED_WIDTH = 4096
#: 内点需要覆盖的最少象限数（评审 P1-6）。
MIN_INLIER_QUADRANTS = 3


# ---------------------------------------------------------------------------
# 内部数据结构
# ---------------------------------------------------------------------------


@dataclass
class _BackgroundFeatures:
    """一张底图（裁剪+缩放后）的 ORB 特征，原图与水平镜像各一份。"""

    map_id: str
    crop: tuple[float, float, float, float]
    bg_scale: float
    scaled_width: int
    scaled_height: int
    gray: np.ndarray
    points: np.ndarray
    descriptors: np.ndarray
    mirror_points: np.ndarray
    mirror_descriptors: np.ndarray


@dataclass
class _Candidate:
    """一张地图的一次候选识别结果，附带全部指标。"""

    map_id: str
    inliers: int
    good_matches: int
    score: float
    affine: np.ndarray
    reprojection_error: float
    rotation_deg: float
    scale: float
    mirrored: bool
    hull_ratio: float
    quadrants: int
    ncc: float


# ---------------------------------------------------------------------------
# 纯函数工具
# ---------------------------------------------------------------------------


def _to_grayscale(image: np.ndarray | None) -> np.ndarray | None:
    """把 BGR/BGRA/灰度输入转成 uint8 灰度图；无效输入返回 None。"""
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


def _normalize_rotation_deg(rotation_deg: float) -> float:
    """把旋转角规整到 [0, 360)。"""
    return float(rotation_deg % 360.0)


def _snap_rotation(rotation_deg: float) -> tuple[float, float]:
    """把旋转角吸附到最近的 90 度整数倍，返回 (吸附值[0,360), 最短误差角度)。"""
    snapped = float(nearest_axis_rotation(rotation_deg) % 360)
    delta = abs(((_normalize_rotation_deg(rotation_deg) - snapped + 180.0) % 360.0) - 180.0)
    return snapped, float(delta)


def _relative_close(a: float, b: float, tolerance: float) -> bool:
    if b == 0:
        return a == 0
    return abs(a - b) / abs(b) <= tolerance


def _ncc(warped: np.ndarray, target: np.ndarray, valid: np.ndarray) -> float:
    """在有效像素掩码上计算两张灰度图的归一化互相关系数（NCC）。"""
    if int(valid.sum()) < 50:
        return 0.0
    left = warped[valid].astype(np.float64)
    right = target[valid].astype(np.float64)
    left = left - left.mean()
    right = right - right.mean()
    denom = math.sqrt(float((left * left).sum()) * float((right * right).sum()))
    if denom <= 1e-9:
        return 0.0
    return float((left * right).sum() / denom)


def _read_background(path: Path) -> np.ndarray | None:
    if not path.is_file():
        return None
    image = read_color_image(path)
    if image is None or image.size == 0:
        return None
    return image


def _to_homogeneous(affine: np.ndarray) -> np.ndarray:
    matrix = np.eye(3, dtype=np.float64)
    matrix[:2, :] = np.asarray(affine, dtype=np.float64).reshape(2, 3)
    return matrix


def _file_sha256(path: Path, chunk_size: int = 1 << 20) -> str | None:
    """计算文件 sha256（缓存失效条件之一；读取失败返回 None）。"""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(chunk_size):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _inlier_spatial_stats(
    inlier_points: np.ndarray, width: int, height: int
) -> tuple[float, int]:
    """内点空间分布指标：凸包面积占比、覆盖象限数（评审 P1-6）。"""
    points = np.asarray(inlier_points, dtype=np.float32).reshape(-1, 2)
    if len(points) < 3 or width <= 0 or height <= 0:
        return 0.0, 0
    hull = cv2.convexHull(points)
    area = float(cv2.contourArea(hull)) if len(hull) >= 3 else 0.0
    ratio = area / float(width * height)
    center_x, center_y = width / 2.0, height / 2.0
    quadrants = {(px >= center_x, py >= center_y) for px, py in points}
    return ratio, len(quadrants)


# ---------------------------------------------------------------------------
# 识别结果与 map.yaml 方向的换算
# ---------------------------------------------------------------------------

_ORIENTATION_BY_QUARTER_NORMAL = (
    ImageOrientation.NORMAL,
    ImageOrientation.ROT270,
    ImageOrientation.ROT180,
    ImageOrientation.ROT90,
)
_ORIENTATION_BY_QUARTER_MIRRORED = (
    ImageOrientation.MIRROR_H,
    ImageOrientation.MIRROR_ROT90,
    ImageOrientation.MIRROR_V,
    ImageOrientation.MIRROR_ROT270,
)


def detected_orientation(match: MapMatch) -> ImageOrientation:
    """把识别结果换算成 ``ImageOrientation``（详细设计 4.2 / 4.5 / 5.5）。

    ``MapMatch.affine`` 是「底图像素 -> 小地图（正北画面）」的变换；它的逆
    （正北画面 -> 底图像素）才与 ``map.yaml`` 的 ``image_orientation``（底图相对
    世界坐标的方向）同义。推导结果：

    - 未镜像：声明旋转量 = (-rotation_deg) % 360；
    - 镜像：声明旋转量 = rotation_deg，属 mirror_h 系；
    - 0/90/180/270 分别对应 NORMAL/ROT90/ROT180/ROT270 或
      MIRROR_H/MIRROR_ROT90/MIRROR_V/MIRROR_ROT270。

    镜像集合沿用 ``coords._ORIENTATION_MATRICES`` 的「先水平镜像再顺时针旋转」口径。
    """
    quarter = int(round(match.rotation_deg) // 90) % 4
    if match.mirrored:
        return _ORIENTATION_BY_QUARTER_MIRRORED[quarter]
    return _ORIENTATION_BY_QUARTER_NORMAL[quarter]


def affine_translation_px(affine: tuple[float, ...] | np.ndarray) -> tuple[float, float]:
    """取 2x3 仿射矩阵的平移分量（像素）。"""
    values = np.asarray(affine, dtype=np.float64).ravel()
    if values.size != 6:
        raise ValueError(f"仿射矩阵需要 6 个数: {affine!r}")
    return float(values[2]), float(values[5])


def affine_translation_delta_px(
    previous: tuple[float, ...], current: tuple[float, ...]
) -> float:
    """两个 2x3 仿射矩阵平移分量的距离（像素）。"""
    previous_x, previous_y = affine_translation_px(previous)
    current_x, current_y = affine_translation_px(current)
    return math.hypot(current_x - previous_x, current_y - previous_y)


# ---------------------------------------------------------------------------
# MapIdentifier
# ---------------------------------------------------------------------------


class MapIdentifier:
    """地图识别器（详细设计 5.5）。

    用法::

        identifier = MapIdentifier(maps, config.perception)
        identifier.prepare(reference_minimap_width_px)   # 可选，启动时预热
        match = identifier.identify(minimap_image, expected_scale=0.19)

    线程安全：内部用锁保护特征缓存的构建；``identify`` 匹配阶段只读缓存。
    """

    def __init__(
        self,
        maps: Mapping[str, MapPack],
        cfg: PerceptionConfig,
        *,
        target_oversample: float = BG_TARGET_OVERSAMPLE,
        cache_scale_tolerance: float = CACHE_SCALE_REUSE_TOL,
    ) -> None:
        if target_oversample <= 0:
            raise ValueError("target_oversample 必须大于 0")
        self._maps: dict[str, MapPack] = dict(maps)
        self._cfg = cfg
        self._oversample = float(target_oversample)
        self._cache_scale_tol = float(cache_scale_tolerance)
        self._orb_background = cv2.ORB_create(nfeatures=int(cfg.orb_features_background))
        self._orb_minimap = cv2.ORB_create(nfeatures=int(cfg.orb_features_minimap))
        self._matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        self._features: dict[str, _BackgroundFeatures] = {}
        self._failed: set[str] = set()
        self._lock = threading.Lock()
        self._issues: list[str] = []
        self.last_match: MapMatch | None = None
        self.last_elapsed_s: float = 0.0
        self.last_max_inliers: int = 0

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------
    @property
    def issues(self) -> list[str]:
        """特征构建/缓存过程中出现的问题（不抛异常，供启动日志汇总）。"""
        return list(self._issues)

    @property
    def prepared_map_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._features))

    # ------------------------------------------------------------------
    # 特征准备
    # ------------------------------------------------------------------
    def prepare(
        self,
        reference_minimap_width: float | None = None,
        expected_scale: float | None = None,
    ) -> None:
        """预构建/加载底图 ORB 特征缓存（详细设计 5.5 启动阶段提取）。

        参数至少给出一个：``reference_minimap_width`` 为小地图内容区宽度（像素），
        ``expected_scale`` 为期望缩放比例（``expected_minimap_scale``）。
        """
        if not self._maps:
            return
        if reference_minimap_width is None and expected_scale is None:
            raise ValueError("prepare 需要 reference_minimap_width 或 expected_scale")
        with self._lock:
            self._ensure_ready(reference_minimap_width, expected_scale)

    def _ensure_ready(
        self,
        minimap_width: float | None,
        expected_scale: float | None,
    ) -> None:
        for map_id in sorted(self._maps):
            if map_id in self._failed:
                continue
            pack = self._maps[map_id]
            scale = self._required_bg_scale(pack, minimap_width, expected_scale)
            if scale is None:
                continue
            cached = self._features.get(map_id)
            if cached is not None and _relative_close(
                cached.bg_scale, scale, self._cache_scale_tol
            ):
                continue
            self._load_or_build(map_id, pack, scale)

    def _required_bg_scale(
        self,
        pack: MapPack,
        minimap_width: float | None,
        expected_scale: float | None,
    ) -> float | None:
        crop_width = float(pack.image_bounds[2]) - float(pack.image_bounds[0])
        if crop_width <= 0:
            return None
        if expected_scale is not None and expected_scale > 0:
            reference = expected_scale * crop_width
        elif minimap_width is not None and minimap_width > 0:
            reference = float(minimap_width)
        else:
            return None
        target_width = reference * self._oversample
        target_width = max(_MIN_SCALED_WIDTH, min(_MAX_SCALED_WIDTH, target_width))
        return target_width / crop_width

    def _load_or_build(self, map_id: str, pack: MapPack, needed_scale: float) -> None:
        features = self._load_cache(pack, needed_scale)
        if features is None:
            features = self._build_features(pack, needed_scale)
            if features is not None:
                self._save_cache(pack, features)
        if features is None:
            self._failed.add(map_id)
            return
        self._features[map_id] = features

    def _background_path(self, pack: MapPack) -> Path | None:
        if pack.background_path is not None:
            return pack.background_path
        if pack.pack_dir is not None:
            return pack.pack_dir / "background.png"
        return None

    def _cache_path(self, pack: MapPack) -> Path | None:
        if pack.pack_dir is None:
            return None
        return pack.pack_dir / "features.npz"

    def _record_issue(self, message: str) -> None:
        logger.warning("地图识别：%s", message)
        self._issues.append(message)

    @staticmethod
    def _valid_crop(pack: MapPack, image_shape: tuple[int, int]) -> tuple[int, int, int, int] | None:
        u_min, v_min, u_max, v_max = (float(v) for v in pack.image_bounds)
        height, width = image_shape
        if not (u_max > u_min and v_max > v_min):
            return None
        x0, y0 = int(round(u_min)), int(round(v_min))
        x1, y1 = int(round(u_max)), int(round(v_max))
        if x0 < 0 or y0 < 0 or x1 > width or y1 > height or x1 <= x0 or y1 <= y0:
            return None
        return x0, y0, x1, y1

    def _build_features(self, pack: MapPack, bg_scale: float) -> _BackgroundFeatures | None:
        path = self._background_path(pack)
        if path is None:
            self._record_issue(f"{pack.map_id}: 缺少底图路径")
            return None
        background = _read_background(path)
        if background is None:
            self._record_issue(f"{pack.map_id}: 底图不存在或无法读取: {path}")
            return None
        crop = self._valid_crop(pack, background.shape[:2])
        if crop is None:
            self._record_issue(
                f"{pack.map_id}: image_bounds 超出底图范围或非法: {pack.image_bounds!r}"
            )
            return None
        x0, y0, x1, y1 = crop
        cropped = background[y0:y1, x0:x1]
        scaled_width = max(1, int(round((x1 - x0) * bg_scale)))
        scaled_height = max(1, int(round((y1 - y0) * bg_scale)))
        interpolation = cv2.INTER_AREA if bg_scale < 1.0 else cv2.INTER_LINEAR
        try:
            scaled = cv2.resize(
                cropped, (scaled_width, scaled_height), interpolation=interpolation
            )
        except cv2.error as exc:  # pragma: no cover - 尺寸非法时才触发
            self._record_issue(f"{pack.map_id}: 底图缩放失败: {exc}")
            return None
        gray = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
        points, descriptors = self._extract(gray, self._orb_background)
        mirror_points, mirror_descriptors = self._extract(
            cv2.flip(gray, 1), self._orb_background
        )
        if descriptors is None or mirror_descriptors is None:
            self._record_issue(f"{pack.map_id}: 底图 ORB 特征不足")
            return None
        return _BackgroundFeatures(
            map_id=pack.map_id,
            crop=(float(crop[0]), float(crop[1]), float(crop[2]), float(crop[3])),
            bg_scale=float(bg_scale),
            scaled_width=scaled_width,
            scaled_height=scaled_height,
            gray=gray,
            points=points,
            descriptors=descriptors,
            mirror_points=mirror_points,
            mirror_descriptors=mirror_descriptors,
        )

    @staticmethod
    def _extract(gray: np.ndarray, orb: cv2.ORB) -> tuple[np.ndarray, np.ndarray | None]:
        keypoints, descriptors = orb.detectAndCompute(gray, None)
        if descriptors is None or len(keypoints) < 3:
            return np.empty((0, 2), dtype=np.float32), None
        points = np.float32([keypoint.pt for keypoint in keypoints]).reshape(-1, 2)
        descriptors = np.ascontiguousarray(descriptors, dtype=np.uint8)
        return points, descriptors

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------
    def _load_cache(self, pack: MapPack, needed_scale: float) -> _BackgroundFeatures | None:
        path = self._cache_path(pack)
        background_path = self._background_path(pack)
        if path is None or background_path is None or not path.is_file():
            return None
        try:
            stat = background_path.stat()
        except OSError:
            return None
        try:
            with np.load(path, allow_pickle=False) as data:
                if int(data["cache_version"]) != CACHE_VERSION:
                    return None
                if str(data["map_id"].item()) != pack.map_id:
                    return None
                if str(data["cv2_version"].item()) != cv2.__version__:
                    return None
                if int(data["orb_nfeatures"]) != int(self._cfg.orb_features_background):
                    return None
                if int(data["bg_mtime_ns"]) != int(stat.st_mtime_ns):
                    return None
                if int(data["bg_size"]) != int(stat.st_size):
                    return None
                cached_sha256 = str(data["bg_sha256"].item())
                actual_sha256 = _file_sha256(background_path)
                if actual_sha256 is None or actual_sha256 != cached_sha256:
                    return None
                crop = tuple(float(value) for value in data["crop"].ravel())
                expected_crop = tuple(float(value) for value in pack.image_bounds)
                if len(crop) != 4 or not np.allclose(crop, expected_crop, atol=0.5):
                    return None
                cached_scale = float(data["bg_scale"])
                if cached_scale <= 0 or not _relative_close(
                    cached_scale, needed_scale, self._cache_scale_tol
                ):
                    return None
                points = np.asarray(data["points"], dtype=np.float32).reshape(-1, 2)
                descriptors = np.ascontiguousarray(data["descriptors"], dtype=np.uint8)
                mirror_points = np.asarray(
                    data["mirror_points"], dtype=np.float32
                ).reshape(-1, 2)
                mirror_descriptors = np.ascontiguousarray(
                    data["mirror_descriptors"], dtype=np.uint8
                )
                if descriptors.ndim != 2 or descriptors.shape[1] != _DESCRIPTOR_COLS:
                    return None
                if (
                    mirror_descriptors.ndim != 2
                    or mirror_descriptors.shape[1] != _DESCRIPTOR_COLS
                ):
                    return None
                if len(points) != len(descriptors) or len(mirror_points) != len(
                    mirror_descriptors
                ):
                    return None
                intensity = np.asarray(data["scaled_gray"], dtype=np.uint8)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        height, width = intensity.shape[:2]
        if height <= 0 or width <= 0:
            return None
        return _BackgroundFeatures(
            map_id=pack.map_id,
            crop=crop,
            bg_scale=cached_scale,
            scaled_width=width,
            scaled_height=height,
            gray=np.ascontiguousarray(intensity),
            points=points,
            descriptors=descriptors,
            mirror_points=mirror_points,
            mirror_descriptors=mirror_descriptors,
        )

    def _save_cache(self, pack: MapPack, features: _BackgroundFeatures) -> None:
        path = self._cache_path(pack)
        background_path = self._background_path(pack)
        if path is None or background_path is None:
            return
        try:
            stat = background_path.stat()
        except OSError:
            return
        sha256 = _file_sha256(background_path)
        if sha256 is None:
            return
        payload: dict[str, np.ndarray] = {
            "cache_version": np.int64(CACHE_VERSION),
            "map_id": np.array(pack.map_id, dtype="U64"),
            "cv2_version": np.array(cv2.__version__, dtype="U32"),
            "orb_nfeatures": np.int64(self._cfg.orb_features_background),
            "bg_mtime_ns": np.int64(stat.st_mtime_ns),
            "bg_size": np.int64(stat.st_size),
            "bg_sha256": np.array(sha256, dtype="U64"),
            "crop": np.asarray(features.crop, dtype=np.float64),
            "bg_scale": np.float64(features.bg_scale),
            "scaled_gray": np.ascontiguousarray(features.gray),
            "points": np.ascontiguousarray(features.points, dtype=np.float32),
            "descriptors": np.ascontiguousarray(features.descriptors, dtype=np.uint8),
            "mirror_points": np.ascontiguousarray(features.mirror_points, dtype=np.float32),
            "mirror_descriptors": np.ascontiguousarray(
                features.mirror_descriptors, dtype=np.uint8
            ),
        }
        temp_path = path.with_name(path.name + ".tmp")
        try:
            with open(temp_path, "wb") as handle:
                np.savez_compressed(handle, **payload)
            os.replace(temp_path, path)
        except OSError as exc:
            logger.warning("地图识别：写入特征缓存失败 %s: %s", path, exc)
            with contextlib.suppress(OSError):
                temp_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # 识别
    # ------------------------------------------------------------------
    def identify(
        self,
        minimap_image: np.ndarray,
        expected_scale: float | None = None,
        timestamp: float = 0.0,
    ) -> MapMatch | None:
        """识别小地图内容区图像对应的地图与仿射变换（详细设计 5.5）。

        参数：
            minimap_image: 小地图内容区图像（BGR/BGRA/灰度均可）。
            expected_scale: 期望缩放比例（小地图像素/底图可游玩区域像素），由
                ``common.coords.expected_minimap_scale`` 计算；None 时跳过缩放校验。
            timestamp: 该帧时间戳（单调时钟秒），写入结果的 ``timestamp`` 字段。

        返回：
            ``MapMatch``；无有效候选或候选间区分不足时返回 None。
        """
        started = time.perf_counter()
        self.last_elapsed_s = 0.0
        self.last_max_inliers = 0
        gray = _to_grayscale(minimap_image)
        if gray is None:
            return None
        minimap_height, minimap_width = gray.shape[:2]
        if minimap_width < MIN_MINIMAP_SIZE or minimap_height < MIN_MINIMAP_SIZE:
            return None
        if float(gray.std()) < MIN_MINIMAP_STD:
            return None
        if not self._maps:
            return None
        with self._lock:
            self._ensure_ready(float(minimap_width), expected_scale)
            features = [
                self._features[map_id] for map_id in sorted(self._features)
            ]
        if not features:
            return None
        minimap_points, minimap_descriptors = self._extract(gray, self._orb_minimap)
        if minimap_descriptors is None:
            self.last_elapsed_s = time.perf_counter() - started
            return None

        candidates: list[_Candidate] = []
        for item in features:
            candidate = self._evaluate_map(
                item,
                gray,
                minimap_points,
                minimap_descriptors,
                minimap_width,
                minimap_height,
                expected_scale,
            )
            if candidate is not None:
                candidates.append(candidate)
        self.last_elapsed_s = time.perf_counter() - started
        if not candidates:
            return None
        self.last_max_inliers = max(candidate.inliers for candidate in candidates)
        candidates.sort(key=lambda candidate: (candidate.inliers, candidate.score), reverse=True)
        best = candidates[0]
        if len(candidates) >= 2:
            second = candidates[1]
            margin = max(0.0, float(self._cfg.map_candidate_margin))
            if best.inliers < second.inliers * (1.0 + margin):
                logger.warning(
                    "地图识别：候选区分不足 best=%s(%d) second=%s(%d) margin=%.2f",
                    best.map_id,
                    best.inliers,
                    second.map_id,
                    second.inliers,
                    margin,
                )
                return None
        match = MapMatch(
            map_id=best.map_id,
            inliers=int(best.inliers),
            score=float(best.score),
            affine=matrix_to_affine(best.affine),
            reprojection_error=float(best.reprojection_error),
            timestamp=float(timestamp),
            mirrored=bool(best.mirrored),
            rotation_deg=float(best.rotation_deg),
            scale=float(best.scale),
            candidates=tuple(candidate.map_id for candidate in candidates),
        )
        self.last_match = match
        return match

    def _ratio_matches(
        self, query_descriptors: np.ndarray, train_descriptors: np.ndarray
    ) -> list[cv2.DMatch]:
        """KNN(k=2) + Lowe 比例筛选，返回通过筛选的匹配（详细设计 5.5）。"""
        if query_descriptors.size == 0 or train_descriptors.size == 0:
            return []
        try:
            knn = self._matcher.knnMatch(query_descriptors, train_descriptors, k=2)
        except cv2.error:  # pragma: no cover - 描述子形状非法时才触发
            return []
        ratio = float(self._cfg.orb_ratio_test)
        good: list[cv2.DMatch] = []
        for pair in knn:
            if len(pair) == 1 or len(pair) == 2 and pair[0].distance < ratio * pair[1].distance:
                good.append(pair[0])
        return good

    def _evaluate_map(
        self,
        features: _BackgroundFeatures,
        minimap_gray: np.ndarray,
        minimap_points: np.ndarray,
        minimap_descriptors: np.ndarray,
        minimap_width: int,
        minimap_height: int,
        expected_scale: float | None,
    ) -> _Candidate | None:
        original = self._match_side(
            features,
            minimap_gray,
            minimap_points,
            minimap_descriptors,
            minimap_width,
            minimap_height,
            mirrored=False,
            expected_scale=expected_scale,
        )
        mirrored = self._match_side(
            features,
            minimap_gray,
            minimap_points,
            minimap_descriptors,
            minimap_width,
            minimap_height,
            mirrored=True,
            expected_scale=expected_scale,
        )
        if original is None:
            return mirrored
        if mirrored is None:
            return original
        return mirrored if mirrored.inliers > original.inliers else original

    def _match_side(
        self,
        features: _BackgroundFeatures,
        minimap_gray: np.ndarray,
        minimap_points: np.ndarray,
        minimap_descriptors: np.ndarray,
        minimap_width: int,
        minimap_height: int,
        *,
        mirrored: bool,
        expected_scale: float | None,
    ) -> _Candidate | None:
        background_points = features.mirror_points if mirrored else features.points
        background_descriptors = (
            features.mirror_descriptors if mirrored else features.descriptors
        )
        good = self._ratio_matches(minimap_descriptors, background_descriptors)
        if len(good) < max(6, int(self._cfg.min_inliers) // 2):
            return None
        src = np.float32(
            [background_points[match.trainIdx] for match in good]
        ).reshape(-1, 1, 2)
        dst = np.float32([minimap_points[match.queryIdx] for match in good]).reshape(-1, 1, 2)
        try:
            affine_estimate, mask = cv2.estimateAffinePartial2D(
                src,
                dst,
                method=cv2.RANSAC,
                ransacReprojThreshold=float(self._cfg.ransac_reproj_px),
                maxIters=2000,
                confidence=0.995,
                refineIters=10,
            )
        except cv2.error:  # pragma: no cover - 退化输入才会触发
            return None
        if affine_estimate is None or mask is None:
            return None
        inlier_mask = mask.ravel().astype(bool)
        inliers = int(inlier_mask.sum())
        if inliers < int(self._cfg.min_inliers):
            return None
        affine_estimate = np.asarray(affine_estimate, dtype=np.float64).reshape(2, 3)
        projected = cv2.transform(src, affine_estimate)
        errors = np.linalg.norm(projected - dst, axis=2).ravel()
        inlier_errors = errors[inlier_mask]
        if inlier_errors.size == 0:
            return None
        median_error = float(np.median(inlier_errors))
        if median_error > float(self._cfg.max_reprojection_error_px):
            return None

        rotation_raw = _normalize_rotation_deg(affine_rotation_deg(affine_estimate))
        rotation_snapped, rotation_error = _snap_rotation(rotation_raw)
        if rotation_error > float(self._cfg.map_rotation_tolerance_deg):
            return None

        affine_full = self._affine_to_background(features, affine_estimate, mirrored)
        if not np.isfinite(affine_full).all():
            return None
        scale_full = affine_scale(affine_estimate) * features.bg_scale
        if scale_full <= 0 or not math.isfinite(scale_full):
            return None
        if (
            expected_scale is not None
            and expected_scale > 0
            and abs(scale_full - expected_scale) / expected_scale
            > float(self._cfg.map_scale_tolerance)
        ):
            return None

        inlier_dst = dst[inlier_mask].reshape(-1, 2)
        hull_ratio, quadrants = _inlier_spatial_stats(
            inlier_dst, minimap_width, minimap_height
        )
        if hull_ratio < float(self._cfg.map_min_inlier_hull_ratio):
            return None
        if quadrants < MIN_INLIER_QUADRANTS:
            return None
        ncc = self._verify_ncc(
            features, minimap_gray, affine_estimate, mirrored, minimap_width, minimap_height
        )
        if ncc < float(self._cfg.map_verify_ncc):
            return None
        return _Candidate(
            map_id=features.map_id,
            inliers=inliers,
            good_matches=len(good),
            score=float(inliers) / float(max(1, len(good))),
            affine=affine_full,
            reprojection_error=median_error,
            rotation_deg=rotation_snapped,
            scale=float(scale_full),
            mirrored=bool(mirrored),
            hull_ratio=float(hull_ratio),
            quadrants=int(quadrants),
            ncc=float(ncc),
        )

    @staticmethod
    def _affine_to_background(
        features: _BackgroundFeatures, affine_estimate: np.ndarray, mirrored: bool
    ) -> np.ndarray:
        """把「缩放裁剪图 -> 小地图」的仿射换算成「原始底图 -> 小地图」。"""
        crop_x0, crop_y0 = features.crop[0], features.crop[1]
        scale = features.bg_scale
        to_background = np.array(
            [
                [scale, 0.0, -scale * crop_x0],
                [0.0, scale, -scale * crop_y0],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        affine = _to_homogeneous(affine_estimate)
        if mirrored:
            affine = affine @ mirror_x_matrix(float(features.scaled_width))
        return affine @ to_background

    @staticmethod
    def _verify_ncc(
        features: _BackgroundFeatures,
        minimap_gray: np.ndarray,
        affine_estimate: np.ndarray,
        mirrored: bool,
        minimap_width: int,
        minimap_height: int,
    ) -> float:
        """把缩放底图按估计仿射 warp 到小地图尺寸，与真实小地图做 NCC 二次验证。"""
        source = cv2.flip(features.gray, 1) if mirrored else features.gray
        warped = cv2.warpAffine(
            source,
            affine_estimate,
            (minimap_width, minimap_height),
            flags=cv2.INTER_LINEAR,
            borderValue=0,
        )
        ones = np.ones_like(source, dtype=np.uint8)
        valid = (
            cv2.warpAffine(
                ones,
                affine_estimate,
                (minimap_width, minimap_height),
                flags=cv2.INTER_NEAREST,
                borderValue=0,
            )
            > 0
        )
        return _ncc(warped, minimap_gray, valid)

    # ------------------------------------------------------------------
    # 刷新判定
    # ------------------------------------------------------------------
    def should_refresh(self, previous: MapMatch | None, current: MapMatch | None) -> bool:
        """判断是否用新识别结果刷新当前战斗标定（实现补充「刷新滞回」口径）。

        新结果内点相对提升 >= ``map_refresh_inlier_gain`` 且平移变化
        <= ``map_refresh_translation_px`` 时才替换（几何基本一致、内点更多 =
        更可信）；地图变化或任一为 None 时返回 True（必须重新识别/刷新）。
        平移变化过大说明小地图窗口或标定环境变了，交由调用方重新识别而非静默替换。
        """
        if previous is None or current is None:
            return True
        if previous.map_id != current.map_id:
            return True
        reference = max(1, previous.inliers)
        gain = (current.inliers - previous.inliers) / float(reference)
        if gain < float(self._cfg.map_refresh_inlier_gain):
            return False
        translation = affine_translation_delta_px(previous.affine, current.affine)
        return translation <= float(self._cfg.map_refresh_translation_px)
