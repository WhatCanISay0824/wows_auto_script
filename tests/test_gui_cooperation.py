"""真实状态机与会话执行器的 GUI 前台协作，感知和输入使用替身。"""

import logging
from types import SimpleNamespace

import pytest
from src.common.types import Rect, State, WindowInfo
from src.runtime.runner import SessionRunner

from tests.test_state_machine import Harness, drive_to_battle_active


def cooperative_runner(tmp_path, monkeypatch, *, behavior=None, autopilot=None):
    h = Harness(tmp_path, behavior=behavior)
    drive_to_battle_active(h)
    if autopilot is not None:
        h.autopilot = autopilot
        h.machine.deps.autopilot = autopilot
    h.perception.detected = State.BATTLE_ACTIVE
    h.perception.latest_frame = lambda *args, **kwargs: None
    h.executor.failed = False
    focus = {"gui": True, "game": False}
    runtime = SimpleNamespace(app=h.app, clock=h.clock, machine=h.machine, engine=h.perception,
        executor=h.executor, autopilot=h.autopilot, logger=logging.getLogger("tests.cooperation"),
        safety=SimpleNamespace(controller=h.controller, heartbeat=lambda now: None),
        bridge=SimpleNamespace(update_window_rect=lambda rect: None),
        window_locator=SimpleNamespace(find=lambda: WindowInfo(1, "game", "game.exe", Rect(0, 0, 2560, 1600),
                                                               foreground=focus["game"]),
                                       is_foreground=lambda hwnd: focus["game"]),
        overload_monitor=None)
    monkeypatch.setattr("src.runtime.runner.time.sleep", lambda duration: None)
    runner = SessionRunner(runtime, gui_foreground=lambda: focus["gui"])
    return h, runner, focus


def test_gui_wait_keeps_battle_and_return_rebuilds_actions(tmp_path, monkeypatch):
    h, runner, focus = cooperative_runner(tmp_path, monkeypatch)
    session, battle = h.machine.session_id, h.machine.battle_id
    runner.step()
    assert h.machine.state is State.BATTLE_ACTIVE
    assert not h.controller.paused
    assert not h.executor.gate_open
    assert runner._gui_waiting
    h.clock.advance(15)
    runner.step()
    assert h.machine.battle_id == battle
    assert h.machine.session_id == session
    focus.update(gui=False, game=True)
    runner.step()
    assert not runner._gui_waiting
    assert h.machine.state is State.BATTLE_ACTIVE
    assert h.executor.gate_open
    assert h.machine.battle_id == battle


def test_transient_unknown_then_gui_uses_grace_without_pausing(tmp_path, monkeypatch):
    h, runner, focus = cooperative_runner(tmp_path, monkeypatch)
    focus.update(gui=False, game=True)
    h.clock.advance(1)
    runner.step()
    focus.update(gui=False, game=False)
    h.clock.advance(0.1)
    runner.step()
    assert runner._focus_grace_until is not None
    assert h.machine.state is State.BATTLE_ACTIVE
    assert not h.controller.paused
    assert not h.executor.gate_open
    stopped = h.autopilot.stopped
    focus.update(gui=True, game=False)
    h.clock.advance(0.1)
    runner.step()
    assert runner._gui_waiting
    assert h.machine.state is State.BATTLE_ACTIVE
    assert not h.controller.paused
    assert not h.executor.gate_open
    assert h.autopilot.stopped == stopped


def test_other_app_focus_still_pauses(tmp_path, monkeypatch):
    h, runner, focus = cooperative_runner(tmp_path, monkeypatch)
    focus["gui"] = False
    h.clock.advance(1)
    runner.step()
    assert runner._focus_grace_until is not None
    assert h.machine.state is State.BATTLE_ACTIVE
    assert not h.executor.gate_open
    h.clock.advance(0.6)
    runner.step()
    assert h.machine.state is State.PAUSED
    assert h.machine.pause_reason == "window_unfocused"
    assert not h.executor.gate_open


def test_manual_pause_during_focus_grace_keeps_manual_priority(tmp_path, monkeypatch):
    h, runner, focus = cooperative_runner(tmp_path, monkeypatch)
    focus.update(gui=False, game=True)
    h.clock.advance(1)
    runner.step()
    focus["game"] = False
    h.clock.advance(0.1)
    runner.step()
    assert runner._focus_grace_until is not None

    h.controller.request_pause("manual", "user requested")
    h.clock.advance(0.1)
    runner.step()
    assert h.machine.state is State.PAUSED
    assert h.machine.pause_reason == "manual"
    assert h.controller.pause_request is not None
    assert h.controller.pause_request.reason == "manual"
    assert runner._focus_grace_until is None
    assert not h.executor.gate_open


def test_emergency_stop_is_processed_during_gui_wait(tmp_path, monkeypatch):
    h, runner, _ = cooperative_runner(tmp_path, monkeypatch)
    runner.step()
    h.controller.request_stop("hotkey")
    runner.step()
    assert h.machine.state is State.STOPPING
    assert not h.executor.gate_open


def test_interrupted_map_closes_then_resumes_without_pausing(tmp_path, monkeypatch):
    h, runner, focus = cooperative_runner(tmp_path, monkeypatch)
    h.autopilot.map_open = True
    from src.common.types import Action

    h.autopilot.close_map_after_gui = lambda now: [Action.key_down("m"), Action.key_up("m")]
    runner.step()
    focus.update(gui=False, game=True)
    runner.step()
    assert h.machine.state is State.BATTLE_ACTIVE
    assert [action.key for action in h.executor.submitted] == ["m", "m"]
    h.clock.advance(1)
    runner.step()
    assert h.machine.state is State.BATTLE_ACTIVE
    assert not runner._gui_waiting
    assert not h.controller.paused


def test_transient_focus_loss_with_open_map_closes_before_restarting_navigation(
    tmp_path, monkeypatch
):
    from src.common.types import Action

    class MapAutopilot:
        def __init__(self):
            self.map_open = True
            self.started = 0
            self.stopped = 0
            self.close_calls = 0

        def start(self, now):
            self.started += 1

        def stop(self):
            self.stopped += 1
            self.map_open = False

        def tick(self, observation, now):
            return [Action.key_down("m", label="new_open"), Action.key_up("m", label="new_open")]

        def close_map_after_gui(self, now):
            self.close_calls += 1
            self.map_open = False
            return [Action.key_down("m", label="gui_close"), Action.key_up("m", label="gui_close")]

    autopilot = MapAutopilot()
    h, runner, focus = cooperative_runner(tmp_path, monkeypatch, autopilot=autopilot)
    focus.update(gui=False, game=True)
    h.clock.advance(1)
    runner.step()
    h.executor.submitted.clear()

    focus.update(gui=False, game=False)
    h.clock.advance(0.1)
    runner.step()
    assert runner._gui_map_interrupted
    focus["game"] = True
    h.clock.advance(0.1)
    runner.step()
    assert autopilot.close_calls == 1
    assert [action.key for action in h.executor.submitted if action.key] == ["m", "m"]

    h.clock.advance(0.6)
    runner.step()
    assert not runner._gui_waiting
    assert h.machine.state is State.BATTLE_ACTIVE
    assert h.executor.gate_open
    assert [action.key for action in h.executor.submitted if action.key] == ["m", "m", "m", "m"]


def test_return_from_gui_does_not_override_manual_pause(tmp_path, monkeypatch):
    h, runner, focus = cooperative_runner(tmp_path, monkeypatch)
    runner.step()
    h.controller.request_pause("manual")
    h.clock.advance(1)
    runner.step()
    assert h.machine.state is State.PAUSED
    focus.update(gui=False, game=True)
    h.clock.advance(1)
    runner.step()
    assert h.machine.state is State.PAUSED
    assert not h.executor.gate_open


def test_return_from_gui_does_not_override_stop(tmp_path, monkeypatch):
    h, runner, focus = cooperative_runner(tmp_path, monkeypatch)
    runner.step()
    h.controller.request_stop("hotkey")
    focus.update(gui=False, game=True)
    runner.step()
    assert h.machine.state is State.STOPPING
    assert not h.executor.gate_open


def test_window_resume_stays_running_and_produces_behavior_actions(tmp_path, monkeypatch):
    from src.behavior.scheduler import BehaviorScheduler
    from src.common.types import ActionKind

    from tests.test_behavior import make_config, make_keymap

    behavior = BehaviorScheduler(make_config(camera_interval_s=(1, 1), long_stare_probability=0), make_keymap())
    h, runner, focus = cooperative_runner(tmp_path, monkeypatch, behavior=behavior)
    battle_id = h.machine.battle_id
    focus.update(gui=False, game=True)
    runner.step()
    h.clock.advance(15)
    runner.step()
    assert any(action.kind is ActionKind.MOVE_REL for action in h.executor.submitted)
    h.executor.submitted.clear()
    focus["game"] = False
    h.clock.advance(1)
    runner.step()
    assert h.machine.state is State.BATTLE_ACTIVE
    assert not h.executor.gate_open
    h.clock.advance(0.6)
    runner.step()
    assert h.controller.paused and h.machine.state is State.PAUSED
    focus["game"] = True
    h.clock.advance(1)
    runner.step()
    assert not h.controller.paused
    for _ in range(12):
        h.clock.advance(0.5)
        runner.step()
        assert h.machine.state is State.BATTLE_ACTIVE
        assert not h.controller.paused
    assert h.machine.battle_id == battle_id
    assert any(action.kind is ActionKind.MOVE_REL for action in h.executor.submitted)


def test_explicit_resume_refreshes_stale_window_cache(tmp_path, monkeypatch):
    h, runner, focus = cooperative_runner(tmp_path, monkeypatch)
    focus.update(gui=False, game=False)
    h.clock.advance(1)
    runner.step()
    assert h.machine.state is State.BATTLE_ACTIVE
    assert not h.executor.gate_open
    h.clock.advance(0.6)
    runner.step()
    assert h.machine.state is State.PAUSED
    # 同一秒内恢复，仍必须刷新刚才的失焦缓存。
    monkeypatch.setattr("src.runtime.runner._ensure_game_foreground",
                        lambda runtime: focus.update(game=True) or True)
    assert runner.request_resume()
    runner.step()
    assert h.machine.state is State.BATTLE_ACTIVE
    assert not h.controller.paused


def test_resume_before_first_behavior_tick_initializes_scheduler(tmp_path, monkeypatch):
    from src.behavior.scheduler import BehaviorScheduler
    from src.common.types import ActionKind

    from tests.test_behavior import make_config, make_keymap

    scheduler = BehaviorScheduler(make_config(camera_interval_s=(1, 1), long_stare_probability=0), make_keymap())
    h, runner, focus = cooperative_runner(tmp_path, monkeypatch, behavior=scheduler)
    assert scheduler.profile is None
    focus.update(gui=False, game=False)
    h.clock.advance(1)
    runner.step()
    focus["game"] = True
    h.clock.advance(1)
    runner.step()
    assert scheduler.running
    h.clock.advance(15)
    runner.step()
    assert any(action.kind is ActionKind.MOVE_REL for action in h.executor.submitted)


def test_switching_to_gui_during_capture_does_not_pause_session(tmp_path, monkeypatch):
    from dataclasses import replace

    h, runner, focus = cooperative_runner(tmp_path, monkeypatch)
    focus.update(gui=False, game=True)
    original = h.perception.observe

    def switch_during_capture(*args, **kwargs):
        focus.update(gui=True, game=False)
        return replace(original(*args, **kwargs), window_missing=True, capture_error="游戏窗口不在前台")

    h.perception.observe = switch_during_capture
    h.clock.advance(1)
    runner.step()
    assert runner._gui_waiting
    assert h.machine.state is State.BATTLE_ACTIVE
    assert not h.controller.paused
    assert not h.executor.gate_open


def test_target_during_focus_pause_queues_resumes_and_drives_real_full_map_click(
    tmp_path, monkeypatch
):
    import numpy as np
    from src.navigation.autopilot import AutoPilotNavigator
    from src.perception.capture import Frame
    from src.runtime.messages import RunCommand, RunCommandKind, RunLifecycle
    from src.runtime.service import RuntimeService

    from tests.test_autopilot import make_config as make_nav_config

    h = Harness(tmp_path)
    drive_to_battle_active(h)
    nav = AutoPilotNavigator(make_nav_config(), h.app.profile, keymap=h.app.keymap)
    h.autopilot = nav
    h.machine.deps.autopilot = nav
    h.perception.detected = State.BATTLE_ACTIVE
    h.perception.latest_frame = lambda *args, **kwargs: None
    h.executor.failed = False
    focus = {"gui": False, "game": True}
    runtime = SimpleNamespace(
        app=h.app,
        clock=h.clock,
        machine=h.machine,
        engine=h.perception,
        executor=h.executor,
        session=h.session,
        autopilot=nav,
        logger=logging.getLogger("tests.cooperation"),
        safety=SimpleNamespace(controller=h.controller, heartbeat=lambda now: None),
        bridge=SimpleNamespace(update_window_rect=lambda rect: None),
        window_locator=SimpleNamespace(
            find=lambda: WindowInfo(
                1, "game", "game.exe", Rect(0, 0, 2560, 1600), foreground=focus["game"]
            ),
            is_foreground=lambda hwnd: focus["game"],
        ),
        overload_monitor=None,
    )
    monkeypatch.setattr("src.runtime.runner.time.sleep", lambda duration: None)
    runner = SessionRunner(runtime, gui_foreground=lambda: focus["gui"])
    pixels = np.full((300, 300, 3), (40, 90, 120), dtype=np.uint8)
    h.perception.last_frame = Frame(pixels, 1.0, Rect(0, 0, 2560, 1600), frame_id=5)
    h.perception.minimap_image = lambda frame: (frame.image[10:90, 20:100], Rect(20, 10, 80, 80))

    h.clock.advance(1)
    runner.step()
    frame = runner.snapshot_state(h.clock.now()).minimap
    assert frame is not None and frame.frame_id == 5

    focus.update(gui=False, game=False)
    h.clock.advance(0.1)
    runner.step()
    assert runner._focus_grace_until is not None
    h.clock.advance(0.6)
    runner.step()
    assert h.machine.state is State.PAUSED
    assert h.machine.pause_reason == "window_unfocused"

    service = RuntimeService(tmp_path, clock=h.clock)
    service._runner = runner
    service._lifecycle = RunLifecycle.PAUSED
    with service._lock:
        service._rebuild_snapshot_locked()
    assert service.snapshot().game.pause_reason == "window_unfocused"

    command = RunCommand(
        RunCommandKind.REPLACE_TARGET,
        target=(0.3, 0.6),
        battle_id=frame.battle_id,
        frame_id=frame.frame_id,
    )
    focus.update(gui=True, game=False)
    monkeypatch.setattr(
        "src.runtime.runner._ensure_game_foreground",
        lambda instance: focus.update(game=True) or True,
    )
    assert service.submit_command(command)
    service._drain_commands()
    assert runner._pending_target is command
    assert not h.controller.paused
    assert nav.target_override is None

    h.executor.submitted.clear()
    runner.step()
    assert h.machine.state is State.BATTLE_ACTIVE
    assert not any(action.key == "m" for action in h.executor.submitted)

    focus["gui"] = False
    h.clock.advance(0.6)
    runner.step()
    assert runner._pending_target is None
    assert nav.target_override == pytest.approx((0.3, 0.6))
    for _ in range(10):
        h.clock.advance(0.2)
        runner.step()
    keys = [action.key for action in h.executor.submitted if action.key]
    assert keys == ["m", "m", "m", "m"]
    from src.common.types import ActionKind

    click = next(action for action in h.executor.submitted if action.kind is ActionKind.CLICK_ABS)
    expected = nav.compute_full_map_target(h.obs())
    assert (click.x, click.y) == expected.screen_px
    service._runner = None
