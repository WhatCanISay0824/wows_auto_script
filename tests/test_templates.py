"""界面模板匹配测试。

模板与场景全部来自 ``tests.helpers.sample_background`` 生成的合成图：从纹理图上裁一块
作为模板，再把它（或它的缩放版本）贴回场景的已知位置，验证命中位置、缩放比例、
搜索区域与阈值判定。纯色/过小/缺失模板必须被拒绝。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
from src.common.errors import ConfigError
from src.common.types import NormalizedRect, Rect
from src.config.models import ClientProfile, FeatureDef, PerceptionConfig, WindowConfig
from src.perception.capture import Frame
from src.perception.templates import (
    MIN_TEMPLATE_SIZE,
    TemplateMatcher,
    compile_feature,
    resolve_template_path,
)

from tests.helpers import sample_background


def _write_image(path: Path, image: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(path), image)
    return path


def _pick_patch(
    background: np.ndarray,
    width: int,
    height: int,
    *,
    x_range: tuple[int, int] | None = None,
    y_range: tuple[int, int] | None = None,
    step: int = 8,
) -> tuple[np.ndarray, int, int]:
    """在合成图中挑一块纹理最丰富的区域裁成模板，返回 ``(模板, x, y)``。"""
    gray = cv2.cvtColor(background, cv2.COLOR_BGR2GRAY)
    frame_h, frame_w = gray.shape[:2]
    x_lo, x_hi = x_range if x_range is not None else (0, frame_w - width)
    y_lo, y_hi = y_range if y_range is not None else (0, frame_h - height)
    best: tuple[float, int, int] | None = None
    for y in range(int(y_lo), int(y_hi) + 1, step):
        for x in range(int(x_lo), int(x_hi) + 1, step):
            std = float(gray[y : y + height, x : x + width].std())
            if best is None or std > best[0]:
                best = (std, x, y)
    assert best is not None, "没有可用的搜索位置"
    assert best[0] >= 10.0, f"合成图缺少纹理区域: std={best[0]:.2f}"
    _, patch_x, patch_y = best
    return background[patch_y : patch_y + height, patch_x : patch_x + width].copy(), patch_x, patch_y


def _feature(
    feature_id: str,
    template_path: Path | None,
    *,
    template_name: str | None = None,
    region: NormalizedRect | None = None,
    threshold: float = 0.8,
    scales: tuple[float, ...] = (0.95, 1.0, 1.05),
) -> FeatureDef:
    return FeatureDef(
        feature_id=feature_id,
        template=template_name if template_name is not None else (template_path.name if template_path else ""),
        region=region if region is not None else NormalizedRect(0.0, 0.0, 1.0, 1.0),
        threshold=threshold,
        scales=tuple(scales),
        template_path=template_path,
    )


def _profile(
    tmp_path: Path,
    features: list[FeatureDef],
    *,
    state_features: dict[str, tuple[str, ...]] | None = None,
) -> ClientProfile:
    return ClientProfile(
        client_id="test",
        display_name="测试客户端",
        window=WindowConfig(),
        language="zh_cn",
        reference_resolution=(640, 480),
        features={feature.feature_id: feature for feature in features},
        state_features=state_features or {},
        profile_dir=tmp_path,
    )


def _frame(image: np.ndarray, timestamp: float = 1.0) -> Frame:
    height, width = image.shape[:2]
    return Frame(image=image, timestamp=timestamp, window_rect=Rect(0, 0, width, height))


def _noise(width: int, height: int, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)


# ---------------------------------------------------------------------------
# 正样本 / 负样本
# ---------------------------------------------------------------------------


def test_match_finds_exact_patch_and_reports_geometry(tmp_path: Path) -> None:
    background = sample_background(640, 480, seed=11)
    patch, patch_x, patch_y = _pick_patch(background, 48, 48)
    template_path = _write_image(tmp_path / "features" / "target.png", patch)
    profile = _profile(tmp_path, [_feature("target", template_path, threshold=0.8, scales=(1.0,))])
    matcher = TemplateMatcher(PerceptionConfig(template_scales=(1.0,)))
    assert matcher.load_profile(profile) == ()
    assert matcher.feature_ids() == ("target",)

    result = matcher.match("target", _frame(background.copy(), timestamp=2.5))
    assert result.feature_id == "target"
    assert result.found
    assert result.score > 0.99
    assert result.scale == 1.0
    assert result.center == (patch_x + 24, patch_y + 24)
    assert result.rect == (patch_x, patch_y, 48, 48)
    assert result.timestamp == 2.5


def test_match_rejects_unrelated_scene(tmp_path: Path) -> None:
    template_source = sample_background(640, 480, seed=11)
    # 固定取一块中等纹理的模板：避免“纹理极强”的合成块在无关背景上碰巧高相关
    patch = template_source[200:260, 300:360].copy()
    assert float(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).std()) > 5.0
    template_path = _write_image(tmp_path / "tpl.png", patch)
    profile = _profile(tmp_path, [_feature("target", template_path, threshold=0.8)])
    matcher = TemplateMatcher(PerceptionConfig())
    matcher.load_profile(profile)

    scene = sample_background(640, 480, seed=22)
    result = matcher.match("target", _frame(scene))
    assert not result.found
    assert result.score < 0.8
    assert result.center is None
    assert result.rect is None


def test_match_respects_search_region(tmp_path: Path) -> None:
    background = sample_background(640, 480, seed=13)
    inside_region = NormalizedRect(0.10, 0.10, 0.30, 0.30)  # 像素 64..192 / 48..144
    patch, patch_x, patch_y = _pick_patch(
        background,
        40,
        40,
        x_range=(64, 192 - 40),
        y_range=(48, 144 - 40),
        step=4,
    )
    template_path = _write_image(tmp_path / "tpl.png", patch)
    outside_region = NormalizedRect(0.55, 0.55, 0.95, 0.95)

    matcher = TemplateMatcher(PerceptionConfig(template_scales=(1.0,)))
    matcher.load_profile(
        _profile(
            tmp_path,
            [
                _feature("inside", template_path, region=inside_region, threshold=0.8, scales=(1.0,)),
                _feature("outside", template_path, region=outside_region, threshold=0.8, scales=(1.0,)),
            ],
        )
    )
    frame = _frame(background.copy())

    inside_result = matcher.match("inside", frame)
    assert inside_result.found
    assert inside_result.center == (patch_x + 20, patch_y + 20)

    outside_result = matcher.match("outside", frame)
    assert not outside_result.found


def test_match_multi_scale_selects_scaled_template(tmp_path: Path) -> None:
    background = sample_background(640, 480, seed=5)
    # 预留 1.05 倍（84 像素）的贴图空间，避免贴到画面外
    patch, patch_x, patch_y = _pick_patch(
        background, 80, 80, x_range=(0, 640 - 84), y_range=(0, 480 - 84), step=16
    )
    scaled = cv2.resize(patch, (84, 84), interpolation=cv2.INTER_LINEAR)  # 1.05 倍
    scene = background.copy()
    scene[patch_y : patch_y + 84, patch_x : patch_x + 84] = scaled
    template_path = _write_image(tmp_path / "tpl.png", patch)

    matcher = TemplateMatcher(PerceptionConfig(template_scales=(0.95, 1.0, 1.05)))
    matcher.load_profile(_profile(tmp_path, [_feature("target", template_path, threshold=0.6)]))
    assert matcher.scales_for("target") == (0.95, 1.0, 1.05)

    result = matcher.match("target", _frame(scene))
    assert result.found, result
    assert result.scale == pytest.approx(1.05)
    assert result.rect is not None
    assert result.rect[2:] == (84, 84)
    assert result.center is not None
    assert abs(result.center[0] - (patch_x + 42)) <= 1
    assert abs(result.center[1] - (patch_y + 42)) <= 1


def test_match_handles_template_bigger_than_region(tmp_path: Path) -> None:
    background = sample_background(640, 480, seed=17)
    patch, _, _ = _pick_patch(background, 80, 80, step=16)
    template_path = _write_image(tmp_path / "tpl.png", patch)
    tiny_region = NormalizedRect(0.0, 0.0, 0.05, 0.08)  # 像素约 32x38，小于模板
    matcher = TemplateMatcher(PerceptionConfig(template_scales=(0.95, 1.0, 1.05)))
    matcher.load_profile(
        _profile(tmp_path, [_feature("target", template_path, region=tiny_region, scales=(1.0,))])
    )
    result = matcher.match("target", _frame(background.copy()))
    assert not result.found
    assert result.score == 0.0


# ---------------------------------------------------------------------------
# 模板加载校验
# ---------------------------------------------------------------------------


def test_pure_color_template_rejected_strict_and_lenient(tmp_path: Path) -> None:
    solid = np.full((40, 40, 3), 128, dtype=np.uint8)
    template_path = _write_image(tmp_path / "solid.png", solid)
    profile = _profile(tmp_path, [_feature("solid", template_path)])

    strict_matcher = TemplateMatcher(PerceptionConfig())
    with pytest.raises(ConfigError) as excinfo:
        strict_matcher.load_profile(profile, strict=True)
    codes = [issue.code for issue in excinfo.value.errors]
    assert "template_pure_color" in codes

    lenient_matcher = TemplateMatcher(PerceptionConfig())
    issues = lenient_matcher.load_profile(profile)
    assert any(issue.code == "template_pure_color" for issue in issues)
    assert not lenient_matcher.has_feature("solid")
    result = lenient_matcher.match("solid", _frame(_noise(64, 48)))
    assert not result.found
    assert result.score == 0.0


def test_register_feature_rejects_pure_color_template(tmp_path: Path) -> None:
    solid = np.full((40, 40, 3), 0, dtype=np.uint8)
    template_path = _write_image(tmp_path / "solid.png", solid)
    matcher = TemplateMatcher(PerceptionConfig())
    with pytest.raises(ConfigError) as excinfo:
        matcher.register_feature("ship_card", template_path)
    assert any(issue.code == "template_pure_color" for issue in excinfo.value.issues)


def test_template_too_small_rejected(tmp_path: Path) -> None:
    small = _noise(4, 4, seed=2)
    template_path = _write_image(tmp_path / "small.png", small)
    profile = _profile(tmp_path, [_feature("small", template_path)])
    matcher = TemplateMatcher(PerceptionConfig())
    issues = matcher.load_profile(profile)
    assert any(issue.code == "template_too_small" for issue in issues)
    assert not matcher.has_feature("small")


def test_missing_template_recorded_not_crash(tmp_path: Path) -> None:
    missing = tmp_path / "features" / "nope.png"
    profile = _profile(tmp_path, [_feature("missing", missing)])
    matcher = TemplateMatcher(PerceptionConfig())
    issues = matcher.load_profile(profile)
    assert any(issue.code == "template_missing" for issue in issues)
    assert not matcher.has_feature("missing")
    result = matcher.match("missing", _frame(_noise(64, 48)))
    assert not result.found
    assert result.timestamp == 1.0

    with pytest.raises(ConfigError):
        TemplateMatcher(PerceptionConfig()).load_profile(profile, strict=True)


def test_bad_threshold_and_scales_detected(tmp_path: Path) -> None:
    background = sample_background(320, 240, seed=3)
    patch, _, _ = _pick_patch(background, 40, 40)
    template_path = _write_image(tmp_path / "tpl.png", patch)
    compiled, issues = compile_feature(
        "bad_threshold", template_path, NormalizedRect(0, 0, 1, 1), threshold=1.5
    )
    assert compiled is None
    assert [issue.code for issue in issues] == ["feature_bad_threshold"]

    compiled, issues = compile_feature(
        "bad_scales", template_path, NormalizedRect(0, 0, 1, 1), scales=(0.0, 1.0)
    )
    assert compiled is None
    assert [issue.code for issue in issues] == ["feature_bad_scales"]


# ---------------------------------------------------------------------------
# 运行时注册与路径解析
# ---------------------------------------------------------------------------


def test_register_feature_with_absolute_path_outside_profile_dir(tmp_path: Path) -> None:
    background = sample_background(640, 480, seed=7)
    patch, patch_x, patch_y = _pick_patch(background, 48, 48)
    card_path = _write_image(tmp_path / "ships" / "test_ship" / "card.png", patch)
    matcher = TemplateMatcher(PerceptionConfig(template_scales=(1.0,)))
    matcher.register_feature(
        "ship_card",
        card_path,
        region=NormalizedRect(0.0, 0.0, 1.0, 1.0),
        threshold=0.8,
        scales=(1.0,),
    )
    assert matcher.has_feature("ship_card")
    frame = _frame(background.copy())
    matcher.set_frame(frame)
    result = matcher.match("ship_card")  # 省略 frame → 使用缓存帧
    assert result.found
    assert result.center == (patch_x + 24, patch_y + 24)


def test_register_feature_with_project_root_relative_path(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    background = sample_background(640, 480, seed=8)
    patch, patch_x, patch_y = _pick_patch(background, 48, 48)
    relative_path = "profiles/cn360/ships/test_ship/card.png"
    _write_image(project_root / relative_path, patch)
    matcher = TemplateMatcher(PerceptionConfig(template_scales=(1.0,)), project_root=project_root)
    matcher.register_feature("ship_card", relative_path, None, 0.8, (1.0,))
    result = matcher.match("ship_card", _frame(background.copy()))
    assert result.found
    assert result.center == (patch_x + 24, patch_y + 24)


def test_register_feature_replace_and_unregister(tmp_path: Path) -> None:
    first_source = sample_background(320, 240, seed=21)
    second_source = sample_background(320, 240, seed=22)
    first_patch, _, _ = _pick_patch(first_source, 40, 40)
    second_patch, second_x, second_y = _pick_patch(second_source, 40, 40)
    first_path = _write_image(tmp_path / "first.png", first_patch)
    second_path = _write_image(tmp_path / "second.png", second_patch)
    matcher = TemplateMatcher(PerceptionConfig(template_scales=(1.0,)))

    matcher.register_feature("card", first_path, None, 0.8, (1.0,))
    matcher.register_feature("card", second_path, None, 0.8, (1.0,))
    result = matcher.match("card", _frame(second_source.copy()))
    assert result.center == (second_x + 20, second_y + 20)

    with pytest.raises(ValueError):
        matcher.register_feature("card", first_path, None, 0.8, (1.0,), replace=False)

    assert matcher.unregister_feature("card")
    assert not matcher.unregister_feature("card")
    assert not matcher.has_feature("card")


def test_resolve_template_path(tmp_path: Path) -> None:
    base_dir = tmp_path / "profiles" / "cn360"
    project_root = tmp_path
    absolute = _write_image(tmp_path / "abs.png", _noise(16, 16))
    assert resolve_template_path(absolute, base_dir=base_dir, project_root=project_root) == absolute

    relative_existing = "features/relative.png"
    _write_image(base_dir / relative_existing, _noise(16, 16))
    assert resolve_template_path(relative_existing, base_dir=base_dir) == base_dir / relative_existing

    project_relative = "ships/s1/card.png"
    _write_image(project_root / project_relative, _noise(16, 16))
    assert resolve_template_path(project_relative, base_dir=base_dir, project_root=project_root) == (
        project_root / project_relative
    )

    missing = resolve_template_path("features/missing.png", base_dir=base_dir)
    assert missing == base_dir / "features" / "missing.png"


def test_profile_relative_template_path_resolved_from_profile_dir(tmp_path: Path) -> None:
    background = sample_background(320, 240, seed=4)
    patch, _, _ = _pick_patch(background, 40, 40)
    _write_image(tmp_path / "features" / "port.png", patch)
    feature = _feature("port_indicator", None, template_name="features/port.png", threshold=0.8, scales=(1.0,))
    profile = _profile(tmp_path, [feature])
    matcher = TemplateMatcher(PerceptionConfig(template_scales=(1.0,)))
    assert matcher.load_profile(profile) == ()
    result = matcher.match("port_indicator", _frame(background.copy()))
    assert result.found


# ---------------------------------------------------------------------------
# 批量匹配 / 缓存帧 / 边界
# ---------------------------------------------------------------------------


def test_match_many_returns_results_for_missing_features(tmp_path: Path) -> None:
    background = sample_background(640, 480, seed=10)
    patch, _, _ = _pick_patch(background, 48, 48)
    template_path = _write_image(tmp_path / "tpl.png", patch)
    matcher = TemplateMatcher(PerceptionConfig(template_scales=(1.0,)))
    matcher.load_profile(_profile(tmp_path, [_feature("known", template_path, threshold=0.8)]))
    frame = _frame(background.copy(), timestamp=4.0)
    results = matcher.match_many(["known", "unknown"], frame)
    assert list(results) == ["known", "unknown"]
    assert results["known"].found
    assert results["known"].timestamp == 4.0
    assert not results["unknown"].found
    assert results["unknown"].score == 0.0
    assert results["unknown"].timestamp == 4.0


def test_match_requires_frame_or_cache(tmp_path: Path) -> None:
    background = sample_background(320, 240, seed=12)
    patch, _, _ = _pick_patch(background, 40, 40)
    template_path = _write_image(tmp_path / "tpl.png", patch)
    matcher = TemplateMatcher(PerceptionConfig(template_scales=(1.0,)))
    matcher.load_profile(_profile(tmp_path, [_feature("target", template_path)]))
    with pytest.raises(ValueError):
        matcher.match("target")
    # 传 ndarray 也能匹配（timestamp 记为 0.0）
    result = matcher.match("target", background)
    assert result.found
    assert result.timestamp == 0.0
    # 之后的调用使用缓存帧
    assert matcher.last_frame is not None
    assert matcher.match("target").found


def test_clear_and_issues_property(tmp_path: Path) -> None:
    background = sample_background(320, 240, seed=14)
    patch, _, _ = _pick_patch(background, 32, 32)
    template_path = _write_image(tmp_path / "tpl.png", patch)
    matcher = TemplateMatcher(PerceptionConfig())
    matcher.load_profile(_profile(tmp_path, [_feature("target", template_path)]))
    assert matcher.issues == ()
    matcher.clear()
    assert matcher.feature_ids() == ()
    assert matcher.last_frame is None


def test_compile_feature_presizes_variants(tmp_path: Path) -> None:
    background = sample_background(320, 240, seed=6)
    patch, _, _ = _pick_patch(background, 60, 40)
    template_path = _write_image(tmp_path / "tpl.png", patch)
    compiled, issues = compile_feature(
        "target",
        template_path,
        NormalizedRect(0.1, 0.2, 0.9, 0.8),
        threshold=0.7,
        scales=(1.0, 1.05),
    )
    assert compiled is not None
    assert issues == ()
    assert compiled.template_size == (60, 40)
    assert compiled.scales == (1.0, 1.05)
    assert len(compiled.variants) == 2
    assert compiled.variants[0][1].shape == (40, 60)
    assert compiled.variants[1][1].shape == (42, 63)
    assert compiled.variant_for(1.05) is compiled.variants[1][1]
    assert compiled.variant_for(1.2) is None
    assert MIN_TEMPLATE_SIZE == 8
