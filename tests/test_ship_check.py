"""ShipChecker 选船确认与轮播查找测试（假感知 + 假步骤执行器，ISS-007）。

覆盖口径（实现补充 10.7）：
- 目标卡可见但未选中：必须先点击，点击后等待新帧，再用 selected_feature 确认；
- 点击后确认失败：FAILED，不能误判 OK；重复点击有上限；
- 目标在屏外：滚动预算正常工作（空 ship_selection_sequence 也不清零预算）；
- 端点判定与总超时；
- 多个相似候选：暂停（FAILED），不猜选；
- 已选中证据命中：直接 OK，不重复点击；
- selected_feature 未配置/模板缺失：保守 FAILED，不把卡片可见当作已选中。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from src.common.types import ActionKind, MatchResult, NormalizedRect, Observation, Rect
from src.config.loader import load_project
from src.config.models import (
    AppConfig,
    ClientProfile,
    GlobalConfig,
    KeyMap,
    ShipConfig,
    ShipProfile,
    StepDef,
    WindowConfig,
)
from src.perception.capture import Frame
from src.state_machine.ship_check import (
    SHIP_CARD_FEATURE,
    SHIP_SELECTED_FEATURE,
    ShipChecker,
    ShipCheckStatus,
)
from src.state_machine.steps import StepStatus

WINDOW_W = 400
WINDOW_H = 200
CARD_SEARCH = NormalizedRect(0.0, 0.8, 1.0, 1.0)
CARD_BAND_TOP = int(CARD_SEARCH.y1 * WINDOW_H)  # 160


# ---------------------------------------------------------------------------
# 假船港世界：轮播条卡片列表 + 选中状态 + 可点击/滚动
# ---------------------------------------------------------------------------


class FakeShipWorld:
    def __init__(self, cards: list[str], target: str = "target", visible: int = 3) -> None:
        self.cards = list(cards)
        self.target = target
        self.visible = visible
        self.offset = 0
        self.selected: str | None = None
        self.select_on_click = True
        self.clicked: list[tuple[int, int]] = []

    @property
    def card_width(self) -> int:
        return WINDOW_W // self.visible

    @property
    def max_offset(self) -> int:
        return max(0, len(self.cards) - self.visible)

    def target_slot(self) -> int | None:
        if self.target not in self.cards:
            return None
        slot = self.cards.index(self.target) - self.offset
        return slot if 0 <= slot < self.visible else None

    def scroll(self, delta: int) -> None:
        steps = abs(int(delta))
        if delta < 0:
            self.offset = min(self.max_offset, self.offset + steps)
        elif delta > 0:
            self.offset = max(0, self.offset - steps)

    def click(self, x: int, y: int) -> None:
        self.clicked.append((x, y))
        if y < CARD_BAND_TOP:
            return
        slot = x // self.card_width
        index = self.offset + slot
        if not 0 <= slot < self.visible or not 0 <= index < len(self.cards):
            return
        card = self.cards[index]
        if self.select_on_click and card == self.target:
            self.selected = self.target

    def render(self) -> np.ndarray:
        image = np.zeros((WINDOW_H, WINDOW_W, 3), dtype=np.uint8)
        for slot in range(self.visible):
            index = self.offset + slot
            left = slot * self.card_width
            right = left + self.card_width
            if index >= len(self.cards):
                continue
            image[:, left:right, :] = 20 + (index * 13) % 120
            image[0:40, left:right, :] = (index * 37) % 255
            image[CARD_BAND_TOP:WINDOW_H, left:right, :] = 40 + (index * 23) % 180
        for slot in range(1, self.visible):
            x = slot * self.card_width
            image[CARD_BAND_TOP:WINDOW_H, x : x + 2, :] = 255
        return image


class FakeEngine:
    """假感知：按世界状态返回 ship_card / ship_selected 匹配。"""

    def __init__(
        self,
        world: FakeShipWorld,
        *,
        selected_registration_ok: bool = True,
        multi_candidates: bool = False,
    ) -> None:
        self.world = world
        self.frame_id = 1
        self.registered: dict[str, dict] = {}
        self.selected_registration_ok = selected_registration_ok
        self.multi_candidates = multi_candidates
        self.match_calls = 0

    def register_feature(self, feature_id, template_path, region=None, threshold=0.85) -> None:
        if feature_id == SHIP_SELECTED_FEATURE and not self.selected_registration_ok:
            raise ValueError("选中态模板无法读取")
        self.registered[feature_id] = {
            "template": str(template_path),
            "region": region,
            "threshold": threshold,
        }

    def latest_frame(self, now=None) -> Frame:
        return Frame(
            image=self.world.render(),
            timestamp=float(now or 0.0),
            window_rect=Rect(0, 0, WINDOW_W, WINDOW_H),
            frame_id=self.frame_id,
        )

    def match_feature(self, feature_id: str) -> MatchResult:
        self.match_calls += 1
        if feature_id == SHIP_SELECTED_FEATURE:
            if self.world.selected == self.world.target:
                slot = self.world.target_slot()
                center = None
                if slot is not None:
                    center = (slot * self.world.card_width + self.world.card_width // 2, WINDOW_H // 2)
                return MatchResult(feature_id, True, 0.97, center=center)
            return MatchResult(feature_id, False, 0.10)
        if feature_id == SHIP_CARD_FEATURE:
            slot = self.world.target_slot()
            if slot is None:
                return MatchResult(feature_id, False, 0.20)
            width = self.world.card_width
            left = slot * width
            return MatchResult(
                feature_id,
                True,
                0.95,
                center=(left + width // 2, (CARD_BAND_TOP + WINDOW_H) // 2),
                rect=(left, CARD_BAND_TOP, width, WINDOW_H - CARD_BAND_TOP),
            )
        return MatchResult(feature_id, False, 0.0)

    def match_feature_candidates(self, feature_id: str) -> tuple[MatchResult, ...]:
        if not self.multi_candidates:
            return ()
        slot = self.world.target_slot()
        if slot is None:
            return ()
        width = self.world.card_width
        left = slot * width
        center_y = (CARD_BAND_TOP + WINDOW_H) // 2
        return (
            MatchResult(feature_id, True, 0.96, center=(left + width // 2 - 40, center_y)),
            MatchResult(feature_id, True, 0.94, center=(left + width // 2 + 40, center_y)),
        )


class FakeSteps:
    """假步骤执行器：按预设状态推进，记录是否用于打开船只列表。"""

    def __init__(self, statuses: tuple[StepStatus, ...] = (), last_error: str = "") -> None:
        self.statuses = list(statuses)
        self.started: list[str] = []
        self.last_error = last_error

    def start(self, steps, *, label: str = "") -> None:
        self.started.append(label)

    def advance(self, now, observation):
        if not self.statuses:
            return StepStatus.DONE, []
        return self.statuses.pop(0), []


# ---------------------------------------------------------------------------
# 组装与驱动
# ---------------------------------------------------------------------------


def make_ship(
    *,
    selected_feature: str | None = "selected.png",
    selected_path: Path | None = Path("selected.png"),
    **overrides,
) -> ShipProfile:
    payload = {
        "ship_id": "target",
        "display_name": "目标船",
        "card_template": "card.png",
        "card_path": Path("card.png"),
        "card_search_region": CARD_SEARCH,
        "selected_feature": selected_feature,
        "selected_region": CARD_SEARCH,
        "selected_path": selected_path,
        "max_scrolls": 20,
        "scroll_direction": "down",
        "scroll_clicks": 1,
        "max_position_scrolls": 10,
        "scroll_settle_s": 0.4,
        "confirm_frames": 2,
        "confirm_timeout_s": 3.0,
        "max_click_attempts": 2,
        "endpoint_stable_scrolls": 2,
        "selection_timeout_s": 60.0,
    }
    payload.update(overrides)
    return ShipProfile(**payload)


class Harness:
    def __init__(
        self,
        world: FakeShipWorld,
        *,
        ship: ShipProfile | None = None,
        sequence: tuple[StepDef, ...] = (),
        steps: FakeSteps | None = None,
        multi_candidates: bool = False,
        selected_registration_ok: bool = True,
    ) -> None:
        self.world = world
        self.steps = steps or FakeSteps()
        self.engine = FakeEngine(
            world,
            selected_registration_ok=selected_registration_ok,
            multi_candidates=multi_candidates,
        )
        profile = ClientProfile(
            client_id="test",
            display_name="test",
            window=WindowConfig(),
            language="zh_cn",
            reference_resolution=(WINDOW_W, WINDOW_H),
            features={},
            ship_selection_sequence=sequence,
        )
        config = AppConfig(
            global_config=GlobalConfig(ship=ShipConfig(ship_id="target", verify_each_battle=True)),
            profile=profile,
            keymap=KeyMap(),
            ship=ship or make_ship(),
            maps={},
            project_root=Path("."),
            config_hash="0" * 16,
        )
        self.checker = ShipChecker(config, self.engine, self.steps)  # type: ignore[arg-type]
        self.now = 0.0
        self.frame_id = 1
        self.actions: list = []

    def tick(self, dt: float = 0.5):
        self.now += dt
        self.frame_id += 1
        self.engine.frame_id = self.frame_id
        observation = Observation(
            timestamp=self.now,
            window_rect=(0, 0, WINDOW_W, WINDOW_H),
            frame_id=self.frame_id,
            frame_timestamp=self.now,
        )
        status, actions = self.checker.tick(self.now, observation)
        self.actions.extend(actions)
        for action in actions:
            self._apply(action)
        return status, actions

    def _apply(self, action) -> None:
        if action.kind is ActionKind.CLICK_ABS:
            self.world.click(action.x, action.y)
        elif action.kind is ActionKind.MOUSE_WHEEL:
            self.world.scroll(action.wheel_delta)

    def run_until_terminal(self, *, max_ticks: int = 300):
        status = self.checker.status
        for _ in range(max_ticks):
            status, _ = self.tick()
            if status in (ShipCheckStatus.OK, ShipCheckStatus.FAILED):
                return status
        raise AssertionError(f"未在 {max_ticks} tick 内结束，最后状态 {status}")

    def wheel_actions(self, label: str | None = None) -> list:
        result = [a for a in self.actions if a.kind is ActionKind.MOUSE_WHEEL]
        if label is None:
            return result
        return [a for a in result if a.label == label]

    def click_actions(self) -> list:
        return [a for a in self.actions if a.kind is ActionKind.CLICK_ABS]


# ---------------------------------------------------------------------------
# 已选中确认
# ---------------------------------------------------------------------------


def test_already_selected_returns_ok_without_click() -> None:
    world = FakeShipWorld(["a", "target", "b", "c"], visible=3)
    world.selected = "target"
    harness = Harness(world)
    status, actions = harness.tick()
    assert status is ShipCheckStatus.OK
    assert actions == []
    assert harness.click_actions() == []
    assert harness.checker.selection_started is False


def test_visible_card_is_only_a_candidate_click_then_confirm() -> None:
    world = FakeShipWorld(["a", "target", "b", "c"], visible=3)
    assert world.target_slot() == 1
    harness = Harness(world)
    status, actions = harness.tick()
    # 卡片可见但未选中：只能作为点击候选，必须点击而不是直接 OK
    assert status is ShipCheckStatus.SELECTING
    assert [a.kind for a in actions] == [ActionKind.CLICK_ABS]
    # 点击动作被“执行”后世界选中目标船；tick 计数是继续用新帧确认
    status = harness.run_until_terminal()
    assert status is ShipCheckStatus.OK
    assert world.selected == "target"
    assert len(harness.click_actions()) == 1


def test_confirm_requires_new_frames_after_click() -> None:
    world = FakeShipWorld(["a", "target", "b", "c"], visible=3)
    harness = Harness(world)
    status, _ = harness.tick()
    assert status is ShipCheckStatus.SELECTING
    # 点击后的第 1 个新帧还不够（confirm_frames=2），不能提前 OK
    status, _ = harness.tick()
    assert status is ShipCheckStatus.SELECTING
    # 第 2 个新帧到达后才允许确认已选中
    status, _ = harness.tick()
    assert status is ShipCheckStatus.OK


def test_click_without_selected_evidence_fails_not_ok() -> None:
    world = FakeShipWorld(["a", "target", "b", "c"], visible=3)
    world.select_on_click = False  # 点击后画面仍没有已选中证据
    harness = Harness(world)
    status = harness.run_until_terminal()
    assert status is ShipCheckStatus.FAILED
    assert "选中" in harness.checker.failure_reason
    # 重复点击有上限（max_click_attempts=2），不能无限点击
    assert len(harness.click_actions()) == 2


def test_selected_feature_not_configured_fails_conservatively() -> None:
    world = FakeShipWorld(["a", "target", "b", "c"], visible=3)
    world.selected = "target"  # 即使看起来已选中，没有标定证据也不能确认
    harness = Harness(world, ship=make_ship(selected_feature=None, selected_path=None))
    status, actions = harness.tick()
    assert status is ShipCheckStatus.FAILED
    assert "selected_feature" in harness.checker.failure_reason
    assert actions == []
    assert harness.click_actions() == []


def test_selected_template_missing_fails_conservatively() -> None:
    world = FakeShipWorld(["a", "target", "b", "c"], visible=3)
    harness = Harness(world, ship=make_ship(selected_feature="selected.png", selected_path=None))
    status = harness.run_until_terminal()
    assert status is ShipCheckStatus.FAILED
    assert "选中态模板缺失" in harness.checker.failure_reason
    assert harness.click_actions() == []


def test_selected_template_registration_failure_fails() -> None:
    world = FakeShipWorld(["a", "target", "b", "c"], visible=3)
    harness = Harness(world, selected_registration_ok=False)
    status, actions = harness.tick()
    assert status is ShipCheckStatus.FAILED
    assert "选中态模板注册失败" in harness.checker.failure_reason
    assert actions == []


def test_real_ship_profile_without_selected_template_fails_conservatively(project_root: Path) -> None:
    """真实 cn360 船只档案：选中态模板尚未标定时，运行期必须保守失败而不是误判 OK。"""
    app = load_project(project_root, "cn360").config
    world = FakeShipWorld(["a", "target", "b", "c"], visible=3)
    engine = FakeEngine(world)
    checker = ShipChecker(app, engine, FakeSteps())  # type: ignore[arg-type]
    observation = Observation(
        timestamp=0.0,
        window_rect=(0, 0, WINDOW_W, WINDOW_H),
        frame_id=1,
        frame_timestamp=0.0,
    )
    status, actions = checker.tick(0.0, observation)
    if app.ship.selected_path is None:
        assert status is ShipCheckStatus.FAILED
        assert "选中态模板" in checker.failure_reason
        assert "标定" in checker.failure_reason
        assert actions == []
    else:  # 已补标定 selected.png 后，这里只要求流程可继续（不在此断言实机成功）
        assert status in (ShipCheckStatus.SELECTING, ShipCheckStatus.OK)


def test_verify_does_not_treat_visible_card_as_selected() -> None:
    world = FakeShipWorld(["a", "target", "b", "c"], visible=3)
    harness = Harness(world)
    result = harness.checker.verify()
    assert result.ok is False
    assert result.card_found is True
    assert result.selected_found is False
    world.selected = "target"
    result = harness.checker.verify()
    assert result.ok is True
    assert result.selected_found is True


# ---------------------------------------------------------------------------
# 滚动查找
# ---------------------------------------------------------------------------


def test_target_off_screen_scrolls_until_found() -> None:
    cards = ["a", "b", "c", "d", "e", "f", "g", "h", "target", "i"]
    world = FakeShipWorld(cards, visible=3)
    harness = Harness(world)
    status = harness.run_until_terminal()
    assert status is ShipCheckStatus.OK
    assert world.selected == "target"
    # 起点定位 + 正向扫描都有滚轮动作，且最后成功点击目标卡
    assert harness.wheel_actions("ship_list_scroll_position")
    assert harness.wheel_actions("ship_list_scroll")
    assert len(harness.click_actions()) == 1


def test_empty_selection_sequence_keeps_scroll_budget() -> None:
    """ISS-007 回归：空 ship_selection_sequence 只表示无需打开列表，不得清零滚动预算。"""
    cards = ["a", "b", "c", "d", "target", "e"]
    world = FakeShipWorld(cards, visible=3)
    harness = Harness(world, sequence=(), steps=FakeSteps())
    assert harness.steps.started == []
    status = harness.run_until_terminal()
    assert status is ShipCheckStatus.OK
    assert len(harness.wheel_actions()) > 0  # 确实滚动了，而不是首轮无动作直接失败
    assert harness.steps.started == []


def test_opening_list_sequence_runs_before_scrolling() -> None:
    cards = ["a", "b", "c", "target", "d"]
    world = FakeShipWorld(cards, visible=3)
    steps = FakeSteps((StepStatus.RUNNING, StepStatus.DONE))
    harness = Harness(world, sequence=(StepDef(action="wait_ms", duration_ms=10),), steps=steps)
    status = harness.run_until_terminal()
    assert status is ShipCheckStatus.OK
    assert steps.started == ["ship_select"]
    # 打开列表期间不应先发滚轮（步骤完成前不滚动）
    first_scroll_at = next(
        index for index, action in enumerate(harness.actions) if action.kind is ActionKind.MOUSE_WHEEL
    )
    assert first_scroll_at > 0


def test_target_left_of_initial_view_is_covered() -> None:
    """目标在初始位置左侧：先回列表起点，再正向扫描，不能直接报不存在。"""
    cards = ["target", "a", "b", "c", "d", "e", "f", "g", "h", "i"]
    world = FakeShipWorld(cards, visible=3)
    world.offset = world.max_offset  # 初始画面在右端，目标在左侧屏外
    assert world.target_slot() is None
    harness = Harness(world)
    status = harness.run_until_terminal()
    assert status is ShipCheckStatus.OK
    assert world.selected == "target"
    # 目标是在“回起点”过程（position）中找到的，不需要正向扫描到右端
    assert harness.wheel_actions("ship_list_scroll_position")
    assert harness.wheel_actions("ship_list_scroll") == []


def test_scroll_budget_exhausted_fails() -> None:
    cards = [f"card{i}" for i in range(40)]
    world = FakeShipWorld(cards, visible=3)
    harness = Harness(
        world,
        ship=make_ship(max_scrolls=3, max_position_scrolls=2, selection_timeout_s=120.0),
    )
    status = harness.run_until_terminal()
    assert status is ShipCheckStatus.FAILED
    assert "max_scrolls" in harness.checker.failure_reason
    assert len(harness.wheel_actions("ship_list_scroll")) == 3


def test_endpoint_detection_ends_search_early() -> None:
    world = FakeShipWorld(["a", "b", "c", "d"], visible=3)  # 目标不存在
    harness = Harness(world, ship=make_ship(max_scrolls=20, max_position_scrolls=5))
    status = harness.run_until_terminal()
    assert status is ShipCheckStatus.FAILED
    assert "端点" in harness.checker.failure_reason
    # 端点判定生效：远小于 max_scrolls 就结束，不会滚满 20 次
    assert len(harness.wheel_actions("ship_list_scroll")) < 20
    assert all(action.label != "ship_list_scroll_reverse" for action in harness.actions)


def test_selection_timeout_fails() -> None:
    world = FakeShipWorld([f"card{i}" for i in range(40)], visible=3)
    harness = Harness(world, ship=make_ship(selection_timeout_s=3.0, max_scrolls=50))
    harness.tick(dt=0.5)
    harness.tick(dt=0.5)
    harness.tick(dt=3.0)
    assert harness.checker.status is ShipCheckStatus.FAILED
    assert "超时" in harness.checker.failure_reason


# ---------------------------------------------------------------------------
# 候选歧义 / 步骤失败
# ---------------------------------------------------------------------------


def test_multiple_candidates_pauses_without_click() -> None:
    world = FakeShipWorld(["a", "target", "b", "c"], visible=3)
    harness = Harness(world, multi_candidates=True)
    status, actions = harness.tick()
    assert status is ShipCheckStatus.FAILED
    assert "候选" in harness.checker.failure_reason
    assert actions == []
    assert harness.click_actions() == []


def test_steps_failure_fails() -> None:
    world = FakeShipWorld(["a", "b", "c"], visible=3)
    steps = FakeSteps((StepStatus.FAILED,), last_error="点击船只面板失败")
    harness = Harness(world, sequence=(StepDef(action="wait_ms", duration_ms=10),), steps=steps)
    status, _ = harness.tick()  # 首轮：可见区域没有候选 → 开始选择并推进步骤
    assert status is ShipCheckStatus.FAILED
    assert "船只选择序列失败" in harness.checker.failure_reason


def test_reset_clears_selection_progress() -> None:
    cards = ["a", "b", "c", "target", "d"]
    world = FakeShipWorld(cards, visible=3)
    harness = Harness(world)
    harness.tick()
    assert harness.checker.selection_started is True
    harness.checker.reset()
    assert harness.checker.selection_started is False
    assert harness.checker.status is ShipCheckStatus.CHECKING
    harness.now = 0.0
    status = harness.run_until_terminal()
    assert status is ShipCheckStatus.OK
