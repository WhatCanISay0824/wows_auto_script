"""本进程恢复身份与 idle/candidate/combat 模式切换回归。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
from src.behavior.random_profile import RandomProfile
from src.behavior.scheduler import BehaviorContext, BehaviorScheduler
from src.common.types import (
    ActionKind,
    CombatObservation,
    EnemyContact,
    NormalizedRect,
    Observation,
    ShipPosition,
    State,
)
from src.config.models import BehaviorConfig, KeyMap, MinimapConfig
from src.perception.combat import MinimapCombatObserver

from tests.test_state_machine import Harness, _match


def _drive_to_queue(h: Harness) -> None:
    h.machine.start()
    h.perception.detected = State.PORT
    h.step()
    h.step()
    h.step()
    h.step()
    h.step()
    assert h.machine.state is State.START_COOP
    h.perception.features["queue_indicator"] = _match("queue_indicator")
    h.step()
    assert h.machine.state is State.QUEUEING
    assert h.machine.battle_id is not None


def _drive_to_paused(h: Harness, paused_state: State) -> None:
    _drive_to_queue(h)
    if paused_state is State.LOADING:
        h.perception.features["loading_indicator"] = _match("loading_indicator")
        h.step()
        assert h.machine.state is State.LOADING


@pytest.mark.parametrize("source", ["user", "window"])
@pytest.mark.parametrize("paused_state", [State.QUEUEING, State.LOADING])
@pytest.mark.parametrize("detected", [State.QUEUEING, State.LOADING, State.BATTLE_ACTIVE])
def test_same_process_resume_keeps_battle_id_and_detected_state(
    tmp_path: Path, source: str, paused_state: State, detected: State
) -> None:
    h = Harness(tmp_path)
    _drive_to_paused(h, paused_state)
    battle_id = h.machine.battle_id
    assert battle_id is not None

    h.perception.detected = detected
    if source == "user":
        h.machine.request_pause("manual")
        assert h.machine.state is State.PAUSED
        h.controller.resume()
        h.machine.request_resume()
        h.step()
    else:
        h.machine.request_pause("window_unfocused")
        assert h.machine.state is State.PAUSED
        h.step()

    assert h.machine.state is detected
    assert h.machine.battle_id == battle_id
    assert h.session.battle_starts == 1
    assert h.session.battles_completed == 0
    assert h.events.battles == []
    if detected is State.BATTLE_ACTIVE:
        assert h.machine.battle is not None
    else:
        assert h.machine.battle is None
        assert h.behavior.started == 0


def test_resume_unknown_after_queue_keeps_paused_and_battle_id(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    _drive_to_queue(h)
    battle_id = h.machine.battle_id
    assert battle_id is not None

    h.machine.request_pause("manual")
    h.perception.detected = None
    h.controller.resume()
    h.machine.request_resume()
    h.step()

    assert h.machine.state is State.PAUSED
    assert h.machine.pause_reason == "resume_unknown"
    assert h.machine.battle_id == battle_id
    assert h.session.battle_starts == 1
    assert h.events.battles == []


def test_new_process_refuses_existing_battle(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.machine.start()
    h.perception.detected = State.BATTLE_ACTIVE
    h.step()
    assert h.machine.state is State.PAUSED
    assert h.machine.battle_id is None

    h.controller.resume()
    h.machine.request_resume()
    h.step()
    assert h.machine.state is State.PAUSED
    assert h.machine.pause_reason == "restart_mid_battle"
    assert h.machine.battle_id is None
    assert h.session.battle_starts == 0
    assert h.events.battles == []


def _make_keymap() -> KeyMap:
    return KeyMap(
        actions={"fire": "mouse_left", "escape": "escape"},
        camera_relative=True,
        camera_sensitivity=1.0,
    )


def _make_config(**overrides) -> BehaviorConfig:
    values = {
        "targeted_combat": True,
        "battle_start_quiet_s": 0.0,
        "idle_camera_interval_s": (2.0, 2.0),
        "idle_camera_move_px": (40, 40),
        "idle_camera_duration_s": (0.6, 0.6),
        "idle_camera_actions_per_minute_max": 180,
    }
    values.update(overrides)
    return replace(BehaviorConfig(), **values)


def _make_scheduler(
    config: BehaviorConfig | None = None, *, started_at: float = 0.0
) -> tuple[BehaviorScheduler, list[tuple[str, dict]]]:
    events: list[tuple[str, dict]] = []
    scheduler = BehaviorScheduler(
        config or _make_config(), _make_keymap(), on_event=lambda name, detail: events.append((name, detail))
    )
    scheduler.start(RandomProfile.generate(_make_config(), seed=1), started_at)
    return scheduler, events


def _observation(
    now: float,
    combat: CombatObservation | None,
    *,
    frame_timestamp: float | None = None,
) -> Observation:
    return Observation(
        timestamp=now,
        window_rect=(100, 50, 1280, 800),
        frame_timestamp=now if frame_timestamp is None else frame_timestamp,
        combat=combat,
    )


def _mode_events(events: list[tuple[str, dict]]) -> list[dict]:
    return [detail for name, detail in events if name == "behavior_mode"]


def test_idle_candidate_confirmed_combat_and_back_to_idle() -> None:
    image = np.full((400, 400, 3), (39, 31, 27), dtype=np.uint8)
    cv2.ellipse(image, (200, 200), (150, 150), 0, 25, 85, (56, 49, 35), -1)
    cv2.fillConvexPoly(image, np.array([[280, 150], [272, 169], [289, 169]]), (25, 75, 250))
    observer = MinimapCombatObserver(MinimapConfig(rect=NormalizedRect(0, 0, 1, 1)))
    own = ShipPosition((200, 200), None, 300, 0, 0.95, "marker")
    scheduler, events = _make_scheduler()

    completed_idle = []
    for index in range(27):
        now = round(index / 10.0, 10)
        frame = CombatObservation(now, index + 1, (200.0, 200.0), None, (), 0)
        completed_idle.extend(scheduler.tick(_observation(now, frame), now))
    assert len(completed_idle) == 4
    assert sum(abs(action.dx) for action in completed_idle) == 40
    assert len([name for name, _ in events if name == "idle_camera_move"]) == 1

    second_start = CombatObservation(4.0, 27, (200.0, 200.0), None, (), 0)
    started = scheduler.tick(_observation(4.0, second_start), 4.0)
    assert len(started) == 1 and abs(started[0].dx) == 1
    assert len([name for name, _ in events if name == "idle_camera_move"]) == 2
    pre_candidate_modes = _mode_events(events)
    assert len(pre_candidate_modes) == 1
    assert pre_candidate_modes[0]["mode"] == "idle"
    assert pre_candidate_modes[0]["reason"] == "no_contact"

    candidate = observer.observe(image, own, 1, 4.2)
    assert candidate.enemy_candidates == 1 and not candidate.enemies
    assert scheduler.tick(_observation(4.2, candidate), 4.2) == []

    confirmed = observer.observe(image, own, 2, 4.7)
    assert len(confirmed.enemies) == 1
    combat_actions = scheduler.tick(_observation(4.7, confirmed), 4.7)
    assert combat_actions
    assert combat_actions[0].label in ("combat_aim", "combat_fire")

    blank = np.full_like(image, (39, 31, 27))
    absent = observer.observe(blank, own, 3, 5.2)
    assert not absent.enemies and absent.enemy_candidates == 0
    assert scheduler.tick(_observation(5.2, absent), 5.2) == []

    idle_moves: list[tuple[float, object]] = []
    for index in range(53, 91):
        now = round(index / 10.0, 10)
        frame = CombatObservation(now, 27 + index, (200.0, 200.0), None, (), 0)
        idle_moves.extend((now, action) for action in scheduler.tick(_observation(now, frame), now))

    assert idle_moves
    assert idle_moves[0][0] >= 7.2
    assert all(action.label == "idle_camera" for _, action in idle_moves)
    assert all(action.kind is ActionKind.MOVE_REL for _, action in idle_moves)
    assert all(abs(action.dx) <= 24 for _, action in idle_moves)
    assert len([name for name, _ in events if name == "idle_camera_move"]) == 3
    modes = [detail["mode"] for detail in _mode_events(events)]
    assert modes[:4] == ["idle", "candidate", "combat", "idle"]


def test_candidate_without_camera_logs_wait_and_cannot_fire() -> None:
    scheduler, events = _make_scheduler()
    frame = CombatObservation(10.0, 100, (100.0, 100.0), None, (), 1)
    assert scheduler.tick(_observation(10.0, frame), 10.0) == []

    mode = _mode_events(events)[-1]
    assert mode["mode"] == "candidate"
    assert mode["reason"] == "camera_unavailable"
    assert mode["candidate_count"] == 1
    assert mode["confirmed_count"] == 0
    assert mode["own_available"] is True
    assert mode["camera_available"] is False
    assert mode["frame_age_s"] == pytest.approx(0.0)

    assert scheduler.tick(_observation(10.1, replace(frame, timestamp=10.1, frame_id=101)), 10.1) == []
    assert len(_mode_events(events)) == 1


@pytest.mark.parametrize(
    "reason", ["own_unavailable", "camera_unavailable", "combat_frame_unavailable", "input_quota"]
)
def test_confirmed_evidence_gaps_are_blocked(reason: str) -> None:
    config = _make_config(actions_per_minute_max=0) if reason == "input_quota" else _make_config()
    scheduler, events = _make_scheduler(config)
    own = None if reason == "own_unavailable" else (100.0, 100.0)
    camera = None if reason == "camera_unavailable" else 90.0
    timestamp = 8.0 if reason == "combat_frame_unavailable" else 10.0
    frame = CombatObservation(
        timestamp, 100, own, camera, (EnemyContact((180.0, 100.0)),), 1
    )

    assert scheduler.tick(_observation(10.0, frame), 10.0) == []
    mode = _mode_events(events)[-1]
    assert mode["mode"] == "blocked"
    assert mode["reason"] == reason


@pytest.mark.parametrize(
    ("blocker", "reason"),
    [("foreground", "foreground"), ("modal", "modal_visible"), ("stale", "stale_frame"), ("future", "future_frame")],
)
def test_blocked_mode_is_deduplicated(blocker: str, reason: str) -> None:
    scheduler, events = _make_scheduler()
    frame = CombatObservation(10.0, 100, (100.0, 100.0), 90.0, (EnemyContact((180.0, 100.0)),), 1)
    context = BehaviorContext()
    observation = _observation(10.0, frame)
    if blocker == "foreground":
        context = replace(context, foreground=False)
    elif blocker == "modal":
        context = replace(context, modal_visible=True)
    elif blocker == "stale":
        frame = replace(frame, timestamp=8.9)
        observation = replace(observation, combat=frame, frame_timestamp=8.9)
    else:
        frame = replace(frame, timestamp=12.0)
        observation = replace(observation, combat=frame, frame_timestamp=12.0)

    assert scheduler.tick(observation, 10.0, context) == []
    assert scheduler.tick(observation, 10.0, context) == []
    blocked = _mode_events(events)
    assert len(blocked) == 1
    assert blocked[0]["mode"] == "blocked"
    assert blocked[0]["reason"] == reason
    assert blocked[0]["candidate_count"] == 1
    assert blocked[0]["confirmed_count"] == 1

    fresh = CombatObservation(10.2, 102, (100.0, 100.0), 90.0, (), 0)
    assert scheduler.tick(_observation(10.2, fresh), 10.2) == []
    assert _mode_events(events)[-1]["mode"] == "idle"


def test_quiet_contact_is_blocked_and_idle_wait_is_not_blocked() -> None:
    scheduler, events = _make_scheduler(_make_config(battle_start_quiet_s=5.0))
    frame = CombatObservation(1.0, 10, (100.0, 100.0), 90.0, (EnemyContact((180.0, 100.0)),), 1)
    assert scheduler.tick(_observation(1.0, frame), 1.0) == []
    assert scheduler.tick(_observation(1.0, frame), 1.0) == []
    quiet = _mode_events(events)
    assert len(quiet) == 1
    assert quiet[0]["mode"] == "blocked"
    assert quiet[0]["reason"] == "quiet"

    scheduler, events = _make_scheduler()
    idle = CombatObservation(0.0, 1, (100.0, 100.0), None, (), 0)
    assert scheduler.tick(_observation(0.0, idle), 0.0) == []
    assert scheduler.tick(_observation(0.1, replace(idle, timestamp=0.1, frame_id=2)), 0.1) == []
    modes = _mode_events(events)
    assert len(modes) == 1
    assert modes[0]["mode"] == "idle"
    assert modes[0]["reason"] == "no_contact"
    assert modes[0]["mode"] != "blocked"
