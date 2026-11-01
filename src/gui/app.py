"""GUI 控制器：服务协议、假服务（测试用）与主窗口装配。

解耦边界：

- :class:`ServiceProtocol` 只声明 GUI 真正使用的服务成员（start/pause/resume/stop/
  shutdown/snapshot/drain_events/lifecycle + plan_preview/confirm_account_switch/
  abort_rotation），运行服务通过 ``src/gui/main.py`` 在 ``main()`` 内延迟导入并注入；
- :class:`FakeService` 供 ``tests/test_gui_smoke.py`` 在无游戏、无真实服务时驱动完整界面流程；
- 本模块（以及 ``src/gui/*``）不导入 ``winput``/``ctypes``/``InputExecutor``/
  ``StateMachine``，不发送游戏输入，不写 ``config.yaml``；
- 所有 Tk 调用都在主线程，通过 ``after()`` 轮询快照（200ms）与事件（100ms）。

M2/M3 新增：启动配置表单 → ``StartOptions``、``plan_preview`` 校验展示、船卡导入对话框、
标定工具子进程轮询、``WAIT_ACCOUNT`` 等待面板（确认切号/结束轮换）。
"""

from __future__ import annotations

import time
import tkinter as tk
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk
from typing import Protocol, runtime_checkable

import yaml

from src.gui import widgets
from src.gui.data_page import DataPage
from src.gui.log_page import LogPage
from src.gui.run_page import PerformancePage, RunPage
from src.gui.ship_import import ShipImportDialog, ShipSaveResult
from src.gui.tools_page import SettingsPage
from src.gui.viewmodel import (
    CLEANUP_ERROR_BANNER,
    CLOSING_STATUS,
    RECENT_EVENT_LINES,
    ROTATION_ABORTED,
    ROTATION_CONFIRM_FAILED,
    ROTATION_CONFIRM_OK,
    TOOL_RUNNING_START_DISABLED,
    AccountOption,
    ProfileOption,
    RecentEvents,
    ShipCandidate,
    StartDebounce,
    button_matrix,
    close_behavior,
    event_notice,
    game_state_display,
    lifecycle_badge_text,
    lifecycle_color,
    log_row_from_event,
    seq_gap_text,
    status_bar_texts,
)
from src.runtime import plan as runtime_plan
from src.runtime.messages import (
    CapabilityFlags,
    CleanupView,
    DashboardView,
    EffectivePlan,
    ErrorView,
    GameView,
    HistoryView,
    InputView,
    LifecycleView,
    NavigationView,
    PerceptionView,
    PerformanceView,
    PlanResult,
    RunCommand,
    RunLifecycle,
    RunSnapshot,
    RuntimeEvent,
    RuntimeEventKind,
    SessionView,
    StartOptions,
    StartResult,
    ValidationIssueView,
)

PAGE_TITLES: tuple[tuple[str, str], ...] = (
    ("run", "数据"),
    ("performance", "性能"),
    ("log", "日志"),
    ("settings", "设置"),
)
PAGE_TITLE_BY_ID = dict(PAGE_TITLES)
CLOSING_TITLE = "正在停止…"

# ---------------------------------------------------------------------------
# 服务协议（GUI 唯一依赖的接口面）
# ---------------------------------------------------------------------------


@runtime_checkable
class ServiceProtocol(Protocol):
    """运行服务门面；真实实现为 ``src.runtime.service.RuntimeService``。"""

    def start(self, options: StartOptions) -> StartResult: ...

    def read_dashboard(self, account_alias: str | None = None) -> DashboardView: ...

    def submit_command(self, command: RunCommand) -> bool: ...

    def request_pause(self, reason: str = "gui_pause", detail: str = "") -> bool: ...

    def request_resume(self) -> bool: ...

    def request_stop(self, reason: str = "gui_stop", detail: str = "") -> bool: ...

    def shutdown(self) -> bool: ...

    def snapshot(self) -> RunSnapshot: ...

    def drain_events(self, limit: int = 200) -> Sequence[RuntimeEvent]: ...

    def plan_preview(self, options: StartOptions) -> PlanResult: ...

    def confirm_account_switch(self) -> bool: ...

    def abort_rotation(self) -> bool: ...

    @property
    def lifecycle(self) -> RunLifecycle: ...


# ---------------------------------------------------------------------------
# 启动配置候选项（只读扫描仓库；不写任何配置文件）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GuiCatalog:
    """启动配置表单可用的客户端/账号/船只候选（只读快照）。"""

    project_root: Path
    profiles: tuple[ProfileOption, ...] = ()
    accounts: tuple[AccountOption, ...] = ()
    rotation_available: bool = False
    ships: tuple[ShipCandidate, ...] = ()
    default_profile_id: str | None = None
    log_levels: tuple[str, ...] = ()


def _read_profile_option(path: Path) -> ProfileOption | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    profile_id = str(data.get("client_id") or path.parent.name).strip()
    if not profile_id:
        return None
    display_name = str(data.get("display_name") or profile_id).strip()
    return ProfileOption(profile_id=profile_id, display_name=display_name)


def load_gui_catalog(project_root: Path, *, profile_id: str | None = None) -> GuiCatalog:
    """只读扫描 ``profiles/*/profile.yaml``、accounts.yaml 与指定客户端的船只档案。"""

    root = Path(project_root)
    profiles: list[ProfileOption] = []
    profiles_dir = root / "profiles"
    if profiles_dir.is_dir():
        for profile_path in sorted(profiles_dir.glob("*/profile.yaml")):
            option = _read_profile_option(profile_path)
            if option is not None:
                profiles.append(option)
    default_profile = profile_id
    if default_profile is None:
        default_profile = profiles[0].profile_id if profiles else runtime_plan.DEFAULT_PROFILE_ID
    if not any(item.profile_id == default_profile for item in profiles):
        profiles.insert(0, ProfileOption(profile_id=default_profile, display_name=default_profile))

    accounts: list[AccountOption] = []
    rotation_available = False
    try:
        accounts_config = runtime_plan.load_accounts_or_none(root)
    except Exception:  # noqa: BLE001 - accounts.yaml 损坏不阻塞 GUI（校验时会报错）
        accounts_config = None
    if accounts_config is not None:
        accounts = [
            AccountOption(alias=item.alias, display_name=item.display_name or item.alias)
            for item in accounts_config.accounts
        ]
        rotation_available = bool(accounts_config.rotation_enabled) and bool(
            accounts_config.enabled_accounts()
        )

    ships: tuple[ShipCandidate, ...] = ()
    try:
        ships = tuple(
            ShipCandidate(ship_id=ship_id, display_name=display_name)
            for ship_id, display_name in runtime_plan.list_ships(root, default_profile)
        )
    except Exception:  # noqa: BLE001 - 船只档案损坏时由校验流程报告
        ships = ()
    try:
        log_levels = tuple(sorted(runtime_plan.LOG_LEVELS))
    except Exception:  # noqa: BLE001
        log_levels = ()
    return GuiCatalog(
        project_root=root,
        profiles=tuple(profiles),
        accounts=tuple(accounts),
        rotation_available=rotation_available,
        ships=ships,
        default_profile_id=default_profile,
        log_levels=log_levels,
    )


# ---------------------------------------------------------------------------
# 快照构造（FakeService 与测试共用）
# ---------------------------------------------------------------------------

_WALL_DEFAULT = "2026-10-31T12:00:00+08:00"


def make_snapshot(
    *,
    lifecycle: RunLifecycle | str = RunLifecycle.STOPPED,
    detail: str | None = None,
    game_state: str = "INIT",
    battle_active: bool = False,
    pause_reason: str | None = None,
    pause_detail: str | None = None,
    session_id: int | None = None,
    battle_id: int | None = None,
    plan: EffectivePlan | None = None,
    perf: PerformanceView | None = None,
    error: ErrorView | None = None,
    cleanup: CleanupView | None = None,
    capabilities: CapabilityFlags | None = None,
    input_view: InputView | None = None,
    perception: PerceptionView | None = None,
    navigation: NavigationView | None = None,
    session: SessionView | None = None,
    dropped_events: int = 0,
    seq: int = 1,
    at: float = 0.0,
    wall_at: str = _WALL_DEFAULT,
) -> RunSnapshot:
    """构造一个完整快照；未提供的视图使用“已停止/未知”安全默认值。"""

    life = RunLifecycle(lifecycle)
    life_detail = detail if detail is not None else f"生命周期：{life.value}"
    return RunSnapshot(
        seq=seq,
        at=at,
        wall_at=wall_at,
        lifecycle=LifecycleView(
            lifecycle=life,
            since_at=0.0,
            since_wall=_WALL_DEFAULT,
            detail=life_detail,
            pending_action=None,
            resume_pending=False,
        ),
        game=GameView(
            state=game_state,
            state_since_wall=None,
            pause_reason=pause_reason,
            pause_detail=pause_detail,
            session_id=session_id,
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
            gate_closed_reason=None if life is RunLifecycle.RUNNING else life.value.lower(),
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
        navigation=navigation
        or NavigationView(
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
            completed=life is RunLifecycle.STOPPED,
        ),
        error=error,
        capabilities=capabilities or CapabilityFlags(),
        perf=perf,
        dropped_events=dropped_events,
    )


# ---------------------------------------------------------------------------
# FakeService（仅测试使用；不连游戏、不发输入、不写文件）
# ---------------------------------------------------------------------------


class FakeService:
    """可编程的假运行服务：记录调用并允许测试推进生命周期/事件/快照。"""

    def __init__(self, *, lifecycle: RunLifecycle | str = RunLifecycle.STOPPED) -> None:
        self.calls: list[str] = []
        self.start_options: list[StartOptions] = []
        self.start_results: list[StartResult] = []
        self.shutdown_called = False
        self._events: list[RuntimeEvent] = []
        self._next_seq = 1
        self._kwargs: dict[str, object] = {
            "lifecycle": RunLifecycle(lifecycle),
            "detail": None,
            "game_state": "INIT",
            "battle_active": False,
            "pause_reason": None,
            "plan": None,
            "perf": None,
            "error": None,
            "capabilities": None,
            "dropped_events": 0,
        }
        self._snapshot = make_snapshot(**self._kwargs)  # type: ignore[arg-type]
        self.pause_requests: list[str] = []
        self.stop_requests: list[str] = []
        self.preview_options: list[StartOptions] = []
        self.plan_preview_result: PlanResult | None = None
        self.confirm_result = True
        self.abort_result = True
        self.target_commands: list[RunCommand] = []

    # -- 快照与状态 ----------------------------------------------------
    def read_dashboard(self, account_alias: str | None = None) -> DashboardView:
        return DashboardView(perf=self._snapshot.perf)

    def submit_command(self, command: RunCommand) -> bool:
        self.target_commands.append(command)
        return self.lifecycle is RunLifecycle.RUNNING

    def snapshot(self) -> RunSnapshot:
        return self._snapshot

    def drain_events(self, limit: int = 200) -> Sequence[RuntimeEvent]:
        self.calls.append("drain_events")
        take = max(0, int(limit))
        drained = self._events[:take]
        del self._events[:take]
        return tuple(drained)

    @property
    def lifecycle(self) -> RunLifecycle:
        return RunLifecycle(self._kwargs["lifecycle"])

    def _rebuild(self, **changes: object) -> None:
        self._kwargs.update(changes)
        self._kwargs["seq"] = int(self._snapshot.seq) + 1
        self._snapshot = make_snapshot(**self._kwargs)  # type: ignore[arg-type]

    def set_lifecycle(self, lifecycle: RunLifecycle | str, *, detail: str | None = None) -> None:
        self._rebuild(lifecycle=RunLifecycle(lifecycle), detail=detail)

    def set_game(
        self,
        *,
        state: str | None = None,
        battle_active: bool | None = None,
        pause_reason: str | None = None,
        battle_id: int | None = None,
    ) -> None:
        changes: dict[str, object] = {}
        if state is not None:
            changes["game_state"] = state
        if battle_active is not None:
            changes["battle_active"] = battle_active
        if pause_reason is not None:
            changes["pause_reason"] = pause_reason
        if battle_id is not None:
            changes["battle_id"] = battle_id
        self._rebuild(**changes)

    def set_plan(self, plan: EffectivePlan | None) -> None:
        self._rebuild(plan=plan)

    def set_performance(self, perf: PerformanceView | None) -> None:
        self._rebuild(perf=perf)

    def set_capabilities(self, capabilities: CapabilityFlags) -> None:
        self._rebuild(capabilities=capabilities)

    def set_dropped_events(self, dropped: int) -> None:
        self._rebuild(dropped_events=int(dropped))

    def set_error(
        self,
        *,
        code: str = "cleanup_failed",
        message: str = "统一清理未完成",
        source: str = "src.runtime.service.RuntimeService",
        detail: str = "",
        recoverable: bool = False,
        cleanup_pending: Sequence[str] = (),
    ) -> None:
        pending = tuple(cleanup_pending)
        self._rebuild(
            lifecycle=RunLifecycle.ERROR,
            error=ErrorView(
                code=code,
                message=message,
                source=source,
                detail=detail,
                at_wall=_WALL_DEFAULT,
                recoverable=recoverable,
            ),
            cleanup=CleanupView(
                requested=True,
                input_released=False,
                input_thread_joined=False,
                watchdog_stopped=False,
                event_sink_stopped=False,
                screenshots_cleaned=False,
                capture_closed=False,
                single_instance_released=False,
                pending_items=pending or ("输入线程未退出",),
                completed=False,
            ),
        )

    def complete_stop(self) -> None:
        self._rebuild(
            lifecycle=RunLifecycle.STOPPED,
            detail="已停止：清理完成",
            cleanup=CleanupView(
                requested=True,
                input_released=True,
                input_thread_joined=True,
                watchdog_stopped=True,
                event_sink_stopped=True,
                screenshots_cleaned=True,
                capture_closed=True,
                single_instance_released=True,
                pending_items=(),
                completed=True,
            ),
            error=None,
        )

    # -- 预检与账号轮换（M2/M3） ---------------------------------------
    def plan_preview(self, options: StartOptions) -> PlanResult:
        self.calls.append("plan_preview")
        self.preview_options.append(options)
        if self.plan_preview_result is not None:
            return self.plan_preview_result
        return PlanResult(ok=True, plan=self._kwargs.get("plan"))  # type: ignore[arg-type]

    def set_plan_preview(self, result: PlanResult | None) -> None:
        self.plan_preview_result = result

    def set_wait_account(
        self,
        current: str | None,
        next_alias: str | None,
        *,
        battles_completed: int = 1,
        rotation_aliases: Sequence[str] = (),
    ) -> None:
        aliases = tuple(rotation_aliases) or tuple(
            item for item in (current, next_alias) if item is not None
        )
        session = SessionView(
            account_alias=current,
            battles_completed=battles_completed,
            battles_target=None,
            battle_in_progress=False,
            elapsed_s=0.0,
            last_battle_duration_s=None,
            daily_used=None,
            rotation_index=0,
            rotation_aliases=aliases,
            next_alias=next_alias,
        )
        self._rebuild(
            lifecycle=RunLifecycle.WAIT_ACCOUNT,
            detail="等待手动切换账号",
            session=session,
            error=None,
            cleanup=CleanupView(
                requested=True,
                input_released=True,
                input_thread_joined=True,
                watchdog_stopped=True,
                event_sink_stopped=True,
                screenshots_cleaned=True,
                capture_closed=True,
                single_instance_released=True,
                pending_items=(),
                completed=True,
            ),
        )

    def confirm_account_switch(self) -> bool:
        self.calls.append("confirm_account_switch")
        if self.lifecycle is not RunLifecycle.WAIT_ACCOUNT:
            return False
        if not self.confirm_result:
            return False
        self.set_lifecycle(RunLifecycle.STARTING, detail="确认切号：下一账号启动")
        return True

    def abort_rotation(self) -> bool:
        self.calls.append("abort_rotation")
        if self.lifecycle is not RunLifecycle.WAIT_ACCOUNT:
            return False
        if not self.abort_result:
            return False
        self.set_lifecycle(RunLifecycle.STOPPED, detail=ROTATION_ABORTED)
        return True

    # -- 事件 ---------------------------------------------------------
    def push_event(
        self,
        kind: RuntimeEventKind | str = RuntimeEventKind.LOG_RECORD,
        *,
        level: str = "INFO",
        message: str = "",
        session_id: int | None = None,
        battle_id: int | None = None,
        detail: dict[str, object] | None = None,
    ) -> RuntimeEvent:
        event = RuntimeEvent(
            seq=self._next_seq,
            at=float(self._next_seq),
            wall_at=_WALL_DEFAULT,
            kind=RuntimeEventKind(kind),
            level=level,
            message=message,
            session_id=session_id,
            battle_id=battle_id,
            detail=detail or {},
        )
        self._next_seq += 1
        self._events.append(event)
        return event

    # -- 控制命令 -----------------------------------------------------
    def start(self, options: StartOptions) -> StartResult:
        self.calls.append("start")
        self.start_options.append(options)
        if self.lifecycle is not RunLifecycle.STOPPED:
            result = StartResult(accepted=False, reason="already_active")
            self.start_results.append(result)
            return result
        self.set_lifecycle(RunLifecycle.STARTING, detail="已接受启动：装配运行时")
        result = StartResult(accepted=True)
        self.start_results.append(result)
        return result

    def request_pause(self, reason: str = "gui_pause", detail: str = "") -> bool:
        self.calls.append("pause")
        self.pause_requests.append(reason)
        if self.lifecycle is RunLifecycle.RUNNING:
            self.set_lifecycle(RunLifecycle.PAUSING, detail=f"暂停请求：{reason}")
        elif self.lifecycle is RunLifecycle.PAUSING:
            self.set_lifecycle(RunLifecycle.PAUSED, detail=f"已暂停：{reason}")
        return True

    def request_resume(self) -> bool:
        self.calls.append("resume")
        if self.lifecycle in (RunLifecycle.PAUSED, RunLifecycle.PAUSING):
            self.set_lifecycle(RunLifecycle.RUNNING, detail="已恢复：运行中")
        return True

    def request_stop(self, reason: str = "gui_stop", detail: str = "") -> bool:
        self.calls.append("stop")
        self.stop_requests.append(reason)
        if self.lifecycle not in (RunLifecycle.STOPPED, RunLifecycle.ERROR):
            self.set_lifecycle(RunLifecycle.STOPPING, detail=f"停止中：{reason}")
        return True

    def shutdown(self) -> bool:
        self.calls.append("shutdown")
        self.shutdown_called = True
        if self.lifecycle not in (RunLifecycle.STOPPED, RunLifecycle.ERROR):
            self.set_lifecycle(RunLifecycle.STOPPING, detail="关闭窗口：正在清理")
        return True


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------


class App:
    """托管台主窗口：顶部运行栏 + 左侧导航 + 中央工作区 + 右/底部摘要 + 状态行。"""

    POLL_EVENTS_MS = 100
    POLL_SNAPSHOT_MS = 200

    def __init__(
        self,
        root: tk.Tk,
        service: ServiceProtocol,
        *,
        poll: bool = True,
        recent_lines: int = RECENT_EVENT_LINES,
        log_max_lines: int = 2000,
        catalog: GuiCatalog | None = None,
        tool_popen_factory: object | None = None,
        tool_auto_poll: bool | None = None,
    ) -> None:
        self.root = root
        self.service = service
        self.poll = poll
        self.project_root = Path(catalog.project_root) if catalog is not None else Path.cwd()
        self.catalog = catalog or load_gui_catalog(self.project_root)
        self._tool_auto_poll = poll if tool_auto_poll is None else bool(tool_auto_poll)
        self._tool_popen_factory = tool_popen_factory
        self._preview_plan: EffectivePlan | None = None
        self._validation: PlanResult | None = None
        self._tool_running = False
        self._ship_dialog: ShipImportDialog | None = None
        self._destroyed = False
        self._closing = False
        self._closing_error = False
        self._closing_shutdown_called = False
        self._events_after: str | None = None
        self._snapshot_after: str | None = None
        self._notice_after: str | None = None
        self._start_debounce = StartDebounce()
        self._recent = RecentEvents(recent_lines)
        self._log_max_lines = log_max_lines
        self._recent_lines = recent_lines
        self._last_snapshot: RunSnapshot | None = None
        self._current_page = "run"
        self._fullscreen = False
        self._background = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gui-data") if poll else None
        self._history_future: Future | None = None
        self._history_context: tuple | None = None
        self._history_next_at = 0.0
        self._idle_perf: PerformanceView | None = None
        cooperate = getattr(service, "enable_gui_cooperation", None)
        if callable(cooperate):
            cooperate()

        root.title("战舰世界 · 托管台")
        root.geometry("1280x820")
        root.minsize(1024, 720)
        root.configure(bg=widgets.BG)
        widgets.configure_styles(root)

        self._build_shell()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self._report_callback_exception  # type: ignore[method-assign]

        if catalog is None:
            self.refresh_catalog(initial=True)
        else:
            self._apply_catalog(initial=True)
        self.poll_once()
        if poll:
            self._schedule_snapshot()
            self._schedule_events()

    # -- 构建 ---------------------------------------------------------
    def _build_shell(self) -> None:
        root = self.root
        root.grid_rowconfigure(3, weight=1)
        root.grid_columnconfigure(1, weight=1)

        self._build_topbar()

        self._banner_slot = tk.Frame(root, bg=widgets.BG)
        self._banner_slot.grid(row=1, column=1, sticky="ew")
        self.banner = widgets.Banner(self._banner_slot)

        self._cleanup_slot = tk.Frame(root, bg=widgets.BG)
        self._cleanup_slot.grid(row=2, column=1, sticky="ew")
        self.cleanup_banner = widgets.Banner(
            self._cleanup_slot,
            buttons=(
                ("继续等待清理", self.continue_waiting),
                ("退出应用", self.exit_anyway),
            ),
        )

        main = tk.Frame(root, bg=widgets.BG)
        self._main = main
        main.grid(row=3, column=1, sticky="nsew")
        main.grid_rowconfigure(0, weight=1)
        main.grid_columnconfigure(1, weight=1)
        main.grid_columnconfigure(3, minsize=widgets.px(240))

        self.sidebar = widgets.Sidebar(root, PAGE_TITLES, self.set_page)
        self.sidebar.grid(row=0, column=0, rowspan=6, sticky="ns")

        self._workspace = tk.Frame(main, bg=widgets.BG)
        self._workspace.grid(row=0, column=1, sticky="nsew")
        self._workspace.grid_rowconfigure(0, weight=1)
        self._workspace.grid_columnconfigure(0, weight=1)
        self._sep_right = tk.Frame(main, bg=widgets.BORDER, width=1)
        self._sep_right.grid(row=0, column=2, sticky="ns")

        self.performance_summary = widgets.PerformanceSummary(main)
        self.performance_summary.grid(row=0, column=3, sticky="nsew", padx=(0, 0))

        self.performance_page = PerformancePage(self._workspace)
        self.log_page = LogPage(self._workspace, max_lines=self._log_max_lines)
        self.settings_page = SettingsPage(
            self._workspace,
            on_fullscreen=self.set_fullscreen,
            on_tool_running=self._on_tool_running,
            on_import_ship=self.open_ship_import,
            on_validate=self._on_tool_validate,
            project_root=self.project_root,
            context_provider=self._tool_context,
            popen_factory=self._tool_popen_factory,  # type: ignore[arg-type]
            auto_poll=self._tool_auto_poll,
        )
        self.run_page = RunPage(
            self.settings_page.config_container,
            on_validate=self.validate_config,
            on_import_ship=self.open_ship_import,
            on_profile_change=self._on_profile_change,
            on_confirm_account_switch=self.on_confirm_account_switch,
            on_abort_rotation=self.on_abort_rotation,
            embedded_scroll=self.settings_page.scroll,
        )
        self.run_page.pack(fill="both", expand=True)
        self.data_page = DataPage(self._workspace, on_target=self._on_target,
                                  on_confirm=self.on_confirm_account_switch, on_abort=self.on_abort_rotation)
        self.pages: dict[str, tk.Frame] = {
            "run": self.data_page,
            "performance": self.performance_page,
            "log": self.log_page,
            "settings": self.settings_page,
        }
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")
            page.grid_remove()

        self.bottom_log = widgets.RecentLogPanel(root, max_lines=self._recent_lines,
                                                on_show_all=lambda: self.set_page("log"))
        self.bottom_log.grid(row=4, column=1, sticky="ew")
        self.statusbar = widgets.StatusBar(root)
        self.statusbar.grid(row=5, column=1, sticky="ew")
        self.set_page("run", initial=True)

    def _build_topbar(self) -> None:
        topbar = tk.Frame(self.root, bg=widgets.PANEL, highlightbackground=widgets.BORDER, highlightthickness=1)
        topbar.grid(row=0, column=1, sticky="ew")
        left = tk.Frame(topbar, bg=widgets.PANEL)
        left.pack(side="left", padx=20, pady=18)
        self.page_title_label = tk.Label(
            left, text="数据", bg=widgets.PANEL, fg=widgets.TEXT, font=widgets.font(10), anchor="w"
        )
        self.page_title_label.pack(anchor="w")
        right = tk.Frame(topbar, bg=widgets.PANEL)
        right.pack(side="right", padx=14, pady=12)
        state_row = tk.Frame(right, bg=widgets.PANEL)
        state_row.pack(side="left", padx=(0, 12))
        self.lifecycle_label = tk.Label(
            state_row,
            text="—",
            bg=widgets.PANEL,
            fg=widgets.SECONDARY,
            font=widgets.font(9),
            anchor="w",
        )
        self.lifecycle_label.pack(side="left")
        self.game_label = tk.Label(
            state_row,
            text="游戏流程：—",
            bg=widgets.PANEL,
            fg=widgets.SECONDARY,
            font=widgets.font(9),
            anchor="w",
        )
        self.buttons: dict[str, ttk.Button] = {}
        for key, text, style, command in (
            ("start", "启动", "Accent.TButton", self.on_start_clicked),
            ("pause", "暂停", "TButton", self.on_pause_clicked),
            ("resume", "恢复", "TButton", self.on_resume_clicked),
            ("stop", "停止", "TButton", self.on_stop_clicked),
        ):
            button = widgets.make_button(right, text, command, style=style)
            button.configure(width=4)
            button.pack(side="left", padx=(0, 6))
            self.buttons[key] = button

    # -- 页面切换 -----------------------------------------------------
    def set_page(self, page_id: str, *, initial: bool = False) -> None:
        if page_id not in self.pages:
            return
        self._current_page = page_id
        for key, page in self.pages.items():
            if key == page_id:
                page.grid()
            else:
                page.grid_remove()
        self.page_title_label.configure(text=PAGE_TITLE_BY_ID[page_id])
        self.sidebar.set_active(page_id)
        if page_id == "log":
            self.bottom_log.grid_remove()
        else:
            self.bottom_log.grid()
        if page_id != "run":
            self.performance_summary.grid_remove()
            self._sep_right.grid_remove()
            self._main.grid_columnconfigure(3, minsize=0)
        else:
            self.performance_summary.grid()
            self._sep_right.grid()
            self._main.grid_columnconfigure(3, minsize=widgets.px(240))
        if initial:
            self._apply_button_matrix(RunLifecycle.STOPPED)

    def _on_target(self, command: RunCommand) -> bool:
        submit = getattr(self.service, "submit_command", None)
        return bool(callable(submit) and submit(command))

    def _read_dashboard(self, alias: str | None) -> DashboardView:
        """后台只读任务；不调用任何 Tk 方法。"""
        return self.service.read_dashboard(alias)

    def _poll_dashboard(self, snapshot: RunSnapshot) -> None:
        plan = snapshot.plan or self._preview_plan
        path = plan.db_path if plan else self.project_root / "data" / "wows_script.db"
        alias = snapshot.session.account_alias if snapshot.plan else self.run_page.account_alias()
        context = (path, alias)
        now = time.monotonic()
        if self._history_future is not None and self._history_future.done():
            try:
                dashboard = self._history_future.result()
                self._idle_perf = dashboard.perf
                if self._history_context == context:
                    self.data_page.set_history(dashboard.history)
            except Exception as exc:
                self.data_page.set_history(HistoryView(error=f"数据读取失败：{exc}"))
            self._history_future = None
        if self._background is not None and self._history_future is None and now >= self._history_next_at:
            self._history_context = context
            self._history_future = self._background.submit(self._read_dashboard, alias)
            self._history_next_at = now + 1.0

    # -- 启动配置（M2） -----------------------------------------------
    def _apply_catalog(self, *, initial: bool = False) -> None:
        self.run_page.set_catalogs(
            self.catalog.profiles,
            self.catalog.accounts,
            rotation_available=self.catalog.rotation_available,
            ships=self.catalog.ships,
        )
        self.settings_page.set_ships(self.catalog.ships)
        if not initial:
            self.run_page.mark_dirty()

    def refresh_catalog(self, *, profile_id: str | None = None, initial: bool = False) -> None:
        """只读刷新客户端/账号/船只候选（不写任何配置文件）。"""

        current = profile_id or self.run_page.selected_profile_id() or self.catalog.default_profile_id
        try:
            self.catalog = load_gui_catalog(self.project_root, profile_id=current)
        except Exception as exc:  # noqa: BLE001 - 候选读取失败不阻塞主界面
            self._show_notice("warning", f"读取客户端/账号/船只候选失败：{exc}")
            return
        self._apply_catalog(initial=initial)

    def _on_profile_change(self, profile_id: str) -> None:
        self.refresh_catalog(profile_id=profile_id)

    def _tool_context(self) -> tuple[str | None, str | None, str | None]:
        return (
            self.run_page.selected_profile_id(),
            self.run_page.selected_ship_id(),
            self.run_page.account_alias(),
        )

    def _start_options(self) -> StartOptions:
        return self.run_page.build_start_options(self.project_root)

    def on_start_clicked(self) -> None:
        if self._destroyed or self._closing:
            return
        if self._tool_running:
            self._show_notice("warning", TOOL_RUNNING_START_DISABLED)
            return
        if not self._start_debounce.try_acquire():
            return
        try:
            options = self._start_options()
        except ValueError as exc:
            self._start_debounce.release()
            self._show_notice("warning", str(exc))
            return
        self.buttons["start"].configure(state="disabled", text="启动中…")
        self._apply_button_matrix(self._current_lifecycle())
        try:
            result = self.service.start(options)
        except Exception as exc:  # noqa: BLE001 - 服务不可用时给出中文提示而不是崩溃
            self._start_debounce.release()
            self.buttons["start"].configure(text="启动")
            self._handle_service_error(exc)
            return
        if result is not None and not result.accepted:
            self._start_debounce.release()
            self.buttons["start"].configure(text="启动")
            reason = result.reason or "未知原因"
            detail = "；".join(f"{item.location}: {item.message}" for item in result.issues[:3])
            message = f"启动被拒绝：{reason}"
            if detail:
                message += f"（{detail}）"
            self._show_notice("warning", message)
            self._apply_button_matrix(self._current_lifecycle())
            return
        self._show_notice("warning", "启动中…（等待运行服务确认）", timeout_ms=4000)

    def validate_config(self) -> None:
        """“校验配置”：调用服务 ``plan_preview`` 并常驻展示 errors/warnings（11.1）。"""

        if self._destroyed or self._closing:
            return
        try:
            options = self._start_options()
        except ValueError as exc:
            self._show_notice("warning", str(exc))
            return
        try:
            result = self.service.plan_preview(options)
        except Exception as exc:  # noqa: BLE001
            self._handle_service_error(exc, action="plan_preview")
            return
        self._validation = result
        self._preview_plan = result.plan if result is not None and result.ok else None
        self.run_page.set_validation(result, dirty=False)
        if result is not None and result.ok:
            self._show_notice(
                "warning",
                f"配置校验通过：{len(result.warnings)} 个警告（启动时服务会再次权威校验）",
                timeout_ms=5000,
            )
        elif result is not None:
            self._show_notice(
                "error",
                f"配置校验失败：{len(result.issues)} 个错误（见运行页列表，含文件/字段/原因）",
                timeout_ms=8000,
            )
        self.poll_once()

    def _on_tool_validate(
        self,
        errors: list[dict[str, str]],
        warnings: list[dict[str, str]],
        exit_code: int,
    ) -> None:
        """标定工具退出后的自动校验结果 → 运行页配置校验栏。"""

        issues = tuple(
            ValidationIssueView(
                severity="error",
                code=str(item.get("code", "")),
                location=str(item.get("location", "")),
                message=str(item.get("message", "")),
            )
            for item in errors
        )
        warnings_view = tuple(
            ValidationIssueView(
                severity="warning",
                code=str(item.get("code", "")),
                location=str(item.get("location", "")),
                message=str(item.get("message", "")),
            )
            for item in warnings
        )
        result = PlanResult(ok=not issues, plan=None, issues=issues, warnings=warnings_view)
        self._validation = result
        self.run_page.set_validation(result, dirty=False)
        if issues:
            self._show_notice(
                "error",
                f"标定工具后校验失败：{len(issues)} 个错误（退出码 {exit_code}，见设置页校验结果）",
                timeout_ms=8000,
            )
        else:
            self._show_notice(
                "warning",
                f"标定工具后校验通过：{len(warnings_view)} 个警告（退出码 {exit_code}）",
                timeout_ms=5000,
            )

    def _on_tool_running(self, running: bool) -> None:
        self._tool_running = bool(running)
        self.run_page.set_tool_running(self._tool_running)
        self._apply_button_matrix(self._current_lifecycle())
        if self._tool_running:
            self._show_notice("warning", TOOL_RUNNING_START_DISABLED)
        else:
            self._show_notice("warning", "标定工具已退出：请重新校验配置后再启动托管。", timeout_ms=6000)

    # -- 船卡导入（M2） -----------------------------------------------
    def open_ship_import(self) -> None:
        if self._destroyed or self._closing:
            return
        lifecycle = self._current_lifecycle()
        if lifecycle is not RunLifecycle.STOPPED:
            self._show_notice("warning", "请先停止脚本并等待清理完成，再导入船卡（运行中不可修改船只档案）。")
            return
        if self.settings_page.tool_running:
            self._show_notice("warning", "标定工具运行中：同一时间只允许一个工具/导入操作。")
            return
        profile_id = self.run_page.selected_profile_id() or self.catalog.default_profile_id
        if not profile_id:
            self._show_notice("warning", "没有可用的客户端配置（profiles/*/profile.yaml）。")
            return
        if self._ship_dialog is not None:
            try:
                self._ship_dialog.top.lift()
            except Exception:  # noqa: BLE001 - 对话框已关闭
                self._ship_dialog = None
            return
        try:
            self._ship_dialog = ShipImportDialog(
                self.root,
                project_root=self.project_root,
                profile_id=profile_id,
                on_saved=self._on_ship_imported,
                close_on_save=True,
            )
        except Exception as exc:  # noqa: BLE001
            self._ship_dialog = None
            self._show_notice("error", f"无法打开船卡导入窗口：{type(exc).__name__}: {exc}")
            return
        self._show_notice("warning", "船卡导入：只保存本机裁剪模板，不上传；保存后需重新校验配置。", timeout_ms=6000)

    def _on_ship_imported(self, result: ShipSaveResult) -> None:
        self._ship_dialog = None
        self.refresh_catalog(profile_id=self.run_page.selected_profile_id())
        self.run_page.set_ship_name(result.display_name)
        self.run_page.set_validation(None)
        self._preview_plan = None
        # 保存后自动做一次只读预检，刷新“生效值”表（不自动启动）。
        if self._current_lifecycle() is RunLifecycle.STOPPED and not self._tool_running:
            self.validate_config()
        self._show_notice(
            "warning",
            f"已保存船卡：{result.display_name}（{result.ship_id}）；尚未实机验证识别效果，请重新校验配置。",
            timeout_ms=8000,
        )

    # -- M3 账号轮换等待面板（10.2） ----------------------------------
    def on_confirm_account_switch(self) -> None:
        if self._destroyed or self._closing:
            return
        try:
            confirmed = self.service.confirm_account_switch()
        except Exception as exc:  # noqa: BLE001
            self._handle_service_error(exc, action="confirm_account_switch")
            return
        if confirmed:
            self.run_page.set_rotation_status(ROTATION_CONFIRM_OK)
            self._show_notice("warning", ROTATION_CONFIRM_OK, timeout_ms=5000)
        else:
            self.run_page.set_rotation_status(ROTATION_CONFIRM_FAILED)
            self._show_notice("warning", ROTATION_CONFIRM_FAILED, timeout_ms=8000)

    def on_abort_rotation(self) -> None:
        if self._destroyed or self._closing:
            return
        try:
            aborted = self.service.abort_rotation()
        except Exception as exc:  # noqa: BLE001
            self._handle_service_error(exc, action="abort_rotation")
            return
        if aborted:
            self.run_page.set_rotation_status(ROTATION_ABORTED)
            self._show_notice("warning", ROTATION_ABORTED, timeout_ms=5000)
        else:
            self._show_notice("warning", "结束轮换失败：当前不在等待手动切号状态。")

    def on_pause_clicked(self) -> None:
        if self._destroyed or self._closing:
            return
        self._call_service("pause", lambda: self.service.request_pause("gui_pause", ""))

    def on_resume_clicked(self) -> None:
        if self._destroyed or self._closing:
            return
        self._call_service("resume", self.service.request_resume)

    def on_stop_clicked(self) -> None:
        if self._destroyed or self._closing:
            return
        if self._current_lifecycle() is RunLifecycle.WAIT_ACCOUNT:
            self.on_abort_rotation()
            return
        self._call_service("stop", lambda: self.service.request_stop("gui_stop", ""))

    def _call_service(self, action: str, call) -> None:
        try:
            call()
        except Exception as exc:  # noqa: BLE001
            self._handle_service_error(exc, action=action)

    # -- 全屏 ---------------------------------------------------------
    def set_fullscreen(self, enabled: bool) -> None:
        """勾选/取消“全屏显示”：立即切换窗口，纯内存态，不改托管状态。"""

        self._fullscreen = bool(enabled)
        try:
            self.root.attributes("-fullscreen", self._fullscreen)
        except tk.TclError as exc:
            self._show_notice("warning", f"无法切换全屏：{exc}")
            return
        if self.settings_page.fullscreen != self._fullscreen:
            self.settings_page.fullscreen_var.set(self._fullscreen)

    @property
    def fullscreen(self) -> bool:
        return self._fullscreen

    # -- 关闭流程（6.5 / 11.5 / 11.6） --------------------------------
    def on_close(self) -> None:
        if self._destroyed or self._closing:
            return
        if self.settings_page.tool_running:
            self._show_notice(
                "warning",
                "标定工具仍在运行：关闭窗口不会结束工具进程；请先关闭工具窗口后再关闭控制台。",
            )
            return
        lifecycle = self._current_lifecycle()
        behavior = close_behavior(lifecycle)
        if behavior == "direct":
            self.destroy()
            return
        self._closing = True
        self._set_closing_ui()
        if behavior == "confirm":
            self._show_cleanup_error()
            return
        try:
            self.service.shutdown()
            self._closing_shutdown_called = True
        except Exception as exc:  # noqa: BLE001
            self._show_notice("error", f"请求停止失败：{exc}")
        self.poll_once()

    def _set_closing_ui(self) -> None:
        for button in self.buttons.values():
            button.configure(state="disabled")
        self.sidebar.set_enabled(False)
        self.banner.show(CLOSING_STATUS, "warning")
        self.statusbar.set_texts(("输入状态：正在让出输入", CLOSING_STATUS, "目标：—"))
        if not self._closing_error:
            self.cleanup_banner.hide()

    def _show_cleanup_error(self) -> None:
        if self._closing_error:
            return
        self._closing_error = True
        self._closing = True
        snapshot = self._last_snapshot
        if snapshot is not None:
            items = "、".join(snapshot.cleanup.pending_items) or (
                snapshot.error.message if snapshot.error is not None else "未知"
            )
            error = snapshot.error
            suffix = f"（{error.code} @ {error.source}）" if error is not None else ""
        else:
            items, suffix = "未知", ""
        self.cleanup_banner.show(CLEANUP_ERROR_BANNER.format(items=items) + suffix, "error")
        for button in self.buttons.values():
            button.configure(state="disabled")
        self.sidebar.set_enabled(False)

    def continue_waiting(self) -> None:
        if self._destroyed:
            return
        self._show_notice("warning", "继续等待清理完成…（清理完成前不会强制结束运行线程）", timeout_ms=6000)
        self.cleanup_banner.show(
            CLEANUP_ERROR_BANNER.format(items=self._pending_items_text()) + "（继续等待中）",
            "error",
        )

    def exit_anyway(self) -> None:
        """用户选择退出应用：明确警告可能残留输入；GUI 不强杀线程（6.5）。"""

        self.log_page.append_note(
            "用户选择退出应用：可能有未释放的键鼠状态/未写完的日志；"
            "建议先用 taskkill /PID <pid> /T /F 结束整棵进程树，并确认无残留进程。",
            level="WARNING",
        )
        self.destroy()

    def _pending_items_text(self) -> str:
        snapshot = self._last_snapshot
        if snapshot is None:
            return "未知"
        return "、".join(snapshot.cleanup.pending_items) or "未知"

    # -- 轮询 ---------------------------------------------------------
    def poll_once(self) -> None:
        if self._destroyed:
            return
        self._poll_snapshot()
        if not self._destroyed:
            self._poll_events()

    def _schedule_snapshot(self) -> None:
        if self._destroyed or not self.poll:
            return
        self._snapshot_after = self.root.after(self.POLL_SNAPSHOT_MS, self._tick_snapshot)

    def _tick_snapshot(self) -> None:
        self._snapshot_after = None
        if self._destroyed:
            return
        try:
            self._poll_snapshot()
        finally:
            self._schedule_snapshot()

    def _schedule_events(self) -> None:
        if self._destroyed or not self.poll:
            return
        self._events_after = self.root.after(self.POLL_EVENTS_MS, self._tick_events)

    def _tick_events(self) -> None:
        self._events_after = None
        if self._destroyed:
            return
        try:
            self._poll_events()
        finally:
            self._schedule_events()

    def _poll_snapshot(self) -> None:
        try:
            snapshot = self.service.snapshot()
        except Exception as exc:  # noqa: BLE001
            self._handle_service_error(exc, action="snapshot")
            return
        if snapshot is None:
            return
        self._last_snapshot = snapshot
        self._render(snapshot)
        if self._closing:
            lifecycle = snapshot.lifecycle.lifecycle
            if lifecycle is RunLifecycle.STOPPED:
                self.destroy()
            elif lifecycle is RunLifecycle.ERROR:
                self._show_cleanup_error()

    def _poll_events(self) -> None:
        try:
            events = self.service.drain_events(limit=200)
        except Exception as exc:  # noqa: BLE001
            self._handle_service_error(exc, action="drain_events")
            return
        if not events:
            return
        self.log_page.append_events(events)
        rows = [log_row_from_event(event) for event in events]
        self._recent.push(rows)
        self.bottom_log.set_rows(self._recent.rows)
        for event in events:
            if event.kind is RuntimeEventKind.ACCOUNT_SWITCH_WAITING:
                self.run_page.set_rotation_status(event.message or "预检状态：等待确认")
            elif event.kind is RuntimeEventKind.ACCOUNT_SWITCH_CONFIRMED:
                self.run_page.set_rotation_status(event.message or ROTATION_CONFIRM_OK)
            notice = event_notice(event)
            if notice is not None:
                self._show_notice(*notice)

    # -- 渲染 ---------------------------------------------------------
    def _render(self, snapshot: RunSnapshot) -> None:
        lifecycle = snapshot.lifecycle.lifecycle
        if self._start_debounce.pending:
            if lifecycle is RunLifecycle.STOPPED:
                self._start_debounce.release()
                self.buttons["start"].configure(text="启动")
            elif lifecycle is not RunLifecycle.STARTING:
                # 启动已被接受：按钮文案恢复（仍由矩阵控制可用性），防抖保持到回到 STOPPED。
                self.buttons["start"].configure(text="启动")

        self.lifecycle_label.configure(
            text=lifecycle_badge_text(lifecycle).split("（")[0],
            fg=lifecycle_color(lifecycle),
        )
        if self._closing:
            self.page_title_label.configure(text=CLOSING_TITLE)
        else:
            self.page_title_label.configure(text=PAGE_TITLE_BY_ID[self._current_page])
        self.game_label.configure(text=game_state_display(snapshot.game.state).split("（")[0])

        capabilities = snapshot.capabilities
        display_plan = snapshot.plan if snapshot.plan is not None else self._preview_plan
        self._poll_dashboard(snapshot)
        self.data_page.render(snapshot, display_plan)
        self.run_page.render(snapshot, capabilities, plan=display_plan)
        perf = snapshot.perf
        if self._idle_perf is not None and (perf is None or lifecycle is RunLifecycle.STOPPED):
            perf = self._idle_perf
        self.performance_summary.render(perf, snapshot.input.pending_actions, capabilities)
        self.performance_page.render(perf, capabilities, pending_actions=snapshot.input.pending_actions)
        self.settings_page.render(display_plan, capabilities)
        self.log_page.set_battle_context(snapshot.game.battle_id)
        self.log_page.set_service_dropped(snapshot.dropped_events)
        self.log_page.set_log_path(display_plan.log_path if display_plan is not None else None)
        self.sidebar.set_footer(
            display_plan.client_display_name if display_plan is not None else "—",
            display_plan.ship_display_name if display_plan is not None else "—",
        )
        if self._closing:
            self.statusbar.set_texts(("输入状态：正在让出输入", CLOSING_STATUS, "目标：—"))
        else:
            self.statusbar.set_texts(status_bar_texts(snapshot))
        editable = lifecycle is RunLifecycle.STOPPED and not self._tool_running and not self._closing
        self.run_page.set_config_editable(editable)
        self._apply_button_matrix(lifecycle, start_ready=snapshot.error is None)

        if not self._closing and lifecycle is RunLifecycle.ERROR and snapshot.error is not None:
            items = "、".join(snapshot.cleanup.pending_items) or snapshot.error.message
            self.banner.show(
                CLEANUP_ERROR_BANNER.format(items=items)
                + f"（{snapshot.error.code} @ {snapshot.error.source}）",
                "error",
            )

    def _apply_button_matrix(self, lifecycle: RunLifecycle | str | None, *, start_ready: bool = True) -> None:
        matrix = button_matrix(
            lifecycle,
            start_ready=start_ready,
            tool_running=self._tool_running,
            closing=self._closing,
        )
        for key in ("start", "pause", "resume", "stop"):
            enabled = matrix.get(key, False)
            if key == "start" and self._start_debounce.pending:
                enabled = False
            self.buttons[key].configure(state="normal" if enabled else "disabled")
        if lifecycle in (RunLifecycle.PAUSED, RunLifecycle.PAUSING):
            self.buttons["pause"].pack_forget()
            self.buttons["resume"].pack(side="left", padx=(0, 6), before=self.buttons["stop"])
        else:
            self.buttons["resume"].pack_forget()
            self.buttons["pause"].pack(side="left", padx=(0, 6), before=self.buttons["stop"])
        try:
            self.run_page.set_import_enabled(matrix.get("import_ship", False))
            self.run_page.set_validate_enabled(matrix.get("validate_config", False))
            self.settings_page.set_tool_allowed(
                lifecycle is RunLifecycle.STOPPED and not self._closing and not self._tool_running
            )
            self.settings_page.set_import_enabled(matrix.get("import_ship", False))
        except tk.TclError:
            pass

    # -- 工具 ---------------------------------------------------------
    def _current_lifecycle(self) -> RunLifecycle | None:
        try:
            value = self.service.lifecycle
        except Exception:  # noqa: BLE001 - 服务异常时按“未停止”保守处理
            return self._last_snapshot.lifecycle.lifecycle if self._last_snapshot is not None else None
        try:
            return RunLifecycle(value)
        except ValueError:
            return None

    def _handle_service_error(self, exc: BaseException, *, action: str = "") -> None:
        prefix = f"与运行服务通信失败（{action}）" if action else "与运行服务通信失败"
        message = f"{prefix}：{type(exc).__name__}: {exc}"
        try:
            self.log_page.append_note(message, level="ERROR")
        except tk.TclError:
            pass
        self._show_notice("error", message)

    def _show_notice(self, level: str, text: str, *, timeout_ms: int | None = None) -> None:
        if self._destroyed:
            return
        try:
            self.banner.show(text, "error" if level == "error" else "warning")
        except tk.TclError:
            return
        if self._notice_after is not None:
            try:
                self.root.after_cancel(self._notice_after)
            except (tk.TclError, ValueError):
                pass
            self._notice_after = None
        if timeout_ms:
            self._notice_after = self.root.after(timeout_ms, self._hide_notice)

    def _hide_notice(self) -> None:
        self._notice_after = None
        if not self._destroyed and not self._closing:
            self.banner.hide()

    def _report_callback_exception(self, exc_type, exc_value, exc_traceback) -> None:
        message = f"界面回调异常：{exc_type.__name__}: {exc_value}"
        try:
            self.log_page.append_note(message, level="ERROR")
        except tk.TclError:
            pass
        try:
            self._show_notice("error", message)
        except tk.TclError:
            pass

    # -- 销毁 ---------------------------------------------------------
    def destroy(self) -> None:
        if self._background is not None:
            self._background.shutdown(wait=False, cancel_futures=True)
        if self._destroyed:
            return
        self._destroyed = True
        if self._ship_dialog is not None:
            try:
                self._ship_dialog.close()
            except Exception:  # noqa: BLE001 - 对话框可能已被用户关闭
                pass
            self._ship_dialog = None
        try:
            self.settings_page.shutdown_timers()
        except tk.TclError:
            pass
        for handle in (self._events_after, self._snapshot_after, self._notice_after):
            if handle is not None:
                try:
                    self.root.after_cancel(handle)
                except (tk.TclError, ValueError):
                    pass
        self._events_after = None
        self._snapshot_after = None
        self._notice_after = None
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def closing_error(self) -> bool:
        return self._closing_error

    @property
    def destroyed(self) -> bool:
        return self._destroyed

    @property
    def current_page(self) -> str:
        return self._current_page

    @property
    def start_pending(self) -> bool:
        return self._start_debounce.pending

    @property
    def tool_running(self) -> bool:
        return self._tool_running

    @property
    def validation(self) -> PlanResult | None:
        return self._validation

    @property
    def preview_plan(self) -> EffectivePlan | None:
        return self._preview_plan

    @property
    def ship_dialog(self) -> ShipImportDialog | None:
        return self._ship_dialog


def seq_gap_notice(gap_total: int, dropped: int = 0) -> str:
    """兼容入口：日志页/测试共用的序号缺口文案。"""

    return seq_gap_text(gap_total, dropped)


__all__ = [
    "App",
    "FakeService",
    "GuiCatalog",
    "PAGE_TITLES",
    "ServiceProtocol",
    "load_gui_catalog",
    "make_snapshot",
]
