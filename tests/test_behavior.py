"""随机行为调度器与随机档案单元测试（详细设计 8.2~8.8；评审 P1-6）。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from src.behavior.random_profile import RandomProfile
from src.behavior.scheduler import BehaviorContext, BehaviorScheduler
from src.common.types import (
    ActionKind,
    CombatObservation,
    EnemyContact,
    NormalizedRect,
    Observation,
    Rect,
    ShipPosition,
)
from src.config.models import BehaviorConfig, ConsumableSlot, KeyMap

FAR = 1000.0  # 用于把不需要的计时器推到很远的间隔（截断正态在 [1000,1000] 上恒为 1000）


def test_targeted_mode_replaces_random_camera_fire_and_watchdog() -> None:
    cfg = make_config(targeted_combat=True, camera_interval_s=(1, 1), attack_interval_s=(1, 1),
                      activity_watchdog_s=(1, 1), battle_start_quiet_s=0)
    scheduler, events = collect_scheduler(cfg, make_profile(consumable_ratio=1))
    for now in (2., 4., 6.):
        assert scheduler.tick(make_observation(now), now) == []
    frame = CombatObservation(10, 1, (100, 100), 90, (EnemyContact((200, 100)),))
    obs = replace(make_observation(10), combat=frame)
    actions = scheduler.tick(obs, 10)
    assert [a.kind for a in actions] == [ActionKind.KEY_TAP]
    assert actions[0].key == 'mouse_left'
    assert all(n not in event_names(events) for n in ('watchdog', 'camera_move', 'fire_burst', 'consumable'))


@pytest.mark.parametrize('reason', ['foreground', 'modal', 'stale', 'stopped', 'quiet', 'not_battle'])
def test_targeted_mode_keeps_all_input_guards(reason: str) -> None:
    scheduler, _ = collect_scheduler(make_config(targeted_combat=True), make_profile())
    obs = replace(make_observation(20), combat=CombatObservation(20, 1, (100, 100), 90, (EnemyContact((200, 100)),)))
    context = make_context()
    now = 20.
    if reason == 'foreground':
        context = replace(context, foreground=False)
    elif reason == 'modal':
        context = replace(context, modal_visible=True)
    elif reason == 'stale':
        now = 25
    elif reason == 'stopped':
        scheduler.stop()
    elif reason == 'quiet':
        now = 5
        obs = replace(obs, frame_timestamp=5, combat=replace(obs.combat, timestamp=5))
    else:
        context = replace(context, in_battle=False)
    assert scheduler.tick(obs, now, context) == []


def test_targeted_fire_counts_press_and_release_in_quota() -> None:
    scheduler, _ = collect_scheduler(make_config(targeted_combat=True, actions_per_minute_max=2), make_profile())
    def obs(now):
        return replace(make_observation(now), combat=CombatObservation(now, int(now), (100, 100), 90,
                       (EnemyContact((200, 100)),)))
    assert len(scheduler.tick(obs(20), 20)) == 1
    assert scheduler.tick(obs(30), 30) == []
    assert len(scheduler.tick(obs(81), 81)) == 1


def combat_observation(
    now: float,
    *,
    enemies: tuple[EnemyContact, ...] = (),
    candidates: int = 0,
    camera: float | None = None,
) -> Observation:
    return replace(
        make_observation(now),
        combat=CombatObservation(now, int(round(now * 10)), (100.0, 100.0), camera, enemies, candidates),
    )


def idle_config(**overrides) -> BehaviorConfig:
    values = {
        "targeted_combat": True,
        "battle_start_quiet_s": 0.0,
        "idle_camera_interval_s": (2.0, 2.0),
        "idle_camera_move_px": (40, 40),
        "idle_camera_duration_s": (0.6, 0.6),
        "idle_camera_actions_per_minute_max": 180,
    }
    values.update(overrides)
    return make_config(**values)


def test_idle_camera_moves_during_initial_quiet_without_enemy() -> None:
    scheduler, events = collect_scheduler(
        idle_config(battle_start_quiet_s=10.0), make_profile(seed=2026)
    )
    scheduler.notify_activity(1.0)
    actions = []
    action_times = []
    for index in range(0, 90):
        now = index / 10.0
        produced = scheduler.tick(combat_observation(now), now)
        actions.extend(produced)
        if produced:
            action_times.append(now)

    assert action_times
    assert action_times[0] <= 5.0
    assert all(action.kind is ActionKind.MOVE_REL for action in actions)
    assert all(action.label == "idle_camera" and action.dy == 0 for action in actions)
    assert not any(action.label == "combat_fire" for action in actions)
    bursts = 1 + sum(
        later - earlier > 1.0
        for earlier, later in zip(action_times, action_times[1:], strict=False)
    )
    movement_events = [detail for event, detail in events if event == "idle_camera_move"]
    assert len(movement_events) == bursts
    assert all("amplitude_px" in detail and "duration_s" in detail for detail in movement_events)


def test_idle_camera_cancels_on_candidate_and_does_not_resume_old_remainder() -> None:
    scheduler, _ = collect_scheduler(idle_config(), make_profile(seed=3))
    actions = []
    for index in range(0, 23):
        now = index / 10.0
        actions = scheduler.tick(combat_observation(now), now)
    assert actions and actions[0].label == "idle_camera"

    assert scheduler.tick(combat_observation(2.3, candidates=1), 2.3) == []
    assert scheduler.tick(combat_observation(2.4), 2.4) == []
    resumed = []
    for index in range(25, 60):
        now = index / 10.0
        produced = scheduler.tick(combat_observation(now), now)
        if produced:
            resumed = produced
            break
    assert resumed
    assert resumed[0].label == "idle_camera"


def test_confirmed_enemy_uses_existing_combat_action_after_idle() -> None:
    scheduler, _ = collect_scheduler(idle_config(), make_profile(seed=4))
    for index in range(0, 23):
        now = index / 10.0
        scheduler.tick(combat_observation(now), now)

    enemy = EnemyContact((200.0, 100.0))
    actions = scheduler.tick(combat_observation(2.3, enemies=(enemy,), camera=90.0), 2.3)
    assert [action.kind for action in actions] == [ActionKind.KEY_TAP]
    assert actions[0].label == "combat_fire"


def test_idle_quota_is_separate_from_combat_quota() -> None:
    scheduler, events = collect_scheduler(
        idle_config(idle_camera_actions_per_minute_max=1), make_profile(seed=5)
    )
    actions = []
    for index in range(0, 23):
        now = index / 10.0
        actions.extend(scheduler.tick(combat_observation(now), now))
    assert actions and actions[0].label == "idle_camera"
    assert scheduler.tick(combat_observation(2.4), 2.4) == []
    assert any(event == "idle_camera_rate_limited" for event, _ in events)

    enemy = EnemyContact((200.0, 100.0))
    actions = scheduler.tick(combat_observation(2.5, enemies=(enemy,), camera=90.0), 2.5)
    assert [action.kind for action in actions] == [ActionKind.KEY_TAP]
    assert actions[0].label == "combat_fire"


def test_idle_still_moves_after_combat_quota_is_exhausted() -> None:
    scheduler, _ = collect_scheduler(
        idle_config(actions_per_minute_max=2), make_profile(seed=6)
    )
    enemy = EnemyContact((200.0, 100.0))
    actions = scheduler.tick(combat_observation(0.0, enemies=(enemy,), camera=90.0), 0.0)
    assert [action.kind for action in actions] == [ActionKind.KEY_TAP]

    idle_actions = []
    for index in range(1, 40):
        now = index / 10.0
        produced = scheduler.tick(combat_observation(now), now)
        if produced:
            idle_actions.extend(produced)
            break
    assert idle_actions
    assert idle_actions[0].label == "idle_camera"


@pytest.mark.parametrize(
    "blocker",
    ["foreground", "modal", "stale", "future", "window_missing", "capture_error", "not_battle", "stop", "resume", "start"],
)
def test_idle_motion_is_cleared_by_lifecycle_and_input_gates(blocker: str) -> None:
    scheduler, _ = collect_scheduler(idle_config(), make_profile(seed=7))
    actions = []
    for index in range(0, 23):
        now = index / 10.0
        actions = scheduler.tick(combat_observation(now), now)
    assert actions and actions[0].label == "idle_camera"

    if blocker == "foreground":
        assert scheduler.tick(combat_observation(2.3), 2.3, make_context(foreground=False)) == []
    elif blocker == "modal":
        assert scheduler.tick(combat_observation(2.3), 2.3, make_context(modal_visible=True)) == []
    elif blocker == "stale":
        assert scheduler.tick(make_observation(2.3, frame_timestamp=1.0), 2.3) == []
    elif blocker == "future":
        assert scheduler.tick(make_observation(2.3, frame_timestamp=2.4), 2.3) == []
    elif blocker == "window_missing":
        assert scheduler.tick(replace(combat_observation(2.3), window_missing=True), 2.3) == []
    elif blocker == "capture_error":
        assert scheduler.tick(replace(combat_observation(2.3), capture_error="boom"), 2.3) == []
    elif blocker == "not_battle":
        assert scheduler.tick(combat_observation(2.3), 2.3, make_context(in_battle=False)) == []
    elif blocker == "stop":
        scheduler.stop()
        assert scheduler.tick(combat_observation(2.3), 2.3) == []
        scheduler.resume(2.4)
    elif blocker == "resume":
        scheduler.resume(2.3)
    else:
        scheduler.start(make_profile(seed=8), 2.3)

    assert scheduler.tick(combat_observation(2.4), 2.4) == []
    resumed = []
    for index in range(25, 80):
        now = index / 10.0
        produced = scheduler.tick(combat_observation(now), now)
        if produced:
            resumed = produced
            break
    assert resumed
    assert resumed[0].label == "idle_camera"
    assert resumed[0].kind is ActionKind.MOVE_REL


def make_keymap() -> KeyMap:
    return KeyMap(
        actions={"fire": "mouse_left", "escape": "escape"},
        camera_relative=True,
        camera_sensitivity=1.0,
        consumables=(ConsumableSlot(key="1", name="损管小组"), ConsumableSlot(key="2", name="维修小组")),
    )


def make_profile(**overrides) -> RandomProfile:
    values = {
        "camera_activity": 1.0,
        "attack_frequency": 1.0,
        "consumable_ratio": 0.15,
        "long_stare_bias": 0.0,
        "seed": 1,
    }
    values.update(overrides)
    return RandomProfile(**values)


def make_config(**overrides) -> BehaviorConfig:
    return replace(BehaviorConfig(), **overrides)


def make_observation(
    now: float,
    *,
    window_rect: tuple[int, int, int, int] = (100, 50, 1280, 800),
    frame_timestamp: float | None = None,
    ship: ShipPosition | None = None,
) -> Observation:
    return Observation(
        timestamp=now,
        window_rect=window_rect,
        ship=ship,
        frame_timestamp=now if frame_timestamp is None else frame_timestamp,
    )


def make_context(**overrides) -> BehaviorContext:
    return BehaviorContext(**overrides)


def collect_scheduler(config: BehaviorConfig, profile: RandomProfile) -> tuple[BehaviorScheduler, list[tuple[str, dict]]]:
    events: list[tuple[str, dict]] = []
    scheduler = BehaviorScheduler(
        config,
        make_keymap(),
        on_event=lambda event, detail: events.append((event, detail)),
    )
    scheduler.start(profile, 0.0)
    return scheduler, events


def event_names(events: list[tuple[str, dict]]) -> list[str]:
    return [event for event, _ in events]


def last_event(events: list[tuple[str, dict]], name: str) -> dict:
    for event, detail in reversed(events):
        if event == name:
            return detail
    raise AssertionError(f"未产生事件 {name}: {event_names(events)}")


def input_actions(actions) -> list:
    return [action for action in actions if action.kind is not ActionKind.WAIT]


# ---------------------------------------------------------------------------
# 随机档案
# ---------------------------------------------------------------------------


def test_random_profile_ranges_and_reproducibility() -> None:
    config = BehaviorConfig()
    for seed in range(60):
        profile = RandomProfile.generate(config, seed=seed)
        assert 0.6 <= profile.camera_activity <= 1.4
        assert 0.5 <= profile.attack_frequency <= 1.5
        assert 0.05 <= profile.consumable_ratio <= 0.25
        assert 0.0 <= profile.long_stare_bias <= 1.0
        assert 0 <= profile.seed < 2**32
        assert RandomProfile.generate(config, seed=seed) == profile


def test_random_profile_seed_drives_all_fields() -> None:
    config = BehaviorConfig()
    first = RandomProfile.generate(config, seed=1)
    second = RandomProfile.generate(config, seed=2)
    assert first != second
    assert first.seed == 1
    assert second.seed == 2


def test_random_profile_from_injected_rng() -> None:
    import random

    config = BehaviorConfig()
    first = RandomProfile.generate(config, rng=random.Random(11))
    second = RandomProfile.generate(config, rng=random.Random(11))
    assert first == second
    assert first.to_dict()["seed"] == first.seed


def test_random_profile_to_dict_has_database_fields() -> None:
    data = make_profile().to_dict()
    assert set(data) == {"camera_activity", "attack_frequency", "consumable_ratio", "long_stare_bias", "seed"}


# ---------------------------------------------------------------------------
# 计时与采样
# ---------------------------------------------------------------------------


def test_quiet_period_suppresses_actions() -> None:
    config = make_config(camera_interval_s=(3.0, 15.0), long_stare_probability=0.0)
    scheduler, _ = collect_scheduler(config, make_profile(camera_activity=1.4, attack_frequency=1.5))
    for now in (0.5, 5.0, 9.9):
        assert scheduler.tick(make_observation(now), now) == []
    assert scheduler.next_camera_at >= 10.0


def test_resume_preserves_consumables_and_emits_actions_without_opening_quiet_period():
    config = make_config(camera_interval_s=(FAR, FAR), attack_interval_s=(1, 1),
                         consumable_max_per_battle=1, fire_burst=(1, 1))
    scheduler, events = collect_scheduler(config, make_profile(consumable_ratio=1.0))
    scheduler.tick(make_observation(11), 11)
    assert scheduler.consumables_used == 1
    profile = scheduler.profile
    scheduler.stop()
    scheduler.resume(20)
    assert scheduler.profile is profile
    assert scheduler.consumables_used == 1
    actions = scheduler.tick(make_observation(21), 21)
    assert any(action.kind is ActionKind.CLICK_ABS for action in actions)
    assert "behavior_resume" in event_names(events)
    assert scheduler.consumables_used == 1


def test_camera_interval_within_configured_bounds() -> None:
    config = make_config(camera_interval_s=(3.0, 15.0), attack_interval_s=(FAR, FAR))
    scheduler, events = collect_scheduler(config, make_profile(seed=9))
    first_camera = scheduler.next_camera_at
    interval = first_camera - 10.0
    assert 3.0 <= interval <= 15.0
    assert scheduler.tick(make_observation(first_camera - 0.01), first_camera - 0.01) == []
    actions = scheduler.tick(make_observation(first_camera), first_camera)
    moves = [action for action in actions if action.kind is ActionKind.MOVE_REL]
    assert moves, "视角计时器到期应产生相对移动"
    assert 3 <= len(moves) <= 6
    assert any(action.kind is ActionKind.WAIT for action in actions)
    detail = last_event(events, "camera_move")
    assert detail["steps"] == len(moves)


def test_camera_frequency_multiplier_shortens_interval() -> None:
    config = make_config(camera_interval_s=(3.0, 15.0), attack_interval_s=(FAR, FAR))
    scheduler, _ = collect_scheduler(config, make_profile())
    slow: list[float] = []
    fast: list[float] = []
    for seed in range(120):
        scheduler.start(make_profile(camera_activity=0.6, seed=seed), 0.0)
        slow.append(scheduler.next_camera_at - 10.0)
        scheduler.start(make_profile(camera_activity=1.4, seed=seed), 0.0)
        fast.append(scheduler.next_camera_at - 10.0)
    assert all(3.0 <= value <= 15.0 for value in slow + fast)
    assert sum(fast) / len(fast) < sum(slow) / len(slow)
    assert len(set(slow)) > 1


def test_attack_interval_within_configured_bounds() -> None:
    config = make_config(camera_interval_s=(FAR, FAR), attack_interval_s=(25.0, 120.0))
    scheduler, _ = collect_scheduler(config, make_profile(attack_frequency=1.5))
    interval = scheduler.next_attack_at - 10.0
    assert 25.0 <= interval <= 120.0


# ---------------------------------------------------------------------------
# 攻击事件：消耗品与开火
# ---------------------------------------------------------------------------


def test_consumable_choice_until_cap_then_fire() -> None:
    config = make_config(
        camera_interval_s=(FAR, FAR),
        attack_interval_s=(1.0, 1.0),
        consumable_max_per_battle=2,
        fire_burst=(1, 1),
        fire_interval_ms=(300, 300),
    )
    scheduler, events = collect_scheduler(config, make_profile(consumable_ratio=1.0, long_stare_bias=0.0))
    actions = scheduler.tick(make_observation(11.0), 11.0)
    assert scheduler.consumables_used == 1
    assert [action.kind for action in actions] == [ActionKind.KEY_DOWN, ActionKind.KEY_UP]
    assert actions[0].key in {"1", "2"}
    first = last_event(events, "consumable")
    assert first["choice"] == "consumable"
    scheduler.tick(make_observation(12.0), 12.0)
    assert scheduler.consumables_used == 2
    # 达到单场上限后即使随机命中消耗品也改为开火
    actions = scheduler.tick(make_observation(13.0), 13.0)
    assert scheduler.consumables_used == 2
    fire = last_event(events, "fire_burst")
    assert fire["choice"] == "fire"
    assert any(action.kind is ActionKind.CLICK_ABS for action in actions)


def test_fire_burst_inside_selected_region_and_window() -> None:
    config = make_config(
        camera_interval_s=(FAR, FAR),
        attack_interval_s=(1.0, 1.0),
        fire_burst=(2, 2),
        fire_interval_ms=(300, 300),
        fire_turn_probability=0.0,
        fire_click_region=NormalizedRect(0.30, 0.30, 0.70, 0.70),
    )
    scheduler, events = collect_scheduler(config, make_profile(consumable_ratio=0.0))
    actions = scheduler.tick(make_observation(11.0), 11.0)
    clicks = [action for action in actions if action.kind is ActionKind.CLICK_ABS]
    assert len(clicks) == 2
    # 窗口 (100,50,1280,800)，落点区域 [0.30,0.30]-[0.70,0.70]
    for action in clicks:
        assert 100 + round(0.30 * 1280) <= action.x <= 100 + round(0.70 * 1280)
        assert 50 + round(0.30 * 800) <= action.y <= 50 + round(0.70 * 800)
    assert not any(action.kind is ActionKind.MOVE_REL for action in actions)
    detail = last_event(events, "fire_burst")
    assert detail["burst"] == 2
    assert detail["turned"] is False


def test_fire_turn_moves_view_before_shooting() -> None:
    config = make_config(
        camera_interval_s=(FAR, FAR),
        attack_interval_s=(1.0, 1.0),
        fire_turn_probability=1.0,
        fire_burst=(1, 1),
    )
    scheduler, events = collect_scheduler(config, make_profile(consumable_ratio=0.0))
    actions = scheduler.tick(make_observation(11.0), 11.0)
    kinds = [action.kind for action in actions]
    assert ActionKind.MOVE_REL in kinds
    assert ActionKind.CLICK_ABS in kinds
    assert kinds.index(ActionKind.MOVE_REL) < kinds.index(ActionKind.CLICK_ABS)
    assert last_event(events, "fire_burst")["turned"] is True


def test_fire_click_uses_context_to_screen_callback() -> None:
    config = make_config(
        camera_interval_s=(FAR, FAR),
        attack_interval_s=(1.0, 1.0),
        fire_burst=(1, 1),
        fire_turn_probability=0.0,
    )
    scheduler, _ = collect_scheduler(config, make_profile(consumable_ratio=0.0))
    context = make_context(
        window_rect=Rect(1000, 500, 1280, 800),
        to_screen=lambda x, y: (1000 + x + 7, 500 + y + 9),
    )
    actions = scheduler.tick(make_observation(11.0), 11.0, context)
    click = next(action for action in actions if action.kind is ActionKind.CLICK_ABS)
    assert 1000 + 7 + round(0.30 * 1280) <= click.x <= 1000 + 7 + round(0.70 * 1280)
    assert 500 + 9 + round(0.30 * 800) <= click.y <= 500 + 9 + round(0.70 * 800)


def test_fire_click_prefers_context_window_rect() -> None:
    config = make_config(
        camera_interval_s=(FAR, FAR),
        attack_interval_s=(1.0, 1.0),
        fire_burst=(1, 1),
        fire_turn_probability=0.0,
    )
    scheduler, _ = collect_scheduler(config, make_profile(consumable_ratio=0.0))
    context = make_context(window_rect=Rect(0, 0, 2560, 1600))
    actions = scheduler.tick(make_observation(11.0), 11.0, context)
    click = next(action for action in actions if action.kind is ActionKind.CLICK_ABS)
    assert round(0.30 * 2560) <= click.x <= round(0.70 * 2560)


def test_fire_click_without_window_rect_is_marked_window_relative() -> None:
    config = make_config(
        camera_interval_s=(FAR, FAR),
        attack_interval_s=(1.0, 1.0),
        fire_burst=(1, 1),
        fire_turn_probability=0.0,
    )
    scheduler, _ = collect_scheduler(config, make_profile(consumable_ratio=0.0))
    observation = make_observation(11.0, window_rect=(0, 0, 0, 0))
    actions = scheduler.tick(observation, 11.0, make_context())
    click = next(action for action in actions if action.kind is ActionKind.CLICK_ABS)
    assert click.label.endswith(":window_rel")


# ---------------------------------------------------------------------------
# 长时间凝视 / 看门狗 / 节流 / 上下文
# ---------------------------------------------------------------------------


def test_long_stare_defers_next_camera() -> None:
    config = make_config(
        camera_interval_s=(3.0, 15.0),
        attack_interval_s=(FAR, FAR),
        long_stare_probability=1.0,
        long_stare_s=(5.0, 20.0),
    )
    scheduler, events = collect_scheduler(config, make_profile(long_stare_bias=1.0))
    due = scheduler.next_camera_at
    actions = scheduler.tick(make_observation(due), due)
    assert actions == []
    detail = last_event(events, "camera_stare")
    duration = detail["duration_s"]
    assert 5.0 <= duration <= 20.0
    assert scheduler.next_camera_at == pytest.approx(due + duration, abs=0.01)


def test_activity_watchdog_triggers_camera_action() -> None:
    config = make_config(
        camera_interval_s=(FAR, FAR),
        attack_interval_s=(FAR, FAR),
        activity_watchdog_s=(35.0, 35.0),
    )
    scheduler, events = collect_scheduler(config, make_profile())
    actions = scheduler.tick(make_observation(45.0), 45.0)
    assert any(action.kind is ActionKind.MOVE_REL for action in actions)
    assert "watchdog" in event_names(events)
    assert scheduler.last_activity_at == pytest.approx(45.0)


def test_notify_activity_defers_watchdog() -> None:
    config = make_config(
        camera_interval_s=(FAR, FAR),
        attack_interval_s=(FAR, FAR),
        activity_watchdog_s=(35.0, 35.0),
    )
    scheduler, _ = collect_scheduler(config, make_profile())
    scheduler.notify_activity(40.0)
    assert scheduler.tick(make_observation(45.0), 45.0) == []
    assert any(action.kind is ActionKind.MOVE_REL for action in scheduler.tick(make_observation(76.0), 76.0))


def test_actions_per_minute_cap_truncates_camera_steps() -> None:
    config = make_config(
        camera_interval_s=(1.0, 1.0),
        attack_interval_s=(FAR, FAR),
        long_stare_probability=0.0,
        move_steps=(5, 5),
        move_px=(100, 100),
        actions_per_minute_max=2,
    )
    scheduler, events = collect_scheduler(config, make_profile())
    actions = scheduler.tick(make_observation(11.0), 11.0)
    assert len(input_actions(actions)) == 2
    total = len(input_actions(actions))
    for now in (12.0, 13.0, 14.0):
        total += len(input_actions(scheduler.tick(make_observation(now), now)))
    assert total == 2
    assert "behavior_rate_limited" in event_names(events)


def test_modal_visible_defers_camera() -> None:
    config = make_config(camera_interval_s=(1.0, 1.0), attack_interval_s=(FAR, FAR), long_stare_probability=0.0)
    scheduler, events = collect_scheduler(config, make_profile())
    due = scheduler.next_camera_at
    assert scheduler.tick(make_observation(due), due, make_context(modal_visible=True)) == []
    assert last_event(events, "behavior_skipped")["reason"] == "modal_visible"
    # 顺延后的下一个到期时间应产生动作
    next_due = scheduler.next_camera_at
    assert len(input_actions(scheduler.tick(make_observation(next_due), next_due))) > 0


def test_foreground_false_defers_camera() -> None:
    config = make_config(camera_interval_s=(1.0, 1.0), attack_interval_s=(FAR, FAR), long_stare_probability=0.0)
    scheduler, events = collect_scheduler(config, make_profile())
    due = scheduler.next_camera_at
    assert scheduler.tick(make_observation(due), due, make_context(foreground=False)) == []
    assert last_event(events, "behavior_skipped")["reason"] == "foreground"


def test_stale_frame_skips_input() -> None:
    config = make_config(camera_interval_s=(1.0, 1.0), attack_interval_s=(FAR, FAR), long_stare_probability=0.0)
    scheduler, events = collect_scheduler(config, make_profile())
    due = scheduler.next_camera_at
    stale = make_observation(due, frame_timestamp=due - 5.0)
    assert scheduler.tick(stale, due) == []
    assert last_event(events, "behavior_skipped")["reason"] == "stale_frame"


def test_missing_observation_skips_input() -> None:
    config = make_config(camera_interval_s=(1.0, 1.0), attack_interval_s=(FAR, FAR))
    scheduler, events = collect_scheduler(config, make_profile())
    due = scheduler.next_camera_at
    assert scheduler.tick(None, due) == []
    assert last_event(events, "behavior_skipped")["reason"] == "no_observation"


def test_not_in_battle_produces_no_actions() -> None:
    config = make_config(camera_interval_s=(1.0, 1.0), attack_interval_s=(1.0, 1.0))
    scheduler, _ = collect_scheduler(config, make_profile())
    for now in (11.0, 12.0, 30.0):
        assert scheduler.tick(make_observation(now), now, make_context(in_battle=False)) == []


def test_stop_disables_actions() -> None:
    config = make_config(camera_interval_s=(1.0, 1.0))
    scheduler, _ = collect_scheduler(config, make_profile())
    scheduler.stop()
    assert not scheduler.running
    assert scheduler.tick(make_observation(11.0), 11.0) == []


def test_scheduler_action_sequence_is_reproducible_with_seed() -> None:
    config = make_config(
        camera_interval_s=(3.0, 15.0),
        attack_interval_s=(25.0, 120.0),
        long_stare_probability=0.15,
    )
    first, _ = collect_scheduler(config, make_profile(seed=2026))
    second, _ = collect_scheduler(config, make_profile(seed=2026))
    first_actions = []
    second_actions = []
    for now in range(0, 200):
        observation = make_observation(float(now))
        first_actions.extend(first.tick(observation, float(now)))
        second_actions.extend(second.tick(observation, float(now)))
    assert first_actions == second_actions


def test_observation_ship_heading_initializes_direction() -> None:
    config = make_config(
        camera_interval_s=(1.0, 1.0),
        attack_interval_s=(FAR, FAR),
        long_stare_probability=0.0,
    )
    scheduler, _ = collect_scheduler(config, make_profile())
    ship = ShipPosition(
        minimap_px=(10.0, 20.0),
        world_xy=(0.0, 0.0),
        heading_deg=90.0,
        speed_mps=5.0,
        confidence=1.0,
        source="marker",
    )
    due = scheduler.next_camera_at
    actions = scheduler.tick(make_observation(due, ship=ship), due)
    moves = [action for action in actions if action.kind is ActionKind.MOVE_REL]
    assert moves
    # 初始朝向来自 ShipPosition.heading_deg（90 度）+ 小幅随机偏转
    assert 45.0 <= scheduler.heading_deg <= 135.0
