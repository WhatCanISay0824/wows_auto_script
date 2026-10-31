"""ISS-002 血条归零证据与退出授权测试（实现补充文档 10.2）。

覆盖四层：
1. 颜色无关的填充列比例（合成样本）：绿/黄/红/橙满血、残血、空条、空条→存活死区、
   遮挡、区域缺失、每船血条模板禁用；
2. 退出授权：连续新帧确认（同帧去重）、证据 TTL、存活/未知矛盾证据撤销、
   帧缺失撤销；
3. 实机样本回归（本地 screenshots/ 存在时运行，缺失自动 skip；截图不入库）：
   - diagnostics/20261030_235258_death.png：铁公爵黄色 32 794/41 200 → alive；
   - 屏幕截图 2026-10-30 211346.png：列宁格勒满血绿 → alive；
   - 屏幕截图 2026-10-30 212021.png：列宁格勒 0 血红色空条 → zero（确认后授权）；
4. profile.yaml battle_hud 新字段的解析落地（默认值/区域）。

测试口径（评审 10.2）：``fill ratio = 填充列数 / 区域宽度``，填充像素 = 高饱和、
高亮度、色相在红橙黄绿区间的 UI 颜色；单列至少 3 个填充像素才算填充列。
空条上限 0.02、存活下限 0.10，两者之间 unknown（不授权）。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
from src.common.types import MatchResult, NormalizedRect, Rect, State
from src.config.loader import load_project, load_ship
from src.config.models import (
    AppConfig,
    BattleHudConfig,
    ClientProfile,
    FeatureDef,
    GlobalConfig,
    KeyMap,
    MinimapConfig,
    ShipProfile,
    WindowConfig,
)
from src.perception.capture import Frame
from src.perception.engine import PerceptionEngine

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 合成画面 600x400；血条区域归一化 x 0.10~0.50（240 列）、y 0.70~0.80（40 行）。
FRAME_W, FRAME_H = 600, 400
BAR_REGION = NormalizedRect(0.10, 0.70, 0.50, 0.80)
BATTLE_START = 0.0
BASE_NOW = 6.0  # 超过 perception.death.ignore_start_s（默认 5 秒）

# 实机 HP 条 UI 颜色（BGR）：满血绿 / 79.6% 黄 / 残血红 / 橙，HSV 均高饱和高亮。
HP_COLORS: dict[str, tuple[int, int, int]] = {
    "green": (21, 221, 156),  # HSV≈(80, 231, 221)
    "yellow": (0, 168, 255),  # HSV≈(20, 255, 255)
    "red": (0, 0, 220),  # HSV≈(0, 255, 220)
    "orange": (0, 90, 255),  # HSV≈(21, 255, 255)
}

SAMPLE_DEATH = Path("screenshots/diagnostics/20261030_235258_death.png")
SAMPLE_FULL_HP = Path("screenshots/屏幕截图 2026-10-30 211346.png")
SAMPLE_ZERO_HP = Path("screenshots/屏幕截图 2026-10-30 212021.png")


# ---------------------------------------------------------------------------
# 合成样本与引擎构造
# ---------------------------------------------------------------------------


def _bar_pixels() -> Rect:
    return BAR_REGION.to_pixels(FRAME_W, FRAME_H)


def _write_marker(tmp_path: Path) -> None:
    """ShipLocator 需要小地图标记模板；合成一个白色箭头（与感知逻辑无关）。"""
    marker = np.full((12, 12, 3), 40, dtype=np.uint8)
    polygon = np.array([[6, 1], [11, 10], [6, 8], [1, 10]], dtype=np.int32)
    cv2.fillPoly(marker, [polygon], (245, 245, 245))
    cv2.imwrite(str(tmp_path / "marker.png"), marker)


def _make_profile(
    tmp_path: Path,
    battle_hud: BattleHudConfig,
    *,
    hp_bar_feature: bool = True,
) -> ClientProfile:
    _write_marker(tmp_path)
    features: dict[str, FeatureDef] = {}
    if hp_bar_feature:
        features["hp_bar_present"] = FeatureDef(
            feature_id="hp_bar_present",
            template="hp_bar_present.png",  # 合成测试不加载模板，仅用其存在性启用血条证据
            region=battle_hud.hp_bar_region or BAR_REGION,
        )
    return ClientProfile(
        client_id="test",
        display_name="测试客户端",
        window=WindowConfig(),
        language="zh_cn",
        reference_resolution=(FRAME_W, FRAME_H),
        features=features,
        battle_hud=battle_hud,
        minimap=MinimapConfig(
            rect=NormalizedRect(0.70, 0.70, 0.98, 0.98),
            marker_template="marker.png",
            marker_hsv_lower=(0, 0, 200),
            marker_hsv_upper=(180, 60, 255),
        ),
    )


def _make_engine(
    tmp_path: Path,
    *,
    battle_hud: BattleHudConfig | None = None,
    hp_bar_feature: bool = True,
    capture: object | None = None,
    feature_matcher: object | None = None,
) -> PerceptionEngine:
    hud = battle_hud if battle_hud is not None else BattleHudConfig(hp_bar_region=BAR_REGION)
    profile = _make_profile(tmp_path, hud, hp_bar_feature=hp_bar_feature)
    config = AppConfig(
        global_config=GlobalConfig(),
        profile=profile,
        keymap=KeyMap(),
        ship=ShipProfile(ship_id="test", display_name="测试船"),
        maps={},
        project_root=tmp_path,
        config_hash="test",
    )
    engine = PerceptionEngine(
        config,
        capture=capture,  # type: ignore[arg-type]
        feature_matcher=feature_matcher,  # type: ignore[arg-type]
    )
    engine.begin_battle(BATTLE_START)
    return engine


class _StaticCapture:
    """按顺序返回预置帧的假截屏源（测试 observe 全链路，不触碰真实窗口）。"""

    def __init__(self, frames: list[Frame]) -> None:
        self._frames = frames
        self._index = 0

    def capture(self) -> Frame:
        frame = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        return frame

    def close(self) -> None:
        return None


class _FixedFeatureMatcher:
    """固定 hp_bar_present 命中的假特征匹配器（其余特征一律未命中）。"""

    def __init__(self, *, hp_bar_found: bool = True) -> None:
        self._hp_bar_found = hp_bar_found

    def _result(self, feature_id: str) -> MatchResult:
        found = self._hp_bar_found if feature_id == "hp_bar_present" else False
        return MatchResult(feature_id=feature_id, found=found, score=1.0 if found else 0.0)

    def set_frame(self, frame: Frame) -> Frame:
        return frame

    def match_many(self, feature_ids, frame: Frame | None = None) -> dict[str, MatchResult]:
        return {feature_id: self._result(feature_id) for feature_id in feature_ids}

    def match(self, feature_id: str, frame: Frame | None = None) -> MatchResult:
        return self._result(feature_id)

    def has_feature(self, feature_id: str) -> bool:
        return feature_id == "hp_bar_present"


def _frame(image: np.ndarray, frame_id: int = 1) -> Frame:
    height, width = image.shape[:2]
    return Frame(image=image, timestamp=float(frame_id), window_rect=Rect(0, 0, width, height), frame_id=frame_id)


def _bar_image(
    fill_fraction: float = 0.0,
    *,
    color: str = "green",
    empty: bool = False,
    occluded: bool = False,
) -> np.ndarray:
    """合成血条画面：深色底的舰体剪影 + 左侧按比例填充 HP 颜色。"""
    image = np.full((FRAME_H, FRAME_W, 3), (24, 26, 30), dtype=np.uint8)
    region = _bar_pixels()
    x1, y1, x2, y2 = region.x, region.y, region.right, region.bottom
    if occluded:
        # 整块区域被遮挡（画面元素盖住血条与舰名），模板同样无法命中。
        image[y1:y2, x1:x2] = (30, 30, 30)
        return image
    image[y1:y2, x1:x2] = (45, 45, 45)  # 血条深色底（空条部分）
    if empty:
        # 0 血：只剩暗红色剪影轮廓（V≈120 不会进填充掩码），加少量单像素噪点。
        cv2.rectangle(image, (x1, y1), (x2 - 1, y2 - 1), (10, 25, 120), 2)
        for px in range(x1 + 10, x2 - 10, 17):
            image[y1 + 1, px] = (0, 0, 220)
        return image
    filled_width = int(round((x2 - x1) * max(0.0, min(1.0, float(fill_fraction)))))
    if filled_width > 0:
        image[y1 + 3 : y2 - 3, x1 : x1 + filled_width] = HP_COLORS[color]
    return image


def _check(
    engine: PerceptionEngine,
    image: np.ndarray,
    *,
    now: float = BASE_NOW,
    frame_id: int = 1,
    hp_bar_found: bool = True,
):
    """按引擎消费方式调用 check_death：特征由测试直接给出（避免加载真实模板）。"""
    frame = _frame(image, frame_id)
    features = {
        "hp_bar_present": MatchResult(
            feature_id="hp_bar_present",
            found=hp_bar_found,
            score=1.0 if hp_bar_found else 0.0,
        )
    }
    return engine.check_death(now, frame, features, None)


# ---------------------------------------------------------------------------
# 1. 颜色无关的填充列比例
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("color", ["green", "yellow", "red", "orange"])
def test_full_bars_of_all_hp_colors_are_alive(tmp_path: Path, color: str) -> None:
    """绿/黄/红/橙满血都按“填充列比例”判存活，不再依赖绿色 HSV。"""
    engine = _make_engine(tmp_path)
    signals = _check(engine, _bar_image(1.0, color=color))
    assert signals.hp_evidence == "alive"
    assert signals.hp_fill_ratio == pytest.approx(1.0, abs=0.02)
    assert signals.hp_zero is False
    assert signals.exit_authorized is False


@pytest.mark.parametrize("fraction", [0.25, 0.5, 0.8])
def test_residual_bars_are_alive(tmp_path: Path, fraction: float) -> None:
    """残血（含红色残血）必须判存活；填充比例与画出的宽度一致。"""
    engine = _make_engine(tmp_path)
    signals = _check(engine, _bar_image(fraction, color="red"))
    assert signals.hp_evidence == "alive"
    assert signals.hp_fill_ratio == pytest.approx(fraction, abs=0.02)
    assert signals.exit_authorized is False


def test_empty_bar_is_zero_and_authorizes_after_confirm_frames(tmp_path: Path) -> None:
    """空血（暗红轮廓）判 zero；连续 2 个新帧后才授权提前退出。"""
    engine = _make_engine(tmp_path)
    image = _bar_image(empty=True)
    first = _check(engine, image, now=BASE_NOW, frame_id=1)
    assert first.hp_evidence == "zero"
    assert first.hp_fill_ratio is not None
    assert first.hp_fill_ratio <= 0.02
    assert first.exit_authorized is False  # 尚未满足连续帧
    assert first.evidence_at == 0.0
    second = _check(engine, image, now=BASE_NOW + 0.5, frame_id=2)
    assert second.exit_authorized is True
    assert second.hp_zero is True
    assert second.evidence_at == pytest.approx(BASE_NOW + 0.5)


def test_ratio_between_empty_and_alive_is_unknown(tmp_path: Path) -> None:
    """0.02~0.10 之间的死区无法判断：证据 unknown、不授权（保守等待自然结算）。"""
    engine = _make_engine(tmp_path)
    image = _bar_image(0.05)
    for frame_id, now in ((1, BASE_NOW), (2, BASE_NOW + 0.5), (3, BASE_NOW + 1.0)):
        signals = _check(engine, image, now=now, frame_id=frame_id)
        assert signals.hp_evidence == "unknown"
        assert signals.hp_fill_ratio == pytest.approx(0.05, abs=0.01)
        assert signals.exit_authorized is False


def test_missing_hp_bar_template_does_not_authorize(tmp_path: Path) -> None:
    """血条模板未命中（遮挡/其他界面）时即使填充为 0 也只给 unknown，绝不归零。"""
    engine = _make_engine(tmp_path)
    image = _bar_image(empty=True)
    for frame_id, now in ((1, BASE_NOW), (2, BASE_NOW + 0.5), (3, BASE_NOW + 1.0)):
        signals = _check(engine, image, now=now, frame_id=frame_id, hp_bar_found=False)
        assert signals.hp_evidence == "unknown"
        assert signals.exit_authorized is False
        assert signals.hp_zero is False


def test_occluded_region_is_unknown_not_zero(tmp_path: Path) -> None:
    """遮挡画面：区域内有像素但模板失配 → unknown，不能当作空条。"""
    engine = _make_engine(tmp_path)
    signals = _check(engine, _bar_image(occluded=True), hp_bar_found=False)
    assert signals.hp_evidence == "unknown"
    assert signals.hp_fill_ratio == pytest.approx(0.0, abs=0.01)
    assert signals.exit_authorized is False


def test_missing_region_is_unavailable(tmp_path: Path) -> None:
    """未配置 hp_bar_region：证据 unavailable、无填充比例、不授权。"""
    engine = _make_engine(tmp_path, battle_hud=BattleHudConfig())
    signals = _check(engine, _bar_image(empty=True))
    assert signals.hp_evidence == "unavailable"
    assert signals.hp_fill_ratio is None
    assert signals.exit_authorized is False


def test_ship_without_hp_bar_template_disables_evidence(tmp_path: Path) -> None:
    """船只没有 hp_bar.png 时禁用血条证据：任何画面都不授权提前退出。"""
    engine = _make_engine(tmp_path)
    engine.configure_ship_hp_bar(ShipProfile(ship_id="no_hp_bar", display_name="无血条模板船只"))
    signals = _check(engine, _bar_image(empty=True))
    assert signals.hp_evidence == "unavailable"
    assert signals.exit_authorized is False


# ---------------------------------------------------------------------------
# 2. 退出授权的时间语义（连续帧 / TTL / 矛盾证据）
# ---------------------------------------------------------------------------


def test_same_frame_repeated_does_not_confirm_zero(tmp_path: Path) -> None:
    """10Hz 主循环复用同一张帧：不能拿同一帧重复确认“连续帧”。"""
    engine = _make_engine(tmp_path)
    image = _bar_image(empty=True)
    for offset in (0.0, 0.1, 0.2):
        signals = _check(engine, image, now=BASE_NOW + offset, frame_id=1)
        assert signals.exit_authorized is False


def test_alive_frame_revokes_authorization(tmp_path: Path) -> None:
    """已授权后出现存活帧：立即撤销授权并回到 alive 证据。"""
    engine = _make_engine(tmp_path)
    empty = _bar_image(empty=True)
    _check(engine, empty, now=BASE_NOW, frame_id=1)
    authorized = _check(engine, empty, now=BASE_NOW + 0.5, frame_id=2)
    assert authorized.exit_authorized is True
    revoked = _check(engine, _bar_image(1.0), now=BASE_NOW + 1.0, frame_id=3)
    assert revoked.exit_authorized is False
    assert revoked.hp_evidence == "alive"
    assert revoked.evidence_at == 0.0


def test_evidence_expires_without_new_frames(tmp_path: Path) -> None:
    """没有新帧时归零证据超过 TTL 失效（过期帧不授权）。"""
    engine = _make_engine(tmp_path)
    image = _bar_image(empty=True)
    _check(engine, image, now=BASE_NOW, frame_id=1)
    _check(engine, image, now=BASE_NOW + 0.5, frame_id=2)
    expired = _check(engine, image, now=BASE_NOW + 2.0, frame_id=2)  # 同一帧，证据不刷新
    assert expired.exit_authorized is False
    refreshed = _check(engine, image, now=BASE_NOW + 2.1, frame_id=3)  # 新帧重新确认
    assert refreshed.exit_authorized is True


def test_capture_failure_revokes_authorization(tmp_path: Path) -> None:
    """帧缺失（截屏失败/窗口丢失）时血条证据不可用，必须撤销退出授权。"""
    engine = _make_engine(tmp_path)
    image = _bar_image(empty=True)
    _check(engine, image, now=BASE_NOW, frame_id=1)
    authorized = _check(engine, image, now=BASE_NOW + 0.5, frame_id=2)
    assert authorized.exit_authorized is True
    # capture=None 的引擎 observe 拿不到帧 → window_missing 分支也应刷新死亡证据。
    observation = engine.observe(now=BASE_NOW + 1.0)
    assert observation.window_missing is True
    assert observation.death is not None
    assert observation.death.hp_evidence == "unavailable"
    assert observation.death.exit_authorized is False


def test_observe_full_chain_authorizes_exit_after_confirmed_zero_frames(tmp_path: Path) -> None:
    """observe 全链路：连续两个新帧的空条画面 → Observation.death.exit_authorized。"""
    empty = _bar_image(empty=True)
    frames = [
        Frame(image=empty, timestamp=BASE_NOW, window_rect=Rect(0, 0, FRAME_W, FRAME_H), frame_id=1),
        Frame(
            image=empty,
            timestamp=BASE_NOW + 0.5,
            window_rect=Rect(0, 0, FRAME_W, FRAME_H),
            frame_id=2,
        ),
    ]
    capture = _StaticCapture(frames)
    engine = _make_engine(
        tmp_path,
        capture=capture,
        feature_matcher=_FixedFeatureMatcher(hp_bar_found=True),
    )
    first = engine.observe(State.BATTLE_ACTIVE, now=BASE_NOW)
    assert first.death is not None
    assert first.death.hp_evidence == "zero"
    assert first.death.exit_authorized is False
    second = engine.observe(State.BATTLE_ACTIVE, now=BASE_NOW + 0.5)
    assert second.death is not None
    assert second.death.exit_authorized is True
    assert second.frame_id == 2
    assert second.death.evidence_at == pytest.approx(BASE_NOW + 0.5)


def test_observe_full_chain_does_not_authorize_without_bar_template(tmp_path: Path) -> None:
    """observe 全链路：空条画面但 hp_bar_present 未命中（遮挡/换界面）→ unknown 不授权。"""
    empty = _bar_image(empty=True)
    frames = [
        Frame(image=empty, timestamp=BASE_NOW, window_rect=Rect(0, 0, FRAME_W, FRAME_H), frame_id=1),
        Frame(
            image=empty,
            timestamp=BASE_NOW + 0.5,
            window_rect=Rect(0, 0, FRAME_W, FRAME_H),
            frame_id=2,
        ),
    ]
    engine = _make_engine(
        tmp_path,
        capture=_StaticCapture(frames),
        feature_matcher=_FixedFeatureMatcher(hp_bar_found=False),
    )
    for now in (BASE_NOW, BASE_NOW + 0.5):
        observation = engine.observe(State.BATTLE_ACTIVE, now=now)
        assert observation.death is not None
        assert observation.death.hp_evidence == "unknown"
        assert observation.death.exit_authorized is False


# ---------------------------------------------------------------------------
# 3. 实机样本回归（截图不入库，缺失自动 skip）
# ---------------------------------------------------------------------------


def _sample_image(relative_path: Path) -> np.ndarray:
    image = cv2.imdecode(np.fromfile(str(PROJECT_ROOT / relative_path), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, f"无法读取实机截图: {relative_path}"
    return image


def _sample_frame(relative_path: Path, frame_id: int = 1) -> Frame:
    image = _sample_image(relative_path)
    height, width = image.shape[:2]
    return Frame(
        image=image,
        timestamp=float(frame_id),
        window_rect=Rect(0, 0, width, height),
        frame_id=frame_id,
    )


def _real_engine(ship_id: str) -> PerceptionEngine:
    app = load_project(PROJECT_ROOT, "cn360").config
    engine = PerceptionEngine(app, capture=None)
    ship = load_ship(PROJECT_ROOT / "profiles" / "cn360" / "ships" / ship_id / "ship.yaml")
    engine.configure_ship_hp_bar(ship)
    engine.begin_battle(BATTLE_START)
    return engine


@pytest.mark.skipif(not (PROJECT_ROOT / SAMPLE_DEATH).is_file(), reason="本地实机样本缺失")
def test_sample_iron_duke_yellow_bar_is_alive() -> None:
    """ISS-002 原始误判样本：铁公爵 32 794/41 200 黄色血条必须判存活、不授权退出。"""
    engine = _real_engine("iron_duke")
    signals = engine.check_death(BASE_NOW, _sample_frame(SAMPLE_DEATH), None, None)
    assert signals.hp_evidence == "alive"
    assert signals.hp_fill_ratio is not None
    assert signals.hp_fill_ratio > 0.5  # 实测 ≈0.82
    assert signals.hp_zero is False
    assert signals.exit_authorized is False


@pytest.mark.skipif(not (PROJECT_ROOT / SAMPLE_FULL_HP).is_file(), reason="本地实机样本缺失")
def test_sample_full_hp_green_bar_is_alive() -> None:
    """满血绿色血条（列宁格勒）必须判存活。"""
    engine = _real_engine("leningrad")
    signals = engine.check_death(BASE_NOW, _sample_frame(SAMPLE_FULL_HP), None, None)
    assert signals.hp_evidence == "alive"
    assert signals.hp_fill_ratio is not None
    assert signals.hp_fill_ratio > 0.5  # 实测 ≈0.71
    assert signals.exit_authorized is False


@pytest.mark.skipif(not (PROJECT_ROOT / SAMPLE_ZERO_HP).is_file(), reason="本地实机样本缺失")
def test_sample_zero_hp_red_bar_is_zero() -> None:
    """0 血红条（列宁格勒 0/15 200）：按空条=zero，连续两帧后授权提前退出。"""
    engine = _real_engine("leningrad")
    first = engine.check_death(BASE_NOW, _sample_frame(SAMPLE_ZERO_HP, frame_id=1), None, None)
    assert first.hp_evidence == "zero"
    assert first.hp_fill_ratio is not None
    assert first.hp_fill_ratio <= 0.02  # 实测 ≈0.007
    assert first.exit_authorized is False
    second = engine.check_death(
        BASE_NOW + 0.5, _sample_frame(SAMPLE_ZERO_HP, frame_id=2), None, None
    )
    assert second.hp_evidence == "zero"
    assert second.exit_authorized is True


# ---------------------------------------------------------------------------
# 4. 配置落地（评审 10.2 的 4 个 battle_hud 字段）
# ---------------------------------------------------------------------------


def test_project_profile_has_iss002_battle_hud_fields(project_root: Path) -> None:
    app = load_project(project_root, "cn360").config
    hud = app.profile.battle_hud
    assert hud.hp_bar_region == NormalizedRect(0.014, 0.737, 0.130, 0.7665)
    assert hud.hp_bar_empty_max_ratio == pytest.approx(0.02)
    assert hud.hp_bar_alive_min_ratio == pytest.approx(0.10)
    assert hud.hp_bar_confirm_frames == 2
    assert hud.hp_bar_evidence_ttl_s == pytest.approx(1.0)
