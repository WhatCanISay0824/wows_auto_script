"""地图识别测试（详细设计 5.5；全部使用合成图，不截屏、不按键）。

覆盖：
- 已知相似变换（缩放/旋转/平移）合成小地图的识别与仿射精度；
- 底图 image_bounds 裁剪偏移、bg_scale 归一后的 affine 语义（原始底图像素 -> 小地图）；
- 镜像底图识别与仿射还原、方向换算；
- 旋转/缩放/负样本等接受条件，多候选区分不足；
- features.npz 缓存的生成、复用、失效重建与损坏恢复；
- 合成图上的耗时 benchmark。
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from src.common.coords import (
    Rect,
    affine_to_matrix,
    apply_affine,
    expected_minimap_scale,
)
from src.common.types import MapMatch
from src.config.models import MapPack, PerceptionConfig
from src.perception.map_identify import (
    BG_TARGET_OVERSAMPLE,
    CACHE_VERSION,
    MapIdentifier,
    affine_translation_delta_px,
    detected_orientation,
)

from tests.helpers import sample_background, warp_image

# ---------------------------------------------------------------------------
# 合成场景参数
# ---------------------------------------------------------------------------

BG_WIDTH = 1400
BG_HEIGHT = 1400
CANVAS = 1400
#: 底图可游玩区域（故意留出偏移，覆盖 crop offset 的换算）。
IMAGE_BOUNDS = (100.0, 80.0, 1300.0, 1280.0)
CROP_WIDTH = IMAGE_BOUNDS[2] - IMAGE_BOUNDS[0]
#: 小地图内容区边长（像素）。
MINIMAP_SIZE = 320
#: 合成小地图的真实缩放（底图可游玩区域 -> 小地图）。
WARP_SCALE = 0.25
WORLD_BOUNDS = (-6000.0, -6000.0, 6000.0, 6000.0)


def _write_map(
    root: Path,
    map_id: str,
    background: np.ndarray,
    *,
    image_bounds: tuple[float, ...] = IMAGE_BOUNDS,
) -> MapPack:
    """把合成底图写成地图包目录（缓存文件也写在 tmp_path 下）。"""
    pack_dir = root / map_id
    pack_dir.mkdir(parents=True, exist_ok=True)
    background_path = pack_dir / "background.png"
    assert cv2.imwrite(str(background_path), background)
    return MapPack(
        map_id=map_id,
        name=map_id,
        size_meters=(12000.0, 12000.0),
        world_bounds=WORLD_BOUNDS,
        image_bounds=tuple(float(value) for value in image_bounds),
        pack_dir=pack_dir,
        background_path=background_path,
    )


def _synth_minimap(
    background: np.ndarray,
    *,
    scale: float = WARP_SCALE,
    angle_deg: float = 0.0,
    tx: float = 0.0,
    ty: float = 0.0,
    size: int = MINIMAP_SIZE,
) -> tuple[np.ndarray, np.ndarray]:
    """用 helpers.warp_image 生成已知相似变换的小地图图像。

    返回 (小地图图像, 期望的原始底图像素 -> 小地图像素 3x3 矩阵)。
    """
    warped, matrix = warp_image(
        background,
        scale=scale,
        angle_deg=angle_deg,
        tx=tx,
        ty=ty,
        output_size=(CANVAS, CANVAS),
    )
    center_x = CANVAS / 2.0 + tx
    center_y = CANVAS / 2.0 + ty
    x0 = int(round(center_x - size / 2.0))
    y0 = int(round(center_y - size / 2.0))
    minimap = np.ascontiguousarray(warped[y0 : y0 + size, x0 : x0 + size])
    translation = np.array([[1.0, 0.0, -x0], [0.0, 1.0, -y0], [0.0, 0.0, 1.0]])
    expected = np.eye(3)
    expected[:2] = matrix
    return minimap, translation @ expected


def _flip_minimap(minimap: np.ndarray, matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """把正常小地图水平翻转，返回 (翻转图, 期望仿射 = 镜像矩阵 @ 原仿射)。"""
    flip = np.array(
        [
            [-1.0, 0.0, minimap.shape[1] - 1.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    return cv2.flip(minimap, 1), flip @ matrix


def _assert_affine_close(
    actual: tuple[float, ...], expected: np.ndarray, atol: float = 1.5
) -> None:
    matrix = affine_to_matrix(actual)
    np.testing.assert_allclose(matrix[:2], expected[:2], atol=atol, rtol=0.0)


def _crop_center_expected(matrix: np.ndarray) -> tuple[float, float]:
    """底图可游玩区域中心（像素）经期望仿射变换后的坐标。"""
    center_u = (IMAGE_BOUNDS[0] + IMAGE_BOUNDS[2]) / 2.0
    center_v = (IMAGE_BOUNDS[1] + IMAGE_BOUNDS[3]) / 2.0
    return apply_affine(matrix, center_u, center_v)


# ---------------------------------------------------------------------------
# 模块级场景：一张底图 + 识别器
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scene(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    root = tmp_path_factory.mktemp("map_scene")
    background = sample_background(BG_WIDTH, BG_HEIGHT, seed=7)
    pack = _write_map(root, "map_alpha", background)
    config = PerceptionConfig()
    identifier = MapIdentifier({"map_alpha": pack}, config)
    return SimpleNamespace(
        background=background,
        pack=pack,
        config=config,
        identifier=identifier,
        temp_root=root,
    )


# ---------------------------------------------------------------------------
# 正常识别与仿射精度
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("angle_deg", "expected_rotation", "tx", "ty"),
    [
        (0.0, 0.0, 0.0, 0.0),
        (-90.0, 90.0, 37.0, -21.0),
        (-180.0, 180.0, -44.0, 15.0),
        (90.0, 270.0, 12.0, 33.0),
    ],
)
def test_identify_synthetic_similarity_transform(
    scene: SimpleNamespace,
    angle_deg: float,
    expected_rotation: float,
    tx: float,
    ty: float,
) -> None:
    """已知相似变换的合成小地图应识别成功，仿射与旋转角符合期望。"""
    minimap, expected_affine = _synth_minimap(
        scene.background, angle_deg=angle_deg, tx=tx, ty=ty
    )
    match = scene.identifier.identify(
        minimap, expected_scale=WARP_SCALE, timestamp=12.5
    )
    assert match is not None
    assert match.map_id == "map_alpha"
    assert match.timestamp == 12.5
    assert match.mirrored is False
    assert match.inliers >= scene.config.min_inliers
    assert match.reprojection_error <= scene.config.max_reprojection_error_px
    assert match.rotation_deg == pytest.approx(expected_rotation, abs=1e-6)
    assert match.scale == pytest.approx(WARP_SCALE, abs=0.01)
    assert match.candidates == ("map_alpha",)
    assert 0.0 < match.score <= 1.0
    _assert_affine_close(match.affine, expected_affine)
    # affine 语义：底图可游玩区域中心经识别仿射后的坐标应与期望仿射一致。
    center_u = (IMAGE_BOUNDS[0] + IMAGE_BOUNDS[2]) / 2.0
    center_v = (IMAGE_BOUNDS[1] + IMAGE_BOUNDS[3]) / 2.0
    expected_center = _crop_center_expected(expected_affine)
    mapped_center = apply_affine(affine_to_matrix(match.affine), center_u, center_v)
    assert mapped_center[0] == pytest.approx(expected_center[0], abs=1.5)
    assert mapped_center[1] == pytest.approx(expected_center[1], abs=1.5)


def test_identify_accepts_expected_scale_from_coords(scene: SimpleNamespace) -> None:
    """传入 common.coords.expected_minimap_scale 的期望值时应通过缩放校验。"""
    minimap, _ = _synth_minimap(scene.background, tx=8.0, ty=-6.0)
    expected = expected_minimap_scale(
        Rect(0, 0, MINIMAP_SIZE, MINIMAP_SIZE), scene.pack.transform()
    )
    assert expected == pytest.approx(MINIMAP_SIZE / CROP_WIDTH, rel=1e-6)
    match = scene.identifier.identify(minimap, expected_scale=expected)
    assert match is not None
    assert match.scale == pytest.approx(WARP_SCALE, abs=0.01)


def test_identify_rejects_scale_out_of_tolerance(scene: SimpleNamespace) -> None:
    """期望缩放偏离实际超过 50% 时，候选应被拒绝并返回 None。"""
    minimap, _ = _synth_minimap(scene.background)
    match = scene.identifier.identify(minimap, expected_scale=0.15)
    assert match is None


def test_identify_rejects_non_axis_rotation(scene: SimpleNamespace) -> None:
    """旋转角不是 90 度整数倍（±10°）时应拒绝（小地图可能随镜头旋转）。"""
    minimap, _ = _synth_minimap(scene.background, angle_deg=-30.0)
    assert scene.identifier.identify(minimap, expected_scale=WARP_SCALE) is None


def test_identify_realistic_six_times_scale_ratio(tmp_path: Path) -> None:
    """贴近实机的尺度差（底图可游玩区 1800px -> 小地图约 300px，约 6 倍）应识别成功。

    这是评审 P0-2 的核心场景：若不做底图裁剪+bg_scale 归一抹平，ORB 金字塔无法覆盖。
    """
    background = sample_background(2000, 2000, seed=17)
    image_bounds = (100.0, 80.0, 1900.0, 1880.0)  # 可游玩区域 1800x1800
    pack = _write_map(tmp_path / "real_scale", "map_real", background, image_bounds=image_bounds)
    warped, matrix = warp_image(
        background, scale=0.16, angle_deg=0.0, tx=0.0, ty=0.0, output_size=(2000, 2000)
    )
    center = 1000
    half = 150
    minimap = np.ascontiguousarray(warped[center - half : center + half, center - half : center + half])
    expected = np.eye(3)
    expected[:2] = matrix
    expected[0, 2] -= center - half
    expected[1, 2] -= center - half
    assert minimap.shape[:2] == (300, 300)
    identifier = MapIdentifier({"map_real": pack}, PerceptionConfig())
    expected_scale = expected_minimap_scale(
        Rect(0, 0, 300, 300), pack.transform()
    )
    assert expected_scale == pytest.approx(300.0 / 1800.0, rel=1e-6)
    match = identifier.identify(minimap, expected_scale=expected_scale)
    assert match is not None
    assert match.map_id == "map_real"
    assert match.inliers >= PerceptionConfig().min_inliers
    assert match.scale == pytest.approx(0.16, abs=0.01)
    _assert_affine_close(match.affine, expected, atol=2.0)


# ---------------------------------------------------------------------------
# 镜像
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("angle_deg", "expected_rotation", "orientation_name"),
    [
        (0.0, 0.0, "MIRROR_H"),
        (-90.0, 270.0, "MIRROR_ROT270"),
    ],
)
def test_identify_mirrored_minimap(
    scene: SimpleNamespace,
    angle_deg: float,
    expected_rotation: float,
    orientation_name: str,
) -> None:
    """镜像小地图应使用镜像特征匹配，mirrored=True 且仿射还原到原始底图坐标。"""
    minimap, normal_affine = _synth_minimap(scene.background, angle_deg=angle_deg)
    mirrored, expected_affine = _flip_minimap(minimap, normal_affine)
    match = scene.identifier.identify(mirrored, expected_scale=WARP_SCALE)
    assert match is not None
    assert match.mirrored is True
    assert match.rotation_deg == pytest.approx(expected_rotation, abs=1e-6)
    assert match.inliers >= scene.config.min_inliers
    _assert_affine_close(match.affine, expected_affine, atol=2.0)
    assert detected_orientation(match).name == orientation_name


def test_detected_orientation_mapping() -> None:
    """MapMatch 的 (mirrored, rotation) 与 map.yaml image_orientation 的换算。"""

    def make(rotation: float, mirrored: bool) -> MapMatch:
        return MapMatch(
            map_id="m",
            inliers=20,
            score=0.8,
            affine=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0),
            reprojection_error=0.5,
            mirrored=mirrored,
            rotation_deg=rotation,
        )

    assert detected_orientation(make(0.0, False)).name == "NORMAL"
    assert detected_orientation(make(90.0, False)).name == "ROT270"
    assert detected_orientation(make(180.0, False)).name == "ROT180"
    assert detected_orientation(make(270.0, False)).name == "ROT90"
    assert detected_orientation(make(0.0, True)).name == "MIRROR_H"
    assert detected_orientation(make(90.0, True)).name == "MIRROR_ROT90"
    assert detected_orientation(make(180.0, True)).name == "MIRROR_V"
    assert detected_orientation(make(270.0, True)).name == "MIRROR_ROT270"


# ---------------------------------------------------------------------------
# 负样本与多候选
# ---------------------------------------------------------------------------


def test_identify_rejects_invalid_images(scene: SimpleNamespace) -> None:
    """纯色/过小/空输入应返回 None，不抛异常。"""
    blank = np.full((MINIMAP_SIZE, MINIMAP_SIZE, 3), 30, dtype=np.uint8)
    assert scene.identifier.identify(blank) is None
    tiny = np.zeros((16, 16, 3), dtype=np.uint8)
    assert scene.identifier.identify(tiny) is None
    assert scene.identifier.identify(None) is None  # type: ignore[arg-type]


def test_identify_rejects_unrelated_map(
    scene: SimpleNamespace, tmp_path: Path
) -> None:
    """另一张不同纹理底图的小地图不应被识别为本场景地图。"""
    other_root = tmp_path / "other"
    other_background = sample_background(BG_WIDTH, BG_HEIGHT, seed=99)
    _write_map(other_root, "map_beta", other_background)
    minimap, _ = _synth_minimap(other_background)
    assert scene.identifier.identify(minimap, expected_scale=WARP_SCALE) is None


def test_identify_requires_candidate_margin(tmp_path: Path) -> None:
    """两张完全相同/高度相似的地图同时命中时，区分不足应返回 None。"""
    background = sample_background(BG_WIDTH, BG_HEIGHT, seed=11)
    pack_a = _write_map(tmp_path, "map_a", background)
    pack_b = _write_map(tmp_path, "map_b", background)
    identifier = MapIdentifier({"map_a": pack_a, "map_b": pack_b}, PerceptionConfig())
    minimap, _ = _synth_minimap(background)
    assert identifier.identify(minimap, expected_scale=WARP_SCALE) is None


def test_identify_multiple_distinct_maps_picks_correct(
    scene: SimpleNamespace, tmp_path: Path
) -> None:
    """配置多张不同地图时，应选择内点明显领先的正确地图。"""
    second_background = sample_background(BG_WIDTH, BG_HEIGHT, seed=23)
    pack_b = _write_map(tmp_path, "map_beta", second_background)
    identifier = MapIdentifier(
        {"map_alpha": scene.pack, "map_beta": pack_b}, PerceptionConfig()
    )
    minimap, expected_affine = _synth_minimap(scene.background)
    match = identifier.identify(minimap, expected_scale=WARP_SCALE)
    assert match is not None
    assert match.map_id == "map_alpha"
    assert "map_alpha" in match.candidates
    _assert_affine_close(match.affine, expected_affine)


# ---------------------------------------------------------------------------
# features.npz 缓存
# ---------------------------------------------------------------------------


def _read_cache(cache_path: Path) -> dict[str, np.ndarray]:
    with np.load(cache_path, allow_pickle=False) as data:
        return {key: np.array(data[key]) for key in data.files}


@pytest.fixture()
def cache_pack(tmp_path: Path) -> MapPack:
    background = sample_background(BG_WIDTH, BG_HEIGHT, seed=31)
    return _write_map(tmp_path / "cache_case", "map_cache", background)


def test_feature_cache_created_with_metadata(cache_pack: MapPack) -> None:
    """首次识别应在 map pack 目录生成 features.npz，并记录 crop/bg_scale 等字段。"""
    identifier = MapIdentifier({"map_cache": cache_pack}, PerceptionConfig())
    background = cv2.imread(str(cache_pack.background_path), cv2.IMREAD_COLOR)
    assert background is not None
    minimap, _ = _synth_minimap(background)
    assert identifier.identify(minimap, expected_scale=WARP_SCALE) is not None
    cache_path = cache_pack.pack_dir / "features.npz"  # type: ignore[operator]
    assert cache_path.is_file()
    cache = _read_cache(cache_path)
    assert int(cache["cache_version"]) == CACHE_VERSION
    np.testing.assert_allclose(cache["crop"], np.asarray(IMAGE_BOUNDS), atol=1e-6)
    # identify 传入了 expected_scale，bg_scale = expected_scale * oversample（评审 P0-2）。
    assert float(cache["bg_scale"]) == pytest.approx(
        BG_TARGET_OVERSAMPLE * WARP_SCALE, rel=1e-6
    )
    stat = cache_pack.background_path.stat()  # type: ignore[union-attr]
    assert int(cache["bg_mtime_ns"]) == int(stat.st_mtime_ns)
    assert int(cache["bg_size"]) == int(stat.st_size)
    # 底图 sha256 作为失效条件之一（实现补充 P2：ORB 缓存记录 sha256）。
    expected_sha256 = hashlib.sha256(cache_pack.background_path.read_bytes()).hexdigest()  # type: ignore[union-attr]
    assert str(cache["bg_sha256"].item()) == expected_sha256
    assert int(cache["orb_nfeatures"]) == PerceptionConfig().orb_features_background
    assert str(cache["cv2_version"].item()) == cv2.__version__
    points = cache["points"]
    descriptors = cache["descriptors"]
    mirror_descriptors = cache["mirror_descriptors"]
    assert points.ndim == 2 and points.shape[1] == 2
    assert descriptors.shape == (points.shape[0], 32)
    assert mirror_descriptors.shape == (points.shape[0], 32)
    assert cache["scaled_gray"].shape == (
        int(round(CROP_WIDTH * float(cache["bg_scale"]))),
        int(round(CROP_WIDTH * float(cache["bg_scale"]))),
    )


def test_feature_cache_reused_and_invalidated(cache_pack: MapPack) -> None:
    """缓存命中时不重写 features.npz；底图 mtime 变化后自动重建。"""
    background = cv2.imread(str(cache_pack.background_path), cv2.IMREAD_COLOR)
    assert background is not None
    minimap, _ = _synth_minimap(background)
    first = MapIdentifier({"map_cache": cache_pack}, PerceptionConfig())
    assert first.identify(minimap) is not None
    cache_path = cache_pack.pack_dir / "features.npz"  # type: ignore[operator]
    first_mtime_ns = cache_path.stat().st_mtime_ns

    second = MapIdentifier({"map_cache": cache_pack}, PerceptionConfig())
    assert second.identify(minimap) is not None
    assert cache_path.stat().st_mtime_ns == first_mtime_ns

    background_path = cache_pack.background_path
    assert background_path is not None
    stat = background_path.stat()
    os.utime(
        background_path,
        ns=(stat.st_atime_ns, stat.st_mtime_ns + 10 * 10**9),
    )
    third = MapIdentifier({"map_cache": cache_pack}, PerceptionConfig())
    assert third.identify(minimap) is not None
    rebuilt = _read_cache(cache_path)
    new_stat = background_path.stat()
    assert int(rebuilt["bg_mtime_ns"]) == int(new_stat.st_mtime_ns)


def test_feature_cache_corruption_rebuilds(cache_pack: MapPack) -> None:
    """features.npz 损坏时应重建而不是崩溃。"""
    background = cv2.imread(str(cache_pack.background_path), cv2.IMREAD_COLOR)
    assert background is not None
    minimap, _ = _synth_minimap(background)
    cache_path = cache_pack.pack_dir / "features.npz"  # type: ignore[operator]
    cache_path.write_bytes(b"not-a-real-npz")
    identifier = MapIdentifier({"map_cache": cache_pack}, PerceptionConfig())
    match = identifier.identify(minimap, expected_scale=WARP_SCALE)
    assert match is not None
    assert cache_path.is_file()
    assert int(_read_cache(cache_path)["cache_version"]) == CACHE_VERSION


def test_feature_cache_invalidated_by_orb_params(cache_pack: MapPack) -> None:
    """ORB 特征数量等参数变化后缓存应失效重建。"""
    background = cv2.imread(str(cache_pack.background_path), cv2.IMREAD_COLOR)
    assert background is not None
    minimap, _ = _synth_minimap(background)
    first = MapIdentifier({"map_cache": cache_pack}, PerceptionConfig())
    assert first.identify(minimap) is not None
    cache_path = cache_pack.pack_dir / "features.npz"  # type: ignore[operator]
    assert int(_read_cache(cache_path)["orb_nfeatures"]) == 2000
    config = PerceptionConfig(orb_features_background=800)
    second = MapIdentifier({"map_cache": cache_pack}, config)
    assert second.identify(minimap) is not None
    assert int(_read_cache(cache_path)["orb_nfeatures"]) == 800


def test_missing_background_records_issue(tmp_path: Path) -> None:
    """底图缺失时不崩溃，返回 None 并记录问题（配置校验会先拦截）。"""
    pack_dir = tmp_path / "broken_map"
    pack_dir.mkdir(parents=True)
    pack = MapPack(
        map_id="broken_map",
        name="broken",
        size_meters=(12000.0, 12000.0),
        world_bounds=WORLD_BOUNDS,
        image_bounds=IMAGE_BOUNDS,
        pack_dir=pack_dir,
        background_path=pack_dir / "missing.png",
    )
    identifier = MapIdentifier({"broken_map": pack}, PerceptionConfig())
    background = sample_background(BG_WIDTH, BG_HEIGHT, seed=5)
    minimap, _ = _synth_minimap(background)
    assert identifier.identify(minimap) is None
    assert identifier.prepared_map_ids == ()
    assert identifier.issues
    assert "broken_map" in identifier.issues[0]


def test_prepare_prebuilds_feature_cache(cache_pack: MapPack) -> None:
    """prepare(reference_minimap_width) 应提前生成缓存。"""
    identifier = MapIdentifier({"map_cache": cache_pack}, PerceptionConfig())
    identifier.prepare(reference_minimap_width=MINIMAP_SIZE)
    assert identifier.prepared_map_ids == ("map_cache",)
    cache_path = cache_pack.pack_dir / "features.npz"  # type: ignore[operator]
    assert cache_path.is_file()


# ---------------------------------------------------------------------------
# 刷新判定与其他工具函数
# ---------------------------------------------------------------------------


def _match(map_id: str, inliers: int, translation: tuple[float, float]) -> MapMatch:
    return MapMatch(
        map_id=map_id,
        inliers=inliers,
        score=0.8,
        affine=(0.25, 0.0, translation[0], 0.0, 0.25, translation[1]),
        reprojection_error=0.5,
    )


def test_should_refresh_rules() -> None:
    """刷新滞回：内点相对提升 >= 阈值且平移变化 <= 阈值时才替换（实现补充口径）。"""
    identifier = MapIdentifier({}, PerceptionConfig())
    previous = _match("m", 100, (10.0, 20.0))
    assert identifier.should_refresh(previous, None) is True
    assert identifier.should_refresh(None, previous) is True
    assert identifier.should_refresh(previous, _match("other", 300, (0.0, 0.0))) is True
    # 内点提升不足：不刷新。
    assert identifier.should_refresh(previous, _match("m", 105, (11.0, 20.0))) is False
    # 内点提升 30% 且平移差 <= 5px：刷新。
    assert identifier.should_refresh(previous, _match("m", 130, (10.0, 20.0))) is True
    # 内点提升明显但平移差 6px（> map_refresh_translation_px=5）：不静默替换。
    assert identifier.should_refresh(previous, _match("m", 130, (10.0, 26.0))) is False
    assert affine_translation_delta_px(previous.affine, _match("m", 100, (13.0, 24.0)).affine) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# 性能 benchmark（合成图）
# ---------------------------------------------------------------------------


def test_identify_timing_benchmark(scene: SimpleNamespace) -> None:
    """合成 320x320 小地图上的耗时记录（冷启动含缓存构建，热路径仅匹配）。"""
    background = sample_background(BG_WIDTH, BG_HEIGHT, seed=41)
    root = scene.temp_root / "bench"
    pack = _write_map(root, "map_bench", background)
    minimap, _ = _synth_minimap(background)
    cold_identifier = MapIdentifier({"map_bench": pack}, PerceptionConfig())
    cold_started = time.perf_counter()
    assert cold_identifier.identify(minimap, expected_scale=WARP_SCALE) is not None
    cold_elapsed = time.perf_counter() - cold_started
    warm_times: list[float] = []
    for _ in range(3):
        started = time.perf_counter()
        assert cold_identifier.identify(minimap, expected_scale=WARP_SCALE) is not None
        warm_times.append(time.perf_counter() - started)
    warm_medium = sorted(warm_times)[len(warm_times) // 2]
    print(
        f"\n[benchmark] 地图识别 合成320x320：冷启动(含ORB构建)={cold_elapsed * 1000:.1f}ms，"
        f"热路径中位数={warm_medium * 1000:.1f}ms"
    )
    assert cold_elapsed < 5.0
    assert warm_medium < 1.0
