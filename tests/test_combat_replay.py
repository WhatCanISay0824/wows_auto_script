"""真实小地图经过生产感知、调度及输入后端的离线回放。"""

from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
from src.behavior.random_profile import RandomProfile
from src.behavior.scheduler import BehaviorScheduler
from src.common.clock import FakeClock
from src.common.images import read_color_image
from src.common.types import ActionKind, NormalizedRect, Rect, State
from src.config.loader import load_project
from src.perception.capture import Frame
from src.perception.combat import camera_bearing
from src.perception.engine import PerceptionEngine

from tests.test_state_machine import Harness, _death_signals, _match, drive_to_battle_active
from tests.test_winput import FakeSendInput, make_sendinput_backend

ROOT = Path(__file__).resolve().parents[1]


class MapReplay:
    """只读取保存的地图图像，逐帧替换时间；不会捕获窗口。"""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.frame_id = 0
        minimap = read_color_image(ROOT / "tests/fixtures/navigation/combat_old.png")
        assert minimap is not None
        self.image = np.zeros((600, 600, 3), dtype=np.uint8)
        self.image[:516, :516] = minimap

    def capture(self) -> Frame:
        self.frame_id += 1
        return Frame(self.image.copy(), self.clock.now(), Rect(0, 0, 600, 600), self.frame_id)


class LargeMapReplay:
    """2560×1600 窗口中只填充当前 516×516 小地图内容区。"""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.frame_id = 0
        minimap = read_color_image(ROOT / "tests/fixtures/navigation/combat_20261103_large.png")
        assert minimap is not None and minimap.shape[:2] == (516, 516)
        self.image = np.zeros((1600, 2560, 3), dtype=np.uint8)
        self.image[1082:1598, 2042:2558] = minimap

    def capture(self) -> Frame:
        self.frame_id += 1
        return Frame(self.image.copy(), self.clock.now(), Rect(0, 0, 2560, 1600), self.frame_id)


def test_large_minimap_uses_raw_color_mask_fallback_with_existing_threshold() -> None:
    clock = FakeClock(start=100)
    capture = LargeMapReplay(clock)
    config = load_project(ROOT, "cn360", ship_id="leningrad").config
    engine = PerceptionEngine(config, capture, clock)
    engine.begin_battle(90)

    first = engine.observe(State.BATTLE_ACTIVE)
    assert first.ship is not None
    assert first.ship.source == "marker"
    assert first.ship.minimap_px == pytest.approx((428.5, 123.5), abs=2)
    assert first.combat.camera_bearing_deg == pytest.approx(180.056, abs=7)

    locator = engine._ship_locator
    minimap = read_color_image(ROOT / "tests/fixtures/navigation/combat_20261103_large.png")
    assert minimap is not None
    raw_mask = locator._raw_color_mask_bgr(minimap)
    assert locator._detect_from_mask(locator._clean_mask(raw_mask), None) is None
    fallback = locator._detect_from_mask(raw_mask, None)
    assert fallback is not None
    assert fallback.score == pytest.approx(0.7647059, abs=0.01)
    assert locator._threshold == 0.75


def test_large_minimap_idle_candidate_combat_and_removal_round_trip() -> None:
    clock = FakeClock(start=100)
    capture = LargeMapReplay(clock)
    config = load_project(ROOT, "cn360", ship_id="leningrad").config
    engine = PerceptionEngine(config, capture, clock)
    engine.begin_battle(90)
    scheduler = BehaviorScheduler(config.global_config.behavior, config.keymap)
    scheduler.start(RandomProfile.generate(config.global_config.behavior, seed=1), 90)

    idle_actions = []
    for _ in range(70):
        clock.advance(0.1)
        observation = engine.observe(State.BATTLE_ACTIVE)
        idle_actions.extend(scheduler.tick(observation, clock.now()))
        if any(action.label == "idle_camera" for action in idle_actions):
            break
    assert any(action.label == "idle_camera" for action in idle_actions)

    clean_image = capture.image.copy()
    cv2.fillConvexPoly(
        capture.image,
        np.array([[2470, 1352], [2462, 1371], [2479, 1371]]),
        (25, 75, 250),
    )
    clock.advance(0.1)
    cached = engine.observe(State.BATTLE_ACTIVE)
    assert cached.combat is not None
    assert cached.combat.enemy_candidates == 0
    assert cached.combat.enemies == ()

    clock.advance(0.6)
    candidate = engine.observe(State.BATTLE_ACTIVE)
    assert candidate.combat is not None
    assert candidate.combat.enemy_candidates == 1
    assert candidate.combat.enemies == ()
    assert scheduler.tick(candidate, clock.now()) == []

    clock.advance(0.6)
    confirmed = engine.observe(State.BATTLE_ACTIVE)
    assert confirmed.combat is not None
    assert len(confirmed.combat.enemies) == 1
    combat_actions = scheduler.tick(confirmed, clock.now())
    assert [action.label for action in combat_actions] == ["combat_fire"]
    assert combat_actions[0].kind is ActionKind.KEY_TAP

    capture.image = clean_image.copy()
    clock.advance(0.6)
    absent = engine.observe(State.BATTLE_ACTIVE)
    assert absent.combat is not None
    assert absent.combat.enemies == () and absent.combat.enemy_candidates == 0
    assert not any(action.label == "combat_fire" for action in scheduler.tick(absent, clock.now()))

    resumed_idle = []
    for _ in range(70):
        clock.advance(0.1)
        observation = engine.observe(State.BATTLE_ACTIVE)
        resumed_idle.extend(scheduler.tick(observation, clock.now()))
        if any(action.label == "idle_camera" for action in resumed_idle):
            break
    assert any(action.label == "idle_camera" for action in resumed_idle)


def test_current_background_color_is_not_camera_sector() -> None:
    config = load_project(ROOT, "cn360", ship_id="leningrad").config
    background = np.full((516, 516, 3), (57, 53, 24), dtype=np.uint8)
    assert camera_bearing(background, (428.5, 123.5), config.profile.minimap) is None


@pytest.mark.parametrize("sample, expected", [
    ("combat_old.png", (81.5, 199.5)),
    ("combat_20261103_large.png", (428.5, 123.5)),
])
def test_asia_profile_uses_full_minimap_and_reliable_marker(sample, expected):
    from src.navigation.autopilot import AutoPilotNavigator

    clock = FakeClock(start=100)
    capture = LargeMapReplay(clock)
    minimap = read_color_image(ROOT / "tests/fixtures/navigation" / sample)
    capture.image[1082:1598, 2042:2558] = minimap
    config = load_project(ROOT, "wg_asia", ship_id="leningrad").config
    engine = PerceptionEngine(config, capture, clock)
    engine.begin_battle(90)
    observation = engine.observe(State.BATTLE_ACTIVE)
    assert engine.minimap_content_rect().to_tuple() == (2042, 1082, 516, 516)
    assert observation.ship is not None and observation.ship.source == "marker"
    assert observation.ship.minimap_px == pytest.approx(expected, abs=2)
    nav = AutoPilotNavigator(config.global_config.navigation, config.profile, keymap=config.keymap)
    nav.start(clock.now())
    nav.tick(observation, clock.now())
    assert nav.own_position_norm == pytest.approx((expected[0] / 516, expected[1] / 516), abs=0.005)
    assert nav.compute_full_map_target(observation).normalized == pytest.approx(
        (0.5, 1 - expected[1] / 516), abs=0.005,
    )


def test_asia_configuration_idle_candidate_combat_round_trip():
    clock = FakeClock(start=100)
    capture = LargeMapReplay(clock)
    minimap = read_color_image(ROOT / "tests/fixtures/navigation/combat_old.png")
    original = capture.image.copy()
    original[1082:1598, 2042:2558] = minimap
    clean = original.copy()
    hsv = cv2.cvtColor(clean, cv2.COLOR_BGR2HSV)
    red = cv2.inRange(hsv, (0, 150, 170), (18, 255, 255)) > 0
    clean[red] = (57, 53, 24)
    capture.image = clean
    config = load_project(ROOT, "wg_asia", ship_id="leningrad").config
    engine = PerceptionEngine(config, capture, clock)
    engine.begin_battle(90)
    events = []
    scheduler = BehaviorScheduler(
        config.global_config.behavior, config.keymap,
        on_event=lambda name, detail: events.append((name, detail)),
    )
    scheduler.start(RandomProfile.generate(config.global_config.behavior, seed=1), 90)

    def advance(seconds):
        actions = []
        for _ in range(round(seconds * 10)):
            clock.advance(0.1)
            actions.extend(scheduler.tick(engine.observe(State.BATTLE_ACTIVE), clock.now()))
        return actions

    assert any(action.label == "idle_camera" for action in advance(7))
    capture.image = original
    actions = advance(2)
    assert any(action.label == "combat_fire" for action in actions)
    modes = [detail["mode"] for name, detail in events if name == "behavior_mode"]
    assert modes[:3] == ["idle", "candidate", "combat"]
    capture.image = clean
    assert any(action.label == "idle_camera" for action in advance(7))
    modes = [detail["mode"] for name, detail in events if name == "behavior_mode"]
    assert modes[-1] == "idle"


def test_real_map_through_engine_scheduler_and_fake_input() -> None:
    clock = FakeClock(start=100)
    capture = MapReplay(clock)
    config = load_project(ROOT, "cn360", ship_id="leningrad").config
    minimap = replace(
        config.profile.minimap,
        rect=NormalizedRect(0, 0, .86, .86),
        border_px=0,
        view_cone_hsv_lower=(95, 60, 35),
    )
    config = replace(config, profile=replace(config.profile, minimap=minimap))
    engine = PerceptionEngine(config, capture, clock)
    engine.begin_battle(90)
    scheduler = BehaviorScheduler(config.global_config.behavior, config.keymap)
    scheduler.start(RandomProfile.generate(config.global_config.behavior, seed=1), 90)
    backend_events = FakeSendInput()
    backend = make_sendinput_backend(backend_events, clock)

    first = engine.observe(State.BATTLE_ACTIVE)
    assert first.ship.source == "marker"
    assert first.ship.minimap_px == pytest.approx((81.5, 199.5), abs=3)
    assert first.combat.camera_bearing_deg == pytest.approx(141.2, abs=7)
    assert first.combat.enemies == ()
    assert scheduler.tick(first, clock.now()) == []

    clock.advance(.6)
    confirmed = engine.observe(State.BATTLE_ACTIVE)
    assert len(confirmed.combat.enemies) == 1
    actions = scheduler.tick(confirmed, clock.now())
    assert [a.kind for a in actions] == [ActionKind.KEY_TAP]
    backend.send(actions)
    # 鼠标只按下、释放；没有绝对移动或无关视角动作。
    assert len(backend_events.mouse_events()) == 2
    assert not backend.pressed_buttons

    clock.advance(2)
    assert scheduler.tick(confirmed, clock.now()) == []  # 过期画面不能继续开火。
    after_gap = engine.observe(State.BATTLE_ACTIVE)
    assert after_gap.combat.enemies == ()
    assert scheduler.tick(after_gap, clock.now()) == []
    clock.advance(.6)
    assert engine.observe(State.BATTLE_ACTIVE).combat.enemies

    # 将真实图中的红橙像素移除，验证目标消失后的生产感知路径。
    hsv = cv2.cvtColor(capture.image, cv2.COLOR_BGR2HSV)
    red = cv2.inRange(hsv, np.array(minimap.enemy_hsv_lower), np.array(minimap.enemy_hsv_upper))
    capture.image[red > 0] = (39, 31, 27)
    clock.advance(3)
    absent = engine.observe(State.BATTLE_ACTIVE)
    assert absent.combat.enemies == ()
    assert scheduler.tick(absent, clock.now()) == []
    assert engine.observe(State.BATTLE_FINISHED).combat is None
    assert engine.observe(State.DEAD_PENDING_EXIT).combat is None


@pytest.mark.parametrize("blocked", ["map", "death", "result"])
def test_state_machine_blocks_targeted_combat_before_scheduler(tmp_path: Path, blocked: str) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.features = {}
    if blocked == "map":
        h.autopilot.blocks_behavior = True
    elif blocked == "death":
        h.perception.death = _death_signals(h.clock.now())
    else:
        h.perception.features = {"battle_results": _match("battle_results")}

    class UnexpectedBehavior:
        def tick(self, *args):
            pytest.fail("状态机没有在调用交战调度前阻止输入")

        def stop(self):
            pass

    h.machine.deps.behavior = UnexpectedBehavior()
    actions = h.step()
    assert not any(a.label in ("combat_aim", "combat_fire") for a in actions)
