"""GUI 视图模型与解耦契约测试（纯逻辑，不创建窗口）。

覆盖：

- 生命周期/游戏流程/暂停原因中文文案与未知值口径；
- 状态区严格四组 A/B/C/D 与 ``STOPPED + BATTLE_ACTIVE`` 提示；
- 7.6 按钮可用性矩阵（8 种生命周期 × 全部按钮）；
- 启动防抖、关闭行为；
- EffectivePlan 三类上限与生效值（None → “—”不显示 0）；
- 性能面板缺失值、monitor “不自动停止”、能力未接入不显示“已启用”；
- 日志有界 2000 行、级别/关键词/场次筛选、seq 缺口提示；
- AST 守护：GUI 不导入 winput/ctypes/InputExecutor/StateMachine，且
  ``main.py`` 对 ``src.runtime.service`` 只在函数内延迟导入。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from src.gui.viewmodel import (
    BUTTON_KEYS,
    CLEANUP_ERROR_BANNER,
    CLOSING_STATUS,
    F12_NOTICE,
    FULLMAP_CALIBRATION_NOTE,
    FULLSCREEN_HINT,
    LOG_LEVEL_OPTIONS,
    NO_ACCOUNT_LABEL,
    ROTATION_CONFIRM_FAILED,
    ROTATION_CONFIRM_OK,
    SHIP_NAME_HINT,
    STOP_BATTLE_WARNING,
    STOP_NOTICE,
    TOOL_SPECS,
    AccountOption,
    LogModel,
    LogRow,
    ProfileOption,
    RecentEvents,
    SeqGapTracker,
    ShipCandidate,
    StartDebounce,
    button_matrix,
    capability_rows,
    close_behavior,
    effective_plan_rows,
    event_notice,
    filter_log_rows,
    format_duration,
    format_mb,
    format_ms,
    format_optional,
    format_pct,
    format_validation_issue,
    game_state_display,
    input_state_text,
    lifecycle_badge_text,
    lifecycle_text,
    match_ship_candidates,
    overload_state_text,
    pause_reason_text,
    performance_rows,
    plan_result_summary,
    rotation_wait_texts,
    run_status_groups,
    seq_gap_text,
    status_bar_texts,
    threshold_status_text,
    tool_arguments,
    tool_spec,
    validation_issue_lines,
)
from src.runtime.messages import (
    CapabilityFlags,
    CleanupView,
    EffectivePlan,
    GameView,
    InputView,
    LifecycleView,
    NavigationView,
    PerceptionView,
    PerformanceView,
    PlanResult,
    RunLifecycle,
    RunSnapshot,
    RuntimeEvent,
    RuntimeEventKind,
    SessionView,
    ValidationIssueView,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUI_DIR = PROJECT_ROOT / "src" / "gui"
WALL = "2026-10-31T12:00:00+08:00"


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


def make_snapshot(
    *,
    lifecycle: RunLifecycle | str = RunLifecycle.STOPPED,
    at: float = 0.0,
    game_state: str = "INIT",
    battle_active: bool = False,
    pause_reason: str | None = None,
    pause_detail: str | None = None,
    battle_id: int | None = None,
    plan: EffectivePlan | None = None,
    perf: PerformanceView | None = None,
    capabilities: CapabilityFlags | None = None,
    input_view: InputView | None = None,
    perception: PerceptionView | None = None,
    session: SessionView | None = None,
    cleanup: CleanupView | None = None,
    dropped_events: int = 0,
) -> RunSnapshot:
    life = RunLifecycle(lifecycle)
    return RunSnapshot(
        seq=1,
        at=at,
        wall_at=WALL,
        lifecycle=LifecycleView(
            lifecycle=life,
            since_at=0.0,
            since_wall=WALL,
            detail=f"生命周期：{life.value}",
        ),
        game=GameView(
            state=game_state,
            state_since_wall=None,
            pause_reason=pause_reason,
            pause_detail=pause_detail,
            session_id=None,
            battle_id=battle_id,
            battle_active=battle_active,
            last_transition=None,
        ),
        perception=perception
        or PerceptionView(
            last_frame_id=0,
            last_frame_at=None,
            last_frame_wall=None,
            last_frame_age_s=None,
            capture_ms_avg=None,
            window_found=True,
            window_foreground=True,
            window_client_rect=None,
            capture_error=None,
        ),
        input=input_view
        or InputView(
            gate_open=life is RunLifecycle.RUNNING,
            gate_closed_reason=None if life is RunLifecycle.RUNNING else "stopped",
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
        session=session
        or SessionView(
            account_alias=None,
            battles_completed=0,
            battles_target=None,
            battle_in_progress=battle_active,
            elapsed_s=0.0,
            last_battle_duration_s=None,
            daily_used=None,
            rotation_index=None,
            rotation_aliases=(),
            next_alias=None,
        ),
        plan=plan,
        cleanup=cleanup
        or CleanupView(
            requested=False,
            input_released=False,
            input_thread_joined=False,
            watchdog_stopped=False,
            event_sink_stopped=False,
            screenshots_cleaned=False,
            capture_closed=False,
            single_instance_released=False,
            pending_items=(),
            completed=False,
        ),
        error=None,
        capabilities=capabilities or CapabilityFlags(),
        perf=perf,
        dropped_events=dropped_events,
    )


def sample_plan(tmp_path: Path) -> EffectivePlan:
    return EffectivePlan(
        client_id="cn360",
        client_display_name="国服 360",
        ship_id="iron_duke",
        ship_display_name="铁公爵",
        account_alias=None,
        account_display_name=None,
        max_battles_override=None,
        rotation_limit=None,
        safety_battle_cap=20,
        daily_cap=30,
        daily_used=3,
        allowed_hours=None,
        max_session_minutes=240.0,
        navigation_mode="autopilot",
        emergency_stop_key="F12",
        resume_key="F11",
        heartbeat_s=10.0,
        config_hash="abc123",
        log_path=tmp_path / "logs" / "session_20261031.log",
        db_path=tmp_path / "data" / "wows_script.db",
        diagnostics_dir=tmp_path / "screenshots" / "diagnostics",
        profiles_dir=tmp_path / "profiles",
        start_paused=False,
    )


def make_row(seq: int, *, level: str = "INFO", message: str = "", battle_id: int | None = None) -> LogRow:
    return LogRow(
        seq=seq,
        wall_at="12:00:01.0",
        level=level,
        source="session#1",
        message=message,
        battle_id=battle_id,
    )


# ---------------------------------------------------------------------------
# 文案与格式化
# ---------------------------------------------------------------------------


def test_lifecycle_text_all_states() -> None:
    expected = {
        RunLifecycle.STOPPED: "已停止",
        RunLifecycle.STARTING: "启动中",
        RunLifecycle.RUNNING: "运行中",
        RunLifecycle.PAUSING: "暂停中",
        RunLifecycle.PAUSED: "已暂停",
        RunLifecycle.STOPPING: "停止中",
        RunLifecycle.WAIT_ACCOUNT: "等待手动切号",
        RunLifecycle.ERROR: "清理未完成",
    }
    for lifecycle, text in expected.items():
        assert lifecycle_text(lifecycle) == text
        assert lifecycle_badge_text(lifecycle) == f"{text}（{lifecycle.value}）"
    assert lifecycle_text(None) == "—"


def test_game_state_and_pause_reason_text() -> None:
    assert game_state_display("BATTLE_ACTIVE") == "战斗中（BATTLE_ACTIVE）"
    assert game_state_display("INIT") == "初始化识别（INIT）"
    assert "未知状态" in game_state_display("SOMETHING_NEW")
    assert game_state_display(None) == "—"
    assert pause_reason_text("window_unfocused").startswith("游戏窗口不在前台")
    assert "未知原因" in pause_reason_text("brand_new_reason")
    assert pause_reason_text(None) == "—"


def test_format_none_is_dash_not_zero() -> None:
    assert format_optional(None) == "—"
    assert format_mb(None) == "—"
    assert format_pct(None) == "—"
    assert format_ms(None) == "—"
    assert format_optional(0.0, " %") == "0.0 %"
    assert format_duration(3725.4) == "01:02:05"
    assert format_duration(None) == "—"


# ---------------------------------------------------------------------------
# 状态区四组
# ---------------------------------------------------------------------------


def test_run_status_groups_strict_four_groups() -> None:
    groups = run_status_groups(make_snapshot())
    assert set(groups) == {"A", "B", "C", "D"}
    a_keys = [line.key for line in groups["A"]]
    assert a_keys == ["lifecycle", "since", "detail"]
    assert groups["A"][0].value == "已停止（STOPPED）"


def test_stopped_with_battle_active_warns_not_exited() -> None:
    snapshot = make_snapshot(
        lifecycle=RunLifecycle.STOPPED,
        game_state="BATTLE_ACTIVE",
        battle_active=True,
    )
    groups = run_status_groups(snapshot)
    warning_lines = [line for line in groups["B"] if line.key == "stop_battle_warning"]
    assert warning_lines
    assert warning_lines[0].value == STOP_BATTLE_WARNING
    assert "游戏仍在战斗中" in warning_lines[0].value
    assert "未替你退出" in warning_lines[0].value


def test_status_groups_unknown_values_are_dash() -> None:
    groups = run_status_groups(make_snapshot(perf=None))
    d_values = {line.key: line.value for line in groups["D"]}
    assert d_values["frame"].startswith("—")
    assert "未接入确认" in d_values["input_gate"]
    assert "未验证" in d_values["navigation"]
    assert "不可用" in d_values["hp"]


def test_pause_reason_group_uses_chinese_and_raw() -> None:
    snapshot = make_snapshot(
        lifecycle=RunLifecycle.PAUSED,
        game_state="PAUSED",
        pause_reason="window_lost",
        pause_detail="重新寻找窗口",
    )
    lines = {line.key: line.value for line in run_status_groups(snapshot)["C"]}
    assert lines["pause_reason"] == "找不到游戏窗口"
    assert lines["pause_raw"] == "window_lost"
    assert lines["pause_detail"] == "重新寻找窗口"


# ---------------------------------------------------------------------------
# 按钮矩阵
# ---------------------------------------------------------------------------

_EXPECTED_ENABLED: dict[RunLifecycle, set[str]] = {
    RunLifecycle.STOPPED: {
        "validate_config",
        "start",
        "import_ship",
        "run_write_tool",
        "open_logs",
        "view_stats",
    },
    RunLifecycle.STARTING: {"stop", "open_logs"},
    RunLifecycle.RUNNING: {"validate_config", "pause", "stop", "open_logs"},
    RunLifecycle.PAUSING: {"validate_config", "stop", "open_logs"},
    RunLifecycle.PAUSED: {"validate_config", "resume", "stop", "open_logs"},
    RunLifecycle.STOPPING: {"open_logs"},
    RunLifecycle.WAIT_ACCOUNT: {
        "validate_config",
        "stop",
        "confirm_account",
        "abort_rotation",
        "open_logs",
        "view_stats",
    },
    RunLifecycle.ERROR: {"validate_config", "open_logs", "view_stats"},
}


def test_button_matrix_matches_design_7_6() -> None:
    for lifecycle, enabled in _EXPECTED_ENABLED.items():
        matrix = button_matrix(lifecycle)
        assert set(matrix) == set(BUTTON_KEYS)
        assert {key for key, value in matrix.items() if value} == enabled, lifecycle


def test_button_matrix_conditions() -> None:
    assert button_matrix(RunLifecycle.STOPPED, start_ready=False)["start"] is False
    assert button_matrix(RunLifecycle.STOPPED, tool_running=True)["start"] is False
    assert button_matrix(RunLifecycle.STOPPED, tool_running=True)["run_write_tool"] is False
    assert button_matrix(RunLifecycle.WAIT_ACCOUNT, account_switch_ready=False)["confirm_account"] is False
    assert close_behavior(RunLifecycle.STOPPED) == "direct"
    assert close_behavior(RunLifecycle.STOPPING) == "wait"
    assert close_behavior(RunLifecycle.ERROR) == "confirm"
    assert close_behavior(RunLifecycle.RUNNING) == "async"
    assert close_behavior(RunLifecycle.WAIT_ACCOUNT) == "async"


def test_button_matrix_closing_disables_everything() -> None:
    matrix = button_matrix(RunLifecycle.RUNNING, closing=True)
    assert not any(matrix.values())


# ---------------------------------------------------------------------------
# 生效值
# ---------------------------------------------------------------------------


def test_effective_plan_none_shows_dash() -> None:
    rows = {line.key: line.value for line in effective_plan_rows(None)}
    assert rows["cap_battle"] == "—"
    assert rows["cap_safety"] == "—"
    assert rows["cap_daily"] == "—"
    assert "0" not in rows["cap_battle"]


def test_effective_plan_three_caps_and_values(tmp_path: Path) -> None:
    plan = sample_plan(tmp_path)
    rows = {line.key: line.value for line in effective_plan_rows(plan)}
    assert rows["client"] == "国服 360（cn360）"
    assert rows["ship"] == "铁公爵（iron_duke）"
    assert rows["cap_battle"] == "—"  # 未覆盖本次上限时显示“—”
    assert rows["cap_safety"] == "20 场"
    assert "30 场" in rows["cap_daily"] and "今日已用 3 场" in rows["cap_daily"]
    assert rows["emergency_key"] == "F12"
    assert rows["resume_key"] == "F11"
    assert rows["allowed_hours"] == "不限"
    assert rows["log_path"].endswith("session_20261031.log")


# ---------------------------------------------------------------------------
# 性能面板
# ---------------------------------------------------------------------------


def test_performance_none_is_unavailable_with_reason() -> None:
    rows = {line.key: line.value for line in performance_rows(None)}
    assert "不可用" in rows["unavailable"]
    assert "不可用" in rows["threshold"]
    assert threshold_status_text(None).startswith("保护阈值：不可用")


def test_performance_monitor_mode_never_claims_enabled() -> None:
    perf = PerformanceView(enabled=True, mode="monitor", sampled_wall=WALL)
    assert "监测中（不自动停止）" in threshold_status_text(perf)
    assert "已启用" not in threshold_status_text(perf)
    rows = {line.key: line.value for line in performance_rows(perf)}
    assert rows["threshold"] == "保护阈值：监测中（不自动停止）"


def test_performance_pause_stop_requires_capability() -> None:
    perf = PerformanceView(enabled=True, mode="pause_stop")
    disabled = threshold_status_text(perf, CapabilityFlags())
    assert "未接入" in disabled and "不自动停止" in disabled
    enabled = threshold_status_text(perf, CapabilityFlags(overload_protection=True))
    assert "已启用" in enabled


def test_performance_unavailable_reason_and_no_zero() -> None:
    perf = PerformanceView(enabled=False, mode="off", unavailable_reason="psapi 不可用")
    rows = {line.key: line.value for line in performance_rows(perf)}
    assert "不可用（psapi 不可用）" in rows["unavailable"]
    empty = PerformanceView(enabled=True, mode="monitor")
    values = [line.value for line in performance_rows(empty)]
    assert "0.0 %" not in values
    assert "0 MB" not in values
    assert "—" in values
    assert overload_state_text("stop_pending") == "即将停止"
    assert "未知" in overload_state_text("weird")


def test_performance_rows_cover_required_fields() -> None:
    perf = PerformanceView(
        enabled=True,
        mode="monitor",
        sampled_wall=WALL,
        cpu_pct=12.5,
        mem_total_mb=32768.0,
        mem_available_mb=16384.0,
        commit_available_mb=8192.0,
        script_private_mb=256.0,
        capture_ms_avg=68.0,
        overload_state="normal",
    )
    rows = {line.key: line.value for line in performance_rows(perf)}
    assert rows["cpu"] == "12.5 %"
    assert rows["mem_total"] == "32768 MB"
    assert rows["mem_available"] == "16384 MB"
    assert rows["commit_available"] == "8192 MB"
    assert rows["script_private"] == "256 MB"
    assert rows["capture_ms"] == "68.0 ms"
    assert rows["sampled"] == WALL
    assert rows["overload"] == "正常"


# ---------------------------------------------------------------------------
# 能力开关与状态行
# ---------------------------------------------------------------------------


def test_capability_rows_never_hide_unimplemented() -> None:
    rows = capability_rows(CapabilityFlags())
    assert len(rows) == 13
    assert all("✗ 未接入" in line.value for line in rows)
    enabled = {line.key: line.value for line in capability_rows(CapabilityFlags(input_gate=True))}
    assert enabled["input_gate"] == "✓ 已接入"
    assert capability_rows(None) == ()


def test_input_state_and_status_bar() -> None:
    running = make_snapshot(lifecycle=RunLifecycle.RUNNING)
    assert input_state_text(running) == "托管中"
    unfocused = make_snapshot(
        lifecycle=RunLifecycle.PAUSED,
        game_state="PAUSED",
        pause_reason="window_unfocused",
    )
    assert input_state_text(unfocused) == "等待游戏前台"
    texts = status_bar_texts(running)
    assert texts[0] == "输入状态：托管中"
    assert texts[1].startswith("画面更新：")
    closing = status_bar_texts(running, closing=True)
    assert "不会强杀线程" in closing[1]
    stopping = make_snapshot(
        lifecycle=RunLifecycle.STOPPING,
        cleanup=CleanupView(
            requested=True,
            input_released=True,
            input_thread_joined=False,
            watchdog_stopped=False,
            event_sink_stopped=False,
            screenshots_cleaned=False,
            capture_closed=False,
            single_instance_released=False,
            pending_items=("输入线程未退出",),
            completed=False,
        ),
    )
    assert "清理进度：输入线程未退出" in status_bar_texts(stopping)[1]


def test_notice_copy_constants() -> None:
    assert "F12" in F12_NOTICE and "紧急停止" in F12_NOTICE
    assert "不会替你退出战斗" in STOP_NOTICE
    assert "正在停止" in CLOSING_STATUS
    assert "不会强杀线程" in CLOSING_STATUS
    assert "taskkill /PID <pid> /T /F" in CLEANUP_ERROR_BANNER
    assert "全屏模式只改变窗口显示，不改变托管状态" in FULLSCREEN_HINT


# ---------------------------------------------------------------------------
# 日志模型
# ---------------------------------------------------------------------------


def test_log_model_bounded_to_2000() -> None:
    model = LogModel()
    model.append_rows(make_row(i, message=f"line {i}") for i in range(1, 2101))
    assert len(model) == 2000
    assert model.trimmed_total == 100
    assert model.rows[0].seq == 101
    assert model.rows[-1].seq == 2100


def test_log_model_filters() -> None:
    rows = [
        make_row(1, level="DEBUG", message="debug detail"),
        make_row(2, level="INFO", message="battle start", battle_id=12),
        make_row(3, level="WARNING", message="battle lag", battle_id=12),
        make_row(4, level="ERROR", message="input failed", battle_id=None),
    ]
    assert [row.seq for row in filter_log_rows(rows, minimum_level="INFO")] == [2, 3, 4]
    assert [row.seq for row in filter_log_rows(rows, keyword="battle")] == [2, 3]
    assert [row.seq for row in filter_log_rows(rows, only_battle=True, battle_id=12)] == [2, 3]
    assert filter_log_rows(rows, only_battle=True, battle_id=None) == ()


def test_recent_events_keep_last_eight() -> None:
    recent = RecentEvents()
    recent.push(make_row(i) for i in range(1, 13))
    assert len(recent.rows) == 8
    assert recent.rows[0].seq == 5
    assert recent.rows[-1].seq == 12


def test_seq_gap_tracker_and_text() -> None:
    tracker = SeqGapTracker()
    assert tracker.observe(1) == 0
    assert tracker.observe(2) == 0
    assert tracker.observe(5) == 2
    assert tracker.gap_total == 2
    assert tracker.observe(5) == 0  # 重复/回退序号不重复计数
    text = seq_gap_text(tracker.gap_total, 3)
    assert "缺失 2 条" in text and "丢弃 3 条" in text
    assert seq_gap_text(0, 0) == "事件序号：连续"


def test_start_debounce() -> None:
    debounce = StartDebounce()
    assert debounce.try_acquire() is True
    assert debounce.pending is True
    for _ in range(10):
        assert debounce.try_acquire() is False
    debounce.release()
    assert debounce.pending is False
    assert debounce.try_acquire() is True


def test_event_notice_only_for_real_events() -> None:
    def event(kind: RuntimeEventKind, message: str = "") -> RuntimeEvent:
        return RuntimeEvent(
            seq=1,
            at=1.0,
            wall_at=WALL,
            kind=kind,
            level="ERROR",
            message=message,
        )

    assert event_notice(event(RuntimeEventKind.ERROR, "boom")) == ("error", "boom")
    assert event_notice(event(RuntimeEventKind.INPUT_FAILED, "input")) == ("warning", "input")
    assert event_notice(event(RuntimeEventKind.VALIDATION_FAILED, "bad")) == ("error", "bad")
    assert event_notice(event(RuntimeEventKind.LOG_RECORD, "hello")) is None


# ---------------------------------------------------------------------------
# M2/M3 纯逻辑：船名匹配、校验摘要、轮换文案、标定工具命令
# ---------------------------------------------------------------------------


def test_match_ship_candidates_statuses() -> None:
    ships = [
        ShipCandidate("iron_duke", "铁公爵"),
        ShipCandidate("leningrad", "列宁格勒"),
        ShipCandidate("iron_duke_2", "铁公爵·改"),
    ]
    empty = match_ship_candidates(ships, "  ")
    assert empty.status == "empty" and "请输入船名" in empty.message

    exact = match_ship_candidates(ships, "铁公爵")
    assert exact.status == "exact"
    assert exact.single is not None and exact.single.ship_id == "iron_duke"
    assert "已登记" in exact.message

    by_id = match_ship_candidates(ships, "leningrad")
    assert by_id.status == "exact" and by_id.single is not None

    multiple = match_ship_candidates(ships, "公爵")
    assert multiple.status == "partial_multiple"
    assert len(multiple.matches) == 2
    assert "不自动取第一个" in multiple.message

    none = match_ship_candidates(ships, "不存在的船")
    assert none.status == "none" and "未登记" in none.message

    duplicate = [ShipCandidate("a", "同名"), ShipCandidate("b", "同名")]
    assert match_ship_candidates(duplicate, "同名").status == "partial_multiple"


def test_plan_result_summary_and_issue_lines() -> None:
    issue = ValidationIssueView("error", "ship_id_missing", "config.yaml:ship>ship_id", "找不到船只档案")
    warning = ValidationIssueView("warning", "file_missing", "profiles/cn360/profile.yaml", "模板缺失")
    failed = PlanResult(ok=False, issues=(issue,), warnings=(warning,))
    ok = PlanResult(ok=True, warnings=(warning,))
    assert plan_result_summary(failed) == "配置校验：✗ 1 error / 1 warning"
    assert plan_result_summary(ok) == "配置校验：✔ 0 error / 1 warning"
    assert plan_result_summary(None).startswith("配置校验：尚未校验")
    lines = validation_issue_lines(failed)
    assert lines[0].startswith("【错误】config.yaml:ship>ship_id")
    assert lines[1].startswith("【警告】profiles/cn360/profile.yaml")
    assert validation_issue_lines(None) == ()
    assert format_validation_issue(issue).endswith("找不到船只档案")


def test_rotation_wait_texts_use_current_and_next_alias() -> None:
    session = SessionView(
        account_alias="alt1",
        battles_completed=6,
        battles_target=6,
        battle_in_progress=False,
        elapsed_s=120.0,
        last_battle_duration_s=60.0,
        daily_used=3,
        rotation_index=0,
        rotation_aliases=("alt1", "alt2"),
        next_alias="alt2",
    )
    snapshot = make_snapshot(lifecycle=RunLifecycle.WAIT_ACCOUNT, session=session)
    headline, steps, note = rotation_wait_texts(snapshot)
    assert "alt1" in headline and "alt2" in headline and "6 场" in headline
    assert "脚本不会代登录" in steps
    assert "输入已释放" in note
    default_headline, _, _ = rotation_wait_texts(None)
    assert "（默认）" in default_headline
    assert ROTATION_CONFIRM_FAILED.startswith("预检失败")
    assert "已提交" in ROTATION_CONFIRM_OK


def test_tool_specs_and_arguments(tmp_path: Path) -> None:
    assert {item.tool_id for item in TOOL_SPECS} == {
        "calibrate_ship",
        "calibrate_profile",
        "capture_marker",
        "check_env",
        "validate",
        "stats",
    }
    assert tool_spec("calibrate_ship") is not None and tool_spec("calibrate_ship").write is True
    assert tool_spec("stats") is not None and tool_spec("stats").write is False
    assert tool_spec("no_such_tool") is None

    args = tool_arguments(
        "validate",
        root=tmp_path,
        profile_id="cn360",
        ship_id="iron_duke",
        account_alias="alt1",
        json_output=True,
    )
    assert args[:4] == ("-m", "src.tools.validate", "--root", str(tmp_path))
    assert args[args.index("--ship") + 1] == "iron_duke"
    assert args[args.index("--account") + 1] == "alt1"
    assert "--json" in args

    ship_args = tool_arguments("calibrate_ship", root=tmp_path, profile_id="cn360", ship_id="s1")
    assert "--ship-id" in ship_args and ship_args[ship_args.index("--profile") + 1] == "cn360"
    stats_args = tool_arguments("stats", root=tmp_path, profile_id="cn360")
    assert "--profile" not in stats_args
    with pytest.raises(ValueError):
        tool_arguments("no_such_tool", root=tmp_path)


def test_form_constants_and_labels() -> None:
    assert LOG_LEVEL_OPTIONS == ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
    assert "不上传" in SHIP_NAME_HINT
    assert "不承诺" in FULLMAP_CALIBRATION_NOTE and "navigation.full_map_rect" in FULLMAP_CALIBRATION_NOTE
    assert NO_ACCOUNT_LABEL.startswith("（不使用")
    assert ProfileOption("cn360", "国服 360").label == "国服 360（cn360）"
    assert AccountOption("alt1", "小号一号").label == "小号一号（alt1）"


# ---------------------------------------------------------------------------
# AST 解耦守护
# ---------------------------------------------------------------------------

_FORBIDDEN_MODULE_PREFIXES = ("ctypes", "winput", "src.execution.winput")
_FORBIDDEN_NAME_PARTS = ("InputExecutor", "StateMachine")


def _iter_gui_modules() -> list[Path]:
    return sorted(GUI_DIR.glob("*.py"))


def test_gui_never_imports_input_or_state_machine() -> None:
    assert _iter_gui_modules(), "src/gui 下应有模块"
    for path in _iter_gui_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(_FORBIDDEN_MODULE_PREFIXES), (path, alias.name)
                    assert alias.name != "threading", (path, alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith(_FORBIDDEN_MODULE_PREFIXES), (path, module)
                assert module != "threading", (path, module)
                for alias in node.names:
                    for part in _FORBIDDEN_NAME_PARTS:
                        assert part not in alias.name, (path, alias.name)


def test_gui_only_imports_runtime_messages() -> None:
    allowed_runtime_modules = {"src.runtime.messages", "src.runtime.plan"}
    for path in _iter_gui_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("src.runtime"):
                    allowed = module in allowed_runtime_modules or (
                        path.name == "main.py" and module == "src.runtime.service"
                    )
                    if module == "src.runtime":
                        allowed = all(alias.name in {"plan", "messages"} for alias in node.names)
                    assert allowed, (path, module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("src.runtime"):
                        assert alias.name in allowed_runtime_modules, (path, alias.name)


def test_gui_main_lazy_imports_runtime_service() -> None:
    path = GUI_DIR / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:  # 只看模块顶层语句
        if isinstance(node, ast.ImportFrom):
            assert node.module != "src.runtime.service", "RuntimeService 必须在 main() 内延迟导入"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "src.runtime.service"
    source = path.read_text(encoding="utf-8")
    assert "from src.runtime.service import RuntimeService" in source
