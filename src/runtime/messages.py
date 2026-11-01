"""运行服务对外契约：生命周期、启动选项、快照、事件、命令（GUI 设计文档第 4 节）。

本模块只允许依赖标准库与 ``src.config.models`` 中的只读类型：

- 不导入 tkinter / ctypes / cv2 / 线程实现；
- 不做任何 IO；
- 所有对外类型为 ``@dataclass(frozen=True, slots=True)``；
- ``Mapping`` 字段在构造时浅拷贝，调用方不得修改快照内部对象。

GUI 与服务只通过这里的数据类型通信；实现位于 ``service.py`` / ``runner.py``。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class RunLifecycle(StrEnum):
    """脚本自身生命周期（GUI 设计文档 4.2）。"""

    STOPPED = "STOPPED"            # 未运行；清理已完成；可启动
    STARTING = "STARTING"          # 已接受启动：取锁、加载配置、装配运行时、切前台中
    RUNNING = "RUNNING"            # 运行线程在主循环；输入门禁打开
    PAUSING = "PAUSING"            # 已请求暂停：门禁关闭、等待释放确认/状态机进入 PAUSED
    PAUSED = "PAUSED"              # 已暂停：输入已释放，运行线程仍存活但不再控制
    STOPPING = "STOPPING"          # 已请求停止：等待运行线程退出 + 统一清理完成
    WAIT_ACCOUNT = "WAIT_ACCOUNT"  # 会话间轮换：当前账号已清理完毕，等待用户手动切号确认
    ERROR = "ERROR"                # 清理/生命周期无法确认安全：禁止启动，只允许诊断与退出


def _copy_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return dict(value)


# ---------------------------------------------------------------------------
# 启动选项 / 预检 / 生效值（4.3 / 4.4）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StartOptions:
    root: Path
    profile_id: str | None = None
    ship_name: str | None = None
    ship_id: str | None = None
    account_alias: str | None = None
    rotate: bool = False
    max_battles: int | None = None
    log_level: str | None = None
    start_paused: bool = False


@dataclass(frozen=True, slots=True)
class ValidationIssueView:
    severity: str
    code: str
    location: str
    message: str


@dataclass(frozen=True, slots=True)
class EffectivePlan:
    client_id: str
    client_display_name: str
    ship_id: str
    ship_display_name: str
    account_alias: str | None
    account_display_name: str | None
    max_battles_override: int | None
    rotation_limit: int | None
    safety_battle_cap: int
    daily_cap: int
    daily_used: int | None
    allowed_hours: tuple[str, str] | None
    max_session_minutes: float
    navigation_mode: str
    emergency_stop_key: str
    resume_key: str
    heartbeat_s: float
    config_hash: str
    log_path: Path
    db_path: Path
    diagnostics_dir: Path
    profiles_dir: Path
    start_paused: bool


@dataclass(frozen=True, slots=True)
class PlanResult:
    ok: bool
    plan: EffectivePlan | None = None
    issues: tuple[ValidationIssueView, ...] = ()
    warnings: tuple[ValidationIssueView, ...] = ()


@dataclass(frozen=True, slots=True)
class StartResult:
    accepted: bool
    reason: str | None = None
    issues: tuple[ValidationIssueView, ...] = ()


# ---------------------------------------------------------------------------
# 快照视图（4.5）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LifecycleView:
    lifecycle: RunLifecycle
    since_at: float
    since_wall: str
    detail: str
    pending_action: str | None = None
    resume_pending: bool = False


@dataclass(frozen=True, slots=True)
class GameView:
    state: str
    state_since_wall: str | None
    pause_reason: str | None
    pause_detail: str | None
    session_id: int | None
    battle_id: int | None
    battle_active: bool
    last_transition: str | None


@dataclass(frozen=True, slots=True)
class PerceptionView:
    last_frame_id: int
    last_frame_at: float | None
    last_frame_wall: str | None
    last_frame_age_s: float | None
    capture_ms_avg: float | None
    window_found: bool
    window_foreground: bool
    window_client_rect: tuple[int, int, int, int] | None
    capture_error: str | None


@dataclass(frozen=True, slots=True)
class InputView:
    gate_open: bool
    gate_closed_reason: str | None
    pending_actions: int
    release_count: int
    release_confirmed: bool
    last_release_wall: str | None
    executed: int
    rejected: int
    dropped: int
    expired: int
    failed: bool
    consecutive_errors: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class NavigationView:
    mode: str
    surface: str | None
    click_count: int
    last_click_wall: str | None
    last_click_target_norm: tuple[float, float] | None
    last_click_from_own_marker: bool | None
    move_confirmed: bool
    move_confirmed_wall: str | None
    verify_failed: bool
    blocks_behavior: bool
    ship_speed_mps: float | None
    ship_marker_source: str | None
    ship_marker_confidence: float | None
    phase: str = "idle"
    activation_detected: bool = False
    target_override: tuple[float, float] | None = None
    target_status: str = ""


@dataclass(frozen=True, slots=True)
class MinimapView:
    """最新一张小地图内容区截图，不进入日志事件队列。"""

    frame_id: int
    captured_at: float
    battle_id: int
    window_rect: tuple[int, int, int, int]
    png: bytes
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class BattleRecord:
    battle_id: int
    started_at: str
    ended_at: str | None
    account_alias: str | None
    result: str | None
    map_id: str | None


@dataclass(frozen=True, slots=True)
class HistoryView:
    records: tuple[BattleRecord, ...] = ()
    today_completed: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SessionView:
    account_alias: str | None
    battles_completed: int
    battles_target: int | None
    battle_in_progress: bool
    elapsed_s: float
    last_battle_duration_s: float | None
    daily_used: int | None
    rotation_index: int | None
    rotation_aliases: tuple[str, ...]
    next_alias: str | None


@dataclass(frozen=True, slots=True)
class CleanupView:
    requested: bool
    input_released: bool
    input_thread_joined: bool
    watchdog_stopped: bool
    event_sink_stopped: bool
    screenshots_cleaned: bool
    capture_closed: bool
    single_instance_released: bool
    pending_items: tuple[str, ...]
    completed: bool


@dataclass(frozen=True, slots=True)
class ErrorView:
    code: str
    message: str
    source: str
    detail: str
    at_wall: str | None
    recoverable: bool


@dataclass(frozen=True, slots=True)
class CapabilityFlags:
    """未修复/未实机验证的能力开关；置 True 必须有对应测试与代码位置注释。"""

    input_gate: bool = False
    release_confirmed: bool = False
    single_instance: bool = False
    cleanup_on_error: bool = False
    pause_keeps_battle: bool = False
    battle_context: bool = False
    exit_authorized_gate: bool = False
    autopilot_full_map: bool = False
    autopilot_move_verified: bool = False
    ship_selected_evidence: bool = False
    input_failure_pause: bool = False
    performance_monitor: bool = False
    overload_protection: bool = False


@dataclass(frozen=True, slots=True)
class PerformanceThresholds:
    """过载保护实际生效阈值（单位见字段名；界面只读展示）。"""

    mode: str = "monitor"                 # off | monitor | pause_stop | stop
    sample_hz: float = 1.0
    cpu_warn_pct: float = 90.0
    physical_warn_mb: float = 2500.0
    commit_warn_mb: float = 6000.0
    commit_pause_mb: float = 2000.0
    commit_stop_mb: float = 1200.0
    commit_fast_stop_mb: float = 400.0
    starvation_lateness_s: float = 2.5
    starvation_pause_events: int = 3
    starvation_stop_events: int = 3
    script_private_warn_mb: float = 1024.0


@dataclass(frozen=True, slots=True)
class PerformanceView:
    """性能面板与过载保护状态（GUI 设计文档 6.1 / 6.2）。"""

    enabled: bool = False
    mode: str = "off"                     # off | monitor | pause_stop | stop
    sampled_at: float | None = None
    sampled_wall: str | None = None
    sample_age_s: float | None = None
    unavailable_reason: str | None = None
    cpu_pct: float | None = None
    mem_total_mb: float | None = None
    mem_available_mb: float | None = None
    commit_limit_mb: float | None = None
    commit_available_mb: float | None = None
    script_private_mb: float | None = None
    script_cpu_pct: float | None = None
    capture_ms_avg: float | None = None
    loop_tick_p95_ms: float | None = None
    sampler_lateness_ms: float | None = None
    overload_state: str = "normal"        # normal | warning | pause_pending | paused | stop_pending | stopped
    overload_reason: str | None = None
    overload_detail: str | None = None
    would_pause: bool = False
    would_stop: bool = False
    thresholds: PerformanceThresholds | None = field(default_factory=PerformanceThresholds)
    last_trigger: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.last_trigger is not None:
            object.__setattr__(self, "last_trigger", _copy_mapping(self.last_trigger))


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    seq: int
    at: float
    wall_at: str
    lifecycle: LifecycleView
    game: GameView
    perception: PerceptionView
    input: InputView
    navigation: NavigationView
    session: SessionView
    plan: EffectivePlan | None
    cleanup: CleanupView
    error: ErrorView | None
    capabilities: CapabilityFlags
    perf: PerformanceView | None = None
    dropped_events: int = 0
    minimap: MinimapView | None = None
    gui_waiting: bool = False
    minimap_status: str = ""


@dataclass(frozen=True, slots=True)
class DashboardView:
    history: HistoryView = field(default_factory=HistoryView)
    perf: PerformanceView | None = None


# ---------------------------------------------------------------------------
# 事件（4.6）
# ---------------------------------------------------------------------------


class RuntimeEventKind(StrEnum):
    LIFECYCLE_CHANGED = "lifecycle_changed"
    GAME_STATE_CHANGED = "game_state_changed"
    PAUSE_REQUESTED = "pause_requested"
    PAUSE_CONFIRMED = "pause_confirmed"
    RESUME_REQUESTED = "resume_requested"
    RESUME_CONFIRMED = "resume_confirmed"
    STOP_REQUESTED = "stop_requested"
    INPUT_RELEASED = "input_released"
    CLEANUP_STEP = "cleanup_step"
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    BATTLE_STARTED = "battle_started"
    BATTLE_ENDED = "battle_ended"
    ACCOUNT_SWITCH_WAITING = "account_switch_waiting"
    ACCOUNT_SWITCH_CONFIRMED = "account_switch_confirmed"
    NAV_FEEDBACK = "nav_feedback"
    SHIP_CHECK_FAILED = "ship_check_failed"
    INPUT_FAILED = "input_failed"
    VALIDATION_FAILED = "validation_failed"
    TOOL_STARTED = "tool_started"
    TOOL_EXITED = "tool_exited"
    OVERLOAD_WARNING = "overload_warning"
    OVERLOAD_PAUSE_REQUESTED = "overload_pause_requested"
    OVERLOAD_STOP_REQUESTED = "overload_stop_requested"
    OVERLOAD_RESUME_REQUESTED = "overload_resume_requested"
    LOG_RECORD = "log_record"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    seq: int
    at: float
    wall_at: str
    kind: RuntimeEventKind
    level: str
    message: str
    session_id: int | None = None
    battle_id: int | None = None
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _copy_mapping(self.detail))


# ---------------------------------------------------------------------------
# 命令（4.7）
# ---------------------------------------------------------------------------


class RunCommandKind(StrEnum):
    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    CONFIRM_ACCOUNT_SWITCH = "confirm_account_switch"
    ABORT_ROTATION = "abort_rotation"
    SHUTDOWN = "shutdown"
    REPLACE_TARGET = "replace_target"


@dataclass(frozen=True, slots=True)
class RunCommand:
    kind: RunCommandKind
    reason: str = ""
    detail: str = ""
    target: tuple[float, float] | None = None
    battle_id: int | None = None
    frame_id: int | None = None


__all__ = [
    "BattleRecord",
    "CapabilityFlags",
    "CleanupView",
    "DashboardView",
    "EffectivePlan",
    "ErrorView",
    "GameView",
    "HistoryView",
    "InputView",
    "LifecycleView",
    "MinimapView",
    "NavigationView",
    "PerceptionView",
    "PerformanceThresholds",
    "PerformanceView",
    "PlanResult",
    "RunCommand",
    "RunCommandKind",
    "RunLifecycle",
    "RunSnapshot",
    "RuntimeEvent",
    "RuntimeEventKind",
    "SessionView",
    "StartOptions",
    "StartResult",
    "ValidationIssueView",
]
