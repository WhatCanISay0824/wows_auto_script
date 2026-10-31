"""选船校验层（详细设计 6.8 / 实现补充 10.7，修复 ISS-007）。

会话固定使用一艘船。进入港口后本模块负责“确认当前选中的就是配置船只”：

1. 先用 ``selected_feature``（目标船选中态模板）确认当前是否已选中；命中则直接 OK，
   避免每场重复点击（ISS-007 不再把“卡片可见”当作已选中）；
2. 未命中时在轮播条当前可见区域匹配 ``ship_card``；找到唯一可信候选后点击；
3. 点击后必须等待至少 ``confirm_frames`` 个新 ``frame_id``，再用 ``selected_feature``
   确认“当前已经选中目标船”；重复点击有次数上限；
4. 当前可见区域没有目标卡时执行 ``ship_selection_sequence`` 打开列表（空序列只表示
   “无需打开列表”，**不取消**滚动预算）：先反向滚到画面不再变化、可确认的列表起点，
   再按配置方向扫描；受 ``max_scrolls`` 与 ``selection_timeout_s`` 双约束；起点无法
   确认时改为双向覆盖（预算对半），避免“目标在初始位置左侧就直接报不存在”；
5. 出现多个可信候选、端点/超时/预算用尽仍找不到、或点击后无法确认已选中时返回
   FAILED 并写明原因，交由状态机进入 PAUSED；不猜选、不点击其它船只。

本模块只负责“选船确认”，不执行装备页/信号旗步骤（由状态机的 ``ship_check_sequence``
阶段完成）。``tick`` 是非阻塞的：每次只推进一小步并返回要提交的动作。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from enum import StrEnum

import numpy as np

from ..common.types import Action, MatchResult, Observation, Rect
from ..config.models import AppConfig, ShipProfile
from ..perception.engine import PerceptionEngine
from ..state_machine.steps import StepSequenceExecutor, StepStatus

logger = logging.getLogger(__name__)

SHIP_CARD_FEATURE = "ship_card"
# 目标船“已选中状态”模板的运行期特征 ID（由 ship.yaml 的 selected_feature 注册）
SHIP_SELECTED_FEATURE = "ship_selected"

# 画面签名的采样网格：只用于判断“滚动后画面是否变化”，属于算法内部常数而非业务参数；
# 可调阈值在 ship.yaml 的 endpoint_diff_ratio，采样密度不暴露给配置。
_SIGNATURE_SAMPLES_X = 64
_SIGNATURE_SAMPLES_Y = 24


class ShipCheckStatus(StrEnum):
    CHECKING = "checking"  # 正在匹配已选中证据/目标船卡
    SELECTING = "selecting"  # 正在打开列表、滚动查找、点击或等待新帧确认
    OK = "ok"
    FAILED = "failed"


class _Phase(StrEnum):
    """选船查找内部阶段（不对外暴露；对外只用 :class:`ShipCheckStatus`）。"""

    CHECK = "check"  # 初始检查：当前可见区域是否已选中/是否能找到目标卡
    OPEN = "open"  # 执行 ship_selection_sequence 打开船只列表
    POSITION = "position"  # 反向滚动到可确认的列表起点
    SCAN = "scan"  # 按配置方向扫描
    REVERSE = "reverse"  # 起点无法确认时的反向补扫（双向覆盖）


@dataclass(frozen=True, slots=True)
class SlotCheck:
    """保留给旧接口/诊断的空装备槽结果（ISS-007 后由状态机装备页序列校验）。"""

    slot_id: str
    name: str
    empty_found: bool
    score: float


@dataclass(frozen=True, slots=True)
class ShipCheckResult:
    ok: bool
    reason: str
    card_score: float = 0.0
    slots: tuple[SlotCheck, ...] = ()
    card_found: bool = False
    selected_found: bool = False
    selected_score: float = 0.0

    @property
    def failed_slots(self) -> tuple[SlotCheck, ...]:
        return tuple(slot for slot in self.slots if not slot.empty_found)


class ShipChecker:
    """船只已选中确认 + 轮播条查找（非阻塞，ISS-007）。"""

    def __init__(
        self,
        config: AppConfig,
        engine: PerceptionEngine,
        steps: StepSequenceExecutor,
        *,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.ship: ShipProfile = config.ship
        self.engine = engine
        self.steps = steps
        self._logger = logger_ or logger
        self._verify_each_battle = bool(config.global_config.ship.verify_each_battle)
        self._registered = False
        self._evidence_error = ""
        self._card_error = ""
        self._last_result: ShipCheckResult | None = None
        self._failure_reason = ""
        # --- 查找/确认状态 ---
        self._status = ShipCheckStatus.CHECKING
        self._phase = _Phase.CHECK
        self._selection_started = False
        self._selection_started_at = 0.0
        self._scan_remaining = 0
        self._reverse_remaining = 0
        self._position_remaining = 0
        self._start_confirmed = False
        self._awaiting_scroll = False
        self._scroll_issued_frame_id = 0
        self._last_scroll_at = 0.0
        self._pre_scroll_signature: np.ndarray | None = None
        self._endpoint_hits = 0
        self._confirm_pending = False
        self._confirm_clicked_at = 0.0
        self._confirm_click_frame_id = 0
        self._click_count = 0
        self._probe_frame_id: int | None = None
        self._probe_selected: MatchResult | None = None
        self._probe_card: MatchResult | None = None
        self._candidates_frame_id: int | None = None
        self._candidates: tuple[MatchResult, ...] = ()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def status(self) -> ShipCheckStatus:
        return self._status

    @property
    def last_result(self) -> ShipCheckResult | None:
        return self._last_result

    @property
    def failure_reason(self) -> str:
        return self._failure_reason

    @property
    def verify_each_battle(self) -> bool:
        return self._verify_each_battle

    @property
    def selection_started(self) -> bool:
        return self._selection_started

    def reset(self) -> None:
        """每场开始前重置查找/确认状态（模板注册状态保留）。"""
        self._status = ShipCheckStatus.CHECKING
        self._phase = _Phase.CHECK
        self._selection_started = False
        self._selection_started_at = 0.0
        self._scan_remaining = 0
        self._reverse_remaining = 0
        self._position_remaining = 0
        self._start_confirmed = False
        self._awaiting_scroll = False
        self._scroll_issued_frame_id = 0
        self._last_scroll_at = 0.0
        self._pre_scroll_signature = None
        self._endpoint_hits = 0
        self._confirm_pending = False
        self._confirm_clicked_at = 0.0
        self._confirm_click_frame_id = 0
        self._click_count = 0
        self._probe_frame_id = None
        self._probe_selected = None
        self._probe_card = None
        self._candidates_frame_id = None
        self._candidates = ()
        self._last_result = None
        self._failure_reason = ""

    # ------------------------------------------------------------------
    # 模板注册
    # ------------------------------------------------------------------
    def register_features(self) -> None:
        """注册目标船卡与选中态模板（幂等）。

        选中态模板缺失/未配置时只记录原因，由 :meth:`tick` 保守返回 FAILED；
        不会把卡片可见当作已选中，也不抛异常给主循环。
        """
        if self._registered:
            return
        ship = self.ship
        if ship.card_path is None:
            self._card_error = "船只卡片模板缺失（card_template），无法在轮播条查找目标船"
            self._logger.warning(self._card_error)
        else:
            try:
                self.engine.register_feature(
                    SHIP_CARD_FEATURE,
                    str(ship.card_path),
                    region=ship.card_search_region,
                    threshold=0.85,
                )
            except Exception as exc:  # 配置/模板错误不抛给主循环，转 FAILED
                self._card_error = f"船只卡片模板注册失败: {exc}"
                self._logger.warning(self._card_error)
        if not ship.selected_feature:
            self._evidence_error = (
                "未配置选中态证据 selected_feature：无法确认当前是否已选中目标船；"
                "请用标定工具补充选中态模板（selected.png）后重试，不会把“卡片可见”当作已选中"
            )
            self._logger.warning(self._evidence_error)
        elif ship.selected_path is None:
            self._evidence_error = (
                f"选中态模板缺失: {ship.selected_feature}（待实机标定）；"
                "请先补该模板后重试，不会把“卡片可见”当作已选中"
            )
            self._logger.warning(self._evidence_error)
        else:
            try:
                self.engine.register_feature(
                    SHIP_SELECTED_FEATURE,
                    str(ship.selected_path),
                    region=ship.selected_region,
                    threshold=ship.selected_threshold,
                )
            except Exception as exc:
                self._evidence_error = f"选中态模板注册失败: {exc}"
                self._logger.warning(self._evidence_error)
        self._registered = True

    # ------------------------------------------------------------------
    # 一次性校验（诊断/轻量调用；状态机主流程用 tick）
    # ------------------------------------------------------------------
    def verify(self, now: float | None = None) -> ShipCheckResult:
        """确认当前画面已选中配置船只；不做滚动或点击。"""
        del now
        self.register_features()
        if self._evidence_error:
            result = ShipCheckResult(False, self._evidence_error)
        else:
            selected, card = self._probe(None, force=True)
            if selected.found:
                result = self._make_result(True, "", card, selected)
            elif card.found:
                result = self._make_result(
                    False, "仅看到目标船卡片、未确认已选中（selected_feature 未命中）", card, selected
                )
            else:
                result = self._make_result(False, "未确认目标船已选中，也未找到目标船卡片", card, selected)
        self._last_result = result
        return result

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def tick(
        self,
        now: float,
        observation: Observation,
        *,
        allow_selection: bool = True,
    ) -> tuple[ShipCheckStatus, list[Action]]:
        """推进选船校验；返回 (状态, 待提交动作)。"""
        if self._status in (ShipCheckStatus.OK, ShipCheckStatus.FAILED):
            return self._status, []
        self.register_features()
        now = float(now)
        if self._evidence_error:
            return self._fail(self._evidence_error)
        if self._confirm_pending:
            return self._advance_confirm(now, observation)
        if self._selection_started and now - self._selection_started_at > self.ship.selection_timeout_s:
            return self._fail(
                f"选船查找超时（{self.ship.selection_timeout_s:.1f}s）仍未确认目标船已选中："
                "请检查筛选条件、卡片模板与选中态模板标定"
            )
        selected, card = self._probe(observation)
        self._last_result = self._make_result(selected.found, "", card, selected)
        if selected.found:
            self._status = ShipCheckStatus.OK
            self._logger.info("已确认当前已选中目标船：%s", self.ship.display_name)
            return self._status, []
        if not allow_selection:
            return self._fail("当前未确认已选中目标船，且当前场景不允许执行选船操作")
        if self._card_error:
            return self._fail(self._card_error)
        if self._awaiting_scroll:
            return self._advance_scroll_wait(now, observation)
        if self._phase is not _Phase.OPEN:
            # 优先在当前可见区域找目标卡；卡片可见只作为点击候选，不能当作已选中。
            candidates = self._match_candidates(observation, card)
            if len(candidates) > 1:
                detail = "、".join(
                    f"center={match.center} score={match.score:.2f}" for match in candidates
                )
                return self._fail(
                    f"同一画面出现 {len(candidates)} 个相似可信的船只卡片候选（{detail}）："
                    "不猜选，请缩小卡片模板或重新标定后再试"
                )
            if len(candidates) == 1:
                return self._click_candidate(now, observation, candidates[0])
        if self._phase is _Phase.CHECK:
            self._start_selection(now)
            if self._status is ShipCheckStatus.FAILED:
                return self._status, []
        if self._phase is _Phase.OPEN:
            return self._advance_open(now, observation)
        return self._advance_search(now, observation)

    # ------------------------------------------------------------------
    # 已选中确认（点击后）
    # ------------------------------------------------------------------
    def _advance_confirm(
        self, now: float, observation: Observation
    ) -> tuple[ShipCheckStatus, list[Action]]:
        elapsed = now - self._confirm_clicked_at
        fid = int(observation.frame_id)
        required = max(1, int(self.ship.confirm_frames))
        if fid > 0:
            ready = fid >= self._confirm_click_frame_id + required
        else:
            # 没有帧序号（截屏不可用/旧接口）时按时间保守等待，避免用点击前的旧画面确认
            ready = elapsed >= required * max(0.05, float(self.ship.scroll_settle_s))
        if not ready:
            if elapsed >= self.ship.confirm_timeout_s:
                return self._confirm_failed(f"等待 {required} 个新画面超时")
            return ShipCheckStatus.SELECTING, []
        if elapsed >= self.ship.confirm_timeout_s:
            return self._confirm_failed("点击后确认超时")
        selected, card = self._probe(observation, force=True)
        self._last_result = self._make_result(selected.found, "", card, selected)
        if selected.found:
            self._confirm_pending = False
            self._status = ShipCheckStatus.OK
            self._logger.info("点击后已确认选中目标船：%s", self.ship.display_name)
            return self._status, []
        if card.found and self._click_count < max(1, int(self.ship.max_click_attempts)):
            self._logger.warning(
                "点击后新画面仍未出现已选中证据，重试点击（第 %d 次）", self._click_count + 1
            )
            return self._click_candidate(now, observation, card)
        detail = "目标船卡已不可见" if not card.found else f"已达重复点击上限 {self.ship.max_click_attempts} 次"
        return self._confirm_failed(detail)

    def _confirm_failed(self, detail: str) -> tuple[ShipCheckStatus, list[Action]]:
        self._confirm_pending = False
        reason = (
            f"点击目标船卡后仍无法确认已选中（{detail}）：selected_feature="
            f"{self.ship.selected_feature!r} 未命中；请补做/重做选中态模板标定后重试，"
            "脚本不会把“卡片可见”当作已选中"
        )
        return self._fail(reason)

    def _click_candidate(
        self, now: float, observation: Observation, match: MatchResult
    ) -> tuple[ShipCheckStatus, list[Action]]:
        if self._click_count >= max(1, int(self.ship.max_click_attempts)):
            return self._confirm_failed(f"已达重复点击上限 {self.ship.max_click_attempts} 次")
        action = self._click_center(match, observation, label="ship_select_card")
        if action is None:
            return self._fail("目标船卡片匹配结果缺少中心坐标，无法点击")
        self._click_count += 1
        self._confirm_pending = True
        self._confirm_clicked_at = now
        self._confirm_click_frame_id = int(observation.frame_id)
        self._status = ShipCheckStatus.SELECTING
        self._logger.info("点击目标船卡（第 %d 次）：%s", self._click_count, self.ship.display_name)
        return self._status, [action]

    # ------------------------------------------------------------------
    # 列表打开与滚动查找
    # ------------------------------------------------------------------
    def _start_selection(self, now: float) -> None:
        """开始选船查找。

        ISS-007：空 ``ship_selection_sequence`` 只表示“无需打开列表”，
        **不得**把滚动预算清零——仍按 ``max_scrolls`` 执行滚动查找。
        """
        self._selection_started = True
        self._selection_started_at = now
        self._click_count = 0
        self._start_confirmed = False
        self._endpoint_hits = 0
        self._scan_remaining = 0
        self._reverse_remaining = 0
        self._position_remaining = max(0, int(self.ship.max_position_scrolls))
        sequence = self.config.profile.ship_selection_sequence
        if sequence:
            try:
                self.steps.start(sequence, label="ship_select")
            except Exception as exc:
                self._fail(f"船只选择序列启动失败: {exc}")
                return
            self._phase = _Phase.OPEN
            self._logger.info("开始执行 ship_selection_sequence 打开船只列表")
            return
        self._logger.info(
            "ship_selection_sequence 为空：无需打开列表，直接在轮播条查找（滚动预算 %d 次）",
            self.ship.max_scrolls,
        )
        self._begin_after_open()

    def _begin_after_open(self) -> None:
        if self.ship.cover_list_start and int(self.ship.max_position_scrolls) > 0:
            self._phase = _Phase.POSITION
            self._logger.info("先反向滚动定位船只列表起点，再按配置方向扫描")
            return
        self._begin_scan(start_confirmed=False)

    def _begin_scan(self, *, start_confirmed: bool) -> None:
        total = max(1, int(self.ship.max_scrolls))
        self._start_confirmed = start_confirmed
        if start_confirmed:
            self._scan_remaining = total
            self._reverse_remaining = 0
        else:
            # 起点无法确认：预算对半，先按配置方向、再反向覆盖，避免漏掉初始位置另一侧。
            forward = max(1, total // 2)
            self._scan_remaining = min(forward, total)
            self._reverse_remaining = total - self._scan_remaining
        self._phase = _Phase.SCAN
        self._endpoint_hits = 0
        self._logger.info(
            "开始扫描船只列表：正向预算 %d 次，反向预算 %d 次（起点已确认=%s）",
            self._scan_remaining,
            self._reverse_remaining,
            start_confirmed,
        )

    def _advance_open(
        self, now: float, observation: Observation
    ) -> tuple[ShipCheckStatus, list[Action]]:
        status, actions = self.steps.advance(now, observation)
        if status is StepStatus.FAILED:
            last_error = getattr(self.steps, "last_error", None)
            return self._fail(f"船只选择序列失败，无法打开船只列表: {last_error}")
        if status is StepStatus.RUNNING:
            return ShipCheckStatus.SELECTING, actions
        self._begin_after_open()
        return ShipCheckStatus.SELECTING, actions

    def _advance_search(
        self, now: float, observation: Observation
    ) -> tuple[ShipCheckStatus, list[Action]]:
        if self._awaiting_scroll:
            return self._advance_scroll_wait(now, observation)
        phase = self._phase
        if phase is _Phase.POSITION:
            if self._position_remaining <= 0:
                self._logger.warning(
                    "定位 %d 次后画面仍在变化，无法确认列表起点；改为双向覆盖查找",
                    self.ship.max_position_scrolls,
                )
                self._begin_scan(start_confirmed=False)
                return ShipCheckStatus.SELECTING, []
            self._position_remaining -= 1
            return self._issue_scroll(now, observation, forward=False, label="ship_list_scroll_position")
        if phase is _Phase.SCAN:
            if self._scan_remaining <= 0:
                return self._fail(
                    f"船只列表滚动查找 {self.ship.max_scrolls} 次（max_scrolls）仍未找到目标船卡："
                    "请检查筛选条件、卡片模板与舰船是否可用"
                )
            self._scan_remaining -= 1
            return self._issue_scroll(now, observation, forward=True, label="ship_list_scroll")
        if phase is _Phase.REVERSE:
            if self._reverse_remaining <= 0:
                return self._fail(
                    f"双向滚动查找仍未找到目标船卡（max_scrolls={self.ship.max_scrolls}）："
                    "请检查筛选条件、卡片模板与舰船是否可用"
                )
            self._reverse_remaining -= 1
            return self._issue_scroll(now, observation, forward=False, label="ship_list_scroll_reverse")
        return ShipCheckStatus.SELECTING, []

    def _issue_scroll(
        self,
        now: float,
        observation: Observation,
        *,
        forward: bool,
        label: str,
    ) -> tuple[ShipCheckStatus, list[Action]]:
        # 记录滚动前画面签名：滚动动作执行后与下一帧比较，判断列表是否已到端点。
        self._pre_scroll_signature = self._current_signature(now)
        self._awaiting_scroll = True
        self._scroll_issued_frame_id = int(observation.frame_id)
        self._last_scroll_at = now
        actions: list[Action] = []
        move = self._move_cursor_to_list(observation)
        if move is not None:
            actions.append(move)
        actions.append(Action.wheel(self._wheel_delta(forward), label=label))
        return ShipCheckStatus.SELECTING, actions

    def _advance_scroll_wait(
        self, now: float, observation: Observation
    ) -> tuple[ShipCheckStatus, list[Action]]:
        if now - self._last_scroll_at < max(0.0, float(self.ship.scroll_settle_s)):
            return ShipCheckStatus.SELECTING, []
        fid = int(observation.frame_id)
        if fid > 0 and fid <= self._scroll_issued_frame_id:
            # 还没有滚动后的新画面，继续等待（避免按滚动前画面重复判断/点击）
            return ShipCheckStatus.SELECTING, []
        self._awaiting_scroll = False
        current = self._current_signature(now)
        same = self._views_same(self._pre_scroll_signature, current)
        self._pre_scroll_signature = current
        if same is True:
            self._endpoint_hits += 1
        elif same is False:
            self._endpoint_hits = 0
        if self._endpoint_hits >= max(1, int(self.ship.endpoint_stable_scrolls)):
            return self._handle_endpoint()
        return ShipCheckStatus.SELECTING, []

    def _handle_endpoint(self) -> tuple[ShipCheckStatus, list[Action]]:
        self._endpoint_hits = 0
        phase = self._phase
        if phase is _Phase.POSITION:
            self._logger.info("已到达船只列表起点（滚动前后画面不再变化）")
            self._begin_scan(start_confirmed=True)
            return ShipCheckStatus.SELECTING, []
        if phase is _Phase.SCAN:
            if self._start_confirmed or self._reverse_remaining <= 0:
                return self._fail(
                    "已滚动覆盖到船只列表端点（滚动后画面不再变化）仍未找到目标船卡："
                    "请检查筛选条件、卡片模板与舰船是否可用"
                )
            self._logger.info("按配置方向已到列表端点（起点未确认），反向继续覆盖查找")
            self._phase = _Phase.REVERSE
            self._endpoint_hits = 0
            return ShipCheckStatus.SELECTING, []
        if phase is _Phase.REVERSE:
            return self._fail(
                "双向滚动查找已覆盖到列表端点仍未找到目标船卡：请检查筛选条件、卡片模板与舰船是否可用"
            )
        return ShipCheckStatus.SELECTING, []

    def _wheel_delta(self, forward: bool) -> int:
        clicks = max(1, int(self.ship.scroll_clicks))
        direction = self.ship.scroll_direction.lower()
        delta = -clicks if direction in ("down", "south", "next") else clicks
        return delta if forward else -delta

    # ------------------------------------------------------------------
    # 画面签名（端点判定：滚动后画面不再变化）
    # ------------------------------------------------------------------
    def _current_signature(self, now: float) -> np.ndarray | None:
        latest = getattr(self.engine, "latest_frame", None)
        if not callable(latest):
            return None
        try:
            frame = latest(now)
        except Exception:
            self._logger.debug("获取滚动判定画面失败", exc_info=True)
            return None
        image = getattr(frame, "image", None)
        if image is None:
            return None
        try:
            height, width = image.shape[:2]
            region = self.ship.card_search_region
            if region is not None:
                rect = region.to_pixels(width, height)
                crop = image[rect.y : rect.bottom, rect.x : rect.right]
            else:
                crop = image
            if crop.size == 0:
                return None
            gray = crop if crop.ndim == 2 else crop[..., :3].mean(axis=2)
            rows = min(_SIGNATURE_SAMPLES_Y, int(gray.shape[0]))
            cols = min(_SIGNATURE_SAMPLES_X, int(gray.shape[1]))
            ys = np.linspace(0, gray.shape[0] - 1, num=rows, dtype=int)
            xs = np.linspace(0, gray.shape[1] - 1, num=cols, dtype=int)
            sample = np.asarray(gray[np.ix_(ys, xs)], dtype=np.float32)
            return sample if sample.size else None
        except Exception:
            self._logger.debug("计算滚动画面签名失败", exc_info=True)
            return None

    def _views_same(self, previous: np.ndarray | None, current: np.ndarray | None) -> bool | None:
        """返回 True=画面未变化、False=已变化、None=无法判断（缺画面/尺寸不一致）。"""
        if previous is None or current is None or previous.shape != current.shape:
            return None
        mean_diff = float(np.abs(previous - current).mean()) / 255.0
        return mean_diff <= max(0.0, float(self.ship.endpoint_diff_ratio))

    # ------------------------------------------------------------------
    # 候选匹配与点击
    # ------------------------------------------------------------------
    def _match_candidates(
        self, observation: Observation, card: MatchResult
    ) -> tuple[MatchResult, ...]:
        """返回当前帧去重后的可信卡片候选。

        感知层当前只返回最佳匹配；若引擎提供 ``match_feature_candidates``
        （多候选扩展接口），优先使用它，出现多个可信候选时由调用方暂停。
        """
        fid = int(observation.frame_id)
        if fid > 0 and fid == self._candidates_frame_id:
            return self._candidates
        raw: list[MatchResult] = []
        hook = getattr(self.engine, "match_feature_candidates", None)
        if callable(hook):
            try:
                results = hook(SHIP_CARD_FEATURE) or ()
                raw = [
                    match
                    for match in results
                    if getattr(match, "found", False) and match.center is not None
                ]
            except Exception as exc:
                self._logger.debug("多候选匹配接口失败，回退单候选: %s", exc)
                raw = []
        if not raw and card.found and card.center is not None:
            raw = [card]
        merge_px = max(0, int(self.ship.candidate_merge_px))
        merged: list[MatchResult] = []
        for match in sorted(raw, key=lambda item: item.score, reverse=True):
            if any(_center_distance(match, other) <= merge_px for other in merged):
                continue
            merged.append(match)
        self._candidates_frame_id = fid
        self._candidates = tuple(merged)
        return self._candidates

    def _probe(
        self, observation: Observation | None, *, force: bool = False
    ) -> tuple[MatchResult, MatchResult]:
        """按帧缓存匹配“已选中证据 + 目标船卡”，避免 10Hz 主循环重复做模板匹配。"""
        fid = int(observation.frame_id) if observation is not None else 0
        if not force and fid > 0 and fid == self._probe_frame_id:
            selected = self._probe_selected or _not_found(SHIP_SELECTED_FEATURE)
            card = self._probe_card or _not_found(SHIP_CARD_FEATURE)
            return selected, card
        selected = self._safe_match(SHIP_SELECTED_FEATURE)
        card = (
            _not_found(SHIP_CARD_FEATURE)
            if self._card_error
            else self._safe_match(SHIP_CARD_FEATURE)
        )
        self._probe_frame_id = fid
        self._probe_selected = selected
        self._probe_card = card
        return selected, card

    def _safe_match(self, feature_id: str) -> MatchResult:
        try:
            return self.engine.match_feature(feature_id)
        except Exception as exc:  # 无画面/无特征
            self._logger.debug("特征 %s 匹配失败: %s", feature_id, exc)
            return _not_found(feature_id)

    @staticmethod
    def _make_result(
        ok: bool, reason: str, card: MatchResult, selected: MatchResult
    ) -> ShipCheckResult:
        return ShipCheckResult(
            ok=ok,
            reason=reason,
            card_score=card.score,
            card_found=card.found,
            selected_found=selected.found,
            selected_score=selected.score,
        )

    @staticmethod
    def _click_center(
        match: MatchResult, observation: Observation, *, label: str
    ) -> Action | None:
        if match.center is None:
            return None
        window = Rect(*observation.window_rect)
        screen_x = window.x + int(match.center[0])
        screen_y = window.y + int(match.center[1])
        return Action.click_abs(screen_x, screen_y, label=label)

    def _move_cursor_to_list(self, observation: Observation) -> Action | None:
        """先把光标移到船只列表区域，保证滚轮作用在列表上（评审 P1-4 / T2 建议）。"""
        region = self.ship.card_search_region
        wx = observation.window_rect
        if wx[2] <= 0 or wx[3] <= 0:
            return None
        if region is not None:
            center_x = (region.x1 + region.x2) / 2.0
            center_y = (region.y1 + region.y2) / 2.0
        else:
            center_x, center_y = 0.20, 0.55
        screen_x = wx[0] + int(center_x * wx[2])
        screen_y = wx[1] + int(center_y * wx[3])
        return Action.move_abs(screen_x, screen_y, label="ship_list_cursor")

    # ------------------------------------------------------------------
    def _fail(self, reason: str) -> tuple[ShipCheckStatus, list[Action]]:
        self._status = ShipCheckStatus.FAILED
        self._failure_reason = reason
        if self._last_result is not None:
            self._last_result = replace(self._last_result, ok=False, reason=reason)
        else:
            self._last_result = ShipCheckResult(False, reason)
        self._logger.warning("选船校验失败: %s", reason)
        return self._status, []

    def select_default_region(self) -> None:
        """占位：保留给标定工具/后续扩展。"""


def _not_found(feature_id: str) -> MatchResult:
    return MatchResult(feature_id=feature_id, found=False, score=0.0)


def _center_distance(first: MatchResult, second: MatchResult) -> float:
    if first.center is None or second.center is None:
        return math.inf
    return math.hypot(first.center[0] - second.center[0], first.center[1] - second.center[1])
