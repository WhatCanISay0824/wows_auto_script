"""GUI 冒烟测试：真实 Tk 根窗口 + FakeService（不连游戏、不发输入）。

Tk 不可用（缺 Tk 组件 / 无桌面会话）时整文件 ``pytest.skip``。
所有测试都使用临时目录中的路径（``tmp_path``），不写仓库运行产物。
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from src.gui.viewmodel import (
    CLEANUP_ERROR_BANNER,
    CLOSING_STATUS,
    FULLSCREEN_HINT,
    ROTATION_ABORTED,
    ROTATION_CONFIRM_OK,
    AccountOption,
    ProfileOption,
    ShipCandidate,
    performance_rows,
)
from src.runtime.messages import (
    CapabilityFlags,
    EffectivePlan,
    PerformanceView,
    PlanResult,
    RunLifecycle,
    StartResult,
    ValidationIssueView,
)

pytestmark: list[pytest.MarkDecorator] = []

try:  # Tk 组件缺失时跳过整个文件（2.6 降级口径）
    import tkinter as tk
except Exception:  # noqa: BLE001
    tk = None  # type: ignore[assignment]
    pytestmark = [pytest.mark.skip(reason="Tk 组件不可用，跳过 GUI 冒烟测试")]


WALL = "2026-10-31T12:00:00+08:00"


def button_state(button) -> str:
    """ttk 的 ``cget('state')`` 返回 Tcl index 对象，统一转成字符串比较。"""

    return str(button.cget("state"))


class FakeProc:
    """模拟 ``subprocess.Popen``：poll 按预设序列返回，communicate 返回捕获输出。"""

    def __init__(self, poll_codes: list[int | None], stdout: str = "", stderr: str = "") -> None:
        self._codes = list(poll_codes)
        self.returncode: int | None = None
        self._stdout = stdout
        self._stderr = stderr
        self.communicate_called = False

    def poll(self) -> int | None:
        if not self._codes:
            return self.returncode
        code = self._codes.pop(0)
        if code is not None:
            self.returncode = code
        return code

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.communicate_called = True
        return self._stdout, self._stderr


class FakePopenFactory:
    """记录启动命令并返回预设进程对象（不真正启动子进程/不弹窗口）。"""

    def __init__(self) -> None:
        self.commands: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.queued: list[FakeProc] = []

    def queue(self, poll_codes: list[int | None], stdout: str = "", stderr: str = "") -> FakeProc:
        proc = FakeProc(poll_codes, stdout, stderr)
        self.queued.append(proc)
        return proc

    def __call__(self, argv, **kwargs):
        self.commands.append((tuple(argv), dict(kwargs)))
        if self.queued:
            return self.queued.pop(0)
        return FakeProc([None])

    @property
    def last_command(self) -> tuple[str, ...]:
        return self.commands[-1][0] if self.commands else ()


def default_catalog():
    from src.gui.app import GuiCatalog

    return GuiCatalog(
        project_root=Path.cwd(),
        profiles=(ProfileOption("cn360", "国服 360"),),
        accounts=(AccountOption("alt1", "小号一号"), AccountOption("alt2", "小号二号")),
        rotation_available=True,
        ships=(ShipCandidate("iron_duke", "铁公爵"), ShipCandidate("leningrad", "列宁格勒")),
        default_profile_id="cn360",
        log_levels=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    )


@pytest.fixture
def tk_root():
    if tk is None:
        pytest.skip("Tk 组件不可用")
    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            root = tk.Tk()
        except tk.TclError as exc:  # 偶发的 Tcl 库读取竞争：短暂重试后仍失败才跳过
            last_error = exc
            time.sleep(0.2)
            continue
        root.withdraw()
        try:
            yield root
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass
        return
    pytest.skip(f"Tk 不可用或没有桌面会话：{last_error}")


@pytest.fixture
def fake_service():
    from src.gui.app import FakeService

    return FakeService()


@pytest.fixture
def fake_popen() -> FakePopenFactory:
    return FakePopenFactory()


@pytest.fixture
def app(tk_root, fake_service, fake_popen):
    from src.gui.app import App

    application = App(
        tk_root,
        fake_service,
        poll=False,
        catalog=default_catalog(),
        tool_popen_factory=fake_popen,
        tool_auto_poll=False,
    )
    try:
        yield application
    finally:
        if not application.destroyed:
            application.destroy()


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


# ---------------------------------------------------------------------------
# 结构、初始状态、协议
# ---------------------------------------------------------------------------


def test_initial_state_is_stopped(app) -> None:
    assert "已停止" in app.lifecycle_label.cget("text")
    assert "运行中" not in app.lifecycle_label.cget("text")
    assert app.run_page.groups["A"].value_text("lifecycle") == "已停止（STOPPED）"
    assert button_state(app.buttons["start"]) == "normal"
    assert button_state(app.buttons["pause"]) == "disabled"
    assert button_state(app.buttons["resume"]) == "disabled"
    assert button_state(app.buttons["stop"]) == "disabled"


def test_shell_structure(app) -> None:
    assert sorted(app.pages) == ["log", "performance", "run", "settings"]
    assert app.current_page == "run"
    # 左侧导航、右侧摘要、底部最近日志、底部状态行都存在。
    assert app.sidebar.buttons["run"].cget("style") == "NavActive.TButton"
    assert app.performance_summary.rows_grid is not None
    assert app.bottom_log.winfo_manager() == "grid"
    assert len(app.statusbar._labels) == 3


def test_window_spec_and_polling_intervals(app, tk_root) -> None:
    from src.gui.app import App

    assert App.POLL_EVENTS_MS == 100
    assert App.POLL_SNAPSHOT_MS == 200
    assert tuple(tk_root.minsize()) == (1024, 720)
    tk_root.update_idletasks()
    assert tk_root.geometry().startswith("1280x820")


def test_fake_service_satisfies_protocol() -> None:
    from src.gui.app import FakeService, ServiceProtocol

    assert isinstance(FakeService(), ServiceProtocol)


def test_page_switching_hides_duplicate_panels(app) -> None:
    app.set_page("log")
    assert app.current_page == "log"
    assert app.bottom_log.winfo_manager() == ""
    app.set_page("performance")
    assert app.performance_summary.winfo_manager() == ""
    app.set_page("run")
    assert app.bottom_log.winfo_manager() == "grid"
    assert app.performance_summary.winfo_manager() == "grid"


# ---------------------------------------------------------------------------
# 启动 / 暂停 / 恢复 / 停止
# ---------------------------------------------------------------------------


def test_start_calls_service_once_and_debounces(app, fake_service) -> None:
    for _ in range(10):
        app.on_start_clicked()
    assert fake_service.calls.count("start") == 1
    assert len(fake_service.start_options) == 1
    assert fake_service.lifecycle is RunLifecycle.STARTING
    assert app.start_pending is True

    app.poll_once()
    assert "启动中" in app.lifecycle_label.cget("text")
    assert button_state(app.buttons["start"]) == "disabled"
    assert button_state(app.buttons["pause"]) == "disabled"
    assert button_state(app.buttons["stop"]) == "normal"


def test_start_returns_to_stopped_releases_debounce(app, fake_service) -> None:
    app.on_start_clicked()
    fake_service.set_lifecycle(RunLifecycle.STOPPED)
    app.poll_once()
    assert app.start_pending is False
    assert app.buttons["start"].cget("text") == "启动"
    assert button_state(app.buttons["start"]) == "normal"


def test_start_rejected_shows_reason_and_reenables(app, fake_service) -> None:
    calls: list[object] = []

    def rejecting_start(options):
        calls.append(options)
        return StartResult(accepted=False, reason="already_active")

    fake_service.start = rejecting_start  # type: ignore[method-assign]
    app.on_start_clicked()
    assert len(calls) == 1
    assert app.start_pending is False
    assert "启动被拒绝" in app.banner.text
    assert "already_active" in app.banner.text
    app.on_start_clicked()
    assert len(calls) == 2


def test_pause_resume_buttons_dispatch(app, fake_service) -> None:
    fake_service.set_lifecycle(RunLifecycle.RUNNING)
    app.poll_once()
    assert button_state(app.buttons["pause"]) == "normal"
    app.on_pause_clicked()
    assert "pause" in fake_service.calls
    fake_service.set_lifecycle(RunLifecycle.PAUSED)
    app.poll_once()
    assert button_state(app.buttons["resume"]) == "normal"
    app.on_resume_clicked()
    assert "resume" in fake_service.calls


def test_stop_flow_stopping_then_stopped(app, fake_service) -> None:
    fake_service.set_lifecycle(RunLifecycle.RUNNING)
    app.poll_once()
    app.on_stop_clicked()
    assert "stop" in fake_service.calls
    app.poll_once()
    assert "停止中" in app.lifecycle_label.cget("text")
    assert button_state(app.buttons["stop"]) == "disabled"
    assert button_state(app.buttons["start"]) == "disabled"

    fake_service.complete_stop()
    app.poll_once()
    assert "已停止" in app.lifecycle_label.cget("text")
    assert button_state(app.buttons["start"]) == "normal"


# ---------------------------------------------------------------------------
# 错误与关闭流程
# ---------------------------------------------------------------------------


def test_runtime_error_disables_start_and_shows_cleanup(app, fake_service) -> None:
    fake_service.set_error(cleanup_pending=("输入线程未退出", "看门狗未停止"))
    app.poll_once()
    assert "清理未完成" in app.lifecycle_label.cget("text")
    assert button_state(app.buttons["start"]) == "disabled"
    assert app.banner.visible
    assert "清理未完成" in app.banner.text
    assert "禁止启动" in app.banner.text
    assert "taskkill /PID <pid> /T /F" in CLEANUP_ERROR_BANNER
    # 不提供强杀线程按钮
    assert "强杀" not in app.banner.text


def test_close_when_stopped_destroys_directly(app, fake_service) -> None:
    app.on_close()
    assert app.destroyed is True
    assert fake_service.shutdown_called is False


def test_close_running_is_async_and_waits_for_cleanup(app, fake_service) -> None:
    fake_service.set_lifecycle(RunLifecycle.RUNNING)
    app.poll_once()
    app.on_close()
    assert app.destroyed is False
    assert app.closing is True
    assert fake_service.shutdown_called is True
    assert CLOSING_STATUS in app.banner.text
    app.root.update()  # 关闭过程中窗口仍可响应（不阻塞主线程）
    assert app.destroyed is False

    fake_service.complete_stop()
    app.poll_once()
    assert app.destroyed is True


def test_close_error_shows_cleanup_panel_and_exit_app(app, fake_service) -> None:
    fake_service.set_lifecycle(RunLifecycle.RUNNING)
    app.poll_once()
    app.on_close()
    fake_service.set_error(cleanup_pending=("输入线程未退出", "看门狗未停止"))
    app.poll_once()
    assert app.destroyed is False
    assert app.closing_error is True
    assert app.cleanup_banner.visible
    assert "清理未完成" in app.cleanup_banner.text
    assert "输入线程未退出" in app.cleanup_banner.text
    assert set(app.cleanup_banner.button_labels) == {"继续等待清理", "退出应用"}
    assert button_state(app.buttons["start"]) == "disabled"

    app.exit_anyway()
    assert app.destroyed is True
    assert any("taskkill" in row.message for row in app.log_page.model.rows)


# ---------------------------------------------------------------------------
# 全屏模式
# ---------------------------------------------------------------------------


def test_fullscreen_toggle_calls_root_attributes(app, tk_root) -> None:
    calls: list[tuple[object, ...]] = []
    tk_root.attributes = lambda *args: calls.append(args)  # type: ignore[method-assign]

    app.set_fullscreen(True)
    assert calls == [("-fullscreen", True)]
    assert app.fullscreen is True
    assert app.settings_page.fullscreen_var.get() is True

    app.set_fullscreen(False)
    assert calls[-1] == ("-fullscreen", False)
    assert app.fullscreen is False
    assert app.settings_page.fullscreen_var.get() is False


def test_fullscreen_checkbox_command_updates_root(app, tk_root) -> None:
    calls: list[tuple[object, ...]] = []
    tk_root.attributes = lambda *args: calls.append(args)  # type: ignore[method-assign]
    app.set_page("settings")
    assert app.settings_page.fullscreen_hint.cget("text") == FULLSCREEN_HINT
    app.settings_page.fullscreen_var.set(True)
    app.settings_page._on_fullscreen_clicked()
    assert calls[-1] == ("-fullscreen", True)
    app.settings_page.fullscreen_var.set(False)
    app.settings_page._on_fullscreen_clicked()
    assert calls[-1] == ("-fullscreen", False)
    # 纯内存态：不改变托管状态
    assert "已停止" in app.lifecycle_label.cget("text")


# ---------------------------------------------------------------------------
# 日志与性能展示
# ---------------------------------------------------------------------------


def test_events_flow_to_log_and_recent(app, fake_service) -> None:
    from src.runtime.messages import RuntimeEventKind

    fake_service.push_event(RuntimeEventKind.LOG_RECORD, level="INFO", message="hello battle", battle_id=12)
    fake_service.push_event(RuntimeEventKind.LOG_RECORD, level="ERROR", message="input failed")
    app.poll_once()
    text = app.log_page.text.get("1.0", "end")
    assert "hello battle" in text
    assert "input failed" in text
    assert len(app._recent.rows) == 2
    assert app.log_page.model.rows[-1].level == "ERROR"

    fake_service.set_dropped_events(3)
    app.poll_once()
    assert "丢弃 3 条" in app.log_page.seq_label.cget("text")


def test_log_seq_gap_is_reported(app, fake_service) -> None:
    from src.runtime.messages import RuntimeEventKind

    fake_service.push_event(RuntimeEventKind.LOG_RECORD, message="first")
    app.poll_once()
    gap_event = fake_service.push_event(RuntimeEventKind.LOG_RECORD, message="after gap")
    # 人为制造序号缺口（服务端丢弃）
    object.__setattr__(gap_event, "seq", 99)
    app.poll_once()
    assert "缺失" in app.log_page.seq_label.cget("text")


def test_log_view_bounded_to_2000_lines(app) -> None:
    from src.runtime.messages import RuntimeEvent, RuntimeEventKind

    events = [
        RuntimeEvent(
            seq=index,
            at=float(index),
            wall_at=WALL,
            kind=RuntimeEventKind.LOG_RECORD,
            level="INFO",
            message=f"line {index}",
        )
        for index in range(1, 2101)
    ]
    app.log_page.append_events(events)
    assert len(app.log_page.model) == 2000
    assert app.log_page.model.trimmed_total == 100
    rendered_lines = int(app.log_page.text.index("end-1c").split(".")[0]) - 1
    assert rendered_lines == 2000


def test_performance_monitor_display(app, fake_service) -> None:
    perf = PerformanceView(enabled=True, mode="monitor", sampled_wall=WALL)
    fake_service.set_performance(perf)
    app.poll_once()
    app.set_page("performance")
    assert "监测中（不自动停止）" in app.performance_page.threshold_label.cget("text")
    assert "已启用" not in app.performance_page.threshold_label.cget("text")
    values = app.performance_summary.rows_grid.values()
    assert values["cpu"] == "—"
    assert values["mem_available"] == "—"
    assert performance_rows(perf)  # 与非 Tk 视图一致


def test_performance_unavailable_reason(app, fake_service) -> None:
    fake_service.set_performance(
        PerformanceView(enabled=False, mode="off", unavailable_reason="psapi 不可用")
    )
    app.poll_once()
    values = app.performance_summary.rows_grid.values()
    assert "不可用（psapi 不可用）" in values["unavailable"]
    assert values["cpu"] == "—"


# ---------------------------------------------------------------------------
# 生效值 / 能力开关
# ---------------------------------------------------------------------------


def test_effective_plan_and_capabilities_rendered(app, fake_service, tmp_path: Path) -> None:
    fake_service.set_plan(sample_plan(tmp_path))
    fake_service.set_capabilities(CapabilityFlags(input_gate=True))
    app.poll_once()
    plan_values = app.run_page.plan_grid.values()
    assert plan_values["cap_safety"] == "20 场"
    assert plan_values["cap_battle"] == "—"
    assert "30 场" in plan_values["cap_daily"]
    capability_values = app.settings_page.capability_grid.values()
    assert capability_values["input_gate"] == "✓ 已接入"
    assert "✗ 未接入" in capability_values["single_instance"]
    assert app.sidebar.footer_label.cget("text") == "国服 360\n铁公爵"


def test_effective_plan_none_all_dash(app) -> None:
    values = app.run_page.plan_grid.values()
    assert values["cap_battle"] == "—"
    assert values["cap_safety"] == "—"
    assert values["cap_daily"] == "—"


# ---------------------------------------------------------------------------
# 设置页工具表与全屏地图标定说明
# ---------------------------------------------------------------------------


def test_tool_buttons_and_fullmap_note(app) -> None:
    settings = app.settings_page
    assert set(settings.tool_buttons) == {
        "calibrate_ship",
        "calibrate_profile",
        "capture_marker",
        "check_env",
        "validate",
        "stats",
    }
    assert "待实机验证" in settings.fullmap_status.cget("text")
    settings.show_fullmap_note()
    note = settings.fullmap_note_label.cget("text")
    assert "navigation.full_map_rect" in note
    assert "不承诺" in note


def test_stop_and_f12_copy_present(app) -> None:
    assert "F12 = 紧急停止" in app.run_page.f12_label.cget("text")
    assert "不会替你退出战斗" in app.run_page.stop_label.cget("text")


def test_load_service_missing_module_reports_chinese_error(monkeypatch, tk_root, capsys) -> None:
    """M0 尚未就绪时：返回 None + 中文错误，而不是崩溃（2.3/2.6）。"""

    import builtins

    from src.gui import main as gui_main

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "src.runtime.service":
            raise ImportError("模拟 M0 未完成")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(gui_main, "_show_error_dialog", lambda root, message: None)
    assert gui_main._load_service(tk_root, Path.cwd()) is None
    captured = capsys.readouterr()
    assert "运行服务尚未就绪" in captured.err


# ---------------------------------------------------------------------------
# M2：启动配置表单 / 校验配置 / 船卡导入门禁
# ---------------------------------------------------------------------------


def test_validate_config_shows_errors_then_plan(app, fake_service, tmp_path: Path) -> None:
    error = ValidationIssueView("error", "ship_id_missing", "config.yaml:ship>ship_id", "找不到船只档案")
    warning = ValidationIssueView("warning", "file_missing", "config.yaml:logging", "日志目录缺失")
    fake_service.set_plan_preview(PlanResult(ok=False, issues=(error,), warnings=(warning,)))
    app.validate_config()
    assert app.run_page.validation_summary_label.cget("text") == "配置校验：✗ 1 error / 1 warning"
    validation_text = app.run_page.validation_text.get("1.0", "end")
    assert "config.yaml:ship>ship_id" in validation_text
    assert "找不到船只档案" in validation_text
    assert fake_service.calls.count("plan_preview") == 1

    fake_service.set_plan_preview(PlanResult(ok=True, plan=sample_plan(tmp_path), warnings=(warning,)))
    app.validate_config()
    assert app.run_page.validation_summary_label.cget("text") == "配置校验：✔ 0 error / 1 warning"
    assert app.run_page.plan_grid.values()["cap_safety"] == "20 场"
    assert app.preview_plan is not None
    assert app.sidebar.footer_label.cget("text") == "国服 360\n铁公爵"


def test_start_options_use_form_fields(app, fake_service) -> None:
    app.run_page.ship_name_var.set("铁公爵")
    app.run_page._apply_ship_match()
    app.run_page.max_battles_var.set("3")
    app.run_page.start_paused_var.set(True)
    app.run_page.log_level_var.set("INFO")
    app.run_page.rotate_var.set(True)
    app.run_page.account_combo.set("小号一号（alt1）")
    app.on_start_clicked()
    assert fake_service.calls.count("start") == 1
    options = fake_service.start_options[0]
    assert options.profile_id == "cn360"
    assert options.ship_id == "iron_duke"
    assert options.ship_name == "铁公爵"
    assert options.account_alias == "alt1"
    assert options.rotate is True
    assert options.max_battles == 3
    assert options.log_level == "INFO"
    assert options.start_paused is True


def test_invalid_max_battles_blocks_start(app, fake_service) -> None:
    app.run_page.max_battles_var.set("abc")
    app.on_start_clicked()
    assert fake_service.calls.count("start") == 0
    assert app.start_pending is False
    assert "场次上限" in app.banner.text


@pytest.mark.parametrize("profile_id", ["wg_asia", "wg_eu", "wg_na"])
def test_production_direct_server_selection_reaches_start_and_tools(app, fake_service, profile_id):
    from src.gui.app import load_gui_catalog

    app.project_root = Path(__file__).resolve().parents[1]
    app.catalog = load_gui_catalog(app.project_root)
    app._apply_catalog()
    page = app.run_page
    assert page.profile_combo.winfo_manager() == "pack"
    option = next(item for item in app.catalog.profiles if item.profile_id == profile_id)
    page.profile_combo.set(option.label)
    page._on_profile_selected()
    assert page.selected_profile_id() == profile_id
    assert {ship.ship_id for ship in app.catalog.ships} >= {"iron_duke", "leningrad"}
    page.ship_name_var.set("铁公爵")
    page._apply_ship_match()
    app.on_start_clicked()
    assert fake_service.start_options[-1].profile_id == profile_id
    assert fake_service.start_options[-1].ship_id == "iron_duke"
    assert app._tool_context()[:2] == (profile_id, "iron_duke")


def test_non_stopped_disables_form_and_import(app, fake_service) -> None:
    fake_service.set_lifecycle(RunLifecycle.RUNNING)
    app.poll_once()
    assert button_state(app.run_page.ship_name_entry) == "disabled"
    assert button_state(app.run_page.profile_combo) == "disabled"
    assert button_state(app.run_page.account_combo) == "disabled"
    assert button_state(app.run_page.max_battles_spin) == "disabled"
    assert button_state(app.run_page.import_button) == "disabled"
    assert button_state(app.settings_page.import_ship_button) == "disabled"
    for button in app.settings_page.tool_buttons.values():
        assert button_state(button) == "disabled"
    assert app.settings_page.start_tool("validate") is False
    assert "请先停止脚本" in app.settings_page.tool_result_label.cget("text")


def test_ship_import_request_gated_by_lifecycle(app, fake_service) -> None:
    opened: list[bool] = []
    app.run_page._on_import_ship = lambda: opened.append(True)  # type: ignore[method-assign]
    fake_service.set_lifecycle(RunLifecycle.RUNNING)
    app.poll_once()
    app.run_page.request_import()
    assert opened == []
    fake_service.set_lifecycle(RunLifecycle.STOPPED)
    app.poll_once()
    app.run_page.request_import()
    assert opened == [True]


# ---------------------------------------------------------------------------
# M3：WAIT_ACCOUNT 等待面板
# ---------------------------------------------------------------------------


def _has_entry_widget(widget) -> bool:
    from tkinter import ttk

    for child in widget.winfo_children():
        if isinstance(child, (tk.Entry, ttk.Entry)):
            return True
        if _has_entry_widget(child):
            return True
    return False


def test_wait_account_panel_confirm_and_abort(app, fake_service) -> None:
    fake_service.set_wait_account("alt1", "alt2", battles_completed=6)
    app.poll_once()
    assert app.run_page.wait_panel.winfo_manager() == "pack"
    headline = app.run_page.rotation_headline.cget("text")
    assert "alt1" in headline and "alt2" in headline and "6 场" in headline
    # 等待面板不提供任何凭据输入框（10.1/13.4）
    assert not _has_entry_widget(app.run_page.wait_panel)

    fake_service.confirm_result = False
    app.on_confirm_account_switch()
    assert fake_service.calls.count("confirm_account_switch") == 1
    assert "预检失败" in app.run_page.rotation_status_label.cget("text")
    assert "游戏窗口不在前台" in app.run_page.rotation_status_label.cget("text")

    fake_service.confirm_result = True
    app.on_confirm_account_switch()
    assert fake_service.lifecycle is RunLifecycle.STARTING
    assert app.run_page.rotation_status == ROTATION_CONFIRM_OK


def test_wait_account_stop_button_ends_rotation(app, fake_service) -> None:
    fake_service.set_wait_account("alt1", "alt2")
    app.poll_once()
    app.on_stop_clicked()  # WAIT_ACCOUNT 下“停止”= 结束轮换（abort_rotation）
    assert "abort_rotation" in fake_service.calls
    assert "stop" not in fake_service.calls
    assert fake_service.lifecycle is RunLifecycle.STOPPED
    assert app.run_page.rotation_status == ROTATION_ABORTED
    app.poll_once()
    assert app.run_page.wait_panel.winfo_manager() == ""


# ---------------------------------------------------------------------------
# M2：标定工具子进程（假 Popen，不启动真实工具）
# ---------------------------------------------------------------------------


def test_tool_subprocess_gates_start_and_auto_validates(app, fake_service, fake_popen) -> None:
    fake_popen.queue([None, 0])  # 工具：运行中 → 退出码 0
    fake_popen.queue(
        [0],
        stdout=json.dumps(
            {
                "errors": [],
                "warnings": [{"code": "w", "location": "config.yaml", "message": "测试警告"}],
            }
        ),
    )
    assert app.settings_page.start_tool("validate") is True
    assert "运行中" in app.settings_page.tool_status_labels["validate"].cget("text")
    assert app.tool_running is True
    assert button_state(app.buttons["start"]) == "disabled"

    app.settings_page.poll_once()  # 工具第一次轮询：仍在运行
    assert app.tool_running is True
    app.settings_page.poll_once()  # 工具退出码 0 → 自动启动 validate
    first_command = fake_popen.commands[0][0]
    assert first_command[:4] == (sys.executable, "-m", "src.tools.validate", "--root")
    first_kwargs = fake_popen.commands[0][1]
    assert first_kwargs["cwd"] == str(Path.cwd())
    assert first_kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    assert app.settings_page.POLL_INTERVAL_MS == 1000
    assert len(fake_popen.commands) == 2
    assert "--json" in fake_popen.commands[1][0]
    assert "--profile" in fake_popen.commands[1][0]

    app.settings_page.poll_once()  # validate 退出码 0 → 解析 JSON
    assert "0 error / 1 warning" in app.settings_page.validate_summary.cget("text")
    assert app.tool_running is False
    assert button_state(app.buttons["start"]) == "normal"
    assert app.validation is not None and len(app.validation.warnings) == 1


def test_tool_failure_shows_exit_code_and_hint(app, fake_popen) -> None:
    fake_popen.queue([1])
    fake_popen.queue([0], stdout=json.dumps({"errors": [], "warnings": []}))
    assert app.settings_page.start_tool("calibrate_profile") is True
    app.settings_page.poll_once()
    status = app.settings_page.tool_status_labels["calibrate_profile"].cget("text")
    assert "退出码 1" in status
    result_text = app.settings_page.tool_result_label.cget("text")
    assert "终端" in result_text or "日志" in result_text
    app.settings_page.poll_once()  # 自动 validate 退出
    assert app.tool_running is False


def test_second_tool_refused_while_running(app, fake_popen) -> None:
    fake_popen.queue([None, 0])
    fake_popen.queue([0], stdout=json.dumps({"errors": [], "warnings": []}))
    assert app.settings_page.start_tool("validate") is True
    assert app.settings_page.start_tool("stats") is False
    assert "同一时间" in app.settings_page.tool_result_label.cget("text")
    app.settings_page.poll_once()  # 工具仍在运行
    app.settings_page.poll_once()  # 工具退出 → 自动 validate
    app.settings_page.poll_once()  # validate 退出
    assert app.tool_running is False


def test_close_blocked_while_tool_running(app, fake_popen) -> None:
    fake_popen.queue([None, 0])
    fake_popen.queue([0], stdout=json.dumps({"errors": [], "warnings": []}))
    assert app.settings_page.start_tool("validate") is True
    app.on_close()
    assert app.destroyed is False
    assert "标定工具仍在运行" in app.banner.text
    # 工具退出并完成自动校验后，再次关闭走正常 STOPPED 直接关闭路径
    app.settings_page.poll_once()
    app.settings_page.poll_once()
    app.settings_page.poll_once()
    assert app.tool_running is False
    app.on_close()
    assert app.destroyed is True


@pytest.mark.parametrize("size", ["1280x820", "1024x720"])
def test_data_layout_keeps_square_map_and_stop_accessible(app, tk_root, size):
    tk_root.geometry(size)
    tk_root.deiconify()
    tk_root.update()
    page = app.data_page
    assert page.canvas.winfo_width() == page.canvas.winfo_height()
    assert page.canvas.winfo_width() >= 280
    stop = app.buttons["stop"]
    assert stop.winfo_rootx() + stop.winfo_width() <= tk_root.winfo_rootx() + tk_root.winfo_width()
    # 记录仍在页面中；较矮窗口通过页面滚动访问，不压缩地图。
    page.scroll.canvas.yview_moveto(1)
    tk_root.update()
    assert page.history_table.winfo_rooty() < page.scroll.canvas.winfo_rooty() + page.scroll.canvas.winfo_height()


def test_map_expands_with_window(app, tk_root):
    tk_root.geometry("1024x720")
    tk_root.deiconify()
    tk_root.update()
    small = app.data_page.canvas.winfo_width()
    tk_root.geometry("1600x1000")
    tk_root.update()
    assert app.data_page.canvas.winfo_width() > small


def test_single_catalogs_use_text_and_visible_ship_choice_resolves_duplicate_names(app):
    page = app.run_page
    ships = (ShipCandidate("first", "同名船"), ShipCandidate("second", "同名船"))
    page.set_catalogs((ProfileOption("cn360", "国服 360"),), (), rotation_available=False, ships=ships)
    assert not page.profile_combo.winfo_manager()
    assert not page.account_combo.winfo_manager()
    assert not page.ship_candidate_combo.winfo_manager()
    page.ship_name_entry.set(ships[1].label)
    page._on_name_selected()
    page._apply_ship_match()  # 延迟匹配也必须保留明确选择。
    assert page.ship_name() == "同名船"
    assert page.selected_ship_id() == "second"


def test_production_minimap_crop_reaches_service_and_tk(app, tk_root, tmp_path):
    import cv2
    import numpy as np
    from src.common.types import Rect, State
    from src.config.loader import load_project
    from src.perception.capture import Frame
    from src.perception.engine import PerceptionEngine
    from src.runtime.runner import SessionRunner
    from src.runtime.service import RuntimeService

    from tests.test_runtime_service import _fake_runtime

    config = load_project(Path.cwd()).config
    engine = PerceptionEngine(config, capture=None)
    pixels = np.zeros((1600, 2560, 3), dtype=np.uint8)
    pixels[:, :, 0] = np.arange(2560, dtype=np.uint16) % 255
    pixels[:, :, 1] = np.arange(1600, dtype=np.uint16)[:, None] % 255
    engine._last_frame = Frame(pixels, 1, Rect(0, 0, 2560, 1600), frame_id=51)
    runtime, _, _ = _fake_runtime()
    runtime.clock.advance(1)
    runtime.engine = engine
    runtime.machine.state = State.BATTLE_ACTIVE
    runtime.autopilot.blocks_behavior = False
    runner = SessionRunner(runtime)
    service = RuntimeService(tmp_path, clock=runtime.clock)
    try:
        service._runner = runner
        service._lifecycle = RunLifecycle.RUNNING
        with service._lock:
            service._rebuild_snapshot_locked()
        snapshot = service.snapshot()
        assert snapshot.minimap is not None
        rect = engine.minimap_content_rect()
        actual = cv2.imdecode(np.frombuffer(snapshot.minimap.png, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert np.array_equal(actual, pixels[rect.y:rect.bottom, rect.x:rect.right])
        app.data_page.render(snapshot, None)
        tk_root.deiconify()
        tk_root.update()
        assert app.data_page._photo is not None
        assert app.data_page.can_choose_target
    finally:
        service._runner = None
        service.shutdown()


def test_map_direct_click_accepts_old_same_battle_frame_and_pause_disables_edit(app, fake_service, tk_root):
    import io

    from PIL import Image
    from src.runtime.messages import MinimapView, RunCommandKind

    stream = io.BytesIO()
    Image.new("RGB", (200, 200), "#304c44").save(stream, format="PNG")
    fake_service.set_lifecycle(RunLifecycle.RUNNING)
    fake_service.set_game(state="BATTLE_ACTIVE", battle_active=True, battle_id=7)
    base = fake_service.snapshot()
    fake_service._snapshot = replace(base, at=10,
        minimap=MinimapView(1, 9, 7, (0, 0, 2560, 1600), stream.getvalue(), 200, 200),
        navigation=replace(base.navigation, mode="autopilot"))
    app.poll_once()
    tk_root.deiconify()
    tk_root.update()
    page = app.data_page
    assert page.can_choose_target
    left, top, width, height = page._bounds
    outside_x = left - 1 if left > 0 else left + width + 1
    page.canvas.event_generate("<Button-1>", x=outside_x, y=top + height // 2)
    tk_root.update()
    assert "无法选点" in page.feedback.cget("text")
    assert "不在小地图内容区" in page.feedback.cget("text")
    periodic = replace(fake_service.snapshot(), at=11,
                       navigation=replace(fake_service.snapshot().navigation, target_status=""))
    page.render(periodic, None)
    assert "不在小地图内容区" in page.feedback.cget("text")
    page.canvas.event_generate("<Button-1>", x=left + width // 2, y=top + height // 4)
    tk_root.update()
    assert len(fake_service.target_commands) == 1
    command = fake_service.target_commands[0]
    assert command.kind is RunCommandKind.REPLACE_TARGET
    assert command.target == pytest.approx((0.5, 0.25), abs=0.01)
    assert command.battle_id == 7 and command.frame_id == 1
    assert "pause" not in fake_service.calls
    fake_service._snapshot = replace(fake_service.snapshot(), at=16)
    app.poll_once()
    assert page.can_choose_target
    page.canvas.event_generate("<Button-1>", x=left + width // 4, y=top + height // 2)
    assert len(fake_service.target_commands) == 2
    fake_service.set_lifecycle(RunLifecycle.PAUSED)
    app.poll_once()
    assert not page.can_choose_target
    assert button_state(page.edit_button) == "disabled"
    page.canvas.event_generate("<Button-1>", x=150, y=150)
    assert len(fake_service.target_commands) == 2
    assert "无法选点" in page.feedback.cget("text")
    periodic = replace(fake_service.snapshot(), at=17,
                       navigation=replace(fake_service.snapshot().navigation, target_status=""))
    page.render(periodic, None)
    assert "无法选点" in page.feedback.cget("text")


def test_window_focus_pause_allows_same_battle_click_but_manual_pause_does_not(
    app, fake_service, tk_root
):
    import io

    from PIL import Image
    from src.runtime.messages import MinimapView

    stream = io.BytesIO()
    Image.new("RGB", (200, 200), "#304c44").save(stream, format="PNG")
    fake_service.set_lifecycle(RunLifecycle.PAUSED)
    fake_service.set_game(
        state="PAUSED", battle_active=False, battle_id=7, pause_reason="window_unfocused"
    )
    base = fake_service.snapshot()
    fake_service._snapshot = replace(
        base,
        at=10,
        minimap=MinimapView(1, 9, 7, (0, 0, 2560, 1600), stream.getvalue(), 200, 200),
        navigation=replace(base.navigation, mode="autopilot"),
    )
    app.poll_once()
    tk_root.deiconify()
    tk_root.update()
    page = app.data_page
    assert page.can_choose_target
    left, top, width, height = page._bounds
    page.canvas.event_generate("<Button-1>", x=left + width // 2, y=top + height // 2)
    assert len(fake_service.target_commands) == 1

    fake_service.set_lifecycle(RunLifecycle.PAUSED)
    fake_service.set_game(
        state="PAUSED", battle_active=False, battle_id=7, pause_reason="manual"
    )
    app.poll_once()
    assert not page.can_choose_target
    assert button_state(page.edit_button) == "disabled"


def test_cpu_chart_leaves_gaps_and_expires_missing_samples(app):
    chart = app.performance_page.cpu_chart
    chart.update_sample(100, 20)
    chart.update_sample(101, None)
    assert list(chart.samples) == [(100, 20)]
    chart.update_sample(105, 70)
    # 两点间缺失超过三秒，只保留坐标网格，不连接成连续采样曲线。
    assert len([item for item in chart.find_all() if chart.type(item) == "line"]) == 3
    chart.update_sample(166, None)
    assert not chart.samples


def test_old_map_click_in_tk_reaches_runner_and_full_map_coordinates(app, fake_service, tk_root):
    from src.common.types import ActionKind, Observation

    from tests.test_gui_data import map_runner

    runner, runtime, frame = map_runner()
    runtime.clock.advance(60)
    fake_service.set_lifecycle(RunLifecycle.RUNNING)
    fake_service.set_game(state="BATTLE_ACTIVE", battle_active=True, battle_id=frame.battle_id)
    snapshot = fake_service.snapshot()
    fake_service._snapshot = replace(snapshot, at=runtime.clock.now(), minimap=frame,
                                     navigation=replace(snapshot.navigation, mode="autopilot"))
    fake_service.submit_command = runner.replace_target
    app.poll_once()
    tk_root.deiconify()
    tk_root.update()
    left, top, width, height = app.data_page._bounds
    app.data_page.canvas.event_generate("<Button-1>", x=left+width//3, y=top+height//2)
    assert runner._pending_target is not None
    now = runtime.clock.now()+1
    observation = Observation(timestamp=now, window_rect=frame.window_rect, frame_timestamp=now)
    runner._apply_pending_target(observation, now)
    nav = runtime.autopilot
    actions = []
    for offset in (0, 0.1, 1.2, 1.3, 1.9):
        actions.extend(nav.tick(replace(observation, timestamp=now+offset, frame_timestamp=now+offset), now+offset))
    assert [action.key for action in actions if action.key] == ["m", "m", "m", "m"]
    click = next(action for action in actions if action.kind is ActionKind.CLICK_ABS)
    target = nav.compute_full_map_target(observation)
    assert target.normalized == pytest.approx((1/3, 1/2), abs=0.01)
    assert (click.x, click.y) == target.screen_px
