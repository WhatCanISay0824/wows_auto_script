"""数据页的坐标、只读记录与运行线程地图命令验证。"""

from dataclasses import replace
from datetime import date

import cv2
import numpy as np
import pytest
from src.common.clock import FakeClock
from src.common.types import Observation, Rect, State
from src.gui.data_page import map_point
from src.perception.capture import Frame
from src.runtime.history import read_history
from src.runtime.messages import RunCommand, RunCommandKind
from src.runtime.runner import SessionRunner, _make_minimap_provider
from src.storage.database import Database

from tests.test_autopilot import make_config, make_keymap, make_nav
from tests.test_runtime_service import _fake_runtime


def test_inactive_hud_capture_does_not_match_active_indicator():
    from pathlib import Path

    import yaml
    from src.common.types import NormalizedRect
    from src.perception.templates import TemplateMatcher

    root = Path(__file__).resolve().parents[1]
    profile = yaml.safe_load((root / "profiles/cn360/profile.yaml").read_text(encoding="utf-8"))
    threshold = profile["features"]["autopilot_indicator"]["threshold"]
    matcher = TemplateMatcher()
    template = root / "profiles/cn360/features/autopilot_indicator.png"
    matcher.register_feature("autopilot", template, NormalizedRect(0, 0, 1, 1), threshold)
    inactive = cv2.imread(str(root / "tests/fixtures/navigation/autopilot_inactive.png"))
    result = matcher.match("autopilot", inactive)
    assert result.score > 0.72  # 保留能复现原误报的实机样本。
    assert not result.found
    assert matcher.match("autopilot", cv2.imread(str(template))).found


def test_map_point_ignores_letterbox_and_preserves_normalized_coordinates():
    bounds = (100, 20, 300, 400)
    assert map_point(250, 120, bounds) == pytest.approx((0.5, 0.25))
    assert map_point(99, 120, bounds) is None
    assert map_point(400, 120, bounds) is None
    assert map_point(250, 19, bounds) is None
    assert map_point(10, 10, (0, 0, 0, 0)) is None


def test_history_is_read_only_account_scoped_and_does_not_invent_results(tmp_path):
    path = tmp_path / "records.db"
    db = Database(path)
    session = db.begin_session("cn360", "hash", account_alias="a")
    battle = db.begin_battle(session, account_alias="a")
    db.end_battle(battle, "dead", "menu")
    session_b = db.begin_session("cn360", "hash", account_alias="b")
    db.begin_battle(session_b, account_alias="b")
    db.close()
    before = path.read_bytes()
    view = read_history(path, "a")
    assert view.error is None
    assert view.today_completed == 1
    assert len(view.records) == 1
    assert view.records[0].result == "dead"
    assert view.records[0].account_alias == "a"
    assert read_history(path, "b").records[0].result is None
    assert read_history(path, "a", date(2000, 1, 1)).today_completed == 0
    assert path.read_bytes() == before
    assert read_history(tmp_path / "absent.db", None).records == ()
    assert not (tmp_path / "absent.db").exists()


def map_runner():
    runtime, _, _ = _fake_runtime()
    runtime.clock.advance(1.0)
    runtime.machine.state = State.BATTLE_ACTIVE
    runtime.autopilot = make_nav(make_config(), keymap=make_keymap())
    runtime.autopilot.start(0)
    pixels = np.full((200, 200, 3), (40, 90, 120), dtype=np.uint8)
    runtime.engine.last_frame = Frame(pixels, 1.0, Rect(0, 0, 200, 200), frame_id=5)
    runtime.engine.minimap_image = lambda frame: (frame.image[10:90, 20:100], Rect(20, 10, 80, 80))
    runtime.engine.detect_state = lambda now: State.BATTLE_ACTIVE
    runner = SessionRunner(runtime)
    frame = runner.snapshot_state(1.0).minimap
    assert frame is not None
    return runner, runtime, frame


def test_minimap_reuses_existing_frame_and_publishes_only_the_content_crop():
    runner, runtime, frame = map_runner()
    pixels = cv2.imdecode(np.frombuffer(frame.png, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert pixels.shape == (80, 80, 3)
    assert runner.snapshot_state(1.2).minimap is frame
    runtime.machine.state = State.PORT
    assert runner.snapshot_state(1.3).minimap is None


def test_production_minimap_provider_validates_real_frame_geometry_and_age():
    pixels = np.full((80, 80, 3), (40, 90, 120), dtype=np.uint8)
    frame = Frame(pixels, 10.0, Rect(10, 20, 800, 600), frame_id=5)

    class _Engine:
        last_frame = frame

        @staticmethod
        def minimap_image(candidate):
            assert candidate is frame
            return pixels, Rect(30, 40, 80, 80)

    clock = FakeClock()
    clock.advance(10.5)
    provider = _make_minimap_provider(_Engine(), clock)
    observation = Observation(
        timestamp=10.5,
        window_rect=frame.window_rect.to_tuple(),
        frame_id=5,
        frame_timestamp=10.0,
    )
    assert provider(observation) is pixels
    assert provider(replace(observation, frame_id=6)) is None
    assert provider(replace(observation, window_rect=(0, 20, 800, 600))) is None
    assert provider(replace(observation, frame_timestamp=9.0)) is None

    stale_clock = FakeClock()
    stale_clock.advance(12.1)
    stale_provider = _make_minimap_provider(_Engine(), stale_clock)
    assert stale_provider(observation) is None


def test_pause_and_gui_wait_keep_same_battle_map_but_new_battle_clears_it():
    runner, runtime, frame = map_runner()
    runtime.machine.state = State.PAUSED
    paused = runner.snapshot_state(2)
    assert paused.minimap is frame
    assert "已暂停" in paused.minimap_status
    runtime.machine.state = State.BATTLE_ACTIVE
    runner._gui_waiting = True
    waiting = runner.snapshot_state(3)
    assert waiting.minimap is frame and waiting.minimap.captured_at == 1
    assert "等待游戏前台" in waiting.minimap_status
    runtime.machine.battle_id += 1
    assert runner.snapshot_state(4).minimap is None


def test_target_is_queued_then_applied_from_fresh_game_observation():
    runner, runtime, frame = map_runner()
    command = RunCommand(RunCommandKind.REPLACE_TARGET, target=(0.4, 0.7),
                         battle_id=frame.battle_id, frame_id=frame.frame_id)
    assert runner.replace_target(command)
    assert runtime.autopilot.target_override is None
    stale = Observation(timestamp=1.2, window_rect=frame.window_rect, frame_timestamp=1.0)
    runner._apply_pending_target(stale, 1.2)
    assert runtime.autopilot.target_override is None
    fresh = replace(stale, frame_timestamp=1.5, timestamp=1.5)
    runner._apply_pending_target(fresh, 1.5)
    assert runtime.autopilot.target_override == (0.4, 0.7)
    assert "已交给" in runner.snapshot_state(1.5).navigation.target_status


def test_target_requests_game_focus_once_and_retains_visible_failure(monkeypatch):
    runner, runtime, frame = map_runner()
    calls = []
    runner._gui_foreground = lambda: True
    monkeypatch.setattr("src.runtime.runner._ensure_game_foreground",
                        lambda instance: calls.append(instance) or False)
    command = RunCommand(RunCommandKind.REPLACE_TARGET, target=(0.4, 0.7),
                         battle_id=frame.battle_id, frame_id=frame.frame_id)
    assert runner.replace_target(command)
    runner.snapshot_state(1.2)
    runner.snapshot_state(1.3)
    assert calls == [runtime]
    assert "切回游戏失败" in runner.snapshot_state(1.3).navigation.target_status
    assert runtime.autopilot.target_override is None


@pytest.mark.parametrize("change", ["other_battle", "unknown_frame", "invalid", "stopped"])
def test_target_rejects_invalid_or_stale_context(change):
    runner, runtime, frame = map_runner()
    command = RunCommand(RunCommandKind.REPLACE_TARGET, target=(0.5, 0.4),
                         battle_id=frame.battle_id, frame_id=frame.frame_id)
    if change == "other_battle":
        command = replace(command, battle_id=999)
    elif change == "unknown_frame":
        command = replace(command, frame_id=999)
    elif change == "invalid":
        command = replace(command, target=(float("nan"), 0.5))
    else:
        runtime.machine.state = State.STOPPING
    assert not runner.replace_target(command)
    assert runtime.autopilot.target_override is None


@pytest.mark.parametrize("change", ["battle", "window", "timeout", "stop", "pause"])
def test_accepted_target_is_rechecked_before_game_input(change):
    runner, runtime, frame = map_runner()
    command = RunCommand(RunCommandKind.REPLACE_TARGET, target=(0.3, 0.4),
                         battle_id=frame.battle_id, frame_id=frame.frame_id)
    assert runner.replace_target(command)
    observation = Observation(timestamp=2, window_rect=frame.window_rect, frame_timestamp=2)
    now = 2.0
    if change == "battle":
        runtime.machine.battle_id += 1
    elif change == "window":
        observation = replace(observation, window_rect=(0, 0, 300, 200))
    elif change == "timeout":
        now = 32
    elif change == "stop":
        runtime.safety.controller.request_stop("manual")
    else:
        runtime.safety.controller.request_pause("manual")
    runner._apply_pending_target(observation, now)
    assert runtime.autopilot.target_override is None


def test_pending_target_cancels_if_window_pause_changes_to_manual():
    runner, runtime, frame = map_runner()
    runtime.machine.state = State.PAUSED
    runtime.machine.pause_reason = "window_unfocused"
    request = runtime.safety.controller.request_pause("window_unfocused")
    command = RunCommand(RunCommandKind.REPLACE_TARGET, target=(0.3, 0.4),
                         battle_id=frame.battle_id, frame_id=frame.frame_id)
    assert runner.replace_target(command)
    assert runner._pending_target is command

    runtime.safety.controller.resume(expected_pause=request)
    runtime.safety.controller.request_pause("manual")
    runner._apply_pending_target(
        Observation(timestamp=2, window_rect=frame.window_rect, frame_timestamp=2), 2
    )
    assert runner._pending_target is None
    assert runtime.autopilot.target_override is None
    assert "人工" in runner.snapshot_state(2).navigation.target_status


def test_new_frame_does_not_invalidate_recent_click_from_previous_frame():
    runner, runtime, frame = map_runner()
    runtime.engine.last_frame = replace(runtime.engine.last_frame, frame_id=6, timestamp=1.4)
    runner.snapshot_state(1.4)
    runtime.clock.advance(0.5)
    assert runner.replace_target(RunCommand(RunCommandKind.REPLACE_TARGET, target=(0.3, 0.6),
                                           battle_id=frame.battle_id, frame_id=5))


def test_old_same_battle_map_is_selectable_and_current_result_screen_cancels_target():
    runner, runtime, frame = map_runner()
    runtime.clock.advance(60)
    assert runner.replace_target(RunCommand(RunCommandKind.REPLACE_TARGET, target=(0.3, 0.6),
                                           battle_id=frame.battle_id, frame_id=frame.frame_id))
    runtime.engine.detect_state = lambda now: State.BATTLE_FINISHED
    runner._apply_pending_target(Observation(timestamp=62, window_rect=frame.window_rect, frame_timestamp=62), 62)
    assert runner._pending_target is None
    assert runtime.autopilot.target_override is None


def test_selected_target_generates_m_open_then_full_map_click_then_m_close():
    from src.common.types import ActionKind

    runner, runtime, frame = map_runner()
    runtime.clock.advance(60)
    command = RunCommand(RunCommandKind.REPLACE_TARGET, target=(0.3, 0.6),
                         battle_id=frame.battle_id, frame_id=frame.frame_id)
    assert runner.replace_target(command)
    now = runtime.clock.now() + 1
    obs = Observation(timestamp=now, window_rect=frame.window_rect, frame_timestamp=now)
    runner._apply_pending_target(obs, now)
    nav = runtime.autopilot
    actions = []
    for offset in (0, 0.1, 1.2, 1.3, 1.9):
        actions.extend(nav.tick(replace(obs, timestamp=now+offset, frame_timestamp=now+offset), now+offset))
    assert [action.kind for action in actions] == [ActionKind.KEY_DOWN, ActionKind.KEY_UP,
        ActionKind.CLICK_ABS, ActionKind.KEY_DOWN, ActionKind.KEY_UP]
    assert [action.key for action in actions if action.key] == ["m", "m", "m", "m"]
    expected = nav.compute_full_map_target(obs)
    assert (actions[2].x, actions[2].y) == expected.screen_px
    assert expected.normalized == (0.3, 0.6)
    assert expected.surface == "full_map"
