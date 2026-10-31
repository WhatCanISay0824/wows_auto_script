"""感知引擎：把单帧画面转换成结构化 Observation（详细设计 5.1 / B.1）。

职责边界（详细设计 2.3）：
- 只回答“画面上有什么”，不做状态决策；
- 不直接调用键鼠，不直接写数据库；
- 地图识别、舰船定位、死亡投票在这里组合成一次 Observation；
- 世界坐标换算只在本模块把 ``BattleCalibration`` 套到舰船位置上，坐标数学在 common.coords。

所有重量级识别都有节流：截屏约 2 Hz（复用最近一帧），地图识别按 map_refresh_s 刷新，
死亡信号复用本帧的特征结果。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import cv2
import numpy as np

from ..common.clock import Clock, SystemClock
from ..common.coords import BattleCalibration, CalibrationMode, expected_minimap_scale
from ..common.errors import CaptureUnavailable, WindowUnavailable
from ..common.types import (
    DeathSignals,
    MapMatch,
    MatchResult,
    Observation,
    Rect,
    ShipPosition,
    State,
    WindowInfo,
)
from ..config.models import AppConfig, BattleHudConfig, ClientProfile, MapPack, PerceptionConfig
from .capture import Frame, GdiCapture
from .death import DeathDetector
from .features import FeatureMatcher
from .map_identify import MapIdentifier
from .ship_locate import ShipLocator
from .state_features import merge_state_features

logger = logging.getLogger(__name__)

# 恢复流程 detect_state 的判定顺序：模态窗口优先，港口最后（详细设计 5.4）
_DETECT_PRIORITY: tuple[State, ...] = (
    State.POST_EXIT_INTERFACE,
    State.EXIT_CONFIRMING,
    State.EXIT_MENU_OPEN,
    State.DEAD_PENDING_EXIT,
    State.BATTLE_FINISHED,
    State.BATTLE_ACTIVE,
    State.LOADING,
    State.QUEUEING,
    State.PORT,
)

# 战斗内每帧需要额外检查的死亡/结算特征（详细设计 5.9 / 5.10）
_BATTLE_EXTRA_FEATURES: tuple[str, ...] = (
    "hp_bar_present",
    "death_banner",
    "battle_results",
    "result_panel",
    "spectator_indicator",
)

# ---------------------------------------------------------------------------
# ISS-002 / 评审 10.2：颜色无关的血条填充列比例
# ---------------------------------------------------------------------------
# HP 条 UI 用高饱和、高亮度的红/橙/黄/绿渲染（UI 层颜色，不受战场光照影响）；
# 空血时只剩暗红色的舰体轮廓，亮度明显偏低。判定：HSV 色相只保留红→黄→绿区间，
# S/V 取高下限；逐列统计填充像素，单列至少 _HP_FILL_COLUMN_MIN_PX 个才计入填充列，
# 滤掉轮廓/抗锯齿边缘。取值由 2026-10-30 三张实机样本标定（见 tests/test_hp_bar.py）：
# 满血绿色 fill≈0.70、79.6% 黄色 fill≈0.77、0 血红色空条 fill≈0.007。
_HP_FILL_SATURATION_MIN = 150
_HP_FILL_VALUE_MIN = 190
#: OpenCV 色相：红≈0、橙≈15、黄≈25、绿≈80；青/蓝（>90）不属于 HP 条颜色。
_HP_FILL_HUE_MAX = 90
#: 红色跨越 0 度时另一侧的色相。
_HP_FILL_HUE_RED_MIN = 160
#: 单列至少多少个填充像素才算“填充列”。
_HP_FILL_COLUMN_MIN_PX = 3
#: 血条区域最小尺寸（像素）；过小说明标定异常，按不可用处理。
_HP_BAR_MIN_REGION_WIDTH = 32
_HP_BAR_MIN_REGION_HEIGHT = 8


def _collect_sequence_features(profile: ClientProfile) -> tuple[str, ...]:
    """收集步骤序列 / 弹窗 / 退出配置引用的特征，供每帧 Observation 一并匹配。

    Observation.features 只包含当前状态的特征表，而 wait_feature 步骤可能等待
    状态特征表之外的特征（例如装备页的 ``loadout_signal_flags``），因此必须补齐。
    """
    feature_ids: set[str] = set()
    sequences = (
        profile.start_sequence,
        profile.start_sequence_full,
        profile.popup_sequence,
        profile.result_sequence,
        profile.ship_selection_sequence,
        profile.ship_check_sequence,
        profile.clear_loadout_sequence or (),
    )
    for sequence in sequences:
        for step in sequence:
            feature_id = getattr(step, "feature", None)
            if feature_id:
                feature_ids.add(str(feature_id))
    feature_ids.update(str(feature_id) for feature_id in profile.popup_features if feature_id)
    exit_config = profile.exit
    for feature_id in (
        exit_config.menu_open_feature,
        exit_config.return_port_feature,
        exit_config.confirm_feature,
        exit_config.direct_exit_feature,
    ):
        if feature_id:
            feature_ids.add(str(feature_id))
    return tuple(sorted(feature_ids))


@dataclass(frozen=True, slots=True)
class PerceptionStats:
    """感知耗时统计（详细设计 5.11）。"""

    capture_count: int
    capture_ms_avg: float
    frames: int
    last_frame_id: int
    map_identify_count: int


class PerceptionEngine:
    """感知引擎门面。"""

    def __init__(
        self,
        config: AppConfig,
        capture: GdiCapture | None,
        clock: Clock | None = None,
        *,
        feature_matcher: FeatureMatcher | None = None,
        map_identifier: MapIdentifier | None = None,
        ship_locator: ShipLocator | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.profile: ClientProfile = config.profile
        self.perception_config: PerceptionConfig = config.global_config.perception
        self._clock = clock or SystemClock()
        self._capture = capture
        self._logger = logger_ or logger
        self._feature_matcher = feature_matcher or FeatureMatcher.from_profile(
            self.profile,
            self.perception_config,
            project_root=config.project_root,
        )
        self._map_identifier = map_identifier or MapIdentifier(config.maps, self.perception_config)
        self._ship_locator = ship_locator or ShipLocator(
            self.profile,
            self.perception_config,
            project_root=config.project_root,
        )
        self._state_features = merge_state_features(self.profile.state_features)
        # 步骤序列/退出配置里引用的特征也必须每帧匹配，否则 wait_feature 永远等不到
        # （例如装备页的 loadout_signal_flags 不在 PORT 状态特征表里）
        self._extra_feature_ids = _collect_sequence_features(self.profile)
        self._state: State = State.INIT
        self._last_frame: Frame | None = None
        self._last_window: WindowInfo | None = None
        self._last_capture_error: str | None = None
        self._calibration: BattleCalibration | None = None
        self._last_map_match: MapMatch | None = None
        self._last_map_check = float("-inf")
        self._last_map_id: str | None = None
        self._death_detector: DeathDetector | None = None
        self._last_death: DeathSignals | None = None
        self._last_ship: ShipPosition | None = None
        self._battle_started_at: float | None = None
        self._capture_interval_s = 1.0 / max(0.5, float(config.global_config.runtime.capture_hz))
        self._capture_count = 0
        self._map_identify_count = 0
        self._minimap_expected_scale = config.global_config.map.expected_minimap_scale
        self._hp_bar_check_enabled = bool(self.profile.features.get("hp_bar_present"))
        # 以下显示/解析类可选：main 启动时按当前船只调用 configure_ship_hp_bar() 覆盖
        self._extras_configured = False

    # ------------------------------------------------------------------
    # 生命周期与状态
    # ------------------------------------------------------------------
    def set_state(self, state: State) -> None:
        self._state = state

    @property
    def state(self) -> State:
        return self._state

    @property
    def calibration(self) -> BattleCalibration | None:
        return self._calibration

    @property
    def last_map_match(self) -> MapMatch | None:
        return self._last_map_match

    @property
    def last_frame(self) -> Frame | None:
        return self._last_frame

    @property
    def feature_matcher(self) -> FeatureMatcher:
        return self._feature_matcher

    @property
    def last_death(self) -> DeathSignals | None:
        """最近一帧的死亡/血条证据结果（诊断与测试用）。"""
        return self._last_death

    def set_calibration(self, calibration: BattleCalibration | None) -> None:
        self._calibration = calibration

    def begin_battle(self, now: float) -> None:
        """进入战斗：重置死亡投票与舰船跟踪（详细设计 6.3）。"""
        self._battle_started_at = float(now)
        # 血条归零证据的连续帧/TTL 属于客户端 battle_hud 标定（评审 10.2）。
        self._death_detector = DeathDetector(
            self.perception_config.death,
            float(now),
            hud=self.profile.battle_hud,
        )
        self._last_death = None
        self._ship_locator.reset()
        self._calibration = None
        self._last_map_match = None
        self._last_map_check = float("-inf")
        self._last_map_id = None

    def end_battle(self) -> None:
        self._death_detector = None
        self._last_death = None
        self._battle_started_at = None
        self._calibration = None
        self._last_map_match = None
        self._ship_locator.reset()

    def close(self) -> None:
        if self._capture is not None:
            self._capture.close()

    # ------------------------------------------------------------------
    # 船只相关模板（每船一张血条模板，换船时由 main 调用覆盖）
    # ------------------------------------------------------------------
    def configure_ship_hp_bar(self, ship) -> None:
        """按当前船只覆盖“己方血条存在”模板。

        - 船只档案提供 ``hp_bar.png`` 时注册为该船专用模板（每船血条文字不同），
          该模板同时是“血条区域有效性”的判据（ISS-002 / 评审 10.2）；
        - 未提供时注销该特征并禁用该信号：血条归零证据变成 ``unavailable``，
          提前退出**不会**获得授权，只等自然结算/死亡提示（保守兜底）。
        """
        template_matcher = self._feature_matcher.template_matcher
        hp_bar_path = getattr(ship, "hp_bar_path", None)
        if hp_bar_path is None:
            if template_matcher.has_feature("hp_bar_present"):
                template_matcher.unregister_feature("hp_bar_present")
            self._hp_bar_check_enabled = False
            self._logger.info("当前船只未提供 hp_bar.png，禁用“血条消失”死亡信号（用死亡提示/结算兜底）")
            return
        feature = self.profile.feature("hp_bar_present")
        region = feature.region if feature is not None else None
        threshold = feature.threshold if feature is not None else 0.82
        try:
            template_matcher.register_feature(
                "hp_bar_present",
                hp_bar_path,
                region=region,
                threshold=threshold,
                replace=True,
            )
            self._hp_bar_check_enabled = True
            self._logger.info("已加载船只专用血条模板: %s", hp_bar_path)
        except Exception as exc:
            template_matcher.unregister_feature("hp_bar_present")
            self._hp_bar_check_enabled = False
            self._logger.warning("船只血条模板加载失败，禁用血条死亡信号: %s", exc)

    # ------------------------------------------------------------------
    # 截屏与特征
    # ------------------------------------------------------------------
    def _capture_frame(self, now: float) -> Frame:
        if self._capture is None:
            raise CaptureUnavailable("未配置截屏源")
        frame = self._capture.capture()
        self._capture_count += 1
        self._last_frame = frame
        self._last_window = WindowInfo(
            hwnd=0,
            title="",
            process_name="",
            client_rect=frame.window_rect,
            foreground=True,
            minimized=False,
            valid=True,
        )
        self._last_capture_error = None
        return frame

    def latest_frame(self, now: float | None = None, *, force: bool = False) -> Frame | None:
        """返回最近一帧；超过截屏间隔时重新截屏（详细设计 5.2）。"""
        now = self._clock.now() if now is None else float(now)
        if not force and self._last_frame is not None and now - self._last_frame.timestamp < self._capture_interval_s:
            return self._last_frame
        try:
            return self._capture_frame(now)
        except (CaptureUnavailable, WindowUnavailable) as exc:
            self._last_capture_error = str(exc)
            self._logger.debug("截屏失败: %s", exc)
            return None

    def match_feature(
        self,
        feature_id: str,
        frame: Frame | None = None,
        now: float | None = None,
    ) -> MatchResult:
        frame = frame or self.latest_frame(now)
        if frame is None:
            raise CaptureUnavailable(self._last_capture_error or "没有可用画面")
        return self._feature_matcher.match(feature_id, frame)

    def match_features(
        self,
        feature_ids: Sequence[str],
        frame: Frame | None = None,
        now: float | None = None,
    ) -> dict[str, MatchResult]:
        frame = frame or self.latest_frame(now)
        if frame is None:
            raise CaptureUnavailable(self._last_capture_error or "没有可用画面")
        return self._feature_matcher.match_many(feature_ids, frame)

    def register_feature(
        self,
        feature_id: str,
        template_path: str,
        region=None,
        threshold: float = 0.85,
    ) -> None:
        """运行时注册模板特征（ship_card / loadout_*，详细设计 6.8）。"""
        self._feature_matcher.template_matcher.register_feature(
            feature_id,
            template_path,
            region=region,
            threshold=threshold,
        )

    def has_feature(self, feature_id: str) -> bool:
        return self._feature_matcher.has_feature(feature_id)

    # ------------------------------------------------------------------
    # 小地图
    # ------------------------------------------------------------------
    def minimap_content_rect(self, frame: Frame | None = None) -> Rect | None:
        """按客户端配置得到小地图内容区（窗口像素坐标，已裁掉边框）。"""
        frame = frame or self._last_frame
        minimap = self.profile.minimap
        if frame is None or minimap is None:
            return None
        region = minimap.rect.to_pixels(frame.window_rect.w, frame.window_rect.h)
        border = max(0, int(minimap.border_px))
        width = region.w - border * 2
        height = region.h - border * 2
        if width <= 8 or height <= 8:
            return None
        return Rect(region.x + border, region.y + border, width, height)

    def minimap_image(self, frame: Frame | None = None) -> tuple[np.ndarray, Rect] | None:
        frame = frame or self._last_frame
        rect = self.minimap_content_rect(frame)
        if frame is None or rect is None:
            return None
        image = frame.crop(rect)
        if image is None or image.size == 0:
            return None
        return image, rect

    def _expected_scale(self, content_rect: Rect) -> float | None:
        if self._minimap_expected_scale is not None:
            return float(self._minimap_expected_scale)
        # 只有唯一地图（或强制指定）时才能推导期望缩放
        if self.config.global_config.map.forced_map_id:
            pack = self.config.maps.get(self.config.global_config.map.forced_map_id)
        elif len(self.config.maps) == 1:
            pack = next(iter(self.config.maps.values()))
        else:
            pack = None
        if pack is None:
            return None
        return expected_minimap_scale(content_rect, pack.transform())

    # ------------------------------------------------------------------
    # 地图识别
    # ------------------------------------------------------------------
    def identify_map(
        self,
        now: float | None = None,
        frame: Frame | None = None,
        *,
        force: bool = False,
    ) -> MapMatch | None:
        """识别当前地图并刷新标定；失败返回 None（详细设计 5.5）。"""
        now = self._clock.now() if now is None else float(now)
        refresh_s = float(self.perception_config.map_refresh_s)
        if not force and self._last_map_match is not None and now - self._last_map_check < refresh_s:
            return self._last_map_match
        frame = frame or self.latest_frame(now)
        if frame is None:
            return None
        mini = self.minimap_image(frame)
        if mini is None:
            self._logger.debug("无法裁剪小地图，跳过地图识别")
            return None
        minimap_image, content_rect = mini
        expected = self._expected_scale(content_rect)
        match = self._map_identifier.identify(minimap_image, expected_scale=expected, timestamp=now)
        self._last_map_check = now
        self._map_identify_count += 1
        if match is None:
            if self._last_map_match is not None:
                self._logger.warning("地图识别失败，保留上一帧结果 %s", self._last_map_match.map_id)
            else:
                self._logger.warning("地图识别失败：小地图 %dx%d", content_rect.w, content_rect.h)
            return self._last_map_match
        previous = self._last_map_match
        if previous is not None and not force and not self._map_identifier.should_refresh(previous, match):
            # 刷新滞回：新结果没有明显优于旧结果时保留旧标定，避免导航点击跳变
            self._logger.debug("地图识别刷新被滞回拒绝：旧 %s / 新 %s", previous.map_id, match.map_id)
            return previous
        if self._last_map_id is not None and self._last_map_id != match.map_id:
            self._logger.warning("地图识别结果变化：%s -> %s", self._last_map_id, match.map_id)
        pack = self.config.maps.get(match.map_id)
        if pack is not None:
            self._check_declared_orientation(pack, match)
        self._last_map_match = match
        self._last_map_id = match.map_id
        self._calibration = self._build_calibration(frame, content_rect, match)
        return match

    def _check_declared_orientation(self, pack: MapPack, match: MapMatch) -> None:
        """校验识别方向与 map.yaml 声明方向（只告警，不覆盖声明方向）。"""
        try:
            from .map_identify import detected_orientation

            detected = detected_orientation(match)
        except Exception:  # pragma: no cover - 兼容旧实现
            return
        if detected is not None and detected != pack.image_orientation:
            self._logger.warning(
                "地图 %s 的识别方向 %s 与 map.yaml 声明 %s 不一致，请重新标定（以声明方向参与坐标换算）",
                pack.map_id,
                detected.value,
                pack.image_orientation.value,
            )

    def _build_calibration(self, frame: Frame, content_rect: Rect, match: MapMatch) -> BattleCalibration | None:
        pack: MapPack | None = self.config.maps.get(match.map_id)
        if pack is None:
            return None
        mode = _calibration_mode(self.config)
        if mode is CalibrationMode.FULL_MAP:
            calibration = BattleCalibration(
                world_image=pack.transform(),
                minimap_rect=content_rect,
                mode=CalibrationMode.FULL_MAP,
                window_rect=frame.window_rect,
            )
        else:
            calibration = BattleCalibration(
                world_image=pack.transform(),
                minimap_rect=content_rect,
                mode=CalibrationMode.MATCH,
                window_rect=frame.window_rect,
                image_to_minimap_matrix=match.affine,  # type: ignore[arg-type]
            )
        # 舰船定位的速度单位换算：世界宽度 / 小地图内容区宽度（米/像素）
        world_width = float(pack.world_bounds[2] - pack.world_bounds[0])
        if content_rect.w > 0 and world_width > 0:
            try:
                self._ship_locator.set_meters_per_pixel(world_width / float(content_rect.w))
            except AttributeError:  # pragma: no cover - 兼容旧定位器
                pass
        return calibration

    def force_map(self, map_id: str, now: float | None = None) -> MapMatch | None:
        """调试/标定用：强制指定地图并构建整图模式标定，跳过 ORB 识别。"""
        now = self._clock.now() if now is None else float(now)
        pack = self.config.maps.get(map_id)
        if pack is None:
            return None
        frame = self.latest_frame(now)
        content_rect = self.minimap_content_rect(frame)
        if frame is None or content_rect is None:
            return None
        self._calibration = BattleCalibration(
            world_image=pack.transform(),
            minimap_rect=content_rect,
            mode=CalibrationMode.FULL_MAP,
            window_rect=frame.window_rect,
        )
        match = MapMatch(
            map_id=map_id,
            inliers=0,
            score=1.0,
            affine=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            reprojection_error=0.0,
            timestamp=now,
        )
        world_width = float(pack.world_bounds[2] - pack.world_bounds[0])
        if content_rect.w > 0 and world_width > 0:
            try:
                self._ship_locator.set_meters_per_pixel(world_width / float(content_rect.w))
            except AttributeError:  # pragma: no cover
                pass
        self._last_map_match = match
        self._last_map_id = map_id
        self._last_map_check = now
        return match

    # ------------------------------------------------------------------
    # 舰船定位
    # ------------------------------------------------------------------
    def locate_own_ship(self, now: float | None = None, frame: Frame | None = None) -> ShipPosition | None:
        """定位自身舰船；世界坐标由当前标定换算（详细设计 5.7）。"""
        now = self._clock.now() if now is None else float(now)
        frame = frame or self.latest_frame(now)
        if frame is None:
            return None
        mini = self.minimap_image(frame)
        if mini is None:
            return None
        minimap_image, content_rect = mini
        position = self._ship_locator.locate(minimap_image, now)
        if position is None:
            self._last_ship = None
            return None
        world_xy = position.world_xy
        if world_xy is None and self._calibration is not None:
            window_px = (content_rect.x + position.minimap_px[0], content_rect.y + position.minimap_px[1])
            try:
                world_xy = self._calibration.minimap_to_world(*window_px)
            except ValueError:
                world_xy = None
        if world_xy is None and self._calibration is None:
            self._last_ship = position
            return position
        result = ShipPosition(
            minimap_px=position.minimap_px,
            world_xy=world_xy,
            heading_deg=position.heading_deg,
            speed_mps=position.speed_mps,
            confidence=position.confidence,
            source=position.source,
        )
        self._last_ship = result
        return result

    # ------------------------------------------------------------------
    # 死亡判定
    # ------------------------------------------------------------------
    def _hp_bar_region(self, frame: Frame) -> Rect | None:
        """己方血条区域（整条血条，含空条底色）的窗口像素矩形。"""
        hud = self.profile.battle_hud
        if hud.hp_bar_region is None:
            return None
        region = hud.hp_bar_region.to_pixels(frame.window_rect.w, frame.window_rect.h)
        if region.w < _HP_BAR_MIN_REGION_WIDTH or region.h < _HP_BAR_MIN_REGION_HEIGHT:
            return None
        return region

    def _hp_bar_fill_ratio(self, frame: Frame, region: Rect | None = None) -> float | None:
        """颜色无关的血条填充列比例（ISS-002 / 评审 10.2）。

        在 ``battle_hud.hp_bar_region``（整条血条区域）内逐列统计“填充像素”：
        高饱和度、高亮度的红/橙/黄/绿都算填充，深色底与空血暗红轮廓不算；
        ``fill ratio = 填充列数 / 区域宽度``，与具体血量颜色无关。
        """
        region = region or self._hp_bar_region(frame)
        if region is None:
            return None
        image = frame.crop(region)
        if image is None or image.size == 0:
            return None
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        hue = hsv[:, :, 0]
        saturation = hsv[:, :, 1]
        value = hsv[:, :, 2]
        hue_ok = (hue <= _HP_FILL_HUE_MAX) | (hue >= _HP_FILL_HUE_RED_MIN)
        fill = hue_ok & (saturation >= _HP_FILL_SATURATION_MIN) & (value >= _HP_FILL_VALUE_MIN)
        column_counts = np.count_nonzero(fill, axis=0)
        width = int(column_counts.size)
        if width <= 0:
            return None
        filled_columns = int(np.count_nonzero(column_counts >= _HP_FILL_COLUMN_MIN_PX))
        return filled_columns / float(width)

    def _hp_green_ratio(self, frame: Frame, region: Rect | None = None) -> float | None:
        """旧绿色 HSV 范围降级为辅助证据（只写日志，不再单独代表血量）。"""
        hud = self.profile.battle_hud
        if hud.hp_fill_hsv_lower is None or hud.hp_fill_hsv_upper is None:
            return None
        region = region or self._hp_bar_region(frame)
        if region is None:
            return None
        image = frame.crop(region)
        if image is None or image.size == 0:
            return None
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        lower = np.array(hud.hp_fill_hsv_lower, dtype=np.uint8)
        upper = np.array(hud.hp_fill_hsv_upper, dtype=np.uint8)
        mask = cv2.inRange(hsv, lower, upper)
        return float(np.count_nonzero(mask)) / float(mask.size)

    def _hp_evidence(
        self,
        frame: Frame | None,
        features: Mapping[str, MatchResult],
        hud: BattleHudConfig,
    ) -> tuple[float | None, str, bool]:
        """汇总血条归零证据：返回 (fill ratio, evidence 等级, hp_bar_present 是否命中)。

        区域有效性判据（评审 10.2）：只认 ``hp_bar_present`` 模板命中（当前船只的
        ``ships/<id>/hp_bar.png``，由 ``configure_ship_hp_bar`` 注册）。模板缺失/未命中
        （遮挡、结算界面、观战切换）时一律不给 zero 证据——否则“没看到血条”会被误判
        成“血条归零”，违反 ISS-002 硬约束。
        """
        # 与旧实现保持一致：模板启用且 profile 声明了该特征，才参与血条判读。
        hp_bar_enabled = bool(self._hp_bar_check_enabled and "hp_bar_present" in self.profile.features)
        hp_bar_result = features.get("hp_bar_present")
        if hp_bar_result is None and hp_bar_enabled and frame is not None:
            # 调用方只传了部分特征时补一次匹配（check_death 也可能被外部单独调用）。
            hp_bar_result = self._feature_matcher.match("hp_bar_present", frame)
        hp_bar_found = bool(hp_bar_result and hp_bar_result.found)
        region = self._hp_bar_region(frame) if (frame is not None and hp_bar_enabled) else None
        fill_ratio = self._hp_bar_fill_ratio(frame, region) if region is not None else None
        if fill_ratio is None:
            return None, "unavailable", hp_bar_found
        if not hp_bar_found:
            # 血条区域有像素但无法确认是己方血条（遮挡/其他界面）：不判读，绝不归零。
            return fill_ratio, "unknown", hp_bar_found
        empty_max = float(hud.hp_bar_empty_max_ratio)
        alive_min = float(hud.hp_bar_alive_min_ratio)
        if fill_ratio <= empty_max:
            return fill_ratio, "zero", hp_bar_found
        if fill_ratio >= alive_min:
            return fill_ratio, "alive", hp_bar_found
        # 空条上限与存活下限之间：无法可靠判断，保守等待自然结算。
        return fill_ratio, "unknown", hp_bar_found

    def check_death(
        self,
        now: float | None = None,
        frame: Frame | None = None,
        features: Mapping[str, MatchResult] | None = None,
        ship: ShipPosition | None = None,
    ) -> DeathSignals:
        """汇总死亡信号与血条归零证据并交给投票器（详细设计 5.9 / 评审 10.2）。"""
        now = self._clock.now() if now is None else float(now)
        empty = DeathSignals(False, False, False, False, False, score=0.0, stable_ms=0, timestamp=now)
        if self._death_detector is None:
            return empty
        frame = frame or self._last_frame
        if features is None:
            ids = self._battle_feature_ids(set())
            features = self._feature_matcher.match_many(ids, frame) if frame is not None else {}
        hud = self.profile.battle_hud
        hp_fill_ratio, hp_evidence, hp_bar_found = self._hp_evidence(frame, features, hud)
        hp_bar_feature = bool(self._hp_bar_check_enabled and "hp_bar_present" in self.profile.features)
        hp_bar_absent = bool(hp_bar_feature and not hp_bar_found)
        banner = bool(features.get(hud.death_banner_feature) and features[hud.death_banner_feature].found)
        spectator = any(
            bool(features.get(feature_id) and features[feature_id].found) for feature_id in hud.spectator_features
        )
        if ship is None:
            ship = self._last_ship
        marker_lost = ship is None or ship.source == "lost"
        if frame is not None and self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(
                "血条证据: fill=%s evidence=%s hp_bar_present=%s green_aux=%s",
                "无" if hp_fill_ratio is None else f"{hp_fill_ratio:.4f}",
                hp_evidence,
                hp_bar_found,
                self._hp_green_ratio(frame),
            )
        votes = {
            "hp_bar_absent": hp_bar_absent,
            "hp_zero": hp_evidence == "zero",
            "banner": banner,
            "marker_lost": marker_lost,
            "spectator_ui": spectator,
        }
        signals = self._death_detector.update(
            votes,
            now,
            hp_fill_ratio=hp_fill_ratio,
            hp_evidence=hp_evidence,
            frame_id=frame.frame_id if frame is not None else None,
        )
        self._last_death = signals
        return signals

    # ------------------------------------------------------------------
    # 状态识别
    # ------------------------------------------------------------------
    def detect_state(self, now: float | None = None, frame: Frame | None = None) -> State | None:
        """恢复流程用：按优先级识别当前界面（详细设计 5.4 / 6.4）。"""
        now = self._clock.now() if now is None else float(now)
        frame = frame or self.latest_frame(now)
        if frame is None:
            return None
        return self._feature_matcher.detect_state(frame, priority=_DETECT_PRIORITY)

    # ------------------------------------------------------------------
    # 观察结果
    # ------------------------------------------------------------------
    def _feature_ids_for(self, state: State) -> tuple[str, ...]:
        ids = tuple(self._state_features.get(state.value, ()))
        ids = tuple(dict.fromkeys(ids + self._extra_feature_ids))
        if state.is_battle:
            ids = tuple(dict.fromkeys(ids + _BATTLE_EXTRA_FEATURES))
        return ids

    def _battle_feature_ids(self, _unused: set[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_BATTLE_EXTRA_FEATURES))

    def observe(self, state: State | None = None, now: float | None = None) -> Observation:
        """产生一帧 Observation；截屏失败时返回 window_missing/capture_error。"""
        now = self._clock.now() if now is None else float(now)
        current = state or self._state
        frame = self.latest_frame(now)
        if frame is None:
            window_rect = self._last_window.client_rect.to_tuple() if self._last_window else (0, 0, 0, 0)
            # 帧缺失时血条证据变成 unavailable：必须让投票器撤销尚未执行的退出授权，
            # 不能沿用上一帧的“确认归零”证据（ISS-002：过期帧不授权）。
            death = self.check_death(now, None, {}) if self._death_detector is not None else None
            return Observation(
                timestamp=now,
                window_rect=window_rect,
                death=death,
                window_missing=True,
                capture_error=self._last_capture_error,
                frame_id=self._last_frame.frame_id if self._last_frame else 0,
                frame_timestamp=self._last_frame.timestamp if self._last_frame else 0.0,
            )
        self._feature_matcher.set_frame(frame)
        ids = self._feature_ids_for(current)
        features = self._feature_matcher.match_many(ids, frame) if ids else {}
        # 死亡/结算特征即使当前状态表没列也一并匹配，保证 BATTLE_ACTIVE 判定完整
        extra = [feature_id for feature_id in _BATTLE_EXTRA_FEATURES if feature_id not in features]
        if extra and current.is_battle:
            features.update(self._feature_matcher.match_many(extra, frame))
        ship = self.locate_own_ship(now, frame)
        death = self.check_death(now, frame, features, ship) if self._death_detector is not None else None
        return Observation(
            timestamp=now,
            window_rect=frame.window_rect.to_tuple(),
            features=features,
            state_guess=None,
            map_match=self._last_map_match,
            ship=ship,
            death=death,
            window_missing=False,
            capture_error=None,
            frame_id=frame.frame_id,
            frame_timestamp=frame.timestamp,
        )

    # ------------------------------------------------------------------
    def stats(self) -> PerceptionStats:
        stats = self._capture.stats() if self._capture is not None else None
        capture_ms = float(getattr(stats, "avg_ms", 0.0)) if stats is not None else 0.0
        frame_id = self._last_frame.frame_id if self._last_frame else 0
        return PerceptionStats(
            capture_count=self._capture_count,
            capture_ms_avg=capture_ms,
            frames=frame_id,
            last_frame_id=frame_id,
            map_identify_count=self._map_identify_count,
        )


def _calibration_mode(config: AppConfig) -> CalibrationMode:
    value = str(getattr(config.global_config.navigation, "calibration_mode", "match") or "match").lower()
    return CalibrationMode.FULL_MAP if value == "full_map" else CalibrationMode.MATCH
