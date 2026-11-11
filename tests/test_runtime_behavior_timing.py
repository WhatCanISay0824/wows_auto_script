"""带截屏耗时的运行器回归；生产感知和调度读取保存小地图，输入使用替身。"""

from dataclasses import replace

import cv2
import numpy as np
import pytest
from src.behavior.random_profile import RandomProfile
from src.behavior.scheduler import BehaviorScheduler
from src.common.types import State
from src.config.loader import load_project
from src.perception.engine import PerceptionEngine

from tests.test_combat_replay import ROOT, LargeMapReplay
from tests.test_gui_cooperation import cooperative_runner


class DelayedMapReplay(LargeMapReplay):
    def capture(self):
        self.clock.advance(0.06)
        return super().capture()


def _replay_runner(tmp_path, monkeypatch):
    h, runner, focus = cooperative_runner(tmp_path, monkeypatch)
    focus.update(gui=False, game=True)
    h.step()
    config = load_project(ROOT, "cn360", ship_id="leningrad").config
    capture = DelayedMapReplay(h.clock)
    engine = PerceptionEngine(config, capture, h.clock)
    engine.begin_battle(h.clock.now())
    events = []
    scheduler = BehaviorScheduler(
        config.global_config.behavior, config.keymap,
        on_event=lambda name, detail: events.append((name, detail)),
    )
    scheduler.start(RandomProfile.generate(config.global_config.behavior, seed=1), h.clock.now() - 20)
    h.machine.deps.behavior = scheduler
    runner.runtime.engine = engine
    runner._last_window_at = float("-inf")
    return h, runner, focus, capture, engine, events


def _run_for(h, runner, duration):
    end = h.clock.now() + duration
    while h.clock.now() < end:
        h.clock.advance(0.1)
        runner.step()


def test_capture_duration_does_not_reset_idle_wait(tmp_path, monkeypatch):
    h, runner, _, capture, _, events = _replay_runner(tmp_path, monkeypatch)
    _run_for(h, runner, 12)

    assert capture.frame_id > 10
    assert h.machine.state is State.BATTLE_ACTIVE
    assert any(action.label == "idle_camera" for action in h.executor.submitted)
    assert not any(detail.get("reason") == "future_frame" for _, detail in events)


@pytest.mark.parametrize("invalid", ["stale", "minimap_delay", "future"])
def test_processing_delay_and_future_frames_still_block_input(tmp_path, monkeypatch, invalid):
    h, runner, _, _, engine, events = _replay_runner(tmp_path, monkeypatch)
    original = engine.observe

    def observe(*args, **kwargs):
        observation = original(*args, **kwargs)
        if invalid == "stale":
            h.clock.advance(1.1)
            return observation
        timestamp = h.clock.now() + 0.5
        return replace(
            observation, frame_timestamp=timestamp,
            combat=replace(observation.combat, timestamp=timestamp),
        )

    if invalid == "minimap_delay":
        original_minimap = runner._minimap_view

        def minimap_view(now):
            result = original_minimap(now)
            h.clock.advance(1.1)
            return result

        monkeypatch.setattr(runner, "_minimap_view", minimap_view)
    else:
        monkeypatch.setattr(engine, "observe", observe)
    _run_for(h, runner, 12)
    assert not any(action.label in ("idle_camera", "combat_aim", "combat_fire")
                   for action in h.executor.submitted)
    reason = "future_frame" if invalid == "future" else "stale_frame"
    assert any(detail.get("reason") == reason for _, detail in events)


def test_gui_return_uses_time_after_forced_capture(tmp_path, monkeypatch):
    h, runner, focus, _, _, _ = _replay_runner(tmp_path, monkeypatch)
    focus.update(gui=True, game=False)
    runner.step()
    resumed = []
    original = h.machine.continue_after_gui

    def continue_after_gui(observation, now):
        resumed.append((observation.frame_timestamp, now))
        return original(observation, now)

    monkeypatch.setattr(h.machine, "continue_after_gui", continue_after_gui)
    focus.update(gui=False, game=True)
    _run_for(h, runner, 10)

    assert resumed
    assert all(frame_time <= now for frame_time, now in resumed)
    assert any(action.label == "idle_camera" for action in h.executor.submitted)


def test_focus_return_uses_time_after_forced_capture(tmp_path, monkeypatch):
    h, runner, focus, _, _, _ = _replay_runner(tmp_path, monkeypatch)
    runner.step()
    focus.update(game=False)
    h.clock.advance(1.1)
    runner.step()
    assert runner._focus_grace_until is not None
    resumed = []
    original = h.machine.continue_after_gui

    def continue_after_gui(observation, now):
        resumed.append((observation.frame_timestamp, now))
        return original(observation, now)

    monkeypatch.setattr(h.machine, "continue_after_gui", continue_after_gui)
    focus.update(game=True)
    h.clock.advance(0.1)
    runner.step()

    assert resumed
    assert all(frame_time <= now for frame_time, now in resumed)
    assert h.machine.state is State.BATTLE_ACTIVE


def test_runner_delayed_capture_idle_combat_round_trip(tmp_path, monkeypatch):
    h, runner, _, capture, _, events = _replay_runner(tmp_path, monkeypatch)
    _run_for(h, runner, 7)
    assert any(action.label == "idle_camera" for action in h.executor.submitted)
    clean = capture.image.copy()
    cv2.fillConvexPoly(
        capture.image, np.array([[2470, 1352], [2462, 1371], [2479, 1371]]), (25, 75, 250),
    )
    h.executor.submitted.clear()
    deadline = h.clock.now() + 2
    while not any(name == "behavior_mode" and detail["mode"] == "candidate" for name, detail in events):
        assert h.clock.now() < deadline
        h.executor.submitted.clear()
        _run_for(h, runner, 0.1)
    assert not any(action.label in ("idle_camera", "combat_fire", "combat_aim")
                   for action in h.executor.submitted)
    _run_for(h, runner, 1.4)
    assert any(action.label == "combat_fire" for action in h.executor.submitted)
    modes = [detail["mode"] for name, detail in events if name == "behavior_mode"]
    assert modes[:3] == ["idle", "candidate", "combat"]

    capture.image = clean
    h.executor.submitted.clear()
    _run_for(h, runner, 7)
    modes = [detail["mode"] for name, detail in events if name == "behavior_mode"]
    assert modes[-1] == "idle"
    assert any(action.label == "idle_camera" for action in h.executor.submitted)
    assert not any(detail.get("reason") == "future_frame" for _, detail in events)
