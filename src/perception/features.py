"""按状态管理界面特征匹配（详细设计 5.4）。

``FeatureMatcher`` 在 ``TemplateMatcher`` 之上提供三项职责：

1. **状态 → 特征表**：默认表来自详细设计 5.4，``profile.state_features`` 可覆盖；
2. **批量匹配**：按当前状态只匹配该状态需要的特征（匹配区域小、速度快）；
3. **连续命中确认**：记录每个特征的连续命中帧数，连续 ``feature_confirm_frames``
   帧命中才算“确认”，避免动画/渐变造成状态抖动；同一帧重复匹配只计一次。

最近一帧会被缓存，``match`` 等方法可省略 ``frame`` 参数复用缓存帧，方便状态机在
10 Hz 主循环里复用 2 Hz 的截屏帧。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType

from ..common.errors import ValidationIssue
from ..common.types import MatchResult, State
from ..config.models import ClientProfile, PerceptionConfig
from .capture import Frame
from .state_features import DEFAULT_STATE_PRIORITY, merge_state_features
from .templates import TemplateMatcher

_log = logging.getLogger(__name__)


def _state_key(state: State | str) -> str:
    return state.value if isinstance(state, State) else str(state)


def _infer_project_root(profile: ClientProfile) -> Path | None:
    """从 ``profiles/<client_id>/`` 推断项目根目录，供项目根相对路径解析。"""
    if profile.profile_dir is None:
        return None
    try:
        candidate = Path(profile.profile_dir).resolve().parent.parent
    except OSError:  # pragma: no cover - 路径异常时保持 None
        return None
    return candidate if candidate.exists() else None


class FeatureMatcher:
    """界面特征匹配与状态特征表管理（详细设计 5.4）。

    典型用法::

        features = FeatureMatcher(profile, cfg)
        features.set_frame(frame)
        results = features.match_state(State.BATTLE_ACTIVE)
        if features.confirmed("battle_hud_indicator"):
            ...
        state = features.detect_state()   # 异常恢复时按优先级整体判断

    ``template_matcher`` 可注入（便于测试或与其它模块共享模板缓存）；注入时由调用方
    负责已调用 ``load_profile``。
    """

    def __init__(
        self,
        profile: ClientProfile,
        cfg: PerceptionConfig | None = None,
        *,
        template_matcher: TemplateMatcher | None = None,
        project_root: Path | None = None,
    ) -> None:
        self.profile = profile
        self._cfg = cfg or PerceptionConfig()
        owns_matcher = template_matcher is None
        root = project_root if project_root is not None else _infer_project_root(profile)
        self._templates = template_matcher or TemplateMatcher(self._cfg, project_root=root)
        if owns_matcher:
            self._templates.load_profile(profile)
        self._state_features: dict[str, tuple[str, ...]] = merge_state_features(profile.state_features)
        self._confirm_frames = max(1, int(self._cfg.feature_confirm_frames))
        self._streaks: dict[str, int] = {}
        self._streak_timestamps: dict[str, float] = {}
        self._frame: Frame | None = None

    @classmethod
    def from_profile(
        cls,
        profile: ClientProfile,
        cfg: PerceptionConfig | None = None,
        **kwargs: object,
    ) -> FeatureMatcher:
        """与构造函数等价的显式工厂方法。"""
        return cls(profile, cfg, **kwargs)  # type: ignore[arg-type]

    # --- 查询 ---------------------------------------------------------
    @property
    def template_matcher(self) -> TemplateMatcher:
        return self._templates

    @property
    def load_issues(self) -> tuple[ValidationIssue, ...]:
        return self._templates.load_issues

    @property
    def state_features(self) -> Mapping[str, tuple[str, ...]]:
        """合并后的状态 → 特征表（只读视图）。"""
        return MappingProxyType(dict(self._state_features))

    @property
    def confirm_frames(self) -> int:
        return self._confirm_frames

    @property
    def cached_frame(self) -> Frame | None:
        return self._frame

    def state_feature_ids(self, state: State | str) -> tuple[str, ...]:
        """返回某个状态需要的特征 id（第一个是主判定）。"""
        return self._state_features.get(_state_key(state), ())

    def available_features(self) -> tuple[str, ...]:
        """返回当前已成功加载的特征 id。"""
        return self._templates.feature_ids()

    def has_feature(self, feature_id: str) -> bool:
        return self._templates.has_feature(feature_id)

    # --- 帧缓存 -------------------------------------------------------
    def set_frame(self, frame: Frame) -> Frame:
        """缓存最近一帧，并同步给模板匹配器。"""
        if not isinstance(frame, Frame):
            raise TypeError(f"FeatureMatcher 只接受 Frame: {type(frame)!r}")
        self._frame = frame
        self._templates.set_frame(frame)
        return frame

    def clear_frame(self) -> None:
        self._frame = None

    def _resolve_frame(self, frame: Frame | None) -> Frame:
        if frame is not None:
            return self.set_frame(frame)
        if self._frame is None:
            raise ValueError("尚无可用帧：请先调用 set_frame() 或在 match 时传入 frame")
        return self._frame

    # --- 匹配 ---------------------------------------------------------
    def match(self, feature_id: str, frame: Frame | None = None) -> MatchResult:
        """匹配单个特征，并推进该特征的连续命中计数。"""
        resolved = self._resolve_frame(frame)
        result = self._templates.match(feature_id, resolved)
        self._advance(feature_id, result.found, resolved.timestamp)
        return result

    def match_many(self, feature_ids: Sequence[str], frame: Frame | None = None) -> dict[str, MatchResult]:
        """批量匹配并推进每个特征的连续命中计数。"""
        resolved = self._resolve_frame(frame)
        results = self._templates.match_many(feature_ids, resolved)
        for feature_id, result in results.items():
            self._advance(feature_id, result.found, resolved.timestamp)
        return results

    def match_state(self, state: State | str, frame: Frame | None = None) -> dict[str, MatchResult]:
        """按状态特征表批量匹配；未加载的特征返回 found=False。"""
        return self.match_many(self.state_feature_ids(state), frame=frame)

    # --- 连续命中确认 -------------------------------------------------
    def _advance(self, feature_id: str, found: bool, timestamp: float) -> None:
        # 同一帧被多个调用方重复匹配时只计一次，保证“连续两帧”语义
        if self._streak_timestamps.get(feature_id) == timestamp:
            return
        self._streak_timestamps[feature_id] = timestamp
        streak = self._streaks.get(feature_id, 0)
        self._streaks[feature_id] = streak + 1 if found else 0

    def hit_streak(self, feature_id: str) -> int:
        return self._streaks.get(feature_id, 0)

    def confirmed(self, feature_id: str) -> bool:
        """连续命中帧数达到 ``feature_confirm_frames`` 时返回 True。"""
        return self._streaks.get(feature_id, 0) >= self._confirm_frames

    def reset_confirmations(self) -> None:
        self._streaks.clear()
        self._streak_timestamps.clear()

    def reset(self) -> None:
        """清空连续命中计数与缓存帧（状态切换/重连时调用）。"""
        self.reset_confirmations()
        self._frame = None

    # --- 整体状态判断 -------------------------------------------------
    def detect_state(
        self,
        frame: Frame | None = None,
        *,
        priority: Sequence[State | str] | None = None,
    ) -> State | None:
        """异常恢复用的整体状态判断（详细设计 5.4）。

        按优先级逐状态检查其全部特征（主判定在前），第一个命中的特征所属状态即为
        判断结果；只检查已加载的特征，未知/未标定特征直接跳过。
        """
        resolved = self._resolve_frame(frame)
        order = priority if priority is not None else DEFAULT_STATE_PRIORITY
        for item in order:
            state = _try_parse_state(item)
            if state is None:
                continue
            for feature_id in self.state_feature_ids(item):
                if not self._templates.has_feature(feature_id):
                    continue
                result = self.match(feature_id, resolved)
                if result.found:
                    return state
        return None


def _try_parse_state(value: State | str) -> State | None:
    if isinstance(value, State):
        return value
    try:
        return State(str(value))
    except ValueError:
        return None
