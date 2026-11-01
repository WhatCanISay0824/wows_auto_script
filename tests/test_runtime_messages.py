"""运行服务对外契约测试（GUI 设计文档 4.1 / 14.1 L1：纯数据、默认值、不可变、事件枚举）。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from src.runtime.messages import (
    CapabilityFlags,
    CleanupView,
    EffectivePlan,
    ErrorView,
    GameView,
    InputView,
    LifecycleView,
    NavigationView,
    PerceptionView,
    PerformanceThresholds,
    PerformanceView,
    PlanResult,
    RunCommand,
    RunCommandKind,
    RunLifecycle,
    RunSnapshot,
    RuntimeEvent,
    RuntimeEventKind,
    SessionView,
    StartOptions,
    StartResult,
    ValidationIssueView,
)


def _snapshot() -> RunSnapshot:
    return RunSnapshot(
        seq=1,
        at=0.0,
        wall_at="2026-10-31T21:00:00+08:00",
        lifecycle=LifecycleView(
            lifecycle=RunLifecycle.STOPPED, since_at=0.0, since_wall="2026-10-31T21:00:00+08:00", detail="未运行"
        ),
        game=GameView(
            state="INIT",
            state_since_wall=None,
            pause_reason=None,
            pause_detail=None,
            session_id=None,
            battle_id=None,
            battle_active=False,
            last_transition=None,
        ),
        perception=PerceptionView(
            last_frame_id=0,
            last_frame_at=None,
            last_frame_wall=None,
            last_frame_age_s=None,
            capture_ms_avg=None,
            window_found=False,
            window_foreground=False,
            window_client_rect=None,
            capture_error=None,
        ),
        input=InputView(
            gate_open=False,
            gate_closed_reason=None,
            pending_actions=0,
            release_count=0,
            release_confirmed=False,
            last_release_wall=None,
            executed=0,
            rejected=0,
            dropped=0,
            expired=0,
            failed=False,
            consecutive_errors=0,
            last_error=None,
        ),
        navigation=NavigationView(
            mode="autopilot",
            surface=None,
            click_count=0,
            last_click_wall=None,
            last_click_target_norm=None,
            last_click_from_own_marker=None,
            move_confirmed=False,
            move_confirmed_wall=None,
            verify_failed=False,
            blocks_behavior=False,
            ship_speed_mps=None,
            ship_marker_source=None,
            ship_marker_confidence=None,
        ),
        session=SessionView(
            account_alias=None,
            battles_completed=0,
            battles_target=None,
            battle_in_progress=False,
            elapsed_s=0.0,
            last_battle_duration_s=None,
            daily_used=None,
            rotation_index=None,
            rotation_aliases=(),
            next_alias=None,
        ),
        plan=None,
        cleanup=CleanupView(
            requested=False,
            input_released=False,
            input_thread_joined=False,
            watchdog_stopped=False,
            event_sink_stopped=False,
            screenshots_cleaned=False,
            capture_closed=False,
            single_instance_released=False,
            pending_items=(),
            completed=True,
        ),
        error=None,
        capabilities=CapabilityFlags(),
    )


def test_lifecycle_values_cover_design_4_2() -> None:
    assert {item.value for item in RunLifecycle} == {
        "STOPPED",
        "STARTING",
        "RUNNING",
        "PAUSING",
        "PAUSED",
        "STOPPING",
        "WAIT_ACCOUNT",
        "ERROR",
    }
    assert RunLifecycle("PAUSED") is RunLifecycle.PAUSED


def test_event_kinds_cover_design_4_6() -> None:
    expected = {
        "lifecycle_changed",
        "game_state_changed",
        "pause_requested",
        "pause_confirmed",
        "resume_requested",
        "resume_confirmed",
        "stop_requested",
        "input_released",
        "cleanup_step",
        "session_started",
        "session_ended",
        "battle_started",
        "battle_ended",
        "account_switch_waiting",
        "account_switch_confirmed",
        "nav_feedback",
        "ship_check_failed",
        "input_failed",
        "validation_failed",
        "tool_started",
        "tool_exited",
        "overload_warning",
        "overload_pause_requested",
        "overload_stop_requested",
        "overload_resume_requested",
        "log_record",
        "error",
    }
    assert {item.value for item in RuntimeEventKind} == expected


def test_command_kinds_cover_design_4_7() -> None:
    assert {item.value for item in RunCommandKind} == {
        "start",
        "pause",
        "resume",
        "stop",
        "confirm_account_switch",
        "abort_rotation",
        "shutdown",
        "replace_target",
    }
    assert RunCommand(kind=RunCommandKind.PAUSE).reason == ""


def test_start_options_defaults() -> None:
    options = StartOptions(root=Path("."))
    assert options.profile_id is None
    assert options.ship_name is None
    assert options.ship_id is None
    assert options.account_alias is None
    assert options.rotate is False
    assert options.max_battles is None
    assert options.log_level is None
    assert options.start_paused is False


def test_start_result_and_plan_result_defaults() -> None:
    result = StartResult(accepted=True)
    assert result.reason is None
    assert result.issues == ()
    plan = PlanResult(ok=False)
    assert plan.plan is None
    assert plan.issues == ()
    assert plan.warnings == ()


def test_snapshot_defaults_and_optional_fields() -> None:
    snapshot = _snapshot()
    assert snapshot.perf is None
    assert snapshot.dropped_events == 0
    assert snapshot.seq == 1


def test_capability_flags_default_all_false() -> None:
    flags = CapabilityFlags()
    for name in (
        "input_gate",
        "release_confirmed",
        "single_instance",
        "cleanup_on_error",
        "pause_keeps_battle",
        "battle_context",
        "exit_authorized_gate",
        "autopilot_full_map",
        "autopilot_move_verified",
        "ship_selected_evidence",
        "input_failure_pause",
        "performance_monitor",
        "overload_protection",
    ):
        assert getattr(flags, name) is False, name


def test_performance_defaults() -> None:
    thresholds = PerformanceThresholds()
    assert thresholds.mode == "monitor"
    assert thresholds.commit_pause_mb == 2000.0
    view = PerformanceView()
    assert view.enabled is False
    assert view.mode == "off"
    assert view.overload_state == "normal"
    assert view.thresholds == thresholds


def test_dataclasses_are_frozen() -> None:
    options = StartOptions(root=Path("."))
    with pytest.raises(FrozenInstanceError):
        options.rotate = True  # type: ignore[misc]
    issue = ValidationIssueView(severity="error", code="c", location="l", message="m")
    with pytest.raises(FrozenInstanceError):
        issue.code = "x"  # type: ignore[misc]
    error = ErrorView(code="c", message="m", source="s", detail="d", at_wall=None, recoverable=True)
    with pytest.raises(FrozenInstanceError):
        error.code = "x"  # type: ignore[misc]
    snapshot = _snapshot()
    with pytest.raises(FrozenInstanceError):
        snapshot.seq = 2  # type: ignore[misc]
    event = RuntimeEvent(
        seq=1, at=0.0, wall_at="2026-10-31T21:00:00+08:00", kind=RuntimeEventKind.ERROR, level="ERROR", message="m"
    )
    with pytest.raises(FrozenInstanceError):
        event.seq = 2  # type: ignore[misc]


def test_mapping_fields_are_copied_on_construction() -> None:
    detail = {"reason": "pause"}
    event = RuntimeEvent(
        seq=1,
        at=0.0,
        wall_at="2026-10-31T21:00:00+08:00",
        kind=RuntimeEventKind.PAUSE_REQUESTED,
        level="WARNING",
        message="暂停",
        detail=detail,
    )
    detail["reason"] = "changed"
    assert event.detail == {"reason": "pause"}

    trigger = {"reason": "commit_low"}
    view = PerformanceView(last_trigger=trigger)
    trigger["reason"] = "changed"
    assert view.last_trigger == {"reason": "commit_low"}


def test_effective_plan_is_read_only_and_complete() -> None:
    plan = EffectivePlan(
        client_id="cn360",
        client_display_name="国服 360",
        ship_id="leningrad",
        ship_display_name="列宁格勒",
        account_alias=None,
        account_display_name=None,
        max_battles_override=None,
        rotation_limit=None,
        safety_battle_cap=20,
        daily_cap=30,
        daily_used=None,
        allowed_hours=None,
        max_session_minutes=240.0,
        navigation_mode="autopilot",
        emergency_stop_key="f12",
        resume_key="f11",
        heartbeat_s=10.0,
        config_hash="abc",
        log_path=Path("logs"),
        db_path=Path("data/wows_script.db"),
        diagnostics_dir=Path("screenshots/diagnostics"),
        profiles_dir=Path("profiles"),
        start_paused=False,
    )
    with pytest.raises(FrozenInstanceError):
        plan.ship_id = "other"  # type: ignore[misc]
