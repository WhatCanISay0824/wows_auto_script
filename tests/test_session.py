"""会话/场次控制单元测试（详细设计 6.7 / 10.6）。"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

import pytest
from src.common.clock import FakeClock, FakeSleeper
from src.config.models import SafetyConfig, SessionConfig
from src.state_machine.session import SessionController, StopReason

TZ = timezone(timedelta(hours=8))


def make_controller(
    *,
    session: SessionConfig | None = None,
    safety: SafetyConfig | None = None,
    wall_start: datetime | None = None,
    count_battles_on=None,
    rng: random.Random | None = None,
    on_event=None,
) -> tuple[SessionController, FakeClock, FakeSleeper]:
    clock = FakeClock(wall_start=wall_start or datetime(2026, 1, 1, 12, 0, 0, tzinfo=TZ))
    sleeper = FakeSleeper(clock)
    controller = SessionController(
        session or SessionConfig(),
        safety or SafetyConfig(),
        clock,
        sleeper=sleeper,
        count_battles_on=count_battles_on,
        rng=rng if rng is not None else random.Random(7),
        on_event=on_event,
    )
    return controller, clock, sleeper


def test_start_stop_and_elapsed() -> None:
    controller, clock, _ = make_controller()
    assert controller.start() is None
    assert controller.started
    assert not controller.stopped
    clock.advance(12.5)
    assert controller.elapsed_s == pytest.approx(12.5)
    request = controller.stop(StopReason.MANUAL, "测试结束")
    assert request.reason == StopReason.MANUAL
    assert controller.stopped
    assert controller.stop_request == request
    summary = controller.summary()
    assert summary.battles_completed == 0
    assert summary.elapsed_s == pytest.approx(12.5)
    assert summary.started_at is not None
    assert summary.to_dict()["stop_reason"] == "manual"


def test_start_twice_raises_but_restart_after_stop_allowed() -> None:
    controller, _, _ = make_controller()
    controller.start()
    with pytest.raises(RuntimeError):
        controller.start()
    controller.stop(StopReason.MANUAL)
    assert controller.start() is None
    assert not controller.stopped


def test_battle_timing_and_counts() -> None:
    controller, clock, _ = make_controller()
    controller.start()
    with pytest.raises(RuntimeError):
        controller.end_battle()
    controller.begin_battle()
    assert controller.battle_in_progress
    with pytest.raises(RuntimeError):
        controller.begin_battle()
    clock.advance(100.0)
    duration = controller.end_battle(result="dead")
    assert duration == pytest.approx(100.0)
    assert controller.battles_completed == 1
    assert controller.last_battle_duration_s == pytest.approx(100.0)
    assert not controller.battle_in_progress


def test_session_battles_cap() -> None:
    safety = SafetyConfig(max_battles_per_session=2)
    controller, _, _ = make_controller(safety=safety)
    controller.start()
    for _ in range(2):
        controller.begin_battle()
        controller.end_battle()
    request = controller.check_stop()
    assert request is not None
    assert request.reason == StopReason.SESSION_BATTLES_CAP
    assert "2" in request.detail


def test_session_duration_cap() -> None:
    # 0.05 分钟 = 3 秒
    safety = SafetyConfig(max_session_minutes=0.05)
    controller, clock, _ = make_controller(safety=safety)
    controller.start()
    clock.advance(2.9)
    assert controller.check_stop() is None
    clock.advance(0.2)
    request = controller.check_stop()
    assert request is not None
    assert request.reason == StopReason.SESSION_DURATION_CAP


def test_daily_battle_cap_uses_database_callback() -> None:
    seen_days: list[date] = []

    def count(day: date) -> int:
        seen_days.append(day)
        return 5

    session = SessionConfig(daily_battle_cap=5)
    controller, clock, _ = make_controller(session=session, count_battles_on=count)
    controller.start()
    request = controller.check_stop()
    assert request is not None
    assert request.reason == StopReason.DAILY_BATTLE_CAP
    assert seen_days == [clock.wall_now().date()]
    assert controller.count_battles_today() == 5


def test_daily_battle_cap_not_reached() -> None:
    session = SessionConfig(daily_battle_cap=5)
    controller, _, _ = make_controller(session=session, count_battles_on=lambda day: 4)
    controller.start()
    assert controller.check_stop() is None


def test_daily_count_error_degrades_gracefully() -> None:
    events: list[tuple[str, dict]] = []

    def fail(day: date) -> int:
        raise RuntimeError("db down")

    session = SessionConfig(daily_battle_cap=1)
    controller, _, _ = make_controller(
        session=session,
        count_battles_on=fail,
        on_event=lambda event, detail: events.append((event, detail)),
    )
    controller.start()
    assert controller.check_stop() is None
    assert any(event == "session_count_error" for event, _ in events)


def test_allowed_hours_inside_and_outside() -> None:
    session = SessionConfig(allowed_hours=("08:00", "23:00"))
    inside, _, _ = make_controller(
        session=session, wall_start=datetime(2026, 1, 1, 9, 30, tzinfo=TZ)
    )
    inside.start()
    assert inside.in_allowed_hours()
    assert inside.check_stop() is None

    outside, _, _ = make_controller(
        session=session, wall_start=datetime(2026, 1, 1, 3, 0, tzinfo=TZ)
    )
    assert outside.start() is not None
    assert not outside.in_allowed_hours()
    request = outside.check_stop()
    assert request is not None
    assert request.reason == StopReason.OUTSIDE_ALLOWED_HOURS


def test_allowed_hours_overnight_range() -> None:
    session = SessionConfig(allowed_hours=("22:00", "02:00"))
    late, _, _ = make_controller(session=session, wall_start=datetime(2026, 1, 1, 23, 30, tzinfo=TZ))
    late.start()
    assert late.in_allowed_hours()
    early, _, _ = make_controller(session=session, wall_start=datetime(2026, 1, 1, 1, 30, tzinfo=TZ))
    early.start()
    assert early.in_allowed_hours()
    noon, _, _ = make_controller(session=session, wall_start=datetime(2026, 1, 1, 12, 0, tzinfo=TZ))
    assert noon.start() is not None
    assert not noon.in_allowed_hours()


def test_null_allowed_hours_always_allowed() -> None:
    controller, _, _ = make_controller(
        session=SessionConfig(allowed_hours=None),
        wall_start=datetime(2026, 1, 1, 3, 0, tzinfo=TZ),
    )
    assert controller.start() is None
    assert controller.in_allowed_hours()


def test_invalid_allowed_hours_raises() -> None:
    with pytest.raises(ValueError, match="allowed_hours"):
        make_controller(session=SessionConfig(allowed_hours=("8点", "23:00")))


def test_between_battle_sampling_and_sleeper() -> None:
    controller, clock, _ = make_controller(rng=random.Random(11))
    controller.start()
    seconds = controller.wait_between_battles()
    assert 15.0 <= seconds <= 60.0
    assert clock.now() == pytest.approx(seconds)


def test_between_battle_sampling_reproducible() -> None:
    first, _, _ = make_controller(rng=random.Random(2024))
    second, _, _ = make_controller(rng=random.Random(2024))
    first_values = [first.sample_between_battle_s() for _ in range(5)]
    second_values = [second.sample_between_battle_s() for _ in range(5)]
    assert first_values == second_values
    assert all(15.0 <= value <= 60.0 for value in first_values)


def test_consecutive_start_failure_threshold() -> None:
    session = SessionConfig(max_consecutive_start_failures=2)
    events: list[str] = []
    controller, _, _ = make_controller(
        session=session, on_event=lambda event, detail: events.append(event)
    )
    controller.start()
    assert controller.record_start_failure("未进入排队") is None
    assert controller.consecutive_start_failures == 1
    request = controller.record_start_failure("未进入排队")
    assert request is not None
    assert request.reason == StopReason.CONSECUTIVE_START_FAILURES
    assert "连续 2 次" in request.detail
    assert events.count("start_failure") == 2


def test_start_success_resets_failure_count() -> None:
    session = SessionConfig(max_consecutive_start_failures=2)
    controller, _, _ = make_controller(session=session)
    controller.start()
    controller.record_start_failure()
    controller.record_start_success()
    assert controller.consecutive_start_failures == 0
    assert controller.record_start_failure() is None


def test_stop_is_idempotent() -> None:
    controller, _, _ = make_controller()
    controller.start()
    first = controller.stop(StopReason.EMERGENCY_STOP)
    second = controller.stop(StopReason.GAME_CRASH)
    assert first is second
    assert controller.stop_request is not None
    assert controller.stop_request.reason == StopReason.EMERGENCY_STOP
    # 已停止后 check_stop 直接返回已有停止请求
    assert controller.check_stop() is first


def test_battle_requires_started_session() -> None:
    controller, _, _ = make_controller()
    with pytest.raises(RuntimeError):
        controller.begin_battle()
    with pytest.raises(RuntimeError):
        controller.check_stop()


def test_elapsed_zero_before_start() -> None:
    controller, clock, _ = make_controller()
    assert controller.elapsed_s == 0.0
    clock.advance(5.0)
    assert controller.elapsed_s == 0.0
