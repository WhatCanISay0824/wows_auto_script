"""自身舰船定位测试（详细设计 5.7；合成小地图，不截屏、不按键）。

覆盖：
- 合成标记在已知位置/朝向时的定位（含非 15° 整数倍朝向的角度容差）；
- 颜色约束（不同颜色诱饵不参与候选）；
- 时序跟踪：速度估计、外推预测、``marker_lost_s`` 后进入 lost；
- 连续 3 帧跳变后重置跟踪；
- reset() 清空状态；
- 未配置 marker_hsv 的退化路径；
- 构造校验与合成图耗时 benchmark。
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from src.common.types import NormalizedRect
from src.config.models import (
    ClientProfile,
    MinimapConfig,
    PerceptionConfig,
    WindowConfig,
)
from src.perception.ship_locate import ShipLocator

from tests.helpers import sample_background

# ---------------------------------------------------------------------------
# 合成数据
# ---------------------------------------------------------------------------

MINIMAP_SIZE = 600
MARKER_SIZE = 26
MARKER_WHITE = (245, 245, 245)
MARKER_HSV_LOWER = (0, 0, 200)
MARKER_HSV_UPPER = (180, 60, 255)
MARKER_JUMP_PX = 60.0
MARKER_LOST_S = 2.0
HEADING_STEP_DEG = 15.0


def _make_marker(size: int = MARKER_SIZE) -> np.ndarray:
    """生成非对称的白色箭头标记模板（深色背景，便于颜色/亮度分割）。"""
    template = np.full((size, size, 3), 40, dtype=np.uint8)
    center = size // 2
    polygon = np.array(
        [[center, 2], [size - 3, size - 3], [center, size - 6], [3, size - 3]],
        dtype=np.int32,
    )
    cv2.fillPoly(template, [polygon], MARKER_WHITE)
    cv2.circle(template, (center, center + 3), 3, (250, 250, 250), -1)
    return template


def _marker_mask(template: np.ndarray) -> np.ndarray:
    """测试用标记形状掩码（等价于配置的白/亮色阈值）。"""
    gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    return np.where(gray >= 150, 255, 0).astype(np.uint8)


def _rotate_expanded(image: np.ndarray, angle_cv_deg: float, interpolation: int) -> np.ndarray:
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


def _render_minimap(
    background: np.ndarray,
    template: np.ndarray,
    position: tuple[float, float],
    heading_deg: float,
    color: tuple[int, int, int] = MARKER_WHITE,
) -> np.ndarray:
    """把标记按航向绘制到合成小地图上（航向顺时针为正）。"""
    shape_mask = (_marker_mask(template) > 0).astype(np.uint8) * 255
    rotated = _rotate_expanded(shape_mask, -heading_deg, cv2.INTER_NEAREST) > 127
    image = background.copy()
    height, width = rotated.shape
    left = int(round(position[0] - width / 2.0))
    top = int(round(position[1] - height / 2.0))
    ys, xs = np.nonzero(rotated)
    px = left + xs
    py = top + ys
    valid = (px >= 0) & (py >= 0) & (px < image.shape[1]) & (py < image.shape[0])
    image[py[valid], px[valid]] = color
    return image


def _dark_minimap_background() -> np.ndarray:
    """深色小地图底图（模拟真实小地图：背景暗、自身标记亮，避免颜色误候选）。"""
    background = sample_background(MINIMAP_SIZE, MINIMAP_SIZE, seed=7)
    return cv2.convertScaleAbs(background, alpha=0.45, beta=0)


def _write_marker(profile_dir: Path, template: np.ndarray) -> Path:
    marker_dir = profile_dir / "marker"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker_path = marker_dir / "marker.png"
    assert cv2.imwrite(str(marker_path), template)
    return marker_path


def _make_profile(
    profile_dir: Path,
    *,
    template: np.ndarray,
    hsv: bool = True,
    marker_template: str = "marker/marker.png",
    angles: int = 24,
    jump_px: float = MARKER_JUMP_PX,
    lost_s: float = MARKER_LOST_S,
) -> ClientProfile:
    _write_marker(profile_dir, template)
    return ClientProfile(
        client_id="test",
        display_name="测试客户端",
        window=WindowConfig(process_names=("WorldOfWarships.exe",)),
        language="zh_cn",
        reference_resolution=(2560, 1600),
        features={},
        minimap=MinimapConfig(
            rect=NormalizedRect(0.86, 0.03, 0.99, 0.24),
            border_px=2,
            marker_template=marker_template,
            marker_angles=angles,
            marker_hsv_lower=MARKER_HSV_LOWER if hsv else None,
            marker_hsv_upper=MARKER_HSV_UPPER if hsv else None,
            marker_jump_px=jump_px,
            marker_lost_s=lost_s,
        ),
        profile_dir=profile_dir,
    )


@pytest.fixture()
def template() -> np.ndarray:
    return _make_marker()


@pytest.fixture()
def background() -> np.ndarray:
    return _dark_minimap_background()


@pytest.fixture()
def locator(tmp_path: Path, template: np.ndarray) -> ShipLocator:
    profile = _make_profile(tmp_path / "profile", template=template)
    return ShipLocator(profile, PerceptionConfig())


# ---------------------------------------------------------------------------
# 单帧定位
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("heading", [0.0, 45.0, 90.0, 135.0, 190.0, 300.0])
def test_locate_marker_position_and_heading(
    locator: ShipLocator, background: np.ndarray, template: np.ndarray, heading: float
) -> None:
    """已知位置/朝向的合成标记应定位成功，角度落在 15° 模板步长内。"""
    position = (250.0, 130.0)
    image = _render_minimap(background, template, position, heading)
    result = locator.locate(image, 0.0)
    assert result is not None
    assert result.source == "marker"
    assert result.world_xy is None
    assert result.minimap_px[0] == pytest.approx(position[0], abs=2.0)
    assert result.minimap_px[1] == pytest.approx(position[1], abs=2.0)
    expected_heading_error = abs((heading - (result.heading_deg or 0.0) + 180.0) % 360.0 - 180.0)
    assert expected_heading_error <= HEADING_STEP_DEG / 2.0 + 1e-6
    assert result.confidence >= 0.85
    assert result.speed_mps == pytest.approx(0.0, abs=1e-6)


def test_locate_non_axis_aligned_heading(
    locator: ShipLocator, background: np.ndarray, template: np.ndarray
) -> None:
    """非 15° 整数倍的朝向吸附到最近的角度模板。"""
    image = _render_minimap(background, template, (200.0, 200.0), 37.0)
    result = locator.locate(image, 0.0)
    assert result is not None
    assert result.heading_deg in (30.0, 45.0)


def test_locate_cold_start_without_marker_returns_none(
    locator: ShipLocator, background: np.ndarray
) -> None:
    """冷启动且画面没有标记时返回 None（不猜测位置）。"""
    assert locator.locate(background, 0.0) is None


def test_locate_empty_image_returns_none(locator: ShipLocator) -> None:
    assert locator.locate(np.zeros((MINIMAP_SIZE, MINIMAP_SIZE, 3), dtype=np.uint8), 0.0) is None
    assert locator.locate(None, 0.0) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 时序跟踪
# ---------------------------------------------------------------------------


def test_locate_moving_speed_estimate(
    locator: ShipLocator, background: np.ndarray, template: np.ndarray
) -> None:
    """匀速移动时由最近位置样本估计速度（像素/秒）。"""
    positions = [(100.0, 100.0), (108.0, 100.0), (116.0, 100.0), (124.0, 100.0), (132.0, 100.0)]
    for index, position in enumerate(positions):
        image = _render_minimap(background, template, position, 90.0)
        result = locator.locate(image, index * 0.5)
        assert result is not None
        assert result.source == "marker"
        assert result.minimap_px[0] == pytest.approx(position[0], abs=2.0)
        assert result.heading_deg == pytest.approx(90.0, abs=1e-6)
    assert result.speed_mps == pytest.approx(16.0, abs=1.5)


def test_locate_meters_per_pixel_converts_speed(
    tmp_path: Path, background: np.ndarray, template: np.ndarray
) -> None:
    """set_meters_per_pixel 后 speed_mps 换算为世界速度（米/秒）。"""
    profile = _make_profile(tmp_path / "profile", template=template)
    locator = ShipLocator(profile, PerceptionConfig(), meters_per_pixel=1.5)
    for index, position in enumerate([(100.0, 100.0), (108.0, 100.0), (116.0, 100.0)]):
        result = locator.locate(_render_minimap(background, template, position, 0.0), index * 0.5)
        assert result is not None
    assert result.speed_mps == pytest.approx(16.0 * 1.5, abs=2.0)
    locator.set_meters_per_pixel(0.5)
    result = locator.locate(_render_minimap(background, template, (124.0, 100.0), 0.0), 1.5)
    assert result is not None
    assert result.speed_mps == pytest.approx(16.0 * 0.5, abs=2.0)


def test_locate_predicted_then_lost(
    locator: ShipLocator, background: np.ndarray, template: np.ndarray
) -> None:
    """丢失标记后先外推（predicted），超过 marker_lost_s 后标记 lost。"""
    for index, position in enumerate([(100.0, 100.0), (108.0, 100.0), (116.0, 100.0)]):
        assert locator.locate(_render_minimap(background, template, position, 0.0), index * 0.5)
    # 最后一帧检测在 t=1.0、位置 116、速度 16 px/s。
    predicted = locator.locate(background, 1.5)
    assert predicted is not None
    assert predicted.source == "predicted"
    assert predicted.minimap_px[0] == pytest.approx(116.0 + 16.0 * 0.5, abs=2.5)
    assert predicted.confidence <= 0.6
    lost = locator.locate(background, 1.0 + MARKER_LOST_S + 0.5)
    assert lost is not None
    assert lost.source == "lost"
    assert lost.confidence <= 0.1
    # 位置不再无限外推：固定在 PREDICTION_MAX_DT_S 内的时间点。
    far = locator.locate(background, 1.0 + MARKER_LOST_S + 5.0)
    assert far is not None
    assert far.source == "lost"


def test_locate_jump_resets_tracking_after_three_frames(
    locator: ShipLocator, background: np.ndarray, template: np.ndarray
) -> None:
    """连续 3 帧位置跳变超过 marker_jump_px 后清空跟踪并接受新位置。"""
    for index in range(3):
        assert locator.locate(_render_minimap(background, template, (100.0, 100.0), 0.0), index * 0.5)
    first = locator.locate(_render_minimap(background, template, (350.0, 350.0), 0.0), 1.5)
    second = locator.locate(_render_minimap(background, template, (350.0, 350.0), 0.0), 2.0)
    third = locator.locate(_render_minimap(background, template, (350.0, 350.0), 0.0), 2.5)
    assert first is not None and first.source == "predicted"
    assert second is not None and second.source == "predicted"
    assert third is not None and third.source == "marker"
    assert third.minimap_px[0] == pytest.approx(350.0, abs=2.0)
    assert third.minimap_px[1] == pytest.approx(350.0, abs=2.0)


def test_locate_reset_clears_tracking(
    locator: ShipLocator, background: np.ndarray, template: np.ndarray
) -> None:
    """reset() 后清空历史，丢失帧不再返回 predicted。"""
    assert locator.locate(_render_minimap(background, template, (100.0, 100.0), 0.0), 0.0)
    locator.reset()
    assert locator.locate(background, 0.5) is None
    result = locator.locate(_render_minimap(background, template, (300.0, 300.0), 0.0), 1.0)
    assert result is not None
    assert result.source == "marker"
    assert result.minimap_px[0] == pytest.approx(300.0, abs=2.0)


def test_locate_invalid_frame_after_tracking_returns_predicted(
    locator: ShipLocator, background: np.ndarray, template: np.ndarray
) -> None:
    """跟踪建立后，坏帧（纯色）按丢失处理返回 predicted，而不是抛异常。"""
    assert locator.locate(_render_minimap(background, template, (100.0, 100.0), 0.0), 0.0)
    result = locator.locate(np.zeros((MINIMAP_SIZE, MINIMAP_SIZE, 3), dtype=np.uint8), 0.5)
    assert result is not None
    assert result.source == "predicted"


# ---------------------------------------------------------------------------
# 颜色约束
# ---------------------------------------------------------------------------


def test_locate_color_constraint_ignores_decoy(
    locator: ShipLocator, background: np.ndarray, template: np.ndarray
) -> None:
    """红色诱饵标记不在自身标记颜色范围内，应被颜色约束过滤。"""
    for index in range(3):
        assert locator.locate(_render_minimap(background, template, (200.0, 200.0), 0.0), index * 0.5)
    image = _render_minimap(background, template, (203.0, 200.0), 0.0, color=(0, 0, 255))
    image = _render_minimap(image, template, (240.0, 200.0), 45.0)
    result = locator.locate(image, 1.5)
    assert result is not None
    assert result.source == "marker"
    assert result.minimap_px[0] == pytest.approx(240.0, abs=2.0)


# ---------------------------------------------------------------------------
# 退化路径：未配置 marker_hsv
# ---------------------------------------------------------------------------


def test_locate_without_hsv_uses_brightness_fallback(
    tmp_path: Path, background: np.ndarray, template: np.ndarray
) -> None:
    """未配置 marker_hsv 时退化为亮度分割 + 局部搜索，仍可定位与跟踪。"""
    profile = _make_profile(tmp_path / "profile", template=template, hsv=False)
    locator = ShipLocator(profile, PerceptionConfig())
    image = _render_minimap(background, template, (250.0, 130.0), 0.0)
    result = locator.locate(image, 0.0)
    assert result is not None
    assert result.source == "marker"
    assert result.minimap_px[0] == pytest.approx(250.0, abs=2.0)
    assert result.minimap_px[1] == pytest.approx(130.0, abs=2.0)


# ---------------------------------------------------------------------------
# 构造校验
# ---------------------------------------------------------------------------


def test_constructor_rejects_missing_minimap(tmp_path: Path) -> None:
    profile = ClientProfile(
        client_id="test",
        display_name="测试",
        window=WindowConfig(),
        language="zh_cn",
        reference_resolution=(2560, 1600),
        features={},
    )
    with pytest.raises(ValueError, match="minimap"):
        ShipLocator(profile, PerceptionConfig())


def test_constructor_rejects_missing_marker_template(tmp_path: Path, template: np.ndarray) -> None:
    profile = _make_profile(tmp_path / "profile", template=template, marker_template="marker/none.png")
    with pytest.raises(FileNotFoundError):
        ShipLocator(profile, PerceptionConfig())


def test_constructor_rejects_pure_color_template(tmp_path: Path) -> None:
    pure = np.full((26, 26, 3), 120, dtype=np.uint8)
    profile = _make_profile(tmp_path / "profile", template=pure)
    with pytest.raises(ValueError, match="纯色"):
        ShipLocator(profile, PerceptionConfig())


def test_constructor_rejects_partial_hsv(tmp_path: Path, template: np.ndarray) -> None:
    profile = _make_profile(tmp_path / "profile", template=template)
    assert profile.minimap is not None
    minimap = MinimapConfig(
        rect=profile.minimap.rect,
        marker_template=profile.minimap.marker_template,
        marker_hsv_lower=(0, 0, 200),
        marker_hsv_upper=None,
    )
    profile = ClientProfile(
        client_id=profile.client_id,
        display_name=profile.display_name,
        window=profile.window,
        language=profile.language,
        reference_resolution=profile.reference_resolution,
        features=profile.features,
        minimap=minimap,
        profile_dir=profile.profile_dir,
    )
    with pytest.raises(ValueError, match="marker_hsv"):
        ShipLocator(profile, PerceptionConfig())


# ---------------------------------------------------------------------------
# 性能 benchmark（合成图）
# ---------------------------------------------------------------------------


def test_locate_timing_benchmark(
    tmp_path: Path, background: np.ndarray, template: np.ndarray
) -> None:
    """合成 600x600 小地图上的定位耗时记录。"""
    profile = _make_profile(tmp_path / "profile", template=template)
    locator = ShipLocator(profile, PerceptionConfig())
    started = time.perf_counter()
    first = locator.locate(_render_minimap(background, template, (100.0, 100.0), 0.0), 0.0)
    cold_elapsed = time.perf_counter() - started
    assert first is not None
    times: list[float] = []
    for index in range(1, 6):
        image = _render_minimap(background, template, (100.0 + index * 4.0, 100.0), 0.0)
        started = time.perf_counter()
        result = locator.locate(image, index * 0.5)
        times.append(time.perf_counter() - started)
        assert result is not None
    warm_medium = sorted(times)[len(times) // 2]
    print(
        f"\n[benchmark] 舰船定位 合成600x600：首帧(含模板构建)={cold_elapsed * 1000:.1f}ms，"
        f"热路径中位数={warm_medium * 1000:.1f}ms"
    )
    assert cold_elapsed < 5.0
    assert warm_medium < 1.0
