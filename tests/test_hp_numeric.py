"""数字零血授权测试（2026-11-01 设计）。

控制器已确认：启用 ``battle_hud.hp_zero_feature`` 后，空血条不再单独授权退出，
必须同时满足己方 ``hp_bar_present`` 命中、``0/`` 模板命中、数字区域内红橙色
当前血量字形只有模板对应的零字形且左侧没有其他有效字形。

负例数字由 ``profiles/cn360/features/hp_zero_value.png`` 中的真实
零字形与真实笔画拼接而成，不复制实现里的掩码/连通域算法。该文件不含账号身份。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
from src.common.types import MatchResult, NormalizedRect, Rect
from src.config.loader import load_project
from src.config.models import (
    AppConfig,
    BattleHudConfig,
    ClientProfile,
    FeatureDef,
    GlobalConfig,
    KeyMap,
    MinimapConfig,
    PerceptionConfig,
    ShipProfile,
    WindowConfig,
)
from src.perception.capture import Frame
from src.perception.engine import PerceptionEngine
from src.perception.templates import TemplateMatcher

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ZERO_SAMPLE = PROJECT_ROOT / "profiles" / "cn360" / "features" / "hp_zero_value.png"
FEATURE_ID = "hp_zero_value"
NUM_REGION = NormalizedRect(0.075, 0.77, 0.17, 0.795)
FRAME_W, FRAME_H = 1600, 1000
BAR_REGION = NormalizedRect(0.014, 0.737, 0.130, 0.7665)
BATTLE_START = 0.0
BASE_NOW = 6.0
BACKGROUND = (24, 26, 30)


def _template() -> np.ndarray:
    image = cv2.imdecode(np.fromfile(str(ZERO_SAMPLE), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, f"无法读取派生零字形样本: {ZERO_SAMPLE}"
    return image


TEMPLATE = _template()
ZERO_GLYPH = TEMPLATE[2:19, 3:12].copy()
ONE_GLYPH = TEMPLATE[2:19, 3:6].copy()


def _bar_image(*, empty: bool = False, full: bool = False) -> np.ndarray:
    image = np.full((FRAME_H, FRAME_W, 3), BACKGROUND, dtype=np.uint8)
    region = BAR_REGION.to_pixels(FRAME_W, FRAME_H)
    image[region.y : region.bottom, region.x : region.right] = (45, 45, 45)
    if empty:
        cv2.rectangle(
            image,
            (region.x, region.y),
            (region.right - 1, region.bottom - 1),
            (10, 25, 120),
            2,
        )
    elif full:
        image[region.y + 3 : region.bottom - 3, region.x : region.right] = (21, 221, 156)
    return image


def _paste(image: np.ndarray, patch: np.ndarray, x: int, y: int) -> None:
    height, width = patch.shape[:2]
    image[y : y + height, x : x + width] = patch


def _compose_number(number: str, *, slash: bool = True) -> np.ndarray:
    """把真实零字形/真实竖笔画拼成数字显示，末尾贴真实的 ``0/`` 模板。

    仅接受由 ``0``、``1`` 组成的字符串；``10/100/1000/10000`` 用来验证末尾零
    左侧还有有效数字时绝不授权。最终 ``0/`` 直接粘贴原模板，保证模板匹配分数
    与真实样本一致。
    """
    image = _bar_image(empty=True)
    region = NUM_REGION.to_pixels(FRAME_W, FRAME_H)
    final_x = region.x + 102
    final_y = region.y + 2
    for index, digit in enumerate(reversed(number[:-1])):
        glyph = ZERO_GLYPH if digit == "0" else ONE_GLYPH
        _paste(image, glyph, final_x - (index + 1) * 14 + 3, final_y + 2)
    patch = TEMPLATE.copy()
    if not slash:
        patch[:, 13:] = BACKGROUND
    _paste(image, patch, final_x, final_y)
    return image


def _compose_nonzero_strokes(count: int = 3) -> np.ndarray:
    """用真实零字形左侧笔画拼出多位非零数字（本地 734 样本不允许读取）。"""
    image = _bar_image(empty=True)
    region = NUM_REGION.to_pixels(FRAME_W, FRAME_H)
    y = region.y + 4
    for index in range(count):
        _paste(image, ONE_GLYPH, region.x + 60 + index * 14, y)
    return image


class _NumericMatcher:
    """固定己方身份命中，并把 ``hp_zero_value`` 交给真实模板匹配器。"""

    def __init__(
        self,
        *,
        hp_bar_found: bool = True,
        has_zero: bool = True,
        spectator_found: bool = False,
    ) -> None:
        self._hp_bar_found = hp_bar_found
        self._has_zero = has_zero
        self._spectator_found = spectator_found
        self._templates = TemplateMatcher(PerceptionConfig(), project_root=PROJECT_ROOT)
        self._templates.register_feature(FEATURE_ID, ZERO_SAMPLE, NUM_REGION, 0.92)

    def _result(self, feature_id: str, frame: Frame | None = None) -> MatchResult:
        if feature_id == FEATURE_ID:
            if not self._has_zero:
                return MatchResult(feature_id=feature_id, found=False, score=0.0)
            if frame is None:
                return MatchResult(feature_id=feature_id, found=False, score=0.0)
            return self._templates.match(feature_id, frame)
        found = (
            self._hp_bar_found
            if feature_id == "hp_bar_present"
            else self._spectator_found if feature_id == "spectator_indicator" else False
        )
        return MatchResult(feature_id=feature_id, found=found, score=1.0 if found else 0.0)

    def set_frame(self, frame: Frame) -> Frame:
        return frame

    def match(self, feature_id: str, frame: Frame | None = None) -> MatchResult:
        return self._result(feature_id, frame)

    def match_many(
        self, feature_ids, frame: Frame | None = None
    ) -> dict[str, MatchResult]:
        return {feature_id: self._result(feature_id, frame) for feature_id in feature_ids}

    def has_feature(self, feature_id: str) -> bool:
        if feature_id == FEATURE_ID:
            return self._has_zero
        return feature_id in ("hp_bar_present", "spectator_indicator")


def _make_profile(
    tmp_path: Path,
    hud: BattleHudConfig,
    *,
    include_zero_feature: bool = True,
    zero_template_path: Path | None = ZERO_SAMPLE,
) -> ClientProfile:
    features: dict[str, FeatureDef] = {
        "hp_bar_present": FeatureDef(
            feature_id="hp_bar_present",
            template="hp_bar_present.png",
            region=hud.hp_bar_region or BAR_REGION,
        )
    }
    if include_zero_feature:
        features[FEATURE_ID] = FeatureDef(
            feature_id=FEATURE_ID,
            template="hp_zero_value.png",
            region=NUM_REGION,
            threshold=0.92,
            template_path=zero_template_path,
        )
    if hud.spectator_features:
        features["spectator_indicator"] = FeatureDef(
            feature_id="spectator_indicator",
            template="spectator_indicator.png",
            region=NormalizedRect(0.0, 0.0, 1.0, 1.0),
        )
    return ClientProfile(
        client_id="test",
        display_name="测试客户端",
        window=WindowConfig(),
        language="zh_cn",
        reference_resolution=(FRAME_W, FRAME_H),
        features=features,
        battle_hud=hud,
        minimap=MinimapConfig(
            rect=NormalizedRect(0.70, 0.70, 0.98, 0.98),
            marker_template="profiles/cn360/marker/marker.png",
        ),
        profile_dir=tmp_path,
    )


def _make_engine(
    tmp_path: Path,
    *,
    hud: BattleHudConfig | None = None,
    include_zero_feature: bool = True,
    zero_template_path: Path | None = ZERO_SAMPLE,
    matcher: _NumericMatcher | None = None,
) -> PerceptionEngine:
    battle_hud = hud if hud is not None else BattleHudConfig(
        hp_bar_region=BAR_REGION,
        hp_zero_feature=FEATURE_ID,
        spectator_features=("spectator_indicator",),
    )
    profile = _make_profile(
        tmp_path,
        battle_hud,
        include_zero_feature=include_zero_feature,
        zero_template_path=zero_template_path,
    )
    config = AppConfig(
        global_config=GlobalConfig(),
        profile=profile,
        keymap=KeyMap(),
        ship=ShipProfile(ship_id="test", display_name="测试船"),
        maps={},
        project_root=PROJECT_ROOT,
        config_hash="test",
    )
    engine = PerceptionEngine(
        config,
        capture=None,
        feature_matcher=matcher or _NumericMatcher(),  # type: ignore[arg-type]
    )
    engine.begin_battle(BATTLE_START)
    return engine


def _frame(image: np.ndarray, frame_id: int) -> Frame:
    return Frame(
        image=image,
        timestamp=float(frame_id),
        window_rect=Rect(0, 0, FRAME_W, FRAME_H),
        frame_id=frame_id,
    )


def _features(
    image: np.ndarray,
    frame_id: int,
    *,
    hp_bar_found: bool = True,
    spectator: bool = False,
) -> tuple[Frame, dict[str, MatchResult]]:
    frame = _frame(image, frame_id)
    features = {
        "hp_bar_present": MatchResult(
            feature_id="hp_bar_present",
            found=hp_bar_found,
            score=1.0 if hp_bar_found else 0.0,
        )
    }
    if spectator:
        features["spectator_indicator"] = MatchResult(
            feature_id="spectator_indicator", found=True, score=1.0
        )
    return frame, features


def _check(
    engine: PerceptionEngine,
    image: np.ndarray,
    *,
    now: float,
    frame_id: int,
    hp_bar_found: bool = True,
    spectator: bool = False,
):
    frame, features = _features(
        image, frame_id, hp_bar_found=hp_bar_found, spectator=spectator
    )
    return engine.check_death(now, frame, features, None)


# ---------------------------------------------------------------------------
# 数字零授权与非零数字拒绝
# ---------------------------------------------------------------------------


def test_numeric_zero_requires_consecutive_new_frames(tmp_path: Path) -> None:
    """数字 0 连续两个新帧才授权；首帧不授权，结果保留实际填充比例。"""
    engine = _make_engine(tmp_path)
    image = _compose_number("0")
    first = _check(engine, image, now=BASE_NOW, frame_id=1)
    assert first.hp_current == 0
    assert first.hp_evidence == "zero"
    assert first.exit_authorized is False
    second = _check(engine, image, now=BASE_NOW + 0.5, frame_id=2)
    assert second.hp_current == 0
    assert second.hp_evidence == "zero"
    assert second.exit_authorized is True
    assert second.hp_fill_ratio is not None
    assert second.hp_fill_ratio <= 0.02


@pytest.mark.parametrize("number", ["10", "100", "1000", "10000"])
def test_positive_numbers_ending_in_zero_are_rejected(tmp_path: Path, number: str) -> None:
    """10/100/1000/10000：末尾 0 即使与模板匹配，左侧有效字形也必须整体拒绝。"""
    engine = _make_engine(tmp_path)
    image = _compose_number(number)
    for frame_id, now in ((1, BASE_NOW), (2, BASE_NOW + 0.5), (3, BASE_NOW + 1.0)):
        signals = _check(engine, image, now=now, frame_id=frame_id)
        assert signals.hp_current is None
        assert signals.hp_evidence != "zero"
        assert signals.exit_authorized is False


def test_empty_bar_with_nonzero_digits_is_not_zero(tmp_path: Path) -> None:
    """残血空条 + 多位非零数字（本机只允许用真实笔画模拟 734）不得授权。"""
    engine = _make_engine(tmp_path)
    image = _compose_nonzero_strokes(3)
    for frame_id, now in ((1, BASE_NOW), (2, BASE_NOW + 0.5), (3, BASE_NOW + 1.0)):
        signals = _check(engine, image, now=now, frame_id=frame_id)
        assert signals.hp_current is None
        assert signals.hp_evidence != "zero"
        assert signals.exit_authorized is False


@pytest.mark.parametrize(
    "background",
    [(120, 120, 120), (90, 70, 40)],
    ids=["gray", "sea_blue"],
)
def test_zero_survives_changed_background(tmp_path: Path, background: tuple[int, int, int]) -> None:
    """前景分类后，0/ 模板前景不变时灰色/海蓝背景也必须命中。"""
    engine = _make_engine(tmp_path)
    image = _bar_image(empty=True)
    image[:] = background
    region = NUM_REGION.to_pixels(FRAME_W, FRAME_H)
    patch = TEMPLATE.copy()
    # 样本的海面背景偏蓝；保留橙色数字和浅灰分隔符，替换字形周围的海面。
    blue_background = (patch[:, :, 0] > patch[:, :, 2]) & (patch[:, :, 1] < 145)
    patch[blue_background] = background
    _paste(image, patch, region.x + 102, region.y + 2)
    first = _check(engine, image, now=BASE_NOW, frame_id=1)
    assert first.hp_evidence == "zero"
    assert first.hp_current == 0
    second = _check(engine, image, now=BASE_NOW + 0.5, frame_id=2)
    assert second.exit_authorized is True


# ---------------------------------------------------------------------------
# 反例与边界
# ---------------------------------------------------------------------------


def test_full_bar_is_alive_even_without_numeric_zero(tmp_path: Path) -> None:
    """满血条且没有数字零证据：沿用条形 alive，hp_current 保持 None。"""
    engine = _make_engine(tmp_path)
    signals = _check(engine, _bar_image(full=True), now=BASE_NOW, frame_id=1)
    assert signals.hp_evidence == "alive"
    assert signals.hp_current is None
    assert signals.exit_authorized is False


def test_blank_numeric_region_does_not_authorize(tmp_path: Path) -> None:
    """空白数字区域 + 空条：数字特征已启用时，空条不能单独给 zero。"""
    engine = _make_engine(tmp_path)
    signals = _check(engine, _bar_image(empty=True), now=BASE_NOW, frame_id=1)
    assert signals.hp_current is None
    assert signals.hp_evidence != "zero"
    assert signals.exit_authorized is False


def test_missing_separator_does_not_authorize(tmp_path: Path) -> None:
    """缺少 ``0/`` 中的分隔符：前景模板不命中，不允许 zero。"""
    engine = _make_engine(tmp_path)
    image = _compose_number("0", slash=False)
    signals = _check(engine, image, now=BASE_NOW, frame_id=1)
    assert signals.hp_current is None
    assert signals.hp_evidence != "zero"
    assert signals.exit_authorized is False


def test_missing_own_hp_identity_does_not_authorize(tmp_path: Path) -> None:
    """己方血条身份未命中：即使数字 0/ 命中也不授权。"""
    engine = _make_engine(tmp_path)
    image = _compose_number("0")
    for frame_id, now in ((1, BASE_NOW), (2, BASE_NOW + 0.5)):
        signals = _check(
            engine, image, now=now, frame_id=frame_id, hp_bar_found=False
        )
        assert signals.hp_current is None
        assert signals.hp_evidence != "zero"
        assert signals.exit_authorized is False


def test_spectator_mode_does_not_authorize(tmp_path: Path) -> None:
    """观战界面看到的不是己方血条：数字零证据也失效。"""
    engine = _make_engine(tmp_path, matcher=_NumericMatcher(spectator_found=True))
    image = _compose_number("0")
    for frame_id, now in ((1, BASE_NOW), (2, BASE_NOW + 0.5)):
        signals = _check(engine, image, now=now, frame_id=frame_id, spectator=True)
        assert signals.hp_current is None
        assert signals.hp_evidence == "unavailable"
        assert signals.exit_authorized is False


def test_duplicate_frame_does_not_confirm_zero(tmp_path: Path) -> None:
    """同一帧重复消费只计一次，不构成“连续新帧”。"""
    engine = _make_engine(tmp_path)
    image = _compose_number("0")
    for now in (BASE_NOW, BASE_NOW + 0.1, BASE_NOW + 0.2):
        signals = _check(engine, image, now=now, frame_id=1)
        assert signals.exit_authorized is False
    refreshed = _check(engine, image, now=BASE_NOW + 0.3, frame_id=2)
    assert refreshed.exit_authorized is True


def test_expired_evidence_does_not_authorize(tmp_path: Path) -> None:
    """证据超过 TTL 且没有新帧：撤销授权。"""
    engine = _make_engine(tmp_path)
    image = _compose_number("0")
    _check(engine, image, now=BASE_NOW, frame_id=1)
    authorized = _check(engine, image, now=BASE_NOW + 0.5, frame_id=2)
    assert authorized.exit_authorized is True
    expired = _check(engine, image, now=BASE_NOW + 2.0, frame_id=2)
    assert expired.exit_authorized is False
    refreshed = _check(engine, image, now=BASE_NOW + 2.1, frame_id=3)
    assert refreshed.exit_authorized is True


def test_missing_zero_template_does_not_authorize(tmp_path: Path) -> None:
    """配置了数字特征但模板缺失：条形 zero 也不能授权。"""
    engine = _make_engine(tmp_path, zero_template_path=None)
    for frame_id, now in ((1, BASE_NOW), (2, BASE_NOW + 0.5)):
        signals = _check(engine, _bar_image(empty=True), now=now, frame_id=frame_id)
        assert signals.hp_current is None
        assert signals.hp_evidence != "zero"
        assert signals.exit_authorized is False


def test_numeric_zero_authorizes_without_fill_ratio(tmp_path: Path) -> None:
    """数字 0 已确认时不要求填充比例；fill_ratio 保持 None，不伪造 0。"""
    hud = BattleHudConfig(
        hp_bar_region=None,
        hp_zero_feature=FEATURE_ID,
    )
    engine = _make_engine(tmp_path, hud=hud)
    image = _compose_number("0")
    first = _check(engine, image, now=BASE_NOW, frame_id=1)
    assert first.hp_evidence == "zero"
    assert first.hp_current == 0
    assert first.hp_fill_ratio is None
    assert first.exit_authorized is False
    second = _check(engine, image, now=BASE_NOW + 0.5, frame_id=2)
    assert second.hp_evidence == "zero"
    assert second.hp_current == 0
    assert second.hp_fill_ratio is None
    assert second.exit_authorized is True


def test_other_profile_without_numeric_feature_keeps_legacy_bar_zero(
    tmp_path: Path,
) -> None:
    """未配置 hp_zero_feature 的旧 profile 保留条形 zero 兼容行为。"""
    hud = BattleHudConfig(hp_bar_region=BAR_REGION)
    engine = _make_engine(
        tmp_path,
        hud=hud,
        include_zero_feature=False,
        matcher=_NumericMatcher(has_zero=False),
    )
    image = _bar_image(empty=True)
    _check(engine, image, now=BASE_NOW, frame_id=1)
    second = _check(engine, image, now=BASE_NOW + 0.5, frame_id=2)
    assert second.hp_evidence == "zero"
    assert second.hp_current is None
    assert second.exit_authorized is True


def test_cn360_profile_loads_hp_zero_feature() -> None:
    """国服配置加载可选数字零特征：区域、阈值、模板文件与默认多尺度。"""
    app = load_project(PROJECT_ROOT, "cn360").config
    hud = app.profile.battle_hud
    assert hud.hp_zero_feature == FEATURE_ID
    feature = app.profile.feature(FEATURE_ID)
    assert feature is not None
    assert feature.region == NormalizedRect(0.075, 0.77, 0.17, 0.795)
    assert feature.threshold == pytest.approx(0.92)
    assert feature.scales == (0.95, 1.0, 1.05)
    assert feature.template_path is not None
    assert feature.template_path.is_file()
