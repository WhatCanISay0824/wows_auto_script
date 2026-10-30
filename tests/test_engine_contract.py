"""感知几何三件套与 Lead 感知引擎的接口契约测试。

本文件不测试引擎实现（由 Lead 完成），只验证：
- ``MapIdentifier`` / ``ShipLocator`` / ``DeathDetector`` 的构造与调用签名符合
  ``docs/plans/2026-10-30-m0-m1-implementation.md`` 中 T4/Engine 的契约；
- 三个类可以被轻量 fake 替换（依赖注入友好）；
- ``MapMatch.affine`` 能直接构造 ``BattleCalibration(image_to_minimap_matrix=...)``
  并完成世界坐标 -> 小地图坐标换算（engine 的实际消费方式）；
- 输出类型可以放进 ``Observation`` 并序列化为 JSON。
"""

from __future__ import annotations

import inspect
import json
import math
from pathlib import Path

import cv2
import numpy as np
import pytest
from src.common.coords import (
    BattleCalibration,
    CalibrationMode,
    Rect,
    affine_to_matrix,
    apply_affine,
    expected_minimap_scale,
)
from src.common.types import DeathSignals, MapMatch, NormalizedRect, Observation, ShipPosition
from src.config.models import (
    ClientProfile,
    DeathConfig,
    MapPack,
    MinimapConfig,
    PerceptionConfig,
    WindowConfig,
)
from src.perception.death import DeathDetector
from src.perception.map_identify import MapIdentifier
from src.perception.ship_locate import ShipLocator

from tests.helpers import sample_background, warp_image

MINIMAP_SIZE = 400
WORLD_BOUNDS = (-6000.0, -6000.0, 6000.0, 6000.0)
MARKER_HSV_LOWER = (0, 0, 200)
MARKER_HSV_UPPER = (180, 60, 255)


def _make_marker(size: int = 26) -> np.ndarray:
    template = np.full((size, size, 3), 40, dtype=np.uint8)
    center = size // 2
    polygon = np.array(
        [[center, 2], [size - 3, size - 3], [center, size - 6], [3, size - 3]],
        dtype=np.int32,
    )
    cv2.fillPoly(template, [polygon], (245, 245, 245))
    cv2.circle(template, (center, center + 3), 3, (250, 250, 250), -1)
    return template


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


def _render_marker(
    background: np.ndarray,
    template: np.ndarray,
    position: tuple[float, float],
    heading_deg: float,
) -> np.ndarray:
    mask = np.where(cv2.cvtColor(template, cv2.COLOR_BGR2GRAY) >= 150, 255, 0).astype(np.uint8)
    rotated = _rotate_expanded(mask, -heading_deg, cv2.INTER_NEAREST) > 127
    image = background.copy()
    height, width = rotated.shape
    left = int(round(position[0] - width / 2.0))
    top = int(round(position[1] - height / 2.0))
    ys, xs = np.nonzero(rotated)
    px = left + xs
    py = top + ys
    valid = (px >= 0) & (py >= 0) & (px < image.shape[1]) & (py < image.shape[0])
    image[py[valid], px[valid]] = (245, 245, 245)
    return image


def _build_scene(tmp_path: Path) -> tuple[MapPack, ClientProfile, np.ndarray, np.ndarray]:
    """构造一张地图包 + 客户端配置 + 合成小地图（含自身标记）。"""
    map_dir = tmp_path / "maps" / "map_demo"
    map_dir.mkdir(parents=True)
    background = cv2.convertScaleAbs(sample_background(1000, 1000, seed=3), alpha=0.45, beta=0)
    background_path = map_dir / "background.png"
    assert cv2.imwrite(str(background_path), background)
    pack = MapPack(
        map_id="map_demo",
        name="演示地图",
        size_meters=(12000.0, 12000.0),
        world_bounds=WORLD_BOUNDS,
        image_bounds=(0.0, 0.0, 1000.0, 1000.0),
        pack_dir=map_dir,
        background_path=background_path,
    )
    profile_dir = tmp_path / "profiles" / "test"
    marker_dir = profile_dir / "marker"
    marker_dir.mkdir(parents=True)
    marker = _make_marker()
    assert cv2.imwrite(str(marker_dir / "marker.png"), marker)
    profile = ClientProfile(
        client_id="test",
        display_name="测试客户端",
        window=WindowConfig(),
        language="zh_cn",
        reference_resolution=(2560, 1600),
        features={},
        minimap=MinimapConfig(
            rect=NormalizedRect(0.86, 0.03, 0.99, 0.24),
            marker_template="marker/marker.png",
            marker_angles=24,
            marker_hsv_lower=MARKER_HSV_LOWER,
            marker_hsv_upper=MARKER_HSV_UPPER,
            marker_jump_px=300.0,
            marker_lost_s=3.0,
        ),
        profile_dir=profile_dir,
    )
    warped, matrix = warp_image(
        background, scale=0.35, angle_deg=0.0, tx=0.0, ty=0.0, output_size=(1000, 1000)
    )
    center = 500
    half = MINIMAP_SIZE // 2
    minimap = np.ascontiguousarray(warped[center - half : center + half, center - half : center + half])
    expected = np.eye(3)
    expected[:2] = matrix
    expected[0, 2] -= center - half
    expected[1, 2] -= center - half
    return pack, profile, minimap, expected


# ---------------------------------------------------------------------------
# 1. 签名契约
# ---------------------------------------------------------------------------


def test_map_identifier_signature() -> None:
    parameters = list(inspect.signature(MapIdentifier.__init__).parameters)
    assert parameters[1:3] == ["maps", "cfg"]
    identify_parameters = list(inspect.signature(MapIdentifier.identify).parameters)
    assert identify_parameters[1:] == ["minimap_image", "expected_scale", "timestamp"]
    assert list(inspect.signature(MapIdentifier.prepare).parameters)[1:] == [
        "reference_minimap_width",
        "expected_scale",
    ]
    assert list(inspect.signature(MapIdentifier.should_refresh).parameters)[1:] == [
        "previous",
        "current",
    ]


def test_ship_locator_signature() -> None:
    parameters = list(inspect.signature(ShipLocator.__init__).parameters)
    assert parameters[1:3] == ["profile", "cfg"]
    assert "project_root" in parameters  # Lead 的 engine 以 project_root= 构造（模板路径解析）
    assert list(inspect.signature(ShipLocator.locate).parameters)[1:] == [
        "minimap_image",
        "timestamp",
    ]
    assert list(inspect.signature(ShipLocator.reset).parameters)[1:] == []


def test_ship_locator_resolves_marker_from_project_root(tmp_path: Path) -> None:
    """profile_dir 缺失时用 project_root 解析 marker_template（engine 的构造方式）。"""
    project_root = tmp_path / "project"
    marker_dir = project_root / "marker"
    marker_dir.mkdir(parents=True)
    assert cv2.imwrite(str(marker_dir / "marker.png"), _make_marker())
    profile = ClientProfile(
        client_id="test",
        display_name="测试",
        window=WindowConfig(),
        language="zh_cn",
        reference_resolution=(2560, 1600),
        features={},
        minimap=MinimapConfig(
            rect=NormalizedRect(0.86, 0.03, 0.99, 0.24),
            marker_template="marker/marker.png",
            marker_hsv_lower=MARKER_HSV_LOWER,
            marker_hsv_upper=MARKER_HSV_UPPER,
        ),
    )
    locator = ShipLocator(profile, PerceptionConfig(), project_root=project_root)
    assert locator is not None


def test_death_detector_signature() -> None:
    parameters = list(inspect.signature(DeathDetector.__init__).parameters)
    assert parameters[1:3] == ["cfg", "battle_started_at"]
    assert list(inspect.signature(DeathDetector.update).parameters)[1:] == ["votes", "timestamp"]
    assert list(inspect.signature(DeathDetector.reset).parameters)[1:] == []
    assert isinstance(DeathDetector.decided, property)


# ---------------------------------------------------------------------------
# 2. 引擎消费路径（真实实现）
# ---------------------------------------------------------------------------


def test_engine_like_observe_with_real_implementations(tmp_path: Path) -> None:
    """按 engine.observe 的消费方式驱动三个真实实现，产出 Observation。"""
    pack, profile, minimap, expected_affine = _build_scene(tmp_path)
    config = PerceptionConfig()
    identifier = MapIdentifier({pack.map_id: pack}, config)
    locator = ShipLocator(profile, config)
    detector = DeathDetector(config.death, battle_started_at=0.0)

    marked = _render_marker(minimap, _make_marker(), (200.0, 200.0), 45.0)
    timestamp = 10.0
    observation = _engine_like_observe(
        identifier, locator, detector, marked, timestamp, expected_scale=0.4
    )

    assert observation.map_match is not None
    assert observation.map_match.map_id == "map_demo"
    assert observation.map_match.timestamp == timestamp
    np.testing.assert_allclose(
        affine_to_matrix(observation.map_match.affine)[:2], expected_affine[:2], atol=2.0
    )
    assert observation.ship is not None
    assert observation.ship.source == "marker"
    assert observation.ship.minimap_px[0] == pytest.approx(200.0, abs=2.0)
    assert observation.death is not None
    assert observation.death.votes
    assert detector.decided is False
    payload = json.dumps(
        {
            "map": observation.map_match.to_dict(),
            "ship": observation.ship.to_dict(),
            "death": observation.death.to_dict(),
        },
        ensure_ascii=False,
    )
    assert "map_demo" in payload


def test_affine_composes_with_battle_calibration(tmp_path: Path) -> None:
    """MapMatch.affine 作为 BattleCalibration.image_to_minimap_matrix 时世界坐标换算正确。"""
    pack, profile, minimap, expected_affine = _build_scene(tmp_path)
    config = PerceptionConfig()
    identifier = MapIdentifier({pack.map_id: pack}, config)
    marked = _render_marker(minimap, _make_marker(), (200.0, 200.0), 0.0)
    expected_scale = expected_minimap_scale(
        Rect(0, 0, MINIMAP_SIZE, MINIMAP_SIZE), pack.transform()
    )
    match = identifier.identify(marked, expected_scale=expected_scale, timestamp=1.0)
    assert match is not None
    calibration = BattleCalibration(
        world_image=pack.transform(),
        minimap_rect=Rect(0, 0, MINIMAP_SIZE, MINIMAP_SIZE),
        mode=CalibrationMode.MATCH,
        window_rect=Rect(0, 0, 2560, 1600),
        image_to_minimap_matrix=match.affine,
    )
    # 世界坐标 -> 底图像素 -> 小地图像素；世界中心应映射到小地图中心附近。
    camera_x, camera_y = calibration.world_to_minimap(0.0, 0.0)
    expected_x, expected_y = apply_affine(expected_affine, *_image_center())
    assert camera_x == pytest.approx(expected_x, abs=2.0)
    assert camera_y == pytest.approx(expected_y, abs=2.0)
    roundtrip_x, roundtrip_y = calibration.minimap_to_world(camera_x, camera_y)
    assert roundtrip_x == pytest.approx(0.0, abs=50.0)
    assert roundtrip_y == pytest.approx(0.0, abs=50.0)
    # 小地图坐标换算成屏幕坐标后可交给 InputExecutor（engine 的点击路径）。
    screen_x, screen_y = calibration.minimap_to_screen(camera_x, camera_y)
    assert 0 <= screen_x < 2560
    assert 0 <= screen_y < 1600


def _image_center() -> tuple[float, float]:
    return 500.0, 500.0


def _engine_like_observe(
    identifier: object,
    locator: object,
    detector: object,
    minimap_image: np.ndarray,
    timestamp: float,
    *,
    expected_scale: float | None = None,
) -> Observation:
    """模拟 Lead 的 PerceptionEngine.observe 调用顺序（便于同时喂真实/fake 实现）。"""
    map_match = identifier.identify(  # type: ignore[attr-defined]
        minimap_image, expected_scale=expected_scale, timestamp=timestamp
    )
    ship = locator.locate(minimap_image, timestamp)  # type: ignore[attr-defined]
    votes = {
        "hp_bar_absent": False,
        "hp_zero": False,
        "banner": False,
        "marker_lost": bool(ship is not None and ship.source == "lost"),
        "spectator_ui": False,
    }
    death = detector.update(votes, timestamp)  # type: ignore[attr-defined]
    return Observation(
        timestamp=timestamp,
        window_rect=(0, 0, 2560, 1600),
        map_match=map_match,
        ship=ship,
        death=death,
        frame_id=1,
        frame_timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# 3. fake 替换契约
# ---------------------------------------------------------------------------


class _FakeMapIdentifier:
    def __init__(self) -> None:
        self.calls = 0

    def identify(
        self,
        minimap_image: np.ndarray,
        expected_scale: float | None = None,
        timestamp: float = 0.0,
    ) -> MapMatch:
        self.calls += 1
        return MapMatch(
            map_id="fake_map",
            inliers=30,
            score=0.8,
            affine=(0.4, 0.0, 0.0, 0.0, 0.4, 0.0),
            reprojection_error=0.5,
            timestamp=timestamp,
        )


class _FakeShipLocator:
    def locate(self, minimap_image: np.ndarray, timestamp: float) -> ShipPosition:
        return ShipPosition(
            minimap_px=(10.0, 10.0),
            world_xy=None,
            heading_deg=90.0,
            speed_mps=5.0,
            confidence=0.8,
            source="marker",
        )


class _FakeDeathDetector:
    def update(self, votes: object, timestamp: float) -> DeathSignals:
        return DeathSignals(
            hp_bar_absent=False,
            hp_zero=False,
            banner=False,
            marker_lost=False,
            spectator_ui=False,
            score=0.0,
            stable_ms=0,
            timestamp=timestamp,
        )


def test_engine_like_observe_with_fakes() -> None:
    """三个依赖都可以被 fake 替换，Observation 字段与类型保持一致。"""
    minimap = np.zeros((MINIMAP_SIZE, MINIMAP_SIZE, 3), dtype=np.uint8)
    observation = _engine_like_observe(
        _FakeMapIdentifier(), _FakeShipLocator(), _FakeDeathDetector(), minimap, 3.5
    )
    assert observation.map_match is not None
    assert observation.map_match.map_id == "fake_map"
    assert observation.ship is not None
    assert observation.ship.source == "marker"
    assert observation.death is not None
    assert observation.death.score == 0.0
    assert observation.frame_id == 1
    assert observation.frame_timestamp == 3.5


# ---------------------------------------------------------------------------
# 4. 死亡判定与状态机消费方式（death.dead 兼容 / detector.decided 权威）
# ---------------------------------------------------------------------------


def test_death_decision_is_visible_via_signals_dead() -> None:
    """判定成立后 DeathSignals.dead 为 True（state_machine.machine 的消费方式）。"""
    detector = DeathDetector(DeathConfig(), battle_started_at=0.0)
    votes = {"hp_bar_absent": True}
    signals = detector.update(votes, 5.0)
    assert signals.dead is False
    detector.update(votes, 6.0)
    signals = detector.update(votes, 6.5)
    assert detector.decided is True
    # 兼容便捷属性：score 下限 0.75 + strong_stable_s=1.5s（>=1000ms）。
    assert signals.score == 0.75
    assert signals.dead is True
    assert detector.decision_raw_score == 0.35
