"""定位缺失不得变成目标失败；验证只接受可靠的新鲜标记观测。"""

from dataclasses import replace

import pytest

from tests.test_autopilot import _Events, make_observation
from tests.test_autopilot_terrain import _Provider, make_terrain_nav, run_full_cycle, water_image


def _verifying():
    events = _Events()
    nav = make_terrain_nav(_Provider(water_image()), events=events)
    nav.start(0)
    observation = make_observation((0.5, 0.8))
    run_full_cycle(nav, observation, 0)
    assert nav.phase == "verify"
    return nav, events, observation


@pytest.mark.parametrize("kind", ["missing", "predicted", "lost", "low_confidence", "nan",
                                  "stale", "future", "duplicate", "late"])
def test_inadequate_positions_keep_target_without_reclick(kind):
    nav, events, observation = _verifying()
    target = nav.last_target
    for t in (2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0):
        sample = replace(observation, timestamp=t, frame_timestamp=t)
        if kind == "missing" or kind == "late" and t < 10:
            sample = replace(sample, ship=None)
        elif kind in ("predicted", "lost"):
            sample = replace(sample, ship=replace(sample.ship, source=kind, speed_mps=10))
        elif kind == "low_confidence":
            sample = replace(sample, ship=replace(sample.ship, confidence=0.2, speed_mps=10))
        elif kind == "nan":
            sample = replace(sample, ship=replace(sample.ship, minimap_px=(float("nan"), 1)))
        elif kind == "stale":
            sample = replace(sample, frame_timestamp=t - 3)
        elif kind == "future":
            sample = replace(sample, frame_timestamp=t + 0.5)
        elif kind == "duplicate":
            sample = replace(sample, frame_timestamp=2)
        assert nav.tick(sample, t) == []
    assert nav.phase == "verify"
    assert nav.clicks == 1
    assert nav.last_target == target
    assert not nav.move_confirmed
    assert not nav.verify_failed
    assert "autopilot_target_rejected" not in events.names()
    assert "autopilot_verify_failed" not in events.names()
    assert events.last("autopilot_verify_waiting")["reason"] == "position_evidence_insufficient"


def test_missing_positions_can_recover_and_confirm_movement():
    nav, events, _ = _verifying()
    nav.tick(make_observation(None, timestamp=10), 10)
    assert nav.phase == "verify"
    nav.tick(make_observation((0.5, 0.8), timestamp=10.5), 10.5)
    nav.tick(make_observation((0.5, 0.8), dx=4, timestamp=11), 11)
    assert nav.move_confirmed
    assert nav.clicks == 1
    assert "autopilot_target_rejected" not in events.names()


def test_reliable_stationary_window_still_rejects_target():
    nav, events, observation = _verifying()
    for index in range(17):
        t = 2 + index * 0.5
        nav.tick(replace(observation, timestamp=t, frame_timestamp=t), t)
    assert nav.phase == "reclick"
    assert events.last("autopilot_target_rejected")["reason"] == "no_movement"
    assert events.last("autopilot_verify_failed")["position_samples"] >= 2


def test_position_loss_interrupts_stationary_evidence():
    nav, events, observation = _verifying()
    for t in (2.0, 2.5, 3.0, 9.0, 10.0):
        nav.tick(replace(observation, timestamp=t, frame_timestamp=t), t)
    assert nav.phase == "verify"
    assert "autopilot_target_rejected" not in events.names()


@pytest.mark.parametrize("missing", [None, "window", "capture"])
def test_entire_observation_loss_keeps_closed_map_target(missing):
    nav, events, _ = _verifying()
    for t in (10, 20, 30):
        sample = None
        if missing is not None:
            sample = make_observation(None, timestamp=t, window_missing=missing == "window")
            if missing == "capture":
                sample = replace(sample, capture_error="capture failed")
        assert nav.tick(sample, t) == []
    assert nav.phase == "verify"
    assert nav.clicks == 1
    assert not nav.map_open
    assert "autopilot_target_rejected" not in events.names()


def test_waiting_recovers_stationary_evidence_and_releases_behavior():
    nav, events, observation = _verifying()
    nav.tick(make_observation(None, timestamp=10), 10)
    for index in range(16):
        t = 10.5 + index * 0.5
        nav.tick(replace(observation, timestamp=t, frame_timestamp=t), t)
    assert not nav.blocks_behavior
    assert nav.phase == "reclick"
    assert events.names().count("autopilot_target_rejected") == 1


def test_explicit_position_loss_breaks_stationary_window():
    nav, events, observation = _verifying()
    for index in range(17):
        t = 2 + index * 0.5
        sample = replace(observation, timestamp=t, frame_timestamp=t)
        if t == 6:
            sample = replace(sample, ship=None)
        nav.tick(sample, t)
    assert nav.phase == "verify"
    assert "autopilot_target_rejected" not in events.names()


def test_waiting_allows_target_override_and_stop():
    nav, _, _ = _verifying()
    nav.tick(make_observation(None, timestamp=10), 10)
    nav.replace_target((0.3, 0.6), 11)
    assert nav.phase == "reclick"
    nav.tick(make_observation(None, timestamp=11), 11)
    assert nav.phase == "open_map"
    nav.stop()
    assert nav.tick(make_observation(None, timestamp=20), 20) == []
    assert not nav.active


def test_predicted_position_cannot_be_pre_map_movement_reference():
    events = _Events()
    nav = make_terrain_nav(_Provider(water_image()), events=events)
    nav.start(0)
    observation = make_observation((0.5, 0.8))
    predicted = replace(observation, ship=replace(observation.ship, source="predicted"))
    run_full_cycle(nav, predicted, 10)
    nav.tick(make_observation((0.5, 0.8), dx=50, timestamp=12), 12)
    assert not nav.move_confirmed
    assert nav.phase == "verify"
