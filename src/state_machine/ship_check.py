"""船只与空装备校验（详细设计 6.8）。

会话固定使用一艘船，不使用信号旗与经济加成卡。进入港口后：
1. 确认当前选中的是配置船只（卡片模板匹配）；
2. 确认所有信号旗槽与经济加成槽为空（空槽模板匹配）；
3. 不匹配时执行 ``ship_selection_sequence`` 打开船只列表，再按配置方向滚动查找并点击目标卡片；
4. 任何一项仍失败则进入 PAUSED，不猜测、不点列表里的其他船只。

本模块是非阻塞的：``tick(now, observation)`` 由状态机每轮推进，返回当前状态与要提交的动作。
模板通过 :class:`PerceptionEngine` 在运行时注册为 ``ship_card`` / ``loadout_<slot_id>`` 特征。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from ..common.types import Action, MatchResult, Observation, Rect
from ..config.models import AppConfig, LoadoutSlotDef, ShipProfile
from ..perception.engine import PerceptionEngine
from ..state_machine.steps import StepSequenceExecutor, StepStatus

logger = logging.getLogger(__name__)

SHIP_CARD_FEATURE = "ship_card"
LOADOUT_FEATURE_PREFIX = "loadout_"


class ShipCheckStatus(StrEnum):
    CHECKING = "checking"  # 正在匹配卡片/空槽
    SELECTING = "selecting"  # 正在船只列表中选择
    OK = "ok"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SlotCheck:
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

    @property
    def failed_slots(self) -> tuple[SlotCheck, ...]:
        return tuple(slot for slot in self.slots if not slot.empty_found)


@dataclass
class _ScrollState:
    remaining: int = 0
    last_scroll_at: float = 0.0


class ShipChecker:
    """船只卡片 + 空装备校验器（非阻塞）。"""

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
        self._status = ShipCheckStatus.CHECKING
        self._registered = False
        self._selection_started = False
        self._selection_done = False
        self._scroll = _ScrollState()
        self._clicked_at = 0.0
        self._last_result: ShipCheckResult | None = None
        self._failure_reason = ""
        self._verify_each_battle = bool(config.global_config.ship.verify_each_battle)

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

    def reset(self) -> None:
        """每场开始前重置（轻量校验开关见 ship.verify_each_battle）。"""
        self._status = ShipCheckStatus.CHECKING
        self._selection_started = False
        self._selection_done = False
        self._scroll = _ScrollState()
        self._clicked_at = 0.0
        self._last_result = None
        self._failure_reason = ""

    @property
    def verify_each_battle(self) -> bool:
        return self._verify_each_battle

    # ------------------------------------------------------------------
    def register_features(self) -> None:
        """把船只卡片与空槽模板注册进感知特征表（幂等）。"""
        if self._registered:
            return
        ship = self.ship
        if ship.card_path is not None:
            try:
                self.engine.register_feature(
                    SHIP_CARD_FEATURE,
                    str(ship.card_path),
                    region=ship.card_search_region,
                    threshold=0.85,
                )
            except Exception as exc:  # 配置/模板错误不抛给主循环，转 FAILED
                self._failure_reason = f"船只卡片模板注册失败: {exc}"
                self._logger.warning(self._failure_reason)
        for slot in self._loadout_slots():
            if slot.template_path is None:
                self._failure_reason = f"空装备槽模板缺失: {slot.slot_id}"
                continue
            try:
                self.engine.register_feature(
                    _loadout_feature_id(slot),
                    str(slot.template_path),
                    region=slot.region,
                    threshold=0.85,
                )
            except Exception as exc:
                self._logger.warning("空装备槽 %s 模板注册失败: %s", slot.slot_id, exc)
        self._registered = True

    def _loadout_slots(self) -> tuple[LoadoutSlotDef, ...]:
        return tuple(self.ship.loadout_slots)

    # ------------------------------------------------------------------
    def verify(self, now: float | None = None) -> ShipCheckResult:
        """一次性校验当前画面（详细设计 6.8 的轻量校验）。"""
        self.register_features()
        card = self._safe_match(SHIP_CARD_FEATURE)
        slot_checks: list[SlotCheck] = []
        for slot in self._loadout_slots():
            if slot.template_path is None:
                slot_checks.append(SlotCheck(slot.slot_id, slot.name, False, 0.0))
                continue
            result = self._safe_match(_loadout_feature_id(slot))
            slot_checks.append(SlotCheck(slot.slot_id, slot.name, result.found, result.score))
        card_ok = card.found
        slots_ok = all(item.empty_found for item in slot_checks)
        if card_ok and slots_ok:
            result = ShipCheckResult(True, "", card.score, tuple(slot_checks), card_found=True)
        elif not card_ok:
            result = ShipCheckResult(
                False, "当前船只不是配置船只（卡片未命中）", card.score, tuple(slot_checks), card_found=False
            )
        else:
            failed = ", ".join(slot.name for slot in slot_checks if not slot.empty_found)
            result = ShipCheckResult(False, f"装备槽非空: {failed}", card.score, tuple(slot_checks), card_found=True)
        self._last_result = result
        return result

    def _safe_match(self, feature_id: str) -> MatchResult:
        try:
            return self.engine.match_feature(feature_id)
        except Exception as exc:  # 无画面/无特征
            self._logger.debug("特征 %s 匹配失败: %s", feature_id, exc)
            return MatchResult(feature_id=feature_id, found=False, score=0.0)

    # ------------------------------------------------------------------
    def tick(
        self,
        now: float,
        observation: Observation,
        *,
        allow_selection: bool = True,
    ) -> tuple[ShipCheckStatus, list[Action]]:
        """推进校验流程；返回 (状态, 待提交动作)。"""
        if self._status in (ShipCheckStatus.OK, ShipCheckStatus.FAILED):
            return self._status, []
        if not self._registered:
            self.register_features()
        fresh = self.verify(now)
        if fresh.ok:
            self._status = ShipCheckStatus.OK
            return self._status, []
        actions: list[Action] = []
        steps_status, step_actions = self.steps.advance(now, observation)
        actions.extend(step_actions)
        # 尝试空装备预设（详细设计 6.8：有预设时优先选择预设）
        preset = self.ship.loadout_preset_feature
        if preset and not fresh.ok and self._card_found(fresh):
            preset_action = self._click_feature_center(preset, observation)
            if preset_action is not None:
                actions.append(preset_action)
                self._status = ShipCheckStatus.CHECKING
                return self._status, actions
        if self._card_found(fresh):
            # 卡片已找到，只差空槽：没有预设可点则失败
            if self.ship.loadout_slots:
                self._status = ShipCheckStatus.FAILED
                self._failure_reason = fresh.reason
            else:
                self._status = ShipCheckStatus.OK
            return self._status, actions
        # 卡片未命中：进入船只列表选择
        if not allow_selection:
            self._status = ShipCheckStatus.FAILED
            self._failure_reason = fresh.reason
            return self._status, actions
        if not self._selection_started:
            self._start_selection()
        if steps_status is StepStatus.FAILED:
            self._status = ShipCheckStatus.FAILED
            self._failure_reason = "船只选择序列失败"
            return self._status, actions
        if steps_status is StepStatus.RUNNING:
            return ShipCheckStatus.SELECTING, actions
        # 选择序列完成（船只列表已打开）：滚动查找卡片
        actions.extend(self._scroll_search(now, observation))
        return ShipCheckStatus.SELECTING, actions

    def _start_selection(self) -> None:
        self._selection_started = True
        self._selection_done = False
        self._scroll = _ScrollState(remaining=max(0, int(self.ship.max_scrolls)))
        sequence = self.config.profile.ship_selection_sequence
        if sequence:
            try:
                self.steps.start(sequence, label="ship_select")
            except Exception as exc:
                self._logger.warning("船只选择序列启动失败: %s", exc)
                self._scroll.remaining = 0
        else:
            self._scroll.remaining = 0

    @property
    def selection_started(self) -> bool:
        return self._selection_started

    def _scroll_search(self, now: float, observation: Observation) -> list[Action]:
        card = self._safe_match(SHIP_CARD_FEATURE)
        if card.found:
            action = self._click_center(card, observation, label="ship_select_card")
            if action is not None:
                self._clicked_at = now
                self._status = ShipCheckStatus.CHECKING
                return [action]
        sleep_s = 0.4
        if now - self._scroll.last_scroll_at < sleep_s:
            return []
        if self._scroll.remaining <= 0:
            self._status = ShipCheckStatus.FAILED
            self._failure_reason = "船只列表滚动到底仍未找到配置船只"
            return []
        self._scroll.remaining -= 1
        self._scroll.last_scroll_at = now
        direction = self.ship.scroll_direction.lower()
        clicks = max(1, int(self.ship.scroll_clicks))
        delta = -clicks if direction in ("down", "south", "next") else clicks
        actions: list[Action] = []
        move = self._move_cursor_to_list(observation)
        if move is not None:
            actions.append(move)
        actions.append(Action.wheel(delta, label="ship_list_scroll"))
        return actions

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
    def _card_found(self, result: ShipCheckResult) -> bool:
        return result.card_found

    def _click_feature_center(self, feature_id: str, observation: Observation) -> Action | None:
        try:
            match = self.engine.match_feature(feature_id)
        except Exception:
            return None
        if not match.found:
            return None
        return self._click_center(match, observation, label=f"click_{feature_id}")

    @staticmethod
    def _click_center(match: MatchResult, observation: Observation, *, label: str) -> Action | None:
        if match.center is None:
            return None
        window = Rect(*observation.window_rect)
        screen_x = window.x + int(match.center[0])
        screen_y = window.y + int(match.center[1])
        return Action.click_abs(screen_x, screen_y, label=label)

    # ------------------------------------------------------------------
    def select_default_region(self) -> None:
        """占位：保留给标定工具/后续扩展。"""


def _loadout_feature_id(slot: LoadoutSlotDef) -> str:
    return f"{LOADOUT_FEATURE_PREFIX}{slot.slot_id}"
