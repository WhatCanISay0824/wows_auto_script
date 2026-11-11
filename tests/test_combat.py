"""小地图交战证据和行为；全部使用图像或输入替身。"""

from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
from src.behavior.combat import CombatController
from src.common.images import read_color_image
from src.common.types import ActionKind, CombatObservation, EnemyContact, NormalizedRect, ShipPosition
from src.config.models import BehaviorConfig, MinimapConfig
from src.perception.combat import MinimapCombatObserver, camera_bearing, enemy_markers

ROOT = Path(__file__).resolve().parents[1]
MINIMAP = MinimapConfig(rect=NormalizedRect(0, 0, 1, 1))


def test_real_enemy_markers_exclude_flags_and_text() -> None:
    image = read_color_image(ROOT / "tests/fixtures/navigation/combat_current.png")
    assert image is not None
    points = enemy_markers(image, MINIMAP)
    assert any(np.linalg.norm(np.array(p) - (158, 91)) < 10 for p in points)
    assert any(np.linalg.norm(np.array(p) - (338, 160)) < 14 for p in points)
    assert all(np.linalg.norm(np.array(p) - (182, 276)) > 20 for p in points)  # 占领点旗帜
    assert all(np.linalg.norm(np.array(p) - (135, 265)) > 15 for p in points)  # 红色定位图案
    assert all(np.linalg.norm(np.array(p) - (187, 48)) > 20 for p in points)  # 粉色友舰


def test_real_camera_sector_is_independent_of_ship_heading() -> None:
    image = read_color_image(ROOT / "tests/fixtures/navigation/combat_old.png")
    assert image is not None
    assert camera_bearing(image, (80, 201), MINIMAP) == pytest.approx(140.5, abs=7)


def synthetic_map() -> np.ndarray:
    image = np.full((400, 400, 3), (39, 31, 27), dtype=np.uint8)
    cv2.ellipse(image, (200, 200), (150, 150), 0, 25, 85, (56, 49, 35), -1)
    cv2.fillConvexPoly(image, np.array([[280, 150], [272, 169], [289, 169]]), (25, 75, 250))
    return image


def test_confirmation_uses_distinct_frames_and_clears_when_enemy_disappears() -> None:
    observer = MinimapCombatObserver(MINIMAP)
    own = ShipPosition((200, 200), None, 300, 0, .95, "marker")
    image = synthetic_map()
    first = observer.observe(image, own, 1, 10)
    assert not first.enemies
    assert first.enemy_candidates == 1
    assert not observer.observe(image, own, 1, 10).enemies
    assert not observer.observe(image, own, 1, 10.2).enemies
    confirmed = observer.observe(image, own, 2, 10.5)
    assert len(confirmed.enemies) == 1
    assert confirmed.enemy_candidates == 1
    assert confirmed.camera_bearing_deg == pytest.approx(145, abs=8)
    blank = np.full_like(image, (39, 31, 27))
    absent = observer.observe(blank, own, 3, 11)
    assert not absent.enemies
    assert absent.enemy_candidates == 0
    assert not observer.observe(image, own, 4, 11.5).enemies
    assert not observer.observe(image, own, 5, 16).enemies  # 断帧后重新确认
    unreliable = observer.observe(image, replace(own, source="predicted"), 6, 16.5)
    assert not unreliable.enemies
    assert unreliable.enemy_candidates == 0


def test_hollow_symbols_objectives_and_pink_do_not_authorize_attack() -> None:
    image = np.full((400, 400, 3), (39, 31, 27), dtype=np.uint8)
    red = (25, 75, 250)
    cv2.circle(image, (100, 100), 35, red, 1)
    cv2.rectangle(image, (90, 90), (110, 110), red, 1)
    cv2.fillPoly(image, [np.array([[94, 95], [105, 95], [105, 108], [101, 108], [101, 101], [94, 101]])], red)
    cv2.polylines(image, [np.array([[230, 130], [220, 155], [240, 155]])], True, red, 1)
    cv2.fillConvexPoly(image, np.array([[300, 130], [290, 155], [310, 155]]), (170, 110, 255))
    assert enemy_markers(image, MINIMAP) == ()
    assert camera_bearing(image, (200, 200), MINIMAP) is None


def combat_frame(t: float, frame_id: int, *, camera: float | None = 90, enemies=True) -> CombatObservation:
    return CombatObservation(
        timestamp=t, frame_id=frame_id, own_px=(100, 100), camera_bearing_deg=camera,
        enemies=(EnemyContact((180, 100)),) if enemies else (),
    )


def test_turn_requires_newer_feedback_before_fire() -> None:
    cfg = replace(BehaviorConfig(), targeted_combat=True)
    controller = CombatController(cfg)
    frame = combat_frame(10, 1, camera=0)
    actions, _ = controller.tick(frame, 10)
    assert len(actions) == 1 and actions[0].kind is ActionKind.MOVE_REL
    assert 0 < actions[0].dx <= cfg.aim_step_max_px
    assert actions[0].dy == 0
    assert controller.tick(frame, 10.6)[0] == []
    assert controller.tick(combat_frame(10, 1, camera=90), 10.6)[0] == []
    actions, _ = controller.tick(combat_frame(11, 2, camera=90), 11)
    assert len(actions) == 1 and actions[0].kind is ActionKind.KEY_TAP
    assert actions[0].key == "mouse_left"
    assert actions[0].expires_at is not None


def test_fire_repeats_within_interval_without_random_view_motion() -> None:
    cfg = replace(BehaviorConfig(), targeted_combat=True, combat_fire_interval_s=(3, 5))
    controller = CombatController(cfg)
    actions, _ = controller.tick(combat_frame(10, 1), 10)
    assert [a.kind for a in actions] == [ActionKind.KEY_TAP]
    assert controller.tick(combat_frame(12, 2), 12)[0] == []
    assert [a.kind for a in controller.tick(combat_frame(15.1, 3), 15.1)[0]] == [ActionKind.KEY_TAP]
    assert controller.tick(combat_frame(16, 4, enemies=False), 16)[0] == []


@pytest.mark.parametrize("camera,age", [(None, 0), (90, 3), (90, -1)])
def test_unknown_camera_and_invalid_frame_age_do_not_act(camera: float | None, age: float) -> None:
    controller = CombatController(replace(BehaviorConfig(), targeted_combat=True))
    assert controller.tick(combat_frame(10-age, 1, camera=camera), 10)[0] == []


def test_controller_reset_discards_target_and_pending_feedback() -> None:
    controller = CombatController(replace(BehaviorConfig(), targeted_combat=True))
    controller.tick(combat_frame(10, 1, camera=0), 10)
    controller.reset()
    assert controller.tick(combat_frame(10.5, 2), 10.5)[0][0].kind is ActionKind.KEY_TAP


def test_camera_that_does_not_respond_stops_turning() -> None:
    controller = CombatController(replace(BehaviorConfig(), targeted_combat=True))
    for frame_id in range(1, 5):
        actions, detail = controller.tick(combat_frame(10+frame_id, frame_id, camera=0), 10+frame_id)
    assert actions == []
    assert detail['reason'] == 'aim_not_responding'


def test_target_stays_stable_when_other_enemy_becomes_closer_to_camera() -> None:
    controller = CombatController(replace(BehaviorConfig(), targeted_combat=True))
    original = EnemyContact((170, 60))
    frame = replace(combat_frame(10, 1, camera=60), enemies=(original,))
    controller.tick(frame, 10)
    frame = replace(frame, timestamp=16, frame_id=2, enemies=(EnemyContact((190, 50)), original))
    _, detail = controller.tick(frame, 16)
    assert detail['target_px'] == original.minimap_px
