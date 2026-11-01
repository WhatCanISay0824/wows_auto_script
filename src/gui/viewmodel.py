"""GUI 视图模型：纯函数与纯数据结构。

本模块**不导入 tkinter、不访问运行服务、不做 IO**，因此可以在无窗口环境
下被 ``tests/test_gui_viewmodel.py`` 直接断言。所有界面中文文案、按钮可用性
矩阵、状态区四组格式化、性能面板取值与缺失值口径都集中在这里，避免散落到
控件代码里各写一份。

口径（GUI 界面设计文档）：

- 缺失值（``None``）一律显示“—”，不得显示 0 冒充有效值（7.5 D 组、6.1）；
- 生命周期与游戏流程分两行展示，不混用（7.5 A/B 组）；
- 停止不等于离开战斗（11.4、附录 B7）；
- 过载保护在 monitor 模式下只显示“监测中（不自动停止）”，能力未接入时
  不得显示“已启用”（15.2、附录 D）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from src.runtime.messages import (
    CapabilityFlags,
    EffectivePlan,
    PerformanceThresholds,
    PerformanceView,
    PlanResult,
    RunLifecycle,
    RunSnapshot,
    RuntimeEvent,
    RuntimeEventKind,
    ValidationIssueView,
)

# ---------------------------------------------------------------------------
# 常量文案（审核用；测试逐条断言）
# ---------------------------------------------------------------------------

F12_NOTICE = "F12 = 紧急停止（在游戏中按键，停止后不能像开关一样恢复）"
STOP_NOTICE = "停止只停止脚本控制并释放键鼠，不会替你退出战斗。"
STOP_BATTLE_WARNING = "脚本已停止控制，游戏仍在战斗中，未替你退出"
CLOSING_STATUS = "正在停止脚本并释放输入，请稍候……（不会强杀线程）"
FULLSCREEN_HINT = "全屏模式只改变窗口显示，不改变托管状态"
LOG_FILE_NOTICE = "界面最多保留 2000 条；完整日志由服务写入本地日志文件，GUI 只展示"
NO_PERFORMANCE_TEXT = "不可用（服务未提供性能数据）"
CLEANUP_ERROR_TITLE = "清理未完成"
CLEANUP_ERROR_BANNER = (
    "清理未完成：{items}；禁止启动。可打开日志定位，必要时用 taskkill /PID <pid> /T /F 结束进程树。"
)
CLEANUP_ERROR_HINT = "清理未完成前不可启动；可继续等待清理，或退出应用（不保证键鼠已释放）。"

LIFECYCLE_TEXT: dict[RunLifecycle, str] = {
    RunLifecycle.STOPPED: "已停止",
    RunLifecycle.STARTING: "启动中",
    RunLifecycle.RUNNING: "运行中",
    RunLifecycle.PAUSING: "暂停中",
    RunLifecycle.PAUSED: "已暂停",
    RunLifecycle.STOPPING: "停止中",
    RunLifecycle.WAIT_ACCOUNT: "等待手动切号",
    RunLifecycle.ERROR: "清理未完成",
}

# 颜色只用于状态与真实异常（GUI交互与预览设计 §3）。
LIFECYCLE_COLOR: dict[RunLifecycle, str] = {
    RunLifecycle.STOPPED: "#6b7280",
    RunLifecycle.STARTING: "#2563eb",
    RunLifecycle.RUNNING: "#15803d",
    RunLifecycle.PAUSING: "#b45309",
    RunLifecycle.PAUSED: "#b45309",
    RunLifecycle.STOPPING: "#2563eb",
    RunLifecycle.WAIT_ACCOUNT: "#a16207",
    RunLifecycle.ERROR: "#b91c1c",
}

GAME_STATE_TEXT: dict[str, str] = {
    "INIT": "初始化识别",
    "PORT": "港口",
    "START_COOP": "开始联合作战",
    "QUEUEING": "排队中",
    "LOADING": "加载中",
    "BATTLE_ACTIVE": "战斗中",
    "BATTLE_FINISHED": "战斗结束",
    "DEAD_PENDING_EXIT": "死亡待退出",
    "EXIT_MENU_OPEN": "退出菜单已打开",
    "EXIT_CONFIRMING": "退出确认中",
    "EXITING": "退出中",
    "POST_EXIT_INTERFACE": "战后界面",
    "ERROR_RECOVERY": "异常恢复中",
    "PAUSED": "已暂停",
    "STOPPING": "停止中",
    "UNKNOWN": "未知",
}

# 暂停原因中文映射（GUI 界面设计文档 7.5，来自现有 request_pause 的 reason 字符串）。
PAUSE_REASON_TEXT: dict[str, str] = {
    "window_lost": "找不到游戏窗口",
    "window_unfocused": "游戏窗口不在前台（失焦暂停）",
    "window_unavailable": "游戏窗口不可用（最小化/尺寸异常）",
    "restart_mid_battle": "检测到中途战斗：新进程不接管已有战斗（等待结算/回港）",
    "loadout_check_failed": "装备页信号旗校验失败（信号旗未清零/装备页不可达）",
    "ship_check_failed": "船只校验失败（未找到/未选中目标船）",
    "recovery_frequency": "异常恢复过于频繁（10 分钟超限）",
    "recovery_mid_battle": "异常恢复时检测到战斗中画面",
    "recovery_failed": "连续 3 次异常恢复失败",
    "heartbeat_timeout": "主循环心跳超时",
    "input_failed": "输入后端连续失败",
    "gui_pause": "用户在控制台点击暂停",
    "gui_start_paused": "用户选择“启动时先暂停”",
    "overload_pause": "电脑负载过高（保护性暂停，可恢复）",
    "emergency_stop": "F12 紧急停止（停止而非暂停）",
}

PERFORMANCE_MODE_TEXT: dict[str, str] = {
    "off": "关闭",
    "monitor": "监测",
    "pause_stop": "暂停 + 停止",
    "stop": "停止",
}

OVERLOAD_STATE_TEXT: dict[str, str] = {
    "normal": "正常",
    "warning": "告警",
    "pause_pending": "即将暂停",
    "paused": "已暂停（过载保护）",
    "stop_pending": "即将停止",
    "stopped": "已停止（过载保护）",
}

OVERLOAD_STATE_EMPHASIS: dict[str, str] = {
    "normal": "normal",
    "warning": "warning",
    "pause_pending": "warning",
    "paused": "warning",
    "stop_pending": "error",
    "stopped": "error",
}

CAPABILITY_TEXT: dict[str, tuple[str, str]] = {
    "input_gate": ("输入门禁（ISS-004/005）", "不承诺“停止 = 输入已停止”"),
    "release_confirmed": ("输入释放确认", "不承诺键鼠已释放"),
    "single_instance": ("单实例（ISS-003）", "命令行与 GUI 互斥不承诺"),
    "cleanup_on_error": ("异常清理（ISS-008）", "异常后可能无法安全重启"),
    "pause_keeps_battle": ("暂停保留战斗上下文", "暂停/恢复不承诺保留本场"),
    "battle_context": ("场次上下文（ISS-010）", "本场事件可能不完整"),
    "exit_authorized_gate": ("退出授权门禁（ISS-002）", "不承诺存活时不退出"),
    "autopilot_full_map": ("全屏地图自动驾驶（ISS-001）", "不承诺自动航行"),
    "autopilot_move_verified": ("航行验证", "不显示“航行已确认”"),
    "ship_selected_evidence": ("选中确认（ISS-007）", "不承诺不会选错船"),
    "input_failure_pause": ("输入失败暂停（ISS-009）", "输入失败后可能仍显示运行中"),
    "performance_monitor": ("性能监测", "不承诺性能数据可用"),
    "overload_protection": ("过载保护", "不承诺过载时自动停止"),
}

TOOL_PLACEHOLDERS: tuple[tuple[str, str, str], ...] = (
    ("船只卡片标定", "框选船卡/空槽，写 ships/<id>/ship.yaml", "M2 再实现"),
    ("界面区域标定", "界面模板/区域，写 profile.yaml", "M2 再实现"),
    ("自身标记标定", "小地图自身标记模板，写 marker/", "M2 再实现"),
    ("环境检查", "窗口/截屏/DPI/输入自检（部分需游戏）", "M2 再实现"),
    ("配置校验", "只读校验（JSON 输出）", "M2 再实现"),
    ("统计工具", "只读数据库统计", "M2 再实现"),
)

LEVEL_ORDER: dict[str, int] = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

LEVEL_FILTER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("全部级别", "DEBUG"),
    ("INFO 及以上", "INFO"),
    ("WARNING 及以上", "WARNING"),
    ("ERROR", "ERROR"),
)

DEFAULT_LOG_MAX_LINES = 2000
RECENT_EVENT_LINES = 8


# ---------------------------------------------------------------------------
# 通用格式化
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatusLine:
    """状态区/性能面板的一行：``label`` + ``value``（可带强调色）。"""

    key: str
    label: str
    value: str
    emphasis: str = "normal"


def format_optional(value: object, unit: str = "", digits: int | None = 1) -> str:
    """``None`` 显示“—”；数值按 ``digits`` 保留小数（``None`` 表示整数）。"""

    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        if digits is None:
            return f"{value:.0f}{unit}"
        return f"{value:.{digits}f}{unit}"
    return f"{value}{unit}"


def format_mb(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.0f} MB"


def format_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f} %"


def format_ms(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f} ms"


def format_seconds(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f} s"


def format_duration(seconds: float | None) -> str:
    """把会话时长格式化为 ``HH:MM:SS``；``None`` 显示“—”。"""

    if seconds is None:
        return "—"
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_norm_point(point: tuple[float, float] | None) -> str:
    if point is None:
        return "—"
    return f"({point[0]:.3f}, {point[1]:.3f})"


# ---------------------------------------------------------------------------
# 生命周期 / 游戏流程 / 暂停原因
# ---------------------------------------------------------------------------


def lifecycle_text(lifecycle: RunLifecycle | str | None) -> str:
    if lifecycle is None:
        return "—"
    try:
        key = RunLifecycle(lifecycle)
    except ValueError:
        return f"未知（{lifecycle}）"
    return LIFECYCLE_TEXT[key]


def lifecycle_badge_text(lifecycle: RunLifecycle | str | None) -> str:
    text = lifecycle_text(lifecycle)
    if isinstance(lifecycle, RunLifecycle):
        return f"{text}（{lifecycle.value}）"
    return text


def lifecycle_color(lifecycle: RunLifecycle | str | None) -> str:
    if lifecycle is None:
        return "#6b7280"
    try:
        key = RunLifecycle(lifecycle)
    except ValueError:
        return "#6b7280"
    return LIFECYCLE_COLOR.get(key, "#6b7280")


def game_state_text(state: str | None) -> str:
    if state is None:
        return "—"
    return GAME_STATE_TEXT.get(state, f"{state}（未知状态）")


def game_state_display(state: str | None) -> str:
    text = game_state_text(state)
    if state is None:
        return "—"
    return f"{text}（{state}）"


def pause_reason_text(reason: str | None) -> str:
    if reason is None:
        return "—"
    return PAUSE_REASON_TEXT.get(reason, f"未知原因（{reason}）")


# ---------------------------------------------------------------------------
# 状态区四组（7.5）
# ---------------------------------------------------------------------------


def run_status_groups(snapshot: RunSnapshot | None) -> dict[str, tuple[StatusLine, ...]]:
    """返回状态区的 A/B/C/D 四组行；``None`` 快照全部显示“—”。"""

    if snapshot is None:
        dashed = tuple(
            StatusLine(key, label, "—")
            for key, label in (
                ("lifecycle", "脚本生命周期"),
                ("since", "停留时长"),
                ("detail", "说明"),
            )
        )
        return {"A": dashed, "B": (), "C": (), "D": ()}

    life = snapshot.lifecycle
    game = snapshot.game
    perception = snapshot.perception
    input_view = snapshot.input
    navigation = snapshot.navigation
    session = snapshot.session
    capabilities = snapshot.capabilities

    group_a = (
        StatusLine(
            "lifecycle",
            "脚本生命周期",
            lifecycle_badge_text(life.lifecycle),
            _lifecycle_emphasis(life.lifecycle),
        ),
        StatusLine("since", "停留时长", format_duration(snapshot.at - life.since_at)),
        StatusLine("detail", "说明", life.detail or "—"),
    )

    group_b: list[StatusLine] = [
        StatusLine("state", "游戏流程状态", game_state_display(game.state)),
        StatusLine("state_since", "流程进入时间", game.state_since_wall or "—"),
        StatusLine("transition", "最近转移", game.last_transition or "—"),
    ]
    if life.lifecycle is RunLifecycle.STOPPED and game.battle_active:
        group_b.append(StatusLine("stop_battle_warning", "提示", STOP_BATTLE_WARNING, "warning"))

    pause_value = pause_reason_text(game.pause_reason)
    if life.lifecycle is RunLifecycle.PAUSED and not game.pause_reason:
        pause_value = "已暂停（未提供原因）"
    group_c = (
        StatusLine("pause_reason", "暂停原因", pause_value, "normal" if not game.pause_reason else "warning"),
        StatusLine("pause_raw", "原始原因", game.pause_reason or "—"),
        StatusLine("pause_detail", "暂停详情", game.pause_detail or "—"),
    )

    foreground = "前台：是" if perception.window_foreground else "前台：否"
    window = "窗口：已找到" if perception.window_found else "窗口：未找到"
    group_d = (
        StatusLine("window", "前台/窗口", f"{foreground}  {window}"),
        StatusLine(
            "battle",
            "场次",
            f"完成 {session.battles_completed} / 本次上限 {format_optional(session.battles_target, '', None)}"
            f" / 今日 {format_optional(session.daily_used, '', None)}",
        ),
        StatusLine("elapsed", "会话时长", format_duration(session.elapsed_s)),
        StatusLine("input_gate", "输入门禁", _input_gate_text(input_view, capabilities)),
        StatusLine("navigation", "导航", _navigation_brief(navigation, capabilities)),
        StatusLine("frame", "最近帧", _frame_text(perception)),
        StatusLine(
            "hp",
            "血条证据",
            "不可用（快照未提供血条证据；GUI 不承诺存活时不退出）"
            if not capabilities.exit_authorized_gate
            else "证据已接入（见快照/诊断）",
        ),
    )
    return {"A": group_a, "B": tuple(group_b), "C": group_c, "D": group_d}


def _lifecycle_emphasis(lifecycle: RunLifecycle) -> str:
    if lifecycle is RunLifecycle.ERROR:
        return "error"
    if lifecycle in (RunLifecycle.PAUSED, RunLifecycle.PAUSING, RunLifecycle.WAIT_ACCOUNT):
        return "warning"
    if lifecycle is RunLifecycle.RUNNING:
        return "ok"
    return "muted"


def _input_gate_text(input_view, capabilities: CapabilityFlags) -> str:
    gate = "打开" if input_view.gate_open else "关闭"
    if not input_view.gate_open and input_view.gate_closed_reason:
        gate = f"关闭（{input_view.gate_closed_reason}）"
    if capabilities.release_confirmed and input_view.release_confirmed:
        release = f"已确认（release #{input_view.release_count}）"
    elif capabilities.release_confirmed:
        release = "等待确认"
    else:
        release = "未接入确认（不承诺已停止输入）"
    failed = "是" if input_view.failed else "否"
    return (
        f"{gate}；待执行 {input_view.pending_actions}；释放：{release}；"
        f"拒绝 {input_view.rejected}；失败 {failed}"
    )


def _navigation_brief(navigation, capabilities: CapabilityFlags) -> str:
    mode = "autopilot（游戏自带自动驾驶）" if navigation.mode == "autopilot" else f"{navigation.mode}"
    clicks = "未发出" if navigation.click_count <= 0 else f"已发出 {navigation.click_count} 次点击"
    if capabilities.autopilot_move_verified:
        confirmed = "航行已确认" if navigation.move_confirmed else "航行未确认"
    else:
        confirmed = "航行已确认：未验证（点击≠航行成功，见依赖与风险）"
    extra = "；最近一次验证失败" if navigation.verify_failed else ""
    return f"{mode}；{clicks}；{confirmed}{extra}"


def _frame_text(perception) -> str:
    if perception.capture_error:
        return f"截屏错误：{perception.capture_error}"
    if perception.last_frame_at is None:
        return "—（还没有新帧）"
    wall = perception.last_frame_wall or "—"
    age = perception.last_frame_age_s
    if age is None:
        return f"{wall}（帧 #{perception.last_frame_id}）"
    text = f"{wall}（帧 #{perception.last_frame_id}，{age:.1f} s 前）"
    if age > 2.0:
        text += "；画面可能不是最新（截屏周期约 2 帧/秒，runtime.capture_hz=2）"
    return text


# ---------------------------------------------------------------------------
# 按钮可用性矩阵（7.6）
# ---------------------------------------------------------------------------

BUTTON_KEYS: tuple[str, ...] = (
    "validate_config",
    "start",
    "pause",
    "resume",
    "stop",
    "import_ship",
    "run_write_tool",
    "confirm_account",
    "abort_rotation",
    "open_logs",
    "view_stats",
)


def button_matrix(
    lifecycle: RunLifecycle | str | None,
    *,
    start_ready: bool = True,
    tool_running: bool = False,
    account_switch_ready: bool = True,
    closing: bool = False,
) -> dict[str, bool]:
    """7.6 按钮矩阵：8 种生命周期 × 全部按钮的纯函数实现。

    ``start_ready`` 表示启动预检已通过且无工具运行；``closing`` 用于窗口关闭
    流程（6.5/11.5：关闭过程中禁用所有控件）。
    """

    if closing:
        return dict.fromkeys(BUTTON_KEYS, False)

    try:
        life = RunLifecycle(lifecycle) if lifecycle is not None else None
    except ValueError:
        life = None

    stop_states = (
        RunLifecycle.STARTING,
        RunLifecycle.RUNNING,
        RunLifecycle.PAUSING,
        RunLifecycle.PAUSED,
        RunLifecycle.WAIT_ACCOUNT,
    )
    return {
        "validate_config": life not in (RunLifecycle.STARTING, RunLifecycle.STOPPING),
        "start": life is RunLifecycle.STOPPED and start_ready and not tool_running,
        "pause": life is RunLifecycle.RUNNING,
        "resume": life is RunLifecycle.PAUSED,
        "stop": life in stop_states,
        "import_ship": life is RunLifecycle.STOPPED and not tool_running,
        "run_write_tool": life is RunLifecycle.STOPPED and not tool_running,
        "confirm_account": life is RunLifecycle.WAIT_ACCOUNT and account_switch_ready,
        "abort_rotation": life is RunLifecycle.WAIT_ACCOUNT,
        "open_logs": True,
        "view_stats": life in (RunLifecycle.STOPPED, RunLifecycle.WAIT_ACCOUNT, RunLifecycle.ERROR),
    }


def close_behavior(lifecycle: RunLifecycle | str | None) -> str:
    """6.5/11.5：关闭窗口行为。``direct``/``async``/``wait``/``confirm``。"""

    try:
        life = RunLifecycle(lifecycle) if lifecycle is not None else None
    except ValueError:
        life = None
    if life is RunLifecycle.STOPPED:
        return "direct"
    if life is RunLifecycle.STOPPING:
        return "wait"
    if life is RunLifecycle.ERROR:
        return "confirm"
    return "async"


# ---------------------------------------------------------------------------
# 生效值（EffectivePlan）
# ---------------------------------------------------------------------------


def effective_plan_rows(plan: EffectivePlan | None) -> tuple[StatusLine, ...]:
    """启动前展示生效值；``plan is None`` 时全部显示“—”而不是 0。"""

    if plan is None:
        return (
            StatusLine("plan_missing", "生效值", "—（服务尚未提供生效值快照，启动前无法展示）", "muted"),
            StatusLine("cap_battle", "本次场次上限", "—"),
            StatusLine("cap_safety", "会话安全上限", "—"),
            StatusLine("cap_daily", "每日上限", "—"),
        )

    account = plan.account_display_name or plan.account_alias or "—"
    rows = [
        StatusLine("client", "客户端", f"{plan.client_display_name}（{plan.client_id}）"),
        StatusLine("ship", "船只", f"{plan.ship_display_name}（{plan.ship_id}）"),
        StatusLine("account", "账号", account),
        StatusLine("navigation", "导航模式", plan.navigation_mode),
        StatusLine("cap_battle", "本次场次上限", format_optional(plan.max_battles_override, "", None)),
        StatusLine("cap_safety", "会话安全上限", format_optional(plan.safety_battle_cap, " 场", None)),
        StatusLine(
            "cap_daily",
            "每日上限",
            f"{format_optional(plan.daily_cap, ' 场', None)}"
            f"（今日已用 {format_optional(plan.daily_used, ' 场', None)}）",
        ),
        StatusLine("session_limit", "会话时长上限", format_optional(plan.max_session_minutes, " 分钟", None)),
        StatusLine("emergency_key", "紧急停止键", plan.emergency_stop_key or "—"),
        StatusLine("resume_key", "恢复键", plan.resume_key or "—"),
        StatusLine("heartbeat", "心跳", format_optional(plan.heartbeat_s, " s")),
        StatusLine("start_paused", "启动时先暂停", "是" if plan.start_paused else "否"),
        StatusLine("config_hash", "配置哈希", plan.config_hash or "—"),
        StatusLine("log_path", "日志文件", str(plan.log_path) if plan.log_path else "—"),
    ]
    allowed = plan.allowed_hours
    rows.append(StatusLine("allowed_hours", "允许时段", f"{allowed[0]}–{allowed[1]}" if allowed else "不限"))
    return tuple(rows)


# ---------------------------------------------------------------------------
# 性能面板（6.1 / 6.2）
# ---------------------------------------------------------------------------


def overload_state_text(state: str | None) -> str:
    if state is None:
        return "—"
    return OVERLOAD_STATE_TEXT.get(state, f"未知（{state}）")


def threshold_status_text(
    perf: PerformanceView | None,
    capabilities: CapabilityFlags | None = None,
) -> str:
    """保护阈值状态文案：monitor 只监测；能力未接入不显示“已启用”。"""

    if perf is None:
        return f"保护阈值：不可用（{NO_PERFORMANCE_TEXT}）"
    if not perf.enabled:
        reason = perf.unavailable_reason or "性能监测未启用"
        return f"保护阈值：不可用（{reason}）"

    if perf.thresholds is None:
        return "保护阈值：托管未启动"
    mode = perf.mode or perf.thresholds.mode or "off"
    if mode == "off":
        return "保护阈值：已关闭"
    if mode == "monitor":
        return "保护阈值：监测中（不自动停止）"
    if mode == "pause_stop":
        if capabilities is not None and not capabilities.overload_protection:
            return "保护阈值：阈值已配置，但过载保护能力未接入（不自动停止）"
        return "保护阈值：已启用（过载时暂停/停止）"
    if mode == "stop":
        if capabilities is not None and not capabilities.overload_protection:
            return "保护阈值：阈值已配置，但过载保护能力未接入（不自动停止）"
        return "保护阈值：已启用（过载时停止）"
    return f"保护阈值：未知模式（{mode}）"


def performance_mode_text(perf: PerformanceView | None) -> str:
    if perf is None or not perf.enabled:
        return "未启用"
    return PERFORMANCE_MODE_TEXT.get(perf.mode, f"未知（{perf.mode}）")


def performance_rows(
    perf: PerformanceView | None,
    capabilities: CapabilityFlags | None = None,
) -> tuple[StatusLine, ...]:
    """右栏/性能页共用的核心行；缺失值一律“—”。"""

    if perf is None:
        return (
            StatusLine("unavailable", "监测状态", NO_PERFORMANCE_TEXT, "warning"),
            StatusLine("threshold", "保护阈值", threshold_status_text(perf, capabilities), "warning"),
        )

    rows: list[StatusLine] = []
    if not perf.enabled or perf.unavailable_reason:
        reason = perf.unavailable_reason or "性能监测未启用"
        rows.append(StatusLine("unavailable", "监测状态", f"不可用（{reason}）", "warning"))
    else:
        rows.append(StatusLine("mode", "监测模式", performance_mode_text(perf)))
    rows.extend(
        [
            StatusLine("cpu", "CPU", format_pct(perf.cpu_pct)),
            StatusLine("mem_total", "物理内存", format_mb(perf.mem_total_mb)),
            StatusLine("mem_available", "可用内存", format_mb(perf.mem_available_mb)),
            StatusLine("commit_available", "Commit 余量", format_mb(perf.commit_available_mb)),
            StatusLine("script_private", "脚本进程内存", format_mb(perf.script_private_mb)),
            StatusLine("capture_ms", "截图耗时", format_ms(perf.capture_ms_avg)),
            StatusLine("sampled", "采样时间", _sample_text(perf)),
            StatusLine(
                "threshold",
                "保护阈值",
                threshold_status_text(perf, capabilities),
                "warning" if perf.mode == "monitor" else "normal",
            ),
            StatusLine(
                "overload",
                "过载状态",
                _overload_text(perf),
                OVERLOAD_STATE_EMPHASIS.get(perf.overload_state, "normal"),
            ),
        ]
    )
    return tuple(rows)


def _sample_text(perf: PerformanceView) -> str:
    if perf.sampled_wall:
        return perf.sampled_wall
    if perf.sample_age_s is not None:
        return format_seconds(perf.sample_age_s)
    if perf.sampled_at is not None:
        return format_seconds(perf.sampled_at)
    return "—"


def _overload_text(perf: PerformanceView) -> str:
    text = overload_state_text(perf.overload_state)
    reason = perf.overload_reason or perf.overload_detail
    if reason:
        text += f"：{reason}"
    if perf.would_pause:
        text += "；将触发暂停"
    if perf.would_stop:
        text += "；将触发停止"
    return text


def performance_detail_rows(
    perf: PerformanceView | None,
    capabilities: CapabilityFlags | None = None,
) -> tuple[StatusLine, ...]:
    """性能页额外明细（右栏摘要不显示，避免重复堆叠）。"""

    if perf is None:
        return ()
    thresholds = perf.thresholds
    rows = [
        StatusLine("sample_age", "数据年龄", format_seconds(perf.sample_age_s)),
        StatusLine("commit_limit", "Commit 上限", format_mb(perf.commit_limit_mb)),
        StatusLine("script_cpu", "脚本进程 CPU", format_pct(perf.script_cpu_pct)),
        StatusLine("loop_tick", "主循环耗时 P95", format_ms(perf.loop_tick_p95_ms)),
        StatusLine("lateness", "采样迟到", format_ms(perf.sampler_lateness_ms)),
    ]
    if thresholds is None:
        return tuple(rows)
    rows.extend(
        [
            StatusLine("thr_sample_hz", "采样频率", format_optional(thresholds.sample_hz, " Hz")),
            StatusLine("thr_cpu", "CPU 告警阈值", format_optional(thresholds.cpu_warn_pct, " %")),
            StatusLine("thr_physical", "物理内存告警阈值", format_mb(thresholds.physical_warn_mb)),
            StatusLine("thr_commit_warn", "Commit 告警阈值", format_mb(thresholds.commit_warn_mb)),
            StatusLine("thr_commit_pause", "Commit 暂停阈值", format_mb(thresholds.commit_pause_mb)),
            StatusLine("thr_commit_stop", "Commit 停止阈值", format_mb(thresholds.commit_stop_mb)),
            StatusLine("thr_commit_fast_stop", "Commit 快速停止阈值", format_mb(thresholds.commit_fast_stop_mb)),
            StatusLine("thr_script_private", "脚本内存告警阈值", format_mb(thresholds.script_private_warn_mb)),
            StatusLine("thr_lateness", "采样迟到判定", format_optional(thresholds.starvation_lateness_s, " s")),
        ]
    )
    last = perf.last_trigger
    if last:
        rows.append(StatusLine("last_trigger", "最近触发", "；".join(f"{k}={v}" for k, v in last.items())))
    return tuple(rows)


def performance_threshold_rows(thresholds: PerformanceThresholds | None) -> tuple[StatusLine, ...]:
    if thresholds is None:
        return ()
    return (
        StatusLine("mode", "模式", PERFORMANCE_MODE_TEXT.get(thresholds.mode, thresholds.mode)),
        StatusLine("cpu_warn", "CPU 告警", format_optional(thresholds.cpu_warn_pct, " %")),
        StatusLine("commit_warn", "Commit 告警", format_mb(thresholds.commit_warn_mb)),
        StatusLine("commit_pause", "Commit 暂停", format_mb(thresholds.commit_pause_mb)),
        StatusLine("commit_stop", "Commit 停止", format_mb(thresholds.commit_stop_mb)),
    )


# ---------------------------------------------------------------------------
# 能力开关（15.2）
# ---------------------------------------------------------------------------


def capability_rows(flags: CapabilityFlags | None) -> tuple[StatusLine, ...]:
    if flags is None:
        return ()
    rows: list[StatusLine] = []
    for field_name, (label, impact) in CAPABILITY_TEXT.items():
        enabled = bool(getattr(flags, field_name, False))
        if enabled:
            value = "✓ 已接入"
            emphasis = "ok"
        else:
            value = f"✗ 未接入 → {impact}"
            emphasis = "warning"
        rows.append(StatusLine(field_name, label, value, emphasis))
    return tuple(rows)


# ---------------------------------------------------------------------------
# 输入控制状态与底部状态行（协作方案 §4.1 / GUI设计 §2）
# ---------------------------------------------------------------------------


def input_state_text(snapshot: RunSnapshot | None) -> str:
    if snapshot is None:
        return "—"
    life = snapshot.lifecycle.lifecycle
    input_view = snapshot.input
    if input_view.failed:
        return "输入后端失败"
    if life is RunLifecycle.RUNNING:
        if snapshot.gui_waiting:
            return "等待游戏前台"
        if not input_view.gate_open and input_view.gate_closed_reason == "window_unfocused":
            return "等待游戏前台"
        return "托管中" if input_view.gate_open else "运行中（输入门禁关闭）"
    if life is RunLifecycle.PAUSING:
        return "正在让出输入"
    if life is RunLifecycle.PAUSED:
        if snapshot.lifecycle.resume_pending:
            return "正在恢复"
        if snapshot.game.pause_reason == "window_unfocused":
            return "等待游戏前台"
        return "已暂停"
    if life is RunLifecycle.STARTING:
        return "启动中"
    if life is RunLifecycle.STOPPING:
        return "正在停止"
    if life is RunLifecycle.WAIT_ACCOUNT:
        return "等待手动切号"
    if life is RunLifecycle.ERROR:
        return "异常（清理未完成）"
    return "已停止"


def status_bar_texts(snapshot: RunSnapshot | None, *, closing: bool = False) -> tuple[str, str, str]:
    """底部状态行三段：输入状态 / 画面更新 / 当前目标反馈。"""

    if closing:
        return (
            "输入状态：正在让出输入",
            "清理进度：等待输入释放与统一清理完成（不会强杀线程）",
            "目标：—",
        )
    if snapshot is None:
        return ("输入状态：—", "画面更新：—", "目标：—")
    life = snapshot.lifecycle.lifecycle
    if life is RunLifecycle.STOPPING:
        cleanup = snapshot.cleanup
        pending = "、".join(cleanup.pending_items) if cleanup.pending_items else "等待确认"
        middle = f"清理进度：{pending}"
    else:
        perception = snapshot.perception
        middle = f"画面更新：{perception.last_frame_wall or '—'}"
        if snapshot.game.state == "BATTLE_ACTIVE":
            middle += f"；目标点：{format_norm_point(snapshot.navigation.last_click_target_norm)}"
    return (f"输入状态：{input_state_text(snapshot)}", middle,
            snapshot.navigation.target_status or "目标：—")


# ---------------------------------------------------------------------------
# 日志行与筛选（12.2 / 12.3）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogRow:
    seq: int
    wall_at: str
    level: str
    source: str
    message: str
    session_id: int | None = None
    battle_id: int | None = None

    @property
    def level_name(self) -> str:
        return (self.level or "INFO").upper()


def log_row_from_event(event: RuntimeEvent) -> LogRow:
    wall = event.wall_at or ""
    time_text = wall[11:23] if len(wall) >= 23 else wall
    session = f"session#{event.session_id}" if event.session_id is not None else "session#—"
    battle = f" battle#{event.battle_id}" if event.battle_id is not None else ""
    return LogRow(
        seq=event.seq,
        wall_at=time_text,
        level=(event.level or "INFO").upper(),
        source=f"{session}{battle}",
        message=event.message,
        session_id=event.session_id,
        battle_id=event.battle_id,
    )


def level_at_least(level: str | None, minimum: str) -> bool:
    return LEVEL_ORDER.get((level or "INFO").upper(), 20) >= LEVEL_ORDER.get(minimum.upper(), 20)


def filter_log_rows(
    rows: Iterable[LogRow],
    *,
    minimum_level: str = "INFO",
    keyword: str = "",
    only_battle: bool = False,
    battle_id: int | None = None,
) -> tuple[LogRow, ...]:
    needle = keyword.strip().casefold()
    result: list[LogRow] = []
    for row in rows:
        if not level_at_least(row.level, minimum_level):
            continue
        if needle and needle not in row.message.casefold():
            continue
        if only_battle and (battle_id is None or row.battle_id != battle_id):
            continue
        result.append(row)
    return tuple(result)


def format_log_line(row: LogRow) -> str:
    return f"{row.wall_at:<12} {row.level_name:<7} {row.source:<20} {row.message}"


class LogModel:
    """有界日志视图模型：最多 ``max_lines`` 行，超出裁剪最旧行并计数。"""

    def __init__(self, max_lines: int = DEFAULT_LOG_MAX_LINES) -> None:
        self.max_lines = max(1, int(max_lines))
        self._rows: list[LogRow] = []
        self.trimmed_total = 0

    def __len__(self) -> int:
        return len(self._rows)

    @property
    def rows(self) -> tuple[LogRow, ...]:
        return tuple(self._rows)

    def append_rows(self, rows: Iterable[LogRow]) -> int:
        """追加并返回本次裁剪的行数。"""

        for row in rows:
            self._rows.append(row)
        trimmed = 0
        while len(self._rows) > self.max_lines:
            self._rows.pop(0)
            trimmed += 1
        self.trimmed_total += trimmed
        return trimmed

    def clear(self) -> None:
        self._rows.clear()

    def filtered(
        self,
        *,
        minimum_level: str = "INFO",
        keyword: str = "",
        only_battle: bool = False,
        battle_id: int | None = None,
    ) -> tuple[LogRow, ...]:
        return filter_log_rows(
            self._rows,
            minimum_level=minimum_level,
            keyword=keyword,
            only_battle=only_battle,
            battle_id=battle_id,
        )


class RecentEvents:
    """最近事件（默认 8 条，供底部最近日志/运行页使用）。"""

    def __init__(self, max_lines: int = RECENT_EVENT_LINES) -> None:
        self.max_lines = max(1, int(max_lines))
        self._rows: list[LogRow] = []

    @property
    def rows(self) -> tuple[LogRow, ...]:
        return tuple(self._rows)

    def push(self, rows: Iterable[LogRow]) -> None:
        for row in rows:
            self._rows.append(row)
        if len(self._rows) > self.max_lines:
            del self._rows[: len(self._rows) - self.max_lines]

    def clear(self) -> None:
        self._rows.clear()


class SeqGapTracker:
    """事件序号缺口跟踪（4.8：有界队列丢弃事件时不得静默丢失）。"""

    def __init__(self) -> None:
        self._last: int | None = None
        self.gap_total = 0
        self.gap_events = 0

    @property
    def last_seq(self) -> int | None:
        return self._last

    def observe(self, seq: int) -> int:
        gap = 0
        if self._last is not None and seq > self._last + 1:
            gap = seq - self._last - 1
        if self._last is None or seq > self._last:
            self._last = seq
        if gap:
            self.gap_total += gap
            self.gap_events += 1
        return gap

    def observe_many(self, events: Sequence[RuntimeEvent]) -> int:
        total = 0
        for event in events:
            total += self.observe(event.seq)
        return total

    def reset(self) -> None:
        self._last = None
        self.gap_total = 0
        self.gap_events = 0


def seq_gap_text(gap_total: int, dropped_events: int = 0) -> str:
    if gap_total <= 0 and dropped_events <= 0:
        return "事件序号：连续"
    parts = []
    if gap_total > 0:
        parts.append(f"缺失 {gap_total} 条")
    if dropped_events > 0:
        parts.append(f"服务队列丢弃 {dropped_events} 条")
    return f"事件序号：{'；'.join(parts)}（完整记录见日志文件）"


# ---------------------------------------------------------------------------
# 启动防抖（11.2）
# ---------------------------------------------------------------------------


class StartDebounce:
    """启动按钮防抖：一次启动流程内只接受第一次点击，直到显式释放。"""

    def __init__(self) -> None:
        self._pending = False

    @property
    def pending(self) -> bool:
        return self._pending

    def try_acquire(self) -> bool:
        if self._pending:
            return False
        self._pending = True
        return True

    def release(self) -> None:
        self._pending = False


# ---------------------------------------------------------------------------
# 事件 → 页面提示
# ---------------------------------------------------------------------------

_LEVEL_EMPHASIS = {
    "DEBUG": "muted",
    "INFO": "normal",
    "WARNING": "warning",
    "ERROR": "error",
    "CRITICAL": "error",
}


def event_emphasis(level: str | None) -> str:
    return _LEVEL_EMPHASIS.get((level or "INFO").upper(), "normal")


def event_notice(event: RuntimeEvent) -> tuple[str, str] | None:
    """把关键事件转成一次性横幅（不逐帧弹窗，12.7）。"""

    if event.kind is RuntimeEventKind.ERROR:
        return ("error", event.message or "运行服务报告错误")
    if event.kind is RuntimeEventKind.INPUT_FAILED:
        return ("warning", event.message or "输入后端连续失败")
    if event.kind is RuntimeEventKind.VALIDATION_FAILED:
        return ("error", event.message or "启动校验失败")
    if event.kind is RuntimeEventKind.TOOL_EXITED and "exit_code" in event.detail:
        return ("warning", event.message or "工具子进程已退出")
    return None


# ---------------------------------------------------------------------------
# 启动配置表单：船名匹配（8.4）、校验结果（11.1）、账号轮换（10.2）、标定工具（9.x）
# ---------------------------------------------------------------------------

LOG_LEVEL_OPTIONS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_LOG_LEVEL_LABEL = "默认（跟随 config.yaml）"
NO_ACCOUNT_LABEL = "（不使用账号档案）"
SHIP_NAME_HINT = "只输入名称；新船首次需导入一次船卡（本机图片，不上传）。"
SHIP_SELECT_HINT = "候选船名必须由用户点选，不自动取第一个。"
VALIDATION_DIRTY_TEXT = "配置已修改，请重新校验（启动时服务仍会再次校验）"
VALIDATION_NOT_RUN_TEXT = "配置校验：尚未校验"
TOOL_RUNNING_START_DISABLED = "标定工具运行中：已禁用启动托管（避免与标定/导入争用配置）"
TOOL_FAILED_TEXT = "工具失败（退出码 {code}）；原因见工具窗口/终端输出"
TOOL_FINISHED_TEXT = "已完成（退出码 0），正在重新校验配置…"
TOOL_VALIDATE_RUNNING_TEXT = "校验中…"
FULLMAP_CALIBRATION_NOTE = (
    "全屏地图标定（ISS-001 前置）：未标定——当前不承诺自动航行。"
    "步骤：实机截取 M 全屏地图 → 按 10.1 口径标定内容区 → 写入 config.yaml "
    "navigation.full_map_rect → 重新校验。GUI 第一版只提供说明，不新建标定算法。"
)
ROTATION_WAIT_TITLE = "等待手动切换账号"
ROTATION_WAIT_STEPS = (
    "请在游戏客户端手动完成：1. 退出当前账号并登录下一账号（脚本不会代登录、不保存密码）；"
    "2. 确认已回到港口界面；3. 回到本控制台点击“确认切号并继续”。"
)
ROTATION_WAIT_NOTE = "注意：脚本当前未控制游戏（输入已释放/已停止）；切换期间的客户端操作由你本人负责。"
ROTATION_NO_CREDENTIALS = "本面板不提供密码/令牌/验证码输入框；真实登录与切号必须由你在游戏客户端手动完成。"
ROTATION_CONFIRM_OK = "已提交确认，等待下一账号启动…"
ROTATION_CONFIRM_FAILED = (
    "预检失败：游戏窗口不在前台或未识别到港口；已留在等待状态，请手动回到港口后重试（详见日志）。"
)
ROTATION_ABORTED = "已结束账号轮换。"


@dataclass(frozen=True, slots=True)
class ProfileOption:
    """启动配置表单的客户端候选项（来自 profiles/*/profile.yaml）。"""

    profile_id: str
    display_name: str

    @property
    def label(self) -> str:
        return f"{self.display_name}（{self.profile_id}）"


@dataclass(frozen=True, slots=True)
class AccountOption:
    """启动配置表单的本地账号别名候选项（只读展示，不含任何凭据）。"""

    alias: str
    display_name: str

    @property
    def label(self) -> str:
        return f"{self.display_name}（{self.alias}）" if self.display_name else self.alias


@dataclass(frozen=True, slots=True)
class ShipCandidate:
    """本地已登记船只档案（GUI 候选与导入后刷新共用）。"""

    ship_id: str
    display_name: str

    @property
    def label(self) -> str:
        return f"{self.display_name}（{self.ship_id}）"


@dataclass(frozen=True, slots=True)
class ShipMatch:
    """船名匹配结果（8.4：不猜选，多义必须用户点选）。"""

    status: str  # empty | exact | partial_unique | partial_multiple | none
    matches: tuple[ShipCandidate, ...]
    message: str

    @property
    def single(self) -> ShipCandidate | None:
        return self.matches[0] if len(self.matches) == 1 else None


def match_ship_candidates(candidates: Iterable[ShipCandidate], text: str) -> ShipMatch:
    """按设计 8.4 做本地船名匹配：精确 → 部分唯/多义 → 未登记。"""

    candidates = tuple(candidates)
    name = (text or "").strip()
    if not name:
        return ShipMatch("empty", (), "请输入船名；已登记船只可直接匹配，新船需先导入船卡。")
    exact = tuple(item for item in candidates if item.display_name == name or item.ship_id == name)
    if len(exact) == 1:
        return ShipMatch("exact", exact, f"已登记：{exact[0].display_name}（{exact[0].ship_id}）")
    if len(exact) > 1:
        return ShipMatch(
            "partial_multiple",
            exact,
            f"有 {len(exact)} 个档案同名/同编号，请改用 ship_id 或重命名档案（界面不自动挑一个）",
        )
    partial = tuple(item for item in candidates if name in item.display_name)
    if len(partial) == 1:
        return ShipMatch(
            "partial_unique",
            partial,
            f"匹配到 {partial[0].display_name}（{partial[0].ship_id}），请确认使用",
        )
    if len(partial) > 1:
        return ShipMatch(
            "partial_multiple",
            partial,
            f"匹配到 {len(partial)} 艘船，请在候选列表中选择（不自动取第一个）",
        )
    return ShipMatch(
        "none",
        (),
        f"未登记：没有找到与“{name}”匹配的本地船只档案，请先在设置页“导入船卡”。",
    )


def format_validation_issue(issue: ValidationIssueView) -> str:
    tag = "错误" if str(issue.severity).lower() == "error" else "警告"
    return f"【{tag}】{issue.location}：{issue.message}"


def plan_result_summary(result: PlanResult | None) -> str:
    if result is None:
        return VALIDATION_NOT_RUN_TEXT
    errors = len(result.issues)
    warnings = len(result.warnings)
    mark = "✔" if result.ok else "✗"
    return f"配置校验：{mark} {errors} error / {warnings} warning"


def validation_issue_lines(result: PlanResult | None) -> tuple[str, ...]:
    if result is None:
        return ()
    lines = [format_validation_issue(item) for item in result.issues]
    lines.extend(format_validation_issue(item) for item in result.warnings)
    return tuple(lines)


def issue_emphasis(issue: ValidationIssueView) -> str:
    return "error" if str(issue.severity).lower() == "error" else "warning"


def rotation_wait_texts(snapshot: RunSnapshot | None) -> tuple[str, str, str]:
    """10.2 WAIT_ACCOUNT 面板：当前账号 → 下一账号文案（无凭据输入）。"""

    session = snapshot.session if snapshot is not None else None
    current = (session.account_alias if session is not None else None) or "（默认）"
    next_alias = (session.next_alias if session is not None else None) or "（见 accounts.yaml 轮换计划）"
    completed = session.battles_completed if session is not None else 0
    headline = f"当前账号「{current}」本次已完成 {completed} 场，下一步切换到「{next_alias}」。"
    return headline, ROTATION_WAIT_STEPS, ROTATION_WAIT_NOTE


# -- 标定工具（9.1/9.2/9.3）-------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolSpec:
    tool_id: str
    module: str
    title: str
    purpose: str
    write: bool


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "calibrate_ship",
        "src.tools.calibrate_ship",
        "船只卡片标定",
        "框选船卡/空槽，写 ships/<id>/ship.yaml（写工具，仅已停止可用）",
        True,
    ),
    ToolSpec(
        "calibrate_profile",
        "src.tools.calibrate_profile",
        "界面区域标定",
        "界面模板/区域，写 profile.yaml（写工具，仅已停止可用）",
        True,
    ),
    ToolSpec(
        "capture_marker",
        "src.tools.capture_marker",
        "自身标记标定",
        "小地图自身标记模板，写 marker/（写工具，仅已停止可用）",
        True,
    ),
    ToolSpec(
        "check_env",
        "src.tools.check_env",
        "环境检查",
        "窗口/截屏/DPI/输入自检（部分需游戏）",
        False,
    ),
    ToolSpec("validate", "src.tools.validate", "配置校验", "只读校验（JSON 输出）", False),
    ToolSpec("stats", "src.tools.stats", "统计工具", "只读数据库统计", False),
)
TOOL_BY_ID = {spec.tool_id: spec for spec in TOOL_SPECS}


def tool_spec(tool_id: str) -> ToolSpec | None:
    return TOOL_BY_ID.get(tool_id)


def tool_arguments(
    tool_id: str,
    *,
    root: Path,
    profile_id: str | None = None,
    ship_id: str | None = None,
    account_alias: str | None = None,
    json_output: bool = False,
) -> tuple[str, ...]:
    """构造 ``python -m src.tools.<tool> ...`` 的参数（不含解释器路径）。"""

    spec = TOOL_BY_ID.get(tool_id)
    if spec is None:
        raise ValueError(f"未知标定工具: {tool_id!r}")
    args: list[str] = ["-m", spec.module, "--root", str(root)]
    if tool_id == "stats":
        return tuple(args)
    if tool_id == "validate":
        if profile_id:
            args.extend(["--profile", profile_id])
        if ship_id:
            args.extend(["--ship", ship_id])
        if account_alias:
            args.extend(["--account", account_alias])
        if json_output:
            args.append("--json")
        return tuple(args)
    if profile_id:
        args.extend(["--profile", profile_id])
    if tool_id == "calibrate_ship" and ship_id:
        args.extend(["--ship-id", ship_id])
    return tuple(args)


__all__ = [
    "BUTTON_KEYS",
    "CAPABILITY_TEXT",
    "CLEANUP_ERROR_BANNER",
    "CLEANUP_ERROR_HINT",
    "CLEANUP_ERROR_TITLE",
    "CLOSING_STATUS",
    "DEFAULT_LOG_MAX_LINES",
    "F12_NOTICE",
    "FULLSCREEN_HINT",
    "GAME_STATE_TEXT",
    "LEVEL_FILTER_OPTIONS",
    "LIFECYCLE_TEXT",
    "LOG_FILE_NOTICE",
    "LogModel",
    "LogRow",
    "NO_PERFORMANCE_TEXT",
    "PAUSE_REASON_TEXT",
    "PERFORMANCE_MODE_TEXT",
    "RECENT_EVENT_LINES",
    "RecentEvents",
    "STOP_BATTLE_WARNING",
    "STOP_NOTICE",
    "SeqGapTracker",
    "StartDebounce",
    "StatusLine",
    "TOOL_PLACEHOLDERS",
    "button_matrix",
    "capability_rows",
    "close_behavior",
    "effective_plan_rows",
    "event_emphasis",
    "event_notice",
    "filter_log_rows",
    "format_duration",
    "format_log_line",
    "format_mb",
    "format_ms",
    "format_norm_point",
    "format_optional",
    "format_pct",
    "format_seconds",
    "game_state_display",
    "game_state_text",
    "input_state_text",
    "level_at_least",
    "lifecycle_badge_text",
    "lifecycle_color",
    "lifecycle_text",
    "log_row_from_event",
    "overload_state_text",
    "pause_reason_text",
    "performance_detail_rows",
    "performance_mode_text",
    "performance_rows",
    "performance_threshold_rows",
    "run_status_groups",
    "seq_gap_text",
    "status_bar_texts",
    "threshold_status_text",
    "DEFAULT_LOG_LEVEL_LABEL",
    "FULLMAP_CALIBRATION_NOTE",
    "LOG_LEVEL_OPTIONS",
    "NO_ACCOUNT_LABEL",
    "AccountOption",
    "ProfileOption",
    "ROTATION_ABORTED",
    "ROTATION_CONFIRM_FAILED",
    "ROTATION_CONFIRM_OK",
    "ROTATION_NO_CREDENTIALS",
    "ROTATION_WAIT_NOTE",
    "ROTATION_WAIT_STEPS",
    "ROTATION_WAIT_TITLE",
    "SHIP_NAME_HINT",
    "SHIP_SELECT_HINT",
    "ShipCandidate",
    "ShipMatch",
    "TOOL_BY_ID",
    "TOOL_FAILED_TEXT",
    "TOOL_FINISHED_TEXT",
    "TOOL_RUNNING_START_DISABLED",
    "TOOL_SPECS",
    "TOOL_VALIDATE_RUNNING_TEXT",
    "ToolSpec",
    "VALIDATION_DIRTY_TEXT",
    "VALIDATION_NOT_RUN_TEXT",
    "format_validation_issue",
    "issue_emphasis",
    "match_ship_candidates",
    "plan_result_summary",
    "rotation_wait_texts",
    "tool_arguments",
    "tool_spec",
    "validation_issue_lines",
]
