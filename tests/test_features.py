"""状态特征表与 FeatureMatcher 测试。

用 ``tests.helpers.sample_background`` 合成场景：把不同区域裁出的纹理块作为
port / queue / battle HUD 三个界面特征模板，同一帧里可以组合出不同的可见状态，
验证状态表合并、连续命中确认、detect_state 优先级与缓存帧复用。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
from src.common.types import NormalizedRect, Rect, State
from src.config.models import ClientProfile, FeatureDef, PerceptionConfig, WindowConfig
from src.perception.capture import Frame
from src.perception.features import FeatureMatcher
from src.perception.state_features import (
    DEFAULT_STATE_FEATURES,
    DEFAULT_STATE_PRIORITY,
    default_state_features,
    merge_state_features,
)
from src.perception.templates import TemplateMatcher

from tests.helpers import sample_background


def _write_image(path: Path, image: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(path), image)
    return path


def _noise(width: int, height: int, seed: int = 99) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)


def _profile(
    tmp_path: Path,
    features: dict[str, FeatureDef],
    *,
    state_features: dict[str, tuple[str, ...]] | None = None,
) -> ClientProfile:
    return ClientProfile(
        client_id="test",
        display_name="测试客户端",
        window=WindowConfig(),
        language="zh_cn",
        reference_resolution=(640, 480),
        features=features,
        state_features=state_features or {},
        profile_dir=tmp_path,
    )


def _frame(image: np.ndarray, timestamp: float = 1.0) -> Frame:
    height, width = image.shape[:2]
    return Frame(image=image, timestamp=timestamp, window_rect=Rect(0, 0, width, height))


def _make_icon(seed: int, size: int = 40) -> np.ndarray:
    """画一个高对比、可区分的合成界面图标（避免随机纹理造成误匹配）。"""
    rng = np.random.default_rng(seed)
    icon = np.full((size, size, 3), 25, dtype=np.uint8)
    for _ in range(6):
        p1 = tuple(int(v) for v in rng.integers(0, size, 2))
        p2 = tuple(int(v) for v in rng.integers(0, size, 2))
        color = tuple(int(v) for v in rng.integers(100, 256, 3))
        cv2.line(icon, p1, p2, color, thickness=int(rng.integers(1, 3)))
    for _ in range(4):
        center = tuple(int(v) for v in rng.integers(5, size - 5, 2))
        color = tuple(int(v) for v in rng.integers(100, 256, 3))
        cv2.circle(icon, center, int(rng.integers(3, 9)), color, thickness=-1)
    assert float(cv2.cvtColor(icon, cv2.COLOR_BGR2GRAY).std()) > 10.0
    return icon


def _scene_setup(tmp_path: Path) -> tuple[np.ndarray, ClientProfile, dict[str, tuple[int, int]]]:
    """构造含 port / queue / battle HUD 三个可见特征的合成帧与 profile。

    场景是深色纯色底，三个可区分的图标贴在互不重叠的位置；这样某一图标被遮住后
    模板匹配得分为 0，不会因为随机纹理碰巧高相关而误判。
    """
    background = np.full((480, 640, 3), 30, dtype=np.uint8)
    locations: dict[str, tuple[int, int]] = {
        "port_indicator": (100, 80),
        "queue_indicator": (360, 80),
        "battle_hud_indicator": (300, 330),
    }
    regions = {
        "port_indicator": NormalizedRect(0.0, 0.0, 0.4, 0.4),
        "queue_indicator": NormalizedRect(0.45, 0.0, 0.95, 0.4),
        "battle_hud_indicator": NormalizedRect(0.3, 0.55, 0.95, 0.95),
    }
    features: dict[str, FeatureDef] = {}
    for index, (feature_id, (patch_x, patch_y)) in enumerate(locations.items()):
        icon = _make_icon(seed=1000 + index)
        background[patch_y : patch_y + 40, patch_x : patch_x + 40] = icon
        path = _write_image(tmp_path / "features" / f"{feature_id}.png", icon)
        features[feature_id] = FeatureDef(
            feature_id=feature_id,
            template=f"features/{feature_id}.png",
            region=regions[feature_id],
            threshold=0.8,
            scales=(1.0,),
            template_path=path,
        )
    return background, _profile(tmp_path, features), locations


def _hide_patch(scene: np.ndarray, location: tuple[int, int], *, seed: int) -> np.ndarray:
    hidden = scene.copy()
    patch_x, patch_y = location
    hidden[patch_y : patch_y + 40, patch_x : patch_x + 40] = _noise(40, 40, seed=seed)
    return hidden


# ---------------------------------------------------------------------------
# 状态特征表
# ---------------------------------------------------------------------------


def test_default_state_features_cover_design_table() -> None:
    expected_primary = {
        State.PORT: "port_indicator",
        State.QUEUEING: "queue_indicator",
        State.LOADING: "loading_indicator",
        State.BATTLE_ACTIVE: "battle_hud_indicator",
        State.BATTLE_FINISHED: "battle_results",
        State.EXIT_MENU_OPEN: "exit_menu_panel",
        State.EXIT_CONFIRMING: "exit_confirm_dialog",
        State.POST_EXIT_INTERFACE: "popup_close_button",
    }
    for state, primary in expected_primary.items():
        assert DEFAULT_STATE_FEATURES[state][0] == primary
    # 详细设计 5.4 的 DEAD 行映射到 DEAD_PENDING_EXIT
    assert DEFAULT_STATE_FEATURES[State.DEAD_PENDING_EXIT][0] == "death_banner"

    assert DEFAULT_STATE_PRIORITY[0] is State.POST_EXIT_INTERFACE
    assert DEFAULT_STATE_PRIORITY[-1] is State.PORT
    assert set(DEFAULT_STATE_PRIORITY) == set(DEFAULT_STATE_FEATURES)


def test_merge_state_features_overrides_and_extends() -> None:
    defaults = default_state_features()
    merged = merge_state_features(
        {
            "BATTLE_ACTIVE": ["custom_hud"],
            "DEAD": ["custom_banner"],
            State.PORT: ("port_custom",),  # State 键也允许
        }
    )
    assert merged["BATTLE_ACTIVE"] == ("custom_hud",)
    assert merged["PORT"] == ("port_custom",)
    assert merged["DEAD"] == ("custom_banner",)
    assert merged["QUEUEING"] == defaults["QUEUEING"]
    # 默认表不被修改
    assert default_state_features()["BATTLE_ACTIVE"] == defaults["BATTLE_ACTIVE"]
    assert merge_state_features(None)["PORT"] == defaults["PORT"]


# ---------------------------------------------------------------------------
# FeatureMatcher：状态表 / 批量匹配 / 连续命中
# ---------------------------------------------------------------------------


def test_feature_matcher_matches_state_features(tmp_path: Path) -> None:
    background, profile, _ = _scene_setup(tmp_path)
    matcher = FeatureMatcher(profile, PerceptionConfig(feature_confirm_frames=2))
    frame = _frame(background, timestamp=1.0)

    results = matcher.match_state(State.PORT, frame)
    assert list(results) == list(DEFAULT_STATE_FEATURES[State.PORT])
    assert results["port_indicator"].found
    assert not results["port_battle_button"].found  # 该特征未加载
    assert matcher.hit_streak("port_indicator") == 1
    assert not matcher.confirmed("port_indicator")

    second_frame = _frame(background.copy(), timestamp=2.0)
    matcher.match_state(State.PORT, second_frame)
    assert matcher.hit_streak("port_indicator") == 2
    assert matcher.confirmed("port_indicator")


def test_same_frame_does_not_double_count_confirmation(tmp_path: Path) -> None:
    background, profile, _ = _scene_setup(tmp_path)
    matcher = FeatureMatcher(profile, PerceptionConfig(feature_confirm_frames=2))
    frame = _frame(background, timestamp=5.0)
    matcher.match("port_indicator", frame)
    matcher.match("port_indicator", frame)
    assert matcher.hit_streak("port_indicator") == 1
    assert not matcher.confirmed("port_indicator")


def test_missing_feature_resets_confirmation(tmp_path: Path) -> None:
    background, profile, _ = _scene_setup(tmp_path)
    matcher = FeatureMatcher(profile, PerceptionConfig(feature_confirm_frames=2))
    matcher.match("port_indicator", _frame(background, timestamp=1.0))
    assert matcher.hit_streak("port_indicator") == 1

    negative = _frame(sample_background(640, 480, seed=77), timestamp=2.0)
    result = matcher.match("port_indicator", negative)
    assert not result.found
    assert matcher.hit_streak("port_indicator") == 0
    assert not matcher.confirmed("port_indicator")


def test_confirm_frames_minimum_one(tmp_path: Path) -> None:
    background, profile, _ = _scene_setup(tmp_path)
    matcher = FeatureMatcher(profile, PerceptionConfig(feature_confirm_frames=0))
    assert matcher.confirm_frames == 1
    matcher.match("port_indicator", _frame(background, timestamp=1.0))
    assert matcher.confirmed("port_indicator")


# ---------------------------------------------------------------------------
# 缓存帧
# ---------------------------------------------------------------------------


def test_match_uses_cached_frame(tmp_path: Path) -> None:
    background, profile, _ = _scene_setup(tmp_path)
    matcher = FeatureMatcher(profile, PerceptionConfig())
    with pytest.raises(ValueError):
        matcher.match("port_indicator")

    frame = _frame(background, timestamp=3.0)
    matcher.set_frame(frame)
    assert matcher.cached_frame is frame
    assert matcher.match("port_indicator").found
    assert matcher.cached_frame is frame
    assert matcher.template_matcher.last_frame is frame

    explicit = _frame(background.copy(), timestamp=4.0)
    result = matcher.match("port_indicator", explicit)
    assert result.timestamp == 4.0
    assert matcher.cached_frame is explicit

    matcher.clear_frame()
    with pytest.raises(ValueError):
        matcher.match("port_indicator")


def test_set_frame_requires_frame_instance(tmp_path: Path) -> None:
    _, profile, _ = _scene_setup(tmp_path)
    matcher = FeatureMatcher(profile, PerceptionConfig())
    with pytest.raises(TypeError):
        matcher.set_frame(np.zeros((10, 10, 3), dtype=np.uint8))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# detect_state 优先级
# ---------------------------------------------------------------------------


def test_detect_state_priority(tmp_path: Path) -> None:
    background, profile, locations = _scene_setup(tmp_path)
    matcher = FeatureMatcher(profile, PerceptionConfig())

    frame = _frame(background, timestamp=1.0)
    # BATTLE_ACTIVE 在优先级里排在 QUEUEING / PORT 之前
    assert matcher.detect_state(frame) is State.BATTLE_ACTIVE

    without_battle = _hide_patch(background, locations["battle_hud_indicator"], seed=101)
    assert matcher.detect_state(_frame(without_battle, timestamp=2.0)) is State.QUEUEING

    without_battle_queue = _hide_patch(without_battle, locations["queue_indicator"], seed=102)
    assert matcher.detect_state(_frame(without_battle_queue, timestamp=3.0)) is State.PORT

    empty_scene = sample_background(640, 480, seed=77)
    assert matcher.detect_state(_frame(empty_scene, timestamp=4.0)) is None


def test_detect_state_requires_frame(tmp_path: Path) -> None:
    _, profile, _ = _scene_setup(tmp_path)
    matcher = FeatureMatcher(profile, PerceptionConfig())
    with pytest.raises(ValueError):
        matcher.detect_state()


def test_detect_state_skips_unavailable_features(tmp_path: Path) -> None:
    """未标定（模板缺失）的特征不算命中，也不会让 detect_state 崩溃。"""
    background = sample_background(640, 480, seed=31)
    profile = _profile(
        tmp_path,
        {
            "port_indicator": FeatureDef(
                feature_id="port_indicator",
                template="features/missing.png",
                region=NormalizedRect(0.0, 0.0, 0.4, 0.4),
            )
        },
    )
    matcher = FeatureMatcher(profile, PerceptionConfig())
    assert matcher.available_features() == ()
    assert matcher.detect_state(_frame(background)) is None


# ---------------------------------------------------------------------------
# profile 覆盖 / 注入模板匹配器
# ---------------------------------------------------------------------------


def test_profile_state_features_override(tmp_path: Path) -> None:
    background, base_profile, _ = _scene_setup(tmp_path)
    profile = _profile(
        tmp_path,
        dict(base_profile.features),
        state_features={
            "PORT": ("battle_hud_indicator",),
            "CUSTOM_STATE": ("port_indicator",),
        },
    )
    matcher = FeatureMatcher(profile, PerceptionConfig())
    assert matcher.state_feature_ids(State.PORT) == ("battle_hud_indicator",)
    assert matcher.state_feature_ids("QUEUEING") == DEFAULT_STATE_FEATURES[State.QUEUEING]
    assert matcher.state_feature_ids("CUSTOM_STATE") == ("port_indicator",)

    results = matcher.match_state("PORT", _frame(background))
    assert list(results) == ["battle_hud_indicator"]
    assert results["battle_hud_indicator"].found

    # 只读视图，不允许外部改表
    with pytest.raises(TypeError):
        matcher.state_features["PORT"] = ()  # type: ignore[index]


def test_injected_template_matcher_is_not_reloaded(tmp_path: Path) -> None:
    background, profile, _ = _scene_setup(tmp_path)
    templates = TemplateMatcher(PerceptionConfig(template_scales=(1.0,)))
    templates.load_profile(profile)
    extra_icon = _make_icon(seed=777)
    extra_x, extra_y = 60, 400
    frame_image = background.copy()
    frame_image[extra_y : extra_y + 40, extra_x : extra_x + 40] = extra_icon
    extra_path = _write_image(tmp_path / "extra.png", extra_icon)
    templates.register_feature("ship_card", extra_path, None, 0.8, (1.0,))

    matcher = FeatureMatcher(profile, PerceptionConfig(), template_matcher=templates)
    assert matcher.template_matcher is templates
    assert matcher.has_feature("ship_card")  # 注入的匹配器不会被 load_profile 清空
    result = matcher.match("ship_card", _frame(frame_image))
    assert result.found
    assert result.center == (extra_x + 20, extra_y + 20)


def test_from_profile_factory(tmp_path: Path) -> None:
    _, profile, _ = _scene_setup(tmp_path)
    matcher = FeatureMatcher.from_profile(profile, PerceptionConfig())
    assert isinstance(matcher, FeatureMatcher)
    assert isinstance(matcher.template_matcher, TemplateMatcher)


# ---------------------------------------------------------------------------
# 模板缺失时的降级行为（当前仓库 cn360 的待标定状态）
# ---------------------------------------------------------------------------


def test_missing_templates_degrade_gracefully(tmp_path: Path) -> None:
    features = {
        "port_indicator": FeatureDef(
            feature_id="port_indicator",
            template="features/port_indicator.png",
            region=NormalizedRect(0.0, 0.0, 0.4, 0.4),
        ),
        "battle_hud_indicator": FeatureDef(
            feature_id="battle_hud_indicator",
            template="features/battle_hud_indicator.png",
            region=NormalizedRect(0.3, 0.55, 0.95, 0.95),
        ),
    }
    profile = _profile(tmp_path, features)
    matcher = FeatureMatcher(profile, PerceptionConfig())
    assert matcher.load_issues
    assert all(issue.is_error for issue in matcher.load_issues)
    assert matcher.available_features() == ()

    frame = _frame(_noise(640, 480, seed=5), timestamp=1.0)
    results = matcher.match_state(State.PORT, frame)
    assert results
    assert all(not result.found for result in results.values())
    assert matcher.confirmed("port_indicator") is False
    assert matcher.detect_state(frame) is None


def test_reset_clears_confirmations_and_cached_frame(tmp_path: Path) -> None:
    background, profile, _ = _scene_setup(tmp_path)
    matcher = FeatureMatcher(profile, PerceptionConfig(feature_confirm_frames=2))
    matcher.match("port_indicator", _frame(background, 1.0))
    matcher.match("port_indicator", _frame(background, 2.0))
    assert matcher.confirmed("port_indicator")

    matcher.reset()
    assert not matcher.confirmed("port_indicator")
    assert matcher.hit_streak("port_indicator") == 0
    assert matcher.cached_frame is None
    with pytest.raises(ValueError):
        matcher.match("port_indicator")
