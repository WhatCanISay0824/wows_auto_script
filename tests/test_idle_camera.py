"""无目标短幅水平视角动作：时间、取消、边界与输入队列时序。"""

from __future__ import annotations

import math
import random
import threading
from dataclasses import replace

import pytest
from src.behavior.idle_camera import IdleCameraController
from src.common.clock import FakeClock, FakeSleeper
from src.common.types import Action, ActionKind, CombatObservation, EnemyContact
from src.config.models import BehaviorConfig, InputConfig
from src.execution.queue import InputExecutor


def make_config(**overrides) -> BehaviorConfig:
    values = {
        "targeted_combat": True,
        "max_input_frame_age_s": 0.5,
        "idle_camera_interval_s": (3.0, 4.5),
        "idle_camera_move_px": (24, 48),
        "idle_camera_duration_s": (0.5, 0.7),
        "idle_camera_actions_per_minute_max": 180,
    }
    values.update(overrides)
    return replace(BehaviorConfig(), **values)


def make_frame(
    now: float,
    *,
    frame_id: int = 1,
    own: tuple[float, float] | None = (100.0, 100.0),
    camera: float | None = None,
    enemies: tuple[EnemyContact, ...] = (),
    candidates: int = 0,
) -> CombatObservation:
    return CombatObservation(now, frame_id, own, camera, enemies, candidates)


def smoothstep(value: float) -> float:
    return value * value * (3.0 - 2.0 * value)


def tick_indices(controller: IdleCameraController, first: int, last: int):
    """按 10Hz 序号推进，避免测试自身制造主循环停顿。"""
    actions: list[Action] = []
    detail: dict = {}
    for index in range(first, last + 1):
        now = round(index / 10.0, 10)
        actions, detail = controller.tick(make_frame(now, frame_id=index), now)
    return actions, detail


def test_three_minutes_of_10hz_ticks_respects_start_spacing_and_short_ttl() -> None:
    config = make_config()
    controller = IdleCameraController(config)
    controller.reset(seed=20261103)
    movement_starts: list[float] = []
    movement_amplitudes: dict[int, int] = {}
    completed_movements: set[int] = set()
    directions: set[int] = set()
    movement_dx: dict[int, int] = {}
    first_action_at: float | None = None

    for index in range(1801):
        now = index / 10.0
        frame = make_frame(now, frame_id=index)
        actions, detail = controller.tick(frame, now)
        assert len(actions) <= 1
        if detail.get("reason") == "idle_motion_started":
            movement_starts.append(now)
            amplitude = int(detail["amplitude_px"])
            directions.add(1 if amplitude > 0 else -1)
            movement_id = int(detail["movement_id"])
            movement_amplitudes[movement_id] = abs(amplitude)
            movement_dx[movement_id] = 0
        if actions:
            action = actions[0]
            assert action.kind is ActionKind.MOVE_REL
            assert action.dy == 0
            assert action.label == "idle_camera"
            assert action.expires_at is not None
            assert action.expires_at <= now + 0.15 + 1e-9
            assert action.expires_at <= frame.timestamp + config.max_input_frame_age_s + 1e-9
            movement_id = int(detail["movement_id"])
            movement_dx[movement_id] += int(action.dx)
            if first_action_at is None:
                first_action_at = now
        if detail.get("reason") == "idle_motion_complete":
            completed_movements.add(int(detail["movement_id"]))

    assert movement_starts
    assert movement_starts[0] <= 5.0
    assert first_action_at is not None and first_action_at <= 5.0
    gaps = [
        later - earlier
        for earlier, later in zip(movement_starts, movement_starts[1:], strict=False)
    ]
    assert gaps
    assert all(3.0 - 1e-9 <= gap <= 4.6 for gap in gaps)
    assert max(gaps) - min(gaps) > 0.05
    assert directions == {-1, 1}
    assert all(24 <= amplitude <= 48 for amplitude in movement_amplitudes.values())
    assert completed_movements
    assert all(
        abs(movement_dx[movement_id]) == movement_amplitudes[movement_id]
        for movement_id in completed_movements
    )


def test_steps_are_at_least_200ms_apart_and_follow_smoothstep() -> None:
    config = make_config(
        idle_camera_interval_s=(2.0, 2.0),
        idle_camera_move_px=(40, 40),
        idle_camera_duration_s=(0.6, 0.6),
    )
    controller = IdleCameraController(config)
    controller.reset(seed=7)

    actions, _ = tick_indices(controller, 0, 0)
    assert actions == []

    start_actions, start_detail = tick_indices(controller, 1, 20)
    assert len(start_actions) == 1
    assert abs(int(start_actions[0].dx)) == 1
    assert start_actions[0].expires_at is not None
    assert start_detail["reason"] == "idle_motion_started"
    amplitude = int(start_detail["amplitude_px"])
    assert abs(amplitude) == 40
    movement_id = int(start_detail["movement_id"])

    observed: list[tuple[float, int]] = [(2.0, int(start_actions[0].dx))]
    for step in range(1, 9):
        now = round(2.0 + step / 10.0, 10)
        actions, detail = controller.tick(make_frame(now, frame_id=20 + step), now)
        assert len(actions) <= 1
        if actions:
            assert detail["movement_id"] == movement_id
            observed.append((now, int(actions[0].dx)))
            if detail["reason"] == "idle_motion_complete":
                break

    assert [round(now, 6) for now, _ in observed] == [2.0, 2.2, 2.4, 2.6]
    assert all(
        later - earlier >= 0.2 - 1e-9
        for (earlier, _), (later, _) in zip(observed, observed[1:], strict=False)
    )
    cumulative = observed[0][1]
    assert abs(observed[0][1]) == 1
    for now, dx in observed[1:]:
        elapsed = now - 2.0
        cumulative += dx
        expected = round(amplitude * smoothstep(min(1.0, elapsed / 0.6)))
        assert cumulative == expected
    assert cumulative == amplitude
    steps = [abs(dx) for _, dx in observed]
    assert steps[0] < max(steps)
    assert steps[-1] < max(steps)


def test_idle_requires_fresh_own_position_but_not_camera_direction() -> None:
    config = make_config(idle_camera_interval_s=(2.0, 2.0))
    controller = IdleCameraController(config)
    controller.reset(seed=3)
    now = 10.0

    assert not controller.eligible(None, now)
    assert not controller.eligible(make_frame(now, own=None), now)
    assert not controller.eligible(make_frame(now, own=(math.nan, 100.0)), now)
    assert not controller.eligible(make_frame(now, own=(100.0, math.inf)), now)
    assert not controller.eligible(make_frame(now - 0.51), now)
    assert not controller.eligible(make_frame(now + 0.01), now)
    assert not controller.eligible(make_frame(now, enemies=(EnemyContact((150.0, 100.0)),)), now)
    assert not controller.eligible(make_frame(now, candidates=1), now)
    assert controller.eligible(make_frame(now, camera=None), now)
    assert controller.eligible(make_frame(now, camera=90.0), now)


@pytest.mark.parametrize("enemy_present", [False, True])
def test_enemy_or_candidate_cancels_pending_motion(enemy_present: bool) -> None:
    config = make_config(
        idle_camera_interval_s=(2.0, 2.0),
        idle_camera_move_px=(40, 40),
        idle_camera_duration_s=(0.6, 0.6),
    )
    controller = IdleCameraController(config)
    controller.reset(seed=11)
    tick_indices(controller, 0, 20)
    actions, _ = controller.tick(make_frame(2.2, frame_id=22), 2.2)
    assert len(actions) == 1

    if enemy_present:
        blocker = make_frame(2.3, frame_id=23, enemies=(EnemyContact((150.0, 100.0)),))
    else:
        blocker = make_frame(2.3, frame_id=23, candidates=1)
    actions, detail = controller.tick(blocker, 2.3)
    assert actions == []
    assert detail["reason"] == "idle_ineligible"

    resumed = 0.0
    for index in range(24, 60):
        now = round(index / 10.0, 10)
        actions, _ = controller.tick(make_frame(now, frame_id=index), now)
        if actions:
            resumed = now
            break
    assert resumed >= 4.4, "取消后的旧余量不能继续补发"


def test_tick_gap_over_300ms_cancels_remainder_without_catch_up() -> None:
    config = make_config(
        idle_camera_interval_s=(2.0, 2.0),
        idle_camera_move_px=(40, 40),
        idle_camera_duration_s=(0.6, 0.6),
    )
    controller = IdleCameraController(config)
    controller.reset(seed=13)
    tick_indices(controller, 0, 20)
    actions, _ = controller.tick(make_frame(2.2, frame_id=22), 2.2)
    assert len(actions) == 1

    actions, detail = controller.tick(make_frame(2.55, frame_id=25), 2.55)
    assert actions == []
    assert detail["reason"] == "idle_motion_cancelled_gap"

    next_action = None
    for index in range(26, 70):
        now = round(index / 10.0, 10)
        actions, _ = controller.tick(make_frame(now, frame_id=index), now)
        if actions:
            next_action = now
            break
    assert next_action is not None
    assert next_action == 4.0


@pytest.mark.parametrize("blocker", ["frame", "own", "candidate"])
def test_gap_with_invalid_evidence_clears_motion_and_next_start(blocker: str) -> None:
    config = make_config(
        idle_camera_interval_s=(2.0, 2.0),
        idle_camera_move_px=(40, 40),
        idle_camera_duration_s=(0.6, 0.6),
    )
    controller = IdleCameraController(config)
    controller.reset(seed=31)
    tick_indices(controller, 0, 20)
    actions, _ = controller.tick(make_frame(2.2, frame_id=22), 2.2)
    assert len(actions) == 1

    if blocker == "frame":
        frame = None
    elif blocker == "own":
        frame = make_frame(3.0, frame_id=30, own=None)
    else:
        frame = make_frame(3.0, frame_id=30, candidates=1)
    actions, detail = controller.tick(frame, 3.0)
    assert actions == []
    assert detail["reason"] in ("idle_ineligible", "idle_frame_unavailable")

    actions, detail = controller.tick(make_frame(3.1, frame_id=31), 3.1)
    assert actions == []
    assert detail["reason"] == "idle_wait"
    assert detail["next_start_at"] == pytest.approx(5.1)


def test_slow_0_4s_ticks_produce_bounded_moves_without_gap_starvation() -> None:
    config = make_config(
        idle_camera_interval_s=(2.0, 2.0),
        idle_camera_move_px=(48, 48),
        idle_camera_duration_s=(0.6, 0.6),
    )
    controller = IdleCameraController(config)
    controller.reset(seed=19)

    moves: list[tuple[float, int]] = []
    for index in range(51):
        now = round(index * 0.4, 10)
        actions, _ = controller.tick(make_frame(now, frame_id=index), now)
        moves.extend((now, int(action.dx)) for action in actions)

    assert moves
    assert all(abs(dx) <= 24 for _, dx in moves)
    assert [now for now, _ in moves[:3]] == [2.0, 4.0, 6.0]
    assert all(abs(dx) == 1 for _, dx in moves)


def test_irregular_tick_intervals_do_not_starve_idle_motion_forever() -> None:
    config = make_config(
        idle_camera_interval_s=(1.0, 1.0),
        idle_camera_move_px=(80, 80),
        idle_camera_duration_s=(0.6, 0.6),
    )
    controller = IdleCameraController(config)
    controller.reset(seed=23)

    now = 0.0
    frame_id = 0
    moves: list[tuple[float, int]] = []
    for interval in (0.1, 0.2, 0.4, 0.1, 0.4, 0.2):
        for _ in range(25):
            now = round(now + interval, 10)
            frame_id += 1
            actions, _ = controller.tick(make_frame(now, frame_id=frame_id), now)
            moves.extend((now, int(action.dx)) for action in actions)
            if now >= 20.0:
                break
        if now >= 20.0:
            break

    assert moves
    assert all(abs(dx) <= 24 for _, dx in moves)


def test_single_idle_step_is_capped_at_24_counts() -> None:
    config = make_config(
        idle_camera_interval_s=(0.1, 0.1),
        idle_camera_move_px=(80, 80),
        idle_camera_duration_s=(0.2, 0.2),
    )
    controller = IdleCameraController(config)
    controller.reset(seed=29)

    moves: list[int] = []
    for index in range(5):
        now = round(index / 10.0, 10)
        actions, _ = controller.tick(make_frame(now, frame_id=index), now)
        moves.extend(int(action.dx) for action in actions)

    assert len(moves) == 3
    assert abs(moves[0]) == 1
    assert all(abs(dx) <= 24 for dx in moves)
    assert any(abs(dx) == 24 for dx in moves)


class RecordingBackend:
    """只记录动作的假输入后端；不会接触真实键鼠。"""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.actions: list[Action] = []
        self.times: list[float] = []
        self.release_count = 0
        self._lock = threading.Lock()

    def send(self, actions) -> None:  # noqa: ANN001 - 与 InputBackend 签名一致
        now = self.clock.now()
        with self._lock:
            for action in actions:
                self.actions.append(action)
                self.times.append(now)

    def release_all(self) -> None:
        with self._lock:
            self.release_count += 1


def test_generated_steps_keep_spacing_through_production_input_executor() -> None:
    config = make_config(
        idle_camera_interval_s=(2.0, 2.0),
        idle_camera_move_px=(40, 40),
        idle_camera_duration_s=(0.6, 0.6),
    )
    controller = IdleCameraController(config)
    controller.reset(seed=17)
    generated: list[Action] = []
    for index in range(0, 28):
        now = round(index / 10.0, 10)
        actions, _ = controller.tick(make_frame(now, frame_id=index), now)
        generated.extend(actions)
    assert len(generated) == 4

    clock = FakeClock(start=2.0)
    backend = RecordingBackend(clock)
    executor = InputExecutor(
        backend,
        InputConfig(min_action_interval_ms=150, interval_jitter=0.2),
        clock=clock,
        sleeper=FakeSleeper(clock),
        rng=random.Random(42),
    )
    try:
        for action in generated:
            executor.submit([action])
            assert executor.wait_until_idle(1.0)
            clock.advance(0.2)
    finally:
        executor.stop(drain=False)
        executor.join(2.0)

    assert backend.actions == generated
    assert executor.executed_actions == 4
    assert executor.expired_actions == 0
    intervals = [
        later - earlier
        for earlier, later in zip(backend.times, backend.times[1:], strict=False)
    ]
    assert intervals
    assert all(interval >= 0.2 - 1e-6 for interval in intervals)
