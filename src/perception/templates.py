"""界面特征模板匹配（详细设计 5.1 / 5.3 / 5.4）。

匹配流程：把归一化搜索区域换算成窗口内像素区域 → 裁剪灰度图 → 在预缓存的多个
缩放版本上执行 ``matchTemplate(TM_CCOEFF_NORMED)`` → 取最高分与阈值比较，返回
``MatchResult``（found/score/center/rect/scale/timestamp）。

模板加载校验（详细设计 5.3）：文件存在、尺寸不小于 8×8、不是纯色图。纯色模板在
归一化相关匹配中会产生大量误匹配，加载时直接拒绝。校验失败时：

- ``load_profile()`` 默认只记录 ``ValidationIssue`` 并跳过该特征，不中断启动
  （当前仓库模板待实机标定，跳过而不是崩溃）；
- 显式传 ``strict=True`` 时抛 ``ConfigError``，供标定/校验工具使用；
- ``register_feature()`` 是运行时注册（ship_card、loadout_*），失败直接抛
  ``ConfigError``，由调用方决定是否继续。

线程安全：编译后的特征是不可变对象，注册表在锁内整体替换；匹配只读快照。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ..common.errors import ConfigError, Severity, ValidationIssue
from ..common.types import MatchResult, NormalizedRect, Rect
from ..config.models import ClientProfile, FeatureDef, PerceptionConfig
from .capture import Frame, clip_rect

_log = logging.getLogger(__name__)

#: 模板最小尺寸（详细设计 5.3）
MIN_TEMPLATE_SIZE = 8
#: 灰度标准差低于该值视为纯色模板
PURE_COLOR_STD_THRESHOLD = 2.0
#: 默认多尺度范围（详细设计 5.3：0.95 / 1.0 / 1.05）
DEFAULT_SCALES: tuple[float, ...] = (0.95, 1.0, 1.05)
DEFAULT_THRESHOLD = 0.85


# ---------------------------------------------------------------------------
# 编译后的特征
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, eq=False)
class CompiledFeature:
    """已加载并预缩放的界面特征（不可变，可多线程读取）。"""

    feature_id: str
    region: NormalizedRect
    threshold: float
    scales: tuple[float, ...]
    #: (缩放比例, 灰度模板) 元组；模板尺寸 = 原始尺寸 × 缩放比例
    variants: tuple[tuple[float, np.ndarray], ...]
    template_path: Path | None = None
    template_size: tuple[int, int] = (0, 0)

    def variant_for(self, scale: float) -> np.ndarray | None:
        for item_scale, template in self.variants:
            if abs(item_scale - scale) < 1e-9:
                return template
        return None


# ---------------------------------------------------------------------------
# 路径解析与模板编译
# ---------------------------------------------------------------------------


def resolve_template_path(
    template: str | Path,
    *,
    base_dir: Path | None = None,
    project_root: Path | None = None,
) -> Path:
    """解析模板路径：绝对路径直接使用；相对路径依次尝试 profile 目录与项目根。

    运行时注册的特征（船卡、空装备槽）可能位于 ``ships/<ship_id>/`` 等 profile
    目录之外，因此同时接受项目根相对路径；都不存在时返回相对 profile 的路径，
    便于错误信息给出意图位置。
    """
    raw = Path(template)
    if raw.is_absolute():
        return raw
    candidates: list[Path] = []
    if base_dir is not None:
        candidates.append(Path(base_dir) / raw)
    if project_root is not None:
        candidates.append(Path(project_root) / raw)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if candidates:
        return candidates[0]
    return raw


def compile_feature(
    feature_id: str,
    template_ref: str | Path,
    region: NormalizedRect,
    threshold: float = DEFAULT_THRESHOLD,
    scales: Sequence[float] | None = None,
    *,
    base_dir: Path | None = None,
    project_root: Path | None = None,
    default_scales: Sequence[float] | None = None,
) -> tuple[CompiledFeature | None, tuple[ValidationIssue, ...]]:
    """校验并编译一个界面特征；失败返回 ``(None, issues)``，不抛异常。

    调用方（``load_profile`` / ``register_feature``）决定如何处理 issues。
    """
    issues: list[ValidationIssue] = []
    location = f"features.{feature_id}"

    def _error(code: str, message: str) -> None:
        issues.append(ValidationIssue(Severity.ERROR, code, location, message))

    if not feature_id:
        _error("feature_bad_id", "特征 id 不能为空")
        return None, tuple(issues)
    if not 0.0 <= float(threshold) <= 1.0:
        _error("feature_bad_threshold", f"阈值必须在 0～1 之间: {threshold!r}")
        return None, tuple(issues)

    defaults = tuple(default_scales) if default_scales else DEFAULT_SCALES
    raw_scales = tuple(float(s) for s in (scales if scales is not None else defaults))
    scale_values = tuple(dict.fromkeys(raw_scales))
    if not scale_values or any(scale <= 0.0 for scale in scale_values):
        _error("feature_bad_scales", f"缩放比例必须为正数: {raw_scales!r}")
        return None, tuple(issues)

    path = resolve_template_path(template_ref, base_dir=base_dir, project_root=project_root)
    if not path.is_file():
        _error("template_missing", f"模板文件不存在: {path}")
        return None, tuple(issues)
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        _error("template_unreadable", f"模板文件无法读取（不是有效图片）: {path}")
        return None, tuple(issues)
    height, width = image.shape[:2]
    if width < MIN_TEMPLATE_SIZE or height < MIN_TEMPLATE_SIZE:
        _error(
            "template_too_small",
            f"模板尺寸 {width}x{height} 小于最小值 {MIN_TEMPLATE_SIZE}x{MIN_TEMPLATE_SIZE}: {path}",
        )
        return None, tuple(issues)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    std = float(gray.std())
    if std < PURE_COLOR_STD_THRESHOLD:
        _error(
            "template_pure_color",
            f"模板是纯色图（灰度标准差 {std:.2f} < {PURE_COLOR_STD_THRESHOLD}），归一化匹配会大量误匹配: {path}",
        )
        return None, tuple(issues)

    variants: list[tuple[float, np.ndarray]] = []
    for scale in scale_values:
        scaled_w = max(1, int(round(width * scale)))
        scaled_h = max(1, int(round(height * scale)))
        if abs(scale - 1.0) < 1e-9:
            variant = gray
        else:
            interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
            variant = cv2.resize(gray, (scaled_w, scaled_h), interpolation=interpolation)
        variants.append((scale, variant))

    compiled = CompiledFeature(
        feature_id=feature_id,
        region=region,
        threshold=float(threshold),
        scales=scale_values,
        variants=tuple(variants),
        template_path=path,
        template_size=(width, height),
    )
    return compiled, tuple(issues)


def _coerce_region(region: NormalizedRect | Sequence[float] | None) -> NormalizedRect:
    if region is None:
        return NormalizedRect(0.0, 0.0, 1.0, 1.0)
    if isinstance(region, NormalizedRect):
        return region
    return NormalizedRect.from_list(tuple(float(v) for v in region))


def _as_frame(frame: Frame | np.ndarray) -> Frame:
    if isinstance(frame, Frame):
        return frame
    if not isinstance(frame, np.ndarray):
        raise TypeError(f"帧必须是 Frame 或 ndarray: {type(frame)!r}")
    height, width = frame.shape[:2]
    return Frame(image=frame, timestamp=0.0, window_rect=Rect(0, 0, int(width), int(height)))


def _to_gray(image: np.ndarray) -> np.ndarray:
    if not isinstance(image, np.ndarray) or image.size == 0:
        raise ValueError("帧图像为空，无法做模板匹配")
    if image.ndim == 2:
        return image
    if image.ndim == 3 and image.shape[2] == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if image.ndim == 3 and image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    raise ValueError(f"不支持的帧图像形状: {image.shape}")


# ---------------------------------------------------------------------------
# 模板匹配器
# ---------------------------------------------------------------------------


class TemplateMatcher:
    """界面特征模板匹配器（详细设计 5.3 / 5.4）。

    典型用法::

        matcher = TemplateMatcher(cfg, project_root=project_root)
        matcher.load_profile(profile)                      # 非严格，缺模板只记录
        result = matcher.match("port_indicator", frame)    # found/score/center/...
        matcher.register_feature("ship_card", card_png, region)   # 运行时注册

    ``match`` / ``match_many`` 的 ``frame`` 参数可省略，此时使用最近一次传入的帧
    （由 :meth:`set_frame` 或上一次匹配更新），方便引擎与离线测试复用同一帧。
    """

    def __init__(self, cfg: PerceptionConfig | None = None, *, project_root: Path | None = None) -> None:
        self._cfg = cfg or PerceptionConfig()
        self._project_root = Path(project_root) if project_root is not None else None
        self._default_scales: tuple[float, ...] = tuple(self._cfg.template_scales) or DEFAULT_SCALES
        self._lock = threading.RLock()
        self._features: Mapping[str, CompiledFeature] = {}
        self._issues: tuple[ValidationIssue, ...] = ()
        self._last_frame: Frame | None = None

    # --- 加载 / 注册 ---------------------------------------------------
    @property
    def issues(self) -> tuple[ValidationIssue, ...]:
        """最近一次批量加载产生的校验问题。"""
        return self._issues

    @property
    def load_issues(self) -> tuple[ValidationIssue, ...]:
        return self._issues

    def load_profile(self, profile: ClientProfile, *, strict: bool = False) -> tuple[ValidationIssue, ...]:
        """加载 ``profile.features``；默认宽松（记录问题并跳过），strict 抛 ``ConfigError``。"""
        compiled: dict[str, CompiledFeature] = {}
        issues: list[ValidationIssue] = []
        base_dir = profile.profile_dir
        for feature_id, feature in profile.features.items():
            item, item_issues = self._compile_def(feature, feature_id, base_dir=base_dir)
            issues.extend(item_issues)
            if item is not None:
                compiled[feature_id] = item
        with self._lock:
            self._features = compiled
            self._issues = tuple(issues)
        for issue in issues:
            if issue.is_error:
                _log.warning("界面模板加载失败: %s", issue.format())
        if strict and any(issue.is_error for issue in issues):
            raise ConfigError(issues)
        return tuple(issues)

    def _compile_def(
        self,
        feature: FeatureDef,
        feature_id: str,
        *,
        base_dir: Path | None,
    ) -> tuple[CompiledFeature | None, tuple[ValidationIssue, ...]]:
        template_ref = feature.template_path if feature.template_path is not None else feature.template
        return compile_feature(
            feature_id,
            template_ref,
            feature.region,
            feature.threshold,
            feature.scales,
            base_dir=base_dir,
            project_root=self._project_root,
            default_scales=self._default_scales,
        )

    def register_feature(
        self,
        feature_id: str,
        template_path: str | Path,
        region: NormalizedRect | Sequence[float] | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        scales: Sequence[float] | None = None,
        *,
        replace: bool = True,
    ) -> CompiledFeature:
        """运行时注册特征（ship_card、loadout_* 等）。

        ``template_path`` 允许绝对路径或项目根相对路径（例如 ``ships/<id>/card.png``）；
        校验规则与 profile.features 一致，失败抛 ``ConfigError``。
        """
        compiled, issues = compile_feature(
            feature_id,
            template_path,
            _coerce_region(region),
            threshold,
            scales if scales is not None else self._default_scales,
            project_root=self._project_root,
            default_scales=self._default_scales,
        )
        if compiled is None or any(issue.is_error for issue in issues):
            raise ConfigError(list(issues))
        with self._lock:
            if not replace and feature_id in self._features:
                raise ValueError(f"界面特征已存在: {feature_id}")
            features = dict(self._features)
            features[feature_id] = compiled
            self._features = features
            self._issues = self._issues + tuple(issues)
        return compiled

    def unregister_feature(self, feature_id: str) -> bool:
        """移除运行时注册的特征；不存在返回 False。"""
        with self._lock:
            if feature_id not in self._features:
                return False
            features = dict(self._features)
            del features[feature_id]
            self._features = features
            return True

    def clear(self) -> None:
        with self._lock:
            self._features = {}
            self._issues = ()
            self._last_frame = None

    # --- 查询 ---------------------------------------------------------
    def feature_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._features)

    def has_feature(self, feature_id: str) -> bool:
        with self._lock:
            return feature_id in self._features

    def get_feature(self, feature_id: str) -> CompiledFeature | None:
        with self._lock:
            return self._features.get(feature_id)

    def scales_for(self, feature_id: str) -> tuple[float, ...]:
        feature = self.get_feature(feature_id)
        return feature.scales if feature is not None else ()

    @property
    def last_frame(self) -> Frame | None:
        return self._last_frame

    def set_frame(self, frame: Frame | np.ndarray) -> Frame:
        """缓存最近一帧，供后续省略 frame 参数匹配。"""
        resolved = _as_frame(frame)
        self._last_frame = resolved
        return resolved

    def _resolve_frame(self, frame: Frame | np.ndarray | None) -> Frame:
        if frame is None:
            if self._last_frame is None:
                raise ValueError("尚无可用帧：请先调用 set_frame() 或在 match 时传入 frame")
            return self._last_frame
        return self.set_frame(frame)

    # --- 匹配 ---------------------------------------------------------
    def match(self, feature_id: str, frame: Frame | np.ndarray | None = None) -> MatchResult:
        """匹配单个特征；未加载/加载失败的特征返回 found=False、score=0。"""
        resolved = self._resolve_frame(frame)
        feature = self.get_feature(feature_id)
        if feature is None:
            return MatchResult(feature_id=feature_id, found=False, score=0.0, timestamp=resolved.timestamp)
        gray = _to_gray(resolved.image)
        return self._match_compiled(feature, gray, resolved.timestamp)

    def match_many(
        self,
        feature_ids: Sequence[str],
        frame: Frame | np.ndarray | None = None,
    ) -> dict[str, MatchResult]:
        """批量匹配多个特征；灰度图只转换一次，结果按请求顺序返回。"""
        resolved = self._resolve_frame(frame)
        gray = _to_gray(resolved.image)
        results: dict[str, MatchResult] = {}
        for feature_id in feature_ids:
            feature = self.get_feature(feature_id)
            if feature is None:
                results[feature_id] = MatchResult(
                    feature_id=feature_id,
                    found=False,
                    score=0.0,
                    timestamp=resolved.timestamp,
                )
            else:
                results[feature_id] = self._match_compiled(feature, gray, resolved.timestamp)
        return results

    @staticmethod
    def _match_compiled(feature: CompiledFeature, gray: np.ndarray, timestamp: float) -> MatchResult:
        height, width = gray.shape[:2]
        try:
            roi = clip_rect(feature.region.to_pixels(width, height), width, height)
        except ValueError:
            return MatchResult(feature_id=feature.feature_id, found=False, score=0.0, timestamp=timestamp)
        roi_image = gray[roi.y : roi.bottom, roi.x : roi.right]
        best_score = -1.0
        best_scale = 1.0
        best_location: tuple[int, int] | None = None
        best_size = (0, 0)
        for scale, template in feature.variants:
            template_h, template_w = template.shape[:2]
            if template_h > roi_image.shape[0] or template_w > roi_image.shape[1]:
                continue
            scores = cv2.matchTemplate(roi_image, template, cv2.TM_CCOEFF_NORMED)
            _, max_value, _, max_location = cv2.minMaxLoc(scores)
            if not np.isfinite(max_value):
                continue
            if float(max_value) > best_score:
                best_score = float(max_value)
                best_scale = scale
                best_location = (int(max_location[0]), int(max_location[1]))
                best_size = (template_w, template_h)
        if best_location is None:
            return MatchResult(feature_id=feature.feature_id, found=False, score=0.0, timestamp=timestamp)
        found = best_score >= feature.threshold
        if not found:
            return MatchResult(
                feature_id=feature.feature_id,
                found=False,
                score=max(0.0, min(1.0, best_score)),
                scale=best_scale,
                timestamp=timestamp,
            )
        x = roi.x + best_location[0]
        y = roi.y + best_location[1]
        template_w, template_h = best_size
        return MatchResult(
            feature_id=feature.feature_id,
            found=True,
            score=max(0.0, min(1.0, best_score)),
            center=(x + template_w // 2, y + template_h // 2),
            rect=(x, y, template_w, template_h),
            scale=best_scale,
            timestamp=timestamp,
        )
