"""配置与地图包校验（详细设计 3.5 / 4.7；实现口径见实现补充文档）。

校验分四类：
- 范围校验：频率、距离、超时、概率、权重是否落在允许区间；
- 引用校验：模板/底图/卡片文件是否存在且可读、特征编号、状态名、出生点与航线编号；
- 结构语义校验：步骤动作必填字段、按键映射、退出流程特征等；
- 地图包规则：详细设计 4.7 的全部检查项（错误阻止加载，警告写入日志）。

所有检查只返回 ``ValidationIssue``，不抛异常、不修改配置；加载器负责把 YAML 解析
失败与必填字段缺失记入同一问题清单。``validate_project`` 是校验工具的入口。
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, fields
from pathlib import Path

from ..common.coords import (
    ImageOrientation,
    WorldImageTransform,
    affine_to_matrix,
    apply_affine,
)
from ..common.errors import Severity, ValidationIssue
from ..common.types import State
from .loader import (
    DEATH_SIGNALS,
    INPUT_BACKENDS,
    ROTATION_MODES,
    SCROLL_DIRECTIONS,
    STEP_ACTIONS,
    LoadedProject,
    load_project,
    relative_label,
)
from .models import (
    AccountsConfig,
    AppConfig,
    BehaviorConfig,
    ClientProfile,
    DeathConfig,
    GlobalConfig,
    KeyMap,
    MapPack,
    NavigationConfig,
    PerceptionConfig,
    ShipProfile,
    StepDef,
    TimeoutsConfig,
)

# 模板图片最小边长（详细设计 5.3：尺寸不小于 8×8、不是纯色图）
MIN_TEMPLATE_SIZE = 8
# 纯色判定阈值（灰度标准差）
MIN_TEMPLATE_STD = 1.0
# 合法按键名：小写字母、数字、下划线
KEY_NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")
# 时间 HH:MM
HOUR_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
# 运行期注册的特征（不是 profile.features 里的静态模板）
RUNTIME_FEATURES: frozenset[str] = frozenset({"ship_card"})
# 空装备槽特征前缀（ships/<ship_id>/loadout/ 下的模板注入）
LOADOUT_FEATURE_PREFIX = "loadout_"


# ---------------------------------------------------------------------------
# 报告与问题记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """一次完整项目校验的结果（校验工具与 main 共用）。"""

    issues: tuple[ValidationIssue, ...] = ()

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.is_error]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if not issue.is_error]

    @property
    def has_errors(self) -> bool:
        return any(issue.is_error for issue in self.issues)

    @property
    def ok(self) -> bool:
        """没有 error 即视为通过；warning 不影响退出码。"""
        return not self.has_errors

    def format_lines(self) -> list[str]:
        return [issue.format() for issue in self.issues]

    def summary(self) -> str:
        return f"配置校验完成：{len(self.errors)} 个错误，{len(self.warnings)} 个警告"


def _add(issues: list[ValidationIssue], level: Severity, code: str, location: str, message: str) -> None:
    issues.append(ValidationIssue(level=level, code=code, location=location, message=message))


def _error(issues: list[ValidationIssue], code: str, location: str, message: str) -> None:
    _add(issues, Severity.ERROR, code, location, message)


def _warn(issues: list[ValidationIssue], code: str, location: str, message: str) -> None:
    _add(issues, Severity.WARNING, code, location, message)


def _loc(location: str, field_name: str) -> str:
    return f"{location}: {field_name}"


def _check_positive(issues: list[ValidationIssue], value: float, location: str, label: str) -> None:
    if not math.isfinite(value) or value <= 0:
        _error(issues, "range", location, f"{label} 必须大于 0，当前 {value!r}")


def _check_non_negative(issues: list[ValidationIssue], value: float, location: str, label: str) -> None:
    if not math.isfinite(value) or value < 0:
        _error(issues, "range", location, f"{label} 不能为负，当前 {value!r}")


def _check_probability(issues: list[ValidationIssue], value: float, location: str, label: str) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        _error(issues, "range", location, f"{label} 必须落在 0～1，当前 {value!r}")


def _check_pair(
    issues: list[ValidationIssue],
    pair: tuple[float, float],
    location: str,
    label: str,
    *,
    minimum: float = 0.0,
) -> None:
    low, high = pair
    if not math.isfinite(low) or not math.isfinite(high):
        _error(issues, "range", location, f"{label} 含非有限值 {pair!r}")
        return
    if low < minimum or high < minimum:
        _error(issues, "range", location, f"{label} 不能小于 {minimum}，当前 {pair!r}")
    if low > high:
        _error(issues, "range", location, f"{label} 下限必须不大于上限，当前 {pair!r}")


def _check_int_positive(issues: list[ValidationIssue], value: int, location: str, label: str) -> None:
    if value <= 0:
        _error(issues, "range", location, f"{label} 必须大于 0，当前 {value!r}")


def _check_int_non_negative(issues: list[ValidationIssue], value: int, location: str, label: str) -> None:
    if value < 0:
        _error(issues, "range", location, f"{label} 不能为负，当前 {value!r}")


def _finite(*values: float) -> bool:
    return all(math.isfinite(value) for value in values)


# ---------------------------------------------------------------------------
# 图片文件校验
# ---------------------------------------------------------------------------


def _check_image_file(expected: Path, issues: list[ValidationIssue], location: str) -> None:
    """校验模板/底图文件存在、可读、尺寸不小于 8×8、不是纯色图。"""
    if not expected.is_file():
        _error(issues, "file_missing", location, f"文件缺失（待实机标定）: {expected}")
        return
    try:
        # 延迟导入：配置校验不需要在加载开始阶段初始化 OpenCV
        import cv2
        import numpy as np

        image = cv2.imdecode(np.fromfile(str(expected), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    except Exception as exc:  # pragma: no cover - 取决于本机 OpenCV 环境
        _error(issues, "file_unreadable", location, f"图片无法读取: {exc}")
        return
    if image is None:
        _error(issues, "file_unreadable", location, f"图片无法读取（不是有效图片）: {expected}")
        return
    height, width = image.shape[:2]
    if width < MIN_TEMPLATE_SIZE or height < MIN_TEMPLATE_SIZE:
        _error(
            issues,
            "image_too_small",
            location,
            f"图片尺寸 {width}×{height} 小于 {MIN_TEMPLATE_SIZE}×{MIN_TEMPLATE_SIZE}: {expected}",
        )
    if float(np.asarray(image).std()) < MIN_TEMPLATE_STD:
        _error(issues, "image_uniform", location, f"纯色图片容易误匹配，拒绝作为模板: {expected}")


# ---------------------------------------------------------------------------
# 全局配置
# ---------------------------------------------------------------------------


def validate_global_config(config: GlobalConfig) -> list[ValidationIssue]:
    """全局配置的结构语义、范围与依赖关系校验（config.yaml）。"""
    issues: list[ValidationIssue] = []
    location = "config.yaml"

    # --- runtime ---
    runtime = config.runtime
    loc = _loc(location, "runtime.loop_hz")
    if not 1 <= runtime.loop_hz <= 60:
        _error(issues, "range", loc, f"主循环频率应在 1～60 Hz，当前 {runtime.loop_hz}")
    _check_positive(issues, runtime.capture_hz, _loc(location, "runtime.capture_hz"), "截屏频率")
    if runtime.capture_hz > runtime.loop_hz:
        _warn(
            issues,
            "range",
            _loc(location, "runtime.capture_hz"),
            f"截屏频率 {runtime.capture_hz} 高于决策频率 {runtime.loop_hz}，多余截屏不会被消费",
        )
    if runtime.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        _error(
            issues,
            "enum",
            _loc(location, "runtime.log_level"),
            f"日志级别非法: {runtime.log_level}",
        )

    # --- map ---
    map_config = config.map
    _check_positive(issues, map_config.spawn_tolerance_m, _loc(location, "map.spawn_tolerance_m"), "出生点容差")
    if map_config.expected_minimap_scale is not None:
        _check_positive(
            issues,
            map_config.expected_minimap_scale,
            _loc(location, "map.expected_minimap_scale"),
            "期望小地图缩放比例",
        )

    # --- navigation ---
    nav = config.navigation
    if nav.mode not in ("autopilot", "waypoints"):
        _error(
            issues,
            "enum",
            _loc(location, "navigation.mode"),
            f"未知导航模式 {nav.mode!r}（autopilot / waypoints）",
        )
    if nav.mode == "waypoints" and not map_config.auto_identify and not map_config.forced_map_id:
        _error(
            issues,
            "dependency",
            _loc(location, "map.auto_identify"),
            "waypoints 模式关闭自动识别时必须配置 map.forced_map_id，否则无法确定使用哪张地图",
        )
    if nav.mode == "autopilot":
        if nav.autopilot_target not in ("center", "symmetric"):
            _error(
                issues,
                "enum",
                _loc(location, "navigation.autopilot_target"),
                f"未知敌区目标 {nav.autopilot_target!r}（center / symmetric）",
            )
        _check_pair(issues, nav.autopilot_reclick_s, _loc(location, "navigation.autopilot_reclick_s"), "重发航点间隔")
        if nav.autopilot_reclick_s[0] <= 0:
            _error(issues, "range", _loc(location, "navigation.autopilot_reclick_s"), "重发航点间隔必须为正数")
        if not 0.0 <= nav.autopilot_overshoot <= 2.0:
            _error(
                issues, "range", _loc(location, "navigation.autopilot_overshoot"), "autopilot_overshoot 应在 0～2"
            )
        for index, offset in enumerate(nav.autopilot_offset):
            if not -1.0 <= offset <= 1.0:
                _error(
                    issues,
                    "range",
                    _loc(location, f"navigation.autopilot_offset[{index}]"),
                    "目标偏移应在 -1～1（相对小地图内容区）",
                )
    if nav.surface not in ("minimap", "full_map"):
        _error(
            issues,
            "enum",
            _loc(location, "navigation.surface"),
            f"未知点击面 {nav.surface!r}（minimap / full_map）",
        )
    elif nav.surface == "full_map":
        if nav.mode == "waypoints":
            _error(
                issues,
                "dependency",
                _loc(location, "navigation.surface"),
                "waypoints 模式只支持 surface=minimap；full_map 仅用于 autopilot（实现补充 10.1）",
            )
        if nav.full_map_rect is None:
            _error(
                issues,
                "missing",
                _loc(location, "navigation.full_map_rect"),
                "surface=full_map 时必须配置全屏地图内容区矩形 navigation.full_map_rect",
            )
    if nav.route_end_action != "wait":
        _error(
            issues,
            "unsupported",
            _loc(location, "navigation.route_end_action"),
            f"v1 只支持 route_end_action=wait（当前 {nav.route_end_action!r}）",
        )
    _validate_navigation(issues, nav, location)

    # --- behavior ---
    _validate_behavior(issues, config.behavior, location)

    # --- ship ---
    if not config.ship.ship_id:
        _warn(
            issues,
            "missing",
            _loc(location, "ship.ship_id"),
            "未配置 ship.ship_id，必须通过命令行 --ship 指定船只",
        )

    # --- safety ---
    safety = config.safety
    if not safety.emergency_stop_key:
        _error(issues, "missing", _loc(location, "safety.emergency_stop_key"), "紧急停止热键不能为空")
    if not safety.resume_key:
        _error(issues, "missing", _loc(location, "safety.resume_key"), "恢复热键不能为空")
    _check_positive(issues, safety.max_session_minutes, _loc(location, "safety.max_session_minutes"), "单次会话时长")
    _check_int_positive(
        issues, safety.max_battles_per_session, _loc(location, "safety.max_battles_per_session"), "单次会话场次"
    )
    _check_positive(issues, safety.heartbeat_s, _loc(location, "safety.heartbeat_s"), "心跳超时")
    _check_non_negative(issues, safety.restart_wait_s, _loc(location, "safety.restart_wait_s"), "重启等待")
    _check_int_non_negative(
        issues, safety.restart_max_attempts, _loc(location, "safety.restart_max_attempts"), "重启次数"
    )

    # --- session ---
    session = config.session
    _check_pair(issues, session.between_battle_s, _loc(location, "session.between_battle_s"), "场间等待")
    _check_int_positive(issues, session.daily_battle_cap, _loc(location, "session.daily_battle_cap"), "每日场次上限")
    _check_int_non_negative(
        issues,
        session.max_consecutive_start_failures,
        _loc(location, "session.max_consecutive_start_failures"),
        "连续开局失败上限",
    )
    _check_int_non_negative(
        issues, session.recovery_max_per_10min, _loc(location, "session.recovery_max_per_10min"), "恢复次数上限"
    )
    _validate_allowed_hours(issues, session.allowed_hours, _loc(location, "session.allowed_hours"))

    # --- timeouts ---
    timeout_loc = _loc(location, "timeouts")
    for item in fields(TimeoutsConfig):
        value = getattr(config.timeouts, item.name)
        if not math.isfinite(value) or value <= 0:
            _error(issues, "range", f"{timeout_loc}.{item.name}", f"超时必须大于 0，当前 {value!r}")
    if abs(config.timeouts.heartbeat_s - safety.heartbeat_s) > 1e-9:
        _warn(
            issues,
            "duplicate",
            timeout_loc,
            f"timeouts.heartbeat_s={config.timeouts.heartbeat_s} 与 safety.heartbeat_s={safety.heartbeat_s} 不一致",
        )

    # --- input ---
    input_config = config.input
    if input_config.backend not in INPUT_BACKENDS:
        _error(
            issues,
            "enum",
            _loc(location, "input.backend"),
            f"输入后端非法: {input_config.backend}（允许 {sorted(INPUT_BACKENDS)}）",
        )
    _check_int_non_negative(
        issues, input_config.min_action_interval_ms, _loc(location, "input.min_action_interval_ms"), "最小动作间隔"
    )
    _check_pair(issues, input_config.click_hold_ms, _loc(location, "input.click_hold_ms"), "鼠标按住时间")
    _check_pair(issues, input_config.key_hold_ms, _loc(location, "input.key_hold_ms"), "按键按住时间")
    _check_probability(issues, input_config.interval_jitter, _loc(location, "input.interval_jitter"), "间隔抖动比例")
    _check_int_positive(issues, input_config.queue_max, _loc(location, "input.queue_max"), "动作队列上限")

    # --- logging ---
    logging_config = config.logging
    _check_int_positive(
        issues, logging_config.log_max_bytes, _loc(location, "logging.log_max_bytes"), "单个日志文件大小"
    )
    _check_int_non_negative(
        issues, logging_config.log_backup_count, _loc(location, "logging.log_backup_count"), "日志备份数"
    )
    if not logging_config.db_fallback_log:
        _error(issues, "missing", _loc(location, "logging.db_fallback_log"), "数据库降级日志路径不能为空")
    if not logging_config.diagnostics_dir:
        _error(issues, "missing", _loc(location, "logging.diagnostics_dir"), "诊断截图目录不能为空")
    _check_int_non_negative(
        issues,
        logging_config.diagnostics_max_files,
        _loc(location, "logging.diagnostics_max_files"),
        "诊断截图数量配额",
    )
    _check_non_negative(
        issues,
        logging_config.diagnostics_max_mb,
        _loc(location, "logging.diagnostics_max_mb"),
        "诊断截图容量配额",
    )
    _check_non_negative(issues, logging_config.disk_warn_gb, _loc(location, "logging.disk_warn_gb"), "磁盘告警阈值")
    _check_non_negative(issues, logging_config.disk_pause_gb, _loc(location, "logging.disk_pause_gb"), "磁盘暂停阈值")
    if logging_config.disk_warn_gb < logging_config.disk_pause_gb:
        _error(
            issues,
            "range",
            _loc(location, "logging.disk_warn_gb"),
            f"disk_warn_gb={logging_config.disk_warn_gb} 不能小于 disk_pause_gb={logging_config.disk_pause_gb}",
        )

    # --- perception ---
    _validate_perception(issues, config.perception, location)
    return issues


def _validate_navigation(issues: list[ValidationIssue], nav: NavigationConfig, location: str) -> None:
    def loc(name: str) -> str:
        return _loc(location, f"navigation.{name}")

    _check_positive(issues, nav.arrival_m, loc("arrival_m"), "到达判定距离")
    _check_positive(issues, nav.overshoot_m, loc("overshoot_m"), "越点判定距离")
    if nav.overshoot_m <= nav.arrival_m:
        _error(issues, "range", loc("overshoot_m"), f"越点距离 {nav.overshoot_m} 应大于到达距离 {nav.arrival_m}")
    if not -1.0 <= nav.overshoot_cos_threshold <= 1.0:
        _error(issues, "range", loc("overshoot_cos_threshold"), "越点方向余弦阈值应在 -1～1")
    _check_int_non_negative(issues, nav.click_retry, loc("click_retry"), "点击重试次数")
    _check_positive(issues, nav.click_confirm_first_s, loc("click_confirm_first_s"), "首次移动确认窗口")
    _check_positive(issues, nav.click_confirm_s, loc("click_confirm_s"), "移动确认窗口")
    _check_positive(issues, nav.move_confirm_px, loc("move_confirm_px"), "移动确认像素阈值")
    _check_non_negative(issues, nav.click_min_interval_s, loc("click_min_interval_s"), "导航点击最小间隔")
    _check_non_negative(issues, nav.retry_interval_s, loc("retry_interval_s"), "重试点击间隔")
    _check_positive(issues, nav.stuck_window_s, loc("stuck_window_s"), "卡住判定窗口")
    _check_non_negative(issues, nav.stuck_distance_px, loc("stuck_distance_px"), "卡住判定像素阈值")
    _check_int_non_negative(issues, nav.max_skipped, loc("max_skipped"), "连续跳过航点上限")
    _check_positive(issues, nav.ship_speed_window_s, loc("ship_speed_window_s"), "速度估计窗口")
    if nav.ship_speed_samples < 3:
        _error(issues, "range", loc("ship_speed_samples"), f"速度估计样本数至少 3，当前 {nav.ship_speed_samples}")
    _check_non_negative(issues, nav.ship_lost_degrade_s, loc("ship_lost_degrade_s"), "位置丢失降级时间")
    _check_positive(issues, nav.ship_lost_abandon_s, loc("ship_lost_abandon_s"), "位置丢失放弃时间")
    if nav.ship_lost_abandon_s <= nav.ship_lost_degrade_s:
        _error(
            issues,
            "range",
            loc("ship_lost_abandon_s"),
            f"放弃时间 {nav.ship_lost_abandon_s} 应大于降级时间 {nav.ship_lost_degrade_s}",
        )
    if not nav.full_map_toggle_key:
        _error(issues, "missing", loc("full_map_toggle_key"), "全屏地图开关动作名不能为空")
    # ISS-001：M 全屏地图点选时序与验证参数（实现补充 10.1）
    _check_non_negative(issues, nav.full_map_open_wait_s, loc("full_map_open_wait_s"), "全屏地图开图等待")
    _check_non_negative(issues, nav.full_map_close_wait_s, loc("full_map_close_wait_s"), "全屏地图关图等待")
    _check_positive(issues, nav.full_map_verify_s, loc("full_map_verify_s"), "航向验证窗口")
    _check_int_non_negative(issues, nav.full_map_retry, loc("full_map_retry"), "全屏地图重试次数")
    _check_positive(
        issues,
        nav.autopilot_verify_min_move_px,
        loc("autopilot_verify_min_move_px"),
        "航向验证最小位移",
    )
    _check_positive(
        issues,
        nav.autopilot_verify_min_speed,
        loc("autopilot_verify_min_speed"),
        "航向验证最小船速",
    )
    if nav.autopilot_verify_feature is not None and not nav.autopilot_verify_feature.strip():
        _error(issues, "missing", loc("autopilot_verify_feature"), "航向验证特征名不能为空字符串（null 表示不用特征）")


def _validate_behavior(issues: list[ValidationIssue], behavior: BehaviorConfig, location: str) -> None:
    def loc(name: str) -> str:
        return _loc(location, f"behavior.{name}")

    pairs: list[tuple[str, tuple[float, float], float]] = [
        ("camera_interval_s", behavior.camera_interval_s, 0.0),
        ("attack_interval_s", behavior.attack_interval_s, 0.0),
        ("consumable_ratio_range", behavior.consumable_ratio_range, 0.0),
        ("camera_activity_range", behavior.camera_activity_range, 0.0),
        ("attack_frequency_range", behavior.attack_frequency_range, 0.0),
        ("long_stare_bias_range", behavior.long_stare_bias_range, 0.0),
        ("long_stare_s", behavior.long_stare_s, 0.0),
        ("move_px", (float(behavior.move_px[0]), float(behavior.move_px[1])), 1.0),
        ("move_steps", (float(behavior.move_steps[0]), float(behavior.move_steps[1])), 1.0),
        ("move_duration_ms", (float(behavior.move_duration_ms[0]), float(behavior.move_duration_ms[1])), 0.0),
        ("fire_burst", (float(behavior.fire_burst[0]), float(behavior.fire_burst[1])), 0.0),
        ("fire_interval_ms", (float(behavior.fire_interval_ms[0]), float(behavior.fire_interval_ms[1])), 0.0),
        ("activity_watchdog_s", behavior.activity_watchdog_s, 0.0),
    ]
    for name, pair, minimum in pairs:
        _check_pair(issues, pair, loc(name), name, minimum=minimum)
    _check_positive(issues, behavior.camera_mean_s, loc("camera_mean_s"), "视角间隔均值")
    _check_positive(issues, behavior.camera_sd_s, loc("camera_sd_s"), "视角间隔标准差")
    _check_positive(issues, behavior.attack_mean_s, loc("attack_mean_s"), "攻击间隔均值")
    _check_positive(issues, behavior.attack_sd_s, loc("attack_sd_s"), "攻击间隔标准差")
    if behavior.camera_sd_s > behavior.camera_mean_s * 0.5:
        _warn(
            issues,
            "range",
            loc("camera_sd_s"),
            f"视角间隔标准差 {behavior.camera_sd_s} 偏大（建议约 0.35×均值 {behavior.camera_mean_s}）",
        )
    if behavior.attack_sd_s > behavior.attack_mean_s * 0.5:
        _warn(
            issues,
            "range",
            loc("attack_sd_s"),
            f"攻击间隔标准差 {behavior.attack_sd_s} 偏大（建议约 0.35×均值 {behavior.attack_mean_s}）",
        )
    _check_probability(issues, behavior.consumable_ratio, loc("consumable_ratio"), "消耗品概率")
    _check_probability(
        issues, behavior.long_stare_probability, loc("long_stare_probability"), "长时间凝视概率"
    )
    _check_probability(issues, behavior.fire_turn_probability, loc("fire_turn_probability"), "开火转向概率")
    if not 0.0 < behavior.fire_sector_deg <= 180.0:
        _error(issues, "range", loc("fire_sector_deg"), f"开火扇区半角应在 (0,180]，当前 {behavior.fire_sector_deg}")
    _check_int_non_negative(
        issues, behavior.consumable_max_per_battle, loc("consumable_max_per_battle"), "消耗品单场上限"
    )
    _check_int_positive(issues, behavior.actions_per_minute_max, loc("actions_per_minute_max"), "每分钟动作上限")
    _check_non_negative(issues, behavior.battle_start_quiet_s, loc("battle_start_quiet_s"), "战斗开始静默时长")
    _check_positive(issues, behavior.max_input_frame_age_s, loc("max_input_frame_age_s"), "感知帧最大年龄")


def _validate_perception(
    issues: list[ValidationIssue], perception: PerceptionConfig, location: str
) -> None:
    def loc(name: str) -> str:
        return _loc(location, f"perception.{name}")

    if not perception.template_scales:
        _error(issues, "range", loc("template_scales"), "模板缩放比例列表不能为空")
    for index, scale in enumerate(perception.template_scales):
        if not math.isfinite(scale) or scale <= 0:
            _error(issues, "range", f"{loc('template_scales')}[{index}]", f"缩放比例必须大于 0，当前 {scale!r}")
    _check_int_positive(
        issues, perception.feature_confirm_frames, loc("feature_confirm_frames"), "状态确认帧数"
    )
    _check_positive(issues, perception.state_unknown_timeout_s, loc("state_unknown_timeout_s"), "状态未知超时")
    if perception.capture_method not in ("auto", "window", "screen"):
        _error(
            issues,
            "enum",
            loc("capture_method"),
            f"未知截屏方式 {perception.capture_method!r}（auto / window / screen）",
        )
    _check_int_positive(issues, perception.orb_features_background, loc("orb_features_background"), "底图特征数")
    _check_int_positive(issues, perception.orb_features_minimap, loc("orb_features_minimap"), "小地图特征数")
    if not 0.0 < perception.orb_ratio_test < 1.0:
        _error(issues, "range", loc("orb_ratio_test"), f"Lowe 比例阈值应在 (0,1)，当前 {perception.orb_ratio_test}")
    _check_positive(issues, perception.ransac_reproj_px, loc("ransac_reproj_px"), "RANSAC 重投影阈值")
    _check_int_positive(issues, perception.min_inliers, loc("min_inliers"), "最少内点数")
    _check_positive(
        issues, perception.max_reprojection_error_px, loc("max_reprojection_error_px"), "重投影误差上限"
    )
    if not 0.0 < perception.map_scale_tolerance <= 1.0:
        _error(
            issues, "range", loc("map_scale_tolerance"), f"缩放容差应在 (0,1]，当前 {perception.map_scale_tolerance}"
        )
    if not 0.0 <= perception.map_rotation_tolerance_deg <= 45.0:
        _error(issues, "range", loc("map_rotation_tolerance_deg"), "旋转容差应在 0～45 度")
    _check_probability(issues, perception.map_candidate_margin, loc("map_candidate_margin"), "候选领先比例")
    _check_positive(issues, perception.map_refresh_s, loc("map_refresh_s"), "地图刷新间隔")
    _check_int_positive(issues, perception.map_identify_retries, loc("map_identify_retries"), "地图识别重试帧数")
    _check_probability(
        issues, perception.map_min_inlier_hull_ratio, loc("map_min_inlier_hull_ratio"), "内点凸包面积比例"
    )
    if not -1.0 <= perception.map_verify_ncc <= 1.0:
        _error(issues, "range", loc("map_verify_ncc"), "NCC 二次验证阈值应在 -1～1")
    _check_non_negative(
        issues, perception.map_refresh_inlier_gain, loc("map_refresh_inlier_gain"), "刷新内点提升比例"
    )
    _check_non_negative(
        issues, perception.map_refresh_translation_px, loc("map_refresh_translation_px"), "刷新平移差上限"
    )
    _validate_death(issues, perception.death, location)


def _validate_death(issues: list[ValidationIssue], death: DeathConfig, location: str) -> None:
    def loc(name: str) -> str:
        return _loc(location, f"perception.death.{name}")

    weights = death.weights
    for signal in sorted(DEATH_SIGNALS):
        if signal not in weights:
            _error(issues, "missing", loc("weights"), f"缺少死亡信号权重: {signal}")
        else:
            value = weights[signal]
            if not math.isfinite(value) or value < 0:
                _error(issues, "range", f"{loc('weights')}.{signal}", f"权重必须是非负数字，当前 {value!r}")
    for signal in weights:
        if signal not in DEATH_SIGNALS:
            _warn(issues, "unknown", f"{loc('weights')}.{signal}", f"未知死亡信号: {signal}")
    total = sum(value for value in weights.values() if math.isfinite(value))
    if weights and abs(total - 1.0) > 0.01:
        _warn(issues, "range", loc("weights"), f"死亡信号权重之和为 {total:.3f}，通常应接近 1.0")
    if not 0.0 < death.score_threshold <= 1.0:
        _error(issues, "range", loc("score_threshold"), f"死亡得分阈值应在 (0,1]，当前 {death.score_threshold}")
    _check_positive(issues, death.stable_s, loc("stable_s"), "死亡综合稳定时间")
    _check_positive(issues, death.strong_stable_s, loc("strong_stable_s"), "强信号稳定时间")
    _check_positive(issues, death.marker_lost_stable_s, loc("marker_lost_stable_s"), "标记丢失稳定时间")
    _check_non_negative(issues, death.ignore_start_s, loc("ignore_start_s"), "开局静默时长")
    _check_non_negative(issues, death.banner_latch_s, loc("banner_latch_s"), "死亡提示锁存时间")


def _validate_allowed_hours(
    issues: list[ValidationIssue], allowed_hours: tuple[str, str] | None, location: str
) -> None:
    if allowed_hours is None:
        return
    start, end = allowed_hours
    for value in (start, end):
        if not HOUR_PATTERN.match(value):
            _error(issues, "format", location, f"时间格式必须是 HH:MM（00:00～23:59），当前 {value!r}")
            return
    if start == end:
        _error(issues, "range", location, f"允许时段的起止时间相同: {start}")
    elif start > end:
        _warn(issues, "range", location, f"允许时段 {start}-{end} 跨天，会话控制器只在开始下一场前检查")


# ---------------------------------------------------------------------------
# 按键映射
# ---------------------------------------------------------------------------


def validate_keymap(
    keymap: KeyMap,
    *,
    location: str = "keys.yaml",
    known_keys: Collection[str] | None = None,
) -> list[ValidationIssue]:
    """按键映射校验；``known_keys`` 由输入模块提供时额外校验按键名合法性。"""
    issues: list[ValidationIssue] = []
    if not keymap.actions:
        _error(issues, "missing", _loc(location, "actions"), "actions 不能为空")
    for name in ("escape", "fire", "full_map"):
        if name not in keymap.actions:
            _error(issues, "missing", f"{_loc(location, 'actions')}.{name}", f"缺少必需的动作映射: {name}")
    seen: dict[str, str] = {}
    for name, key in keymap.actions.items():
        field_location = f"{_loc(location, 'actions')}.{name}"
        if not key:
            _error(issues, "missing", field_location, f"动作 {name} 的按键不能为空")
            continue
        if not KEY_NAME_PATTERN.match(key):
            _error(issues, "format", field_location, f"按键名应是小写字母/数字/下划线，当前 {key!r}")
        if key in seen and seen[key] != name:
            _warn(
                issues,
                "duplicate",
                field_location,
                f"按键 {key!r} 同时映射到动作 {seen[key]!r} 与 {name!r}",
            )
        else:
            seen[key] = name
        if known_keys is not None and key not in known_keys:
            _error(issues, "unknown_key", field_location, f"输入模块不支持按键名: {key!r}")
    _check_positive(issues, keymap.camera_sensitivity, _loc(location, "camera.sensitivity"), "视角灵敏度")
    if not keymap.consumables:
        _warn(issues, "empty", _loc(location, "consumables"), "消耗品列表为空，攻击事件只会开火")
    consumable_keys: set[str] = set()
    for index, slot in enumerate(keymap.consumables):
        field_location = f"{_loc(location, 'consumables')}[{index}]"
        if not slot.key:
            _error(issues, "missing", field_location, "消耗品按键不能为空")
        elif slot.key in consumable_keys:
            _warn(issues, "duplicate", field_location, f"消耗品按键重复: {slot.key!r}")
        else:
            consumable_keys.add(slot.key)
        if not slot.name:
            _warn(issues, "missing", f"{field_location}.name", "消耗品缺少名称，日志里只能看到按键")
    return issues


# ---------------------------------------------------------------------------
# 客户端配置
# ---------------------------------------------------------------------------


def _runtime_feature_ids(allowed_runtime_features: Collection[str]) -> frozenset[str]:
    return frozenset(allowed_runtime_features)


def _validate_steps(
    steps: Sequence[StepDef],
    *,
    label: str,
    location: str,
    known_features: Collection[str],
    runtime_features: Collection[str],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for index, step in enumerate(steps):
        step_location = f"{_loc(location, label)}[{index}]"
        if step.action not in STEP_ACTIONS:
            _error(
                issues,
                "enum",
                f"{step_location}.action",
                f"不支持的步骤动作 {step.action!r}（允许 {sorted(STEP_ACTIONS)}）",
            )
            continue
        if step.timeout_ms <= 0:
            _error(issues, "range", f"{step_location}.timeout_ms", f"超时必须大于 0，当前 {step.timeout_ms}")
        if step.retries < 0:
            _error(issues, "range", f"{step_location}.retries", f"重试次数不能为负，当前 {step.retries}")
        if step.wait_after_ms < 0:
            _error(issues, "range", f"{step_location}.wait_after_ms", "等待时间不能为负")
        if step.interval_ms < 0:
            _error(issues, "range", f"{step_location}.interval_ms", "间隔时间不能为负")
        if step.action == "key":
            if not step.key:
                _error(issues, "missing", f"{step_location}.key", "key 步骤缺少 key 字段")
        elif step.action in {"click_feature", "wait_feature", "wait_feature_absent"}:
            if not step.feature:
                _error(issues, "missing", f"{step_location}.feature", f"{step.action} 步骤缺少 feature 字段")
            elif step.feature not in known_features and step.feature not in runtime_features:
                _error(
                    issues,
                    "unknown_feature",
                    f"{step_location}.feature",
                    f"引用了不存在的界面特征: {step.feature}",
                )
        elif step.action == "wait_ms":
            if step.duration_ms <= 0:
                _error(issues, "range", f"{step_location}.duration_ms", "wait_ms 步骤需要大于 0 的 duration_ms")
        elif step.action == "scroll":
            if step.region is None:
                _error(issues, "missing", f"{step_location}.region", "scroll 步骤缺少滚动区域 region")
            if step.scroll_clicks == 0:
                _error(issues, "range", f"{step_location}.scroll_clicks", "scroll 步骤滚动格数不能为 0")
    return issues


def validate_profile(
    profile: ClientProfile,
    *,
    location: str | None = None,
    allowed_runtime_features: Collection[str] = RUNTIME_FEATURES,
) -> list[ValidationIssue]:
    """客户端配置校验（模板文件属于引用校验）。"""
    issues: list[ValidationIssue] = []
    if location is None:
        location = "profile.yaml" if profile.profile_dir is None else f"{profile.profile_dir.name}/profile.yaml"
    if not profile.client_id:
        _error(issues, "missing", _loc(location, "client_id"), "client_id 不能为空")
    elif profile.profile_dir is not None and profile.profile_dir.name != profile.client_id:
        _error(
            issues,
            "mismatch",
            _loc(location, "client_id"),
            f"client_id={profile.client_id!r} 与目录名 {profile.profile_dir.name!r} 不一致",
        )
    if not profile.display_name:
        _error(issues, "missing", _loc(location, "display_name"), "display_name 不能为空")
    if not profile.language:
        _error(issues, "missing", _loc(location, "language"), "language 不能为空")
    width, height = profile.reference_resolution
    if width <= 0 or height <= 0:
        _error(
            issues,
            "range",
            _loc(location, "reference_resolution"),
            f"参考分辨率必须为正整数，当前 {(width, height)!r}",
        )
    if not profile.window.process_names and not profile.window.title_patterns:
        _error(
            issues,
            "missing",
            _loc(location, "window"),
            "process_names 与 title_patterns 不能同时为空，否则无法定位游戏窗口",
        )

    runtime_features = _runtime_feature_ids(allowed_runtime_features)
    known_features = set(profile.features)
    if not profile.features:
        _error(issues, "empty", _loc(location, "features"), "界面特征表不能为空")
    for feature_id, feature in profile.features.items():
        field_location = f"{_loc(location, 'features')}.{feature_id}"
        if not feature.template:
            continue  # 缺失模板已在加载阶段记 error
        if profile.profile_dir is None:
            _warn(issues, "unknown", field_location, "客户端目录未知，跳过模板文件校验")
            continue
        expected = profile.profile_dir / feature.template
        _check_image_file(expected, issues, f"{field_location}.template")
        if not 0.0 < feature.threshold <= 1.0:
            _error(
                issues,
                "range",
                f"{field_location}.threshold",
                f"匹配阈值应在 (0,1]，当前 {feature.threshold}",
            )
        if not feature.scales:
            _error(issues, "range", f"{field_location}.scales", "缩放比例列表不能为空")
        for index, scale in enumerate(feature.scales):
            if not math.isfinite(scale) or scale <= 0:
                _error(issues, "range", f"{field_location}.scales[{index}]", f"缩放比例必须大于 0，当前 {scale!r}")

    # 状态特征表
    valid_states = {state.value for state in State}
    for state_name, feature_ids in profile.state_features.items():
        field_location = f"{_loc(location, 'state_features')}.{state_name}"
        if state_name not in valid_states:
            _error(issues, "enum", field_location, f"未知状态名: {state_name}（允许 {sorted(valid_states)}）")
        if not feature_ids:
            _warn(issues, "empty", field_location, f"状态 {state_name} 没有配置任何特征")
        for feature_id in feature_ids:
            if feature_id not in known_features and feature_id not in runtime_features:
                _error(
                    issues,
                    "unknown_feature",
                    field_location,
                    f"状态 {state_name} 引用了不存在的界面特征: {feature_id}",
                )

    for name in (
        "start_sequence",
        "start_sequence_full",
        "popup_sequence",
        "result_sequence",
        "ship_selection_sequence",
        "ship_check_sequence",
    ):
        issues.extend(
            _validate_steps(
                getattr(profile, name),
                label=name,
                location=location,
                known_features=known_features,
                runtime_features=runtime_features,
            )
        )
    if profile.clear_loadout_sequence is not None:
        issues.extend(
            _validate_steps(
                profile.clear_loadout_sequence,
                label="clear_loadout_sequence",
                location=location,
                known_features=known_features,
                runtime_features=runtime_features,
            )
        )
    if not profile.start_sequence and not profile.start_sequence_full:
        _warn(issues, "empty", _loc(location, "start_sequence"), "没有配置开局步骤，脚本无法自动进入排队")

    for feature_id in profile.popup_features:
        if feature_id not in known_features and feature_id not in runtime_features:
            _error(
                issues,
                "unknown_feature",
                _loc(location, "popup_features"),
                f"弹窗特征不存在: {feature_id}",
            )
    for feature_id in profile.ship_verify_features:
        if feature_id not in known_features and feature_id not in runtime_features:
            _warn(
                issues,
                "runtime_feature",
                _loc(location, "ship_verify_features"),
                f"船只校验特征 {feature_id} 不在 profile.features 中，需在运行期注册",
            )

    # 小地图标定
    if profile.minimap is None:
        _error(issues, "missing", _loc(location, "minimap"), "未配置小地图，导航无法工作")
    else:
        minimap = profile.minimap
        minimap_loc = _loc(location, "minimap")
        _check_int_non_negative(issues, minimap.border_px, f"{minimap_loc}.border_px", "小地图边框裁边")
        if minimap.frame_feature is not None and minimap.frame_feature not in known_features:
            _error(
                issues,
                "unknown_feature",
                f"{minimap_loc}.frame_feature",
                f"小地图边框特征不存在: {minimap.frame_feature}",
            )
        if not 4 <= minimap.marker_angles <= 72:
            _error(issues, "range", f"{minimap_loc}.marker_angles", "标记角度模板数量应在 4～72")
        if minimap.marker_angles % 8 != 0:
            _warn(
                issues,
                "range",
                f"{minimap_loc}.marker_angles",
                f"标记角度模板数量 {minimap.marker_angles} 不是 8 的整数倍，角度覆盖不完整",
            )
        if not minimap.marker_template:
            _error(
                issues,
                "missing",
                f"{minimap_loc}.marker_template",
                "未配置自身舰船标记模板，无法定位舰船",
            )
        elif profile.profile_dir is not None:
            expected = profile.profile_dir / minimap.marker_template
            _check_image_file(expected, issues, f"{minimap_loc}.marker_template")
        _check_positive(issues, minimap.marker_jump_px, f"{minimap_loc}.marker_jump_px", "标记跳变阈值")
        _check_positive(issues, minimap.marker_lost_s, f"{minimap_loc}.marker_lost_s", "标记丢失时间")
        _validate_hsv_pair(
            issues,
            minimap.marker_hsv_lower,
            minimap.marker_hsv_upper,
            f"{minimap_loc}.marker_hsv",
            required=False,
        )

    # 战斗 HUD
    hud = profile.battle_hud
    hud_loc = _loc(location, "battle_hud")
    if hud.hp_bar_region is None:
        _warn(issues, "missing", f"{hud_loc}.hp_bar_region", "未配置己方血条区域，死亡判定缺少血条信号")
    _validate_hsv_pair(issues, hud.hp_fill_hsv_lower, hud.hp_fill_hsv_upper, f"{hud_loc}.hp_fill_hsv", required=False)
    _check_probability(issues, hud.hp_fill_zero_ratio, f"{hud_loc}.hp_fill_zero_ratio", "血条填充比例阈值")
    # ISS-002（评审 10.2）：颜色无关填充列比例的空条/存活阈值与退出授权参数
    _check_probability(
        issues, hud.hp_bar_empty_max_ratio, f"{hud_loc}.hp_bar_empty_max_ratio", "空条填充比例上限"
    )
    _check_probability(
        issues, hud.hp_bar_alive_min_ratio, f"{hud_loc}.hp_bar_alive_min_ratio", "存活填充比例下限"
    )
    if hud.hp_bar_empty_max_ratio >= hud.hp_bar_alive_min_ratio:
        _error(
            issues,
            "range",
            f"{hud_loc}.hp_bar_alive_min_ratio",
            "存活下限必须大于空条上限："
            f"{hud.hp_bar_alive_min_ratio} <= {hud.hp_bar_empty_max_ratio}",
        )
    _check_int_positive(
        issues, hud.hp_bar_confirm_frames, f"{hud_loc}.hp_bar_confirm_frames", "血条归零确认帧数"
    )
    _check_positive(
        issues, hud.hp_bar_evidence_ttl_s, f"{hud_loc}.hp_bar_evidence_ttl_s", "血条归零证据有效期"
    )
    for feature_id in hud.spectator_features:
        if feature_id not in known_features and feature_id not in runtime_features:
            _error(
                issues,
                "unknown_feature",
                f"{hud_loc}.spectator_features",
                f"观战特征不存在: {feature_id}",
            )
    for name, value in (
        ("death_banner_feature", hud.death_banner_feature),
        ("result_feature", hud.result_feature),
    ):
        if value not in known_features and value not in runtime_features:
            _error(issues, "unknown_feature", f"{hud_loc}.{name}", f"引用的界面特征不存在: {value}")

    # 视角与退出
    _check_positive(issues, profile.camera.sensitivity, _loc(location, "camera.sensitivity"), "视角灵敏度")
    exit_config = profile.exit
    exit_loc = _loc(location, "exit")
    if not exit_config.menu_key:
        _error(issues, "missing", f"{exit_loc}.menu_key", "退出菜单按键不能为空")
    _check_int_non_negative(issues, exit_config.post_click_wait_ms, f"{exit_loc}.post_click_wait_ms", "点击后等待")
    for name, feature_id in (
        ("menu_open_feature", exit_config.menu_open_feature),
        ("return_port_feature", exit_config.return_port_feature),
        ("confirm_feature", exit_config.confirm_feature),
        ("direct_exit_feature", exit_config.direct_exit_feature),
        ("port_feature", exit_config.port_feature),
    ):
        if feature_id and feature_id not in known_features and feature_id not in runtime_features:
            _error(issues, "unknown_feature", f"{exit_loc}.{name}", f"退出流程引用的界面特征不存在: {feature_id}")
    return issues


def _validate_hsv_pair(
    issues: list[ValidationIssue],
    lower: tuple[int, int, int] | None,
    upper: tuple[int, int, int] | None,
    location: str,
    *,
    required: bool,
) -> None:
    if lower is None and upper is None:
        if required:
            _error(issues, "missing", location, "未配置 HSV 颜色范围")
        return
    if lower is None or upper is None:
        _error(issues, "missing", location, "HSV 上下限必须同时配置")
        return
    ranges = ((0, 180), (0, 255), (0, 255))  # OpenCV 的 H 为 0～180
    for index, ((low_limit, high_limit), low_value, high_value) in enumerate(zip(ranges, lower, upper, strict=True)):
        if not low_limit <= low_value <= high_limit or not low_limit <= high_value <= high_limit:
            _error(
                issues,
                "range",
                location,
                f"HSV 第 {index} 个分量超出 {low_limit}～{high_limit}: {low_value}～{high_value}",
            )
        elif low_value > high_value:
            _error(issues, "range", location, f"HSV 第 {index} 个分量下限大于上限: {low_value} > {high_value}")


# ---------------------------------------------------------------------------
# 船只配置
# ---------------------------------------------------------------------------


def validate_ship(ship: ShipProfile, *, location: str | None = None) -> list[ValidationIssue]:
    """船只卡片与空装备槽配置校验。"""
    issues: list[ValidationIssue] = []
    if location is None:
        location = "ship.yaml"
    if not ship.ship_id:
        _error(issues, "missing", _loc(location, "ship_id"), "ship_id 不能为空")
    elif ship.ship_dir is not None and ship.ship_dir.name != ship.ship_id:
        _error(
            issues,
            "mismatch",
            _loc(location, "ship_id"),
            f"ship_id={ship.ship_id!r} 与目录名 {ship.ship_dir.name!r} 不一致",
        )
    if not ship.display_name:
        _error(issues, "missing", _loc(location, "display_name"), "display_name 不能为空")
    if not ship.card_template:
        _error(issues, "missing", _loc(location, "card_template"), "card_template 不能为空")
    elif ship.ship_dir is not None:
        expected = ship.ship_dir / ship.card_template
        _check_image_file(expected, issues, _loc(location, "card_template"))
    if ship.hp_bar_template and ship.ship_dir is not None:
        expected_hp = ship.ship_dir / ship.hp_bar_template
        if not expected_hp.is_file():
            _warn(
                issues,
                "file_missing",
                _loc(location, "hp_bar_template"),
                "船只血条模板缺失：该船将不使用“血条消失”死亡信号（改用死亡提示/结算界面兜底）",
            )
    if ship.scroll_direction not in SCROLL_DIRECTIONS:
        _error(
            issues,
            "enum",
            _loc(location, "scroll_direction"),
            f"滚动方向非法: {ship.scroll_direction!r}（允许 {sorted(SCROLL_DIRECTIONS)}）",
        )
    _check_int_positive(issues, ship.max_scrolls, _loc(location, "max_scrolls"), "最大滚动次数")
    if ship.scroll_clicks == 0:
        _error(issues, "range", _loc(location, "scroll_clicks"), "每次滚动格数不能为 0")
    # --- 已选中证据与选船查找参数（ISS-007 / 实现补充 10.7） ---
    if ship.selected_feature:
        if ship.ship_dir is not None and not (ship.ship_dir / ship.selected_feature).is_file():
            _warn(
                issues,
                "file_missing",
                _loc(location, "selected_feature"),
                "选中态模板缺失（待实机标定）：运行时会保守失败并提示补标定，"
                "不会把“目标船卡片可见”当作已选中",
            )
        _check_probability(
            issues, ship.selected_threshold, _loc(location, "selected_threshold"), "选中态模板阈值"
        )
    if ship.selected_name_feature:
        if ship.ship_dir is not None and not (ship.ship_dir / ship.selected_name_feature).is_file():
            _warn(
                issues,
                "file_missing",
                _loc(location, "selected_name_feature"),
                "右侧详情面板船名模板缺失（待标定）：不能只凭该证据确认已选中，仍可用选中态模板",
            )
        _check_probability(
            issues,
            ship.selected_name_threshold,
            _loc(location, "selected_name_threshold"),
            "右侧船名模板阈值",
        )
        if ship.selected_name_region is None:
            _warn(
                issues,
                "missing",
                _loc(location, "selected_name_region"),
                "未配置右侧船名模板搜索区域：运行时回退到右侧面板默认区域",
            )
    _check_int_positive(
        issues, ship.max_position_scrolls, _loc(location, "max_position_scrolls"), "起点定位最大滚动次数"
    )
    _check_non_negative(issues, ship.scroll_settle_s, _loc(location, "scroll_settle_s"), "滚动后等待时间")
    _check_int_positive(issues, ship.confirm_frames, _loc(location, "confirm_frames"), "点击后确认等待帧数")
    _check_positive(issues, ship.confirm_timeout_s, _loc(location, "confirm_timeout_s"), "点击后确认超时")
    _check_int_positive(issues, ship.max_click_attempts, _loc(location, "max_click_attempts"), "重复点击上限")
    _check_int_positive(
        issues,
        ship.endpoint_stable_scrolls,
        _loc(location, "endpoint_stable_scrolls"),
        "列表端点判定次数",
    )
    _check_probability(
        issues, ship.endpoint_diff_ratio, _loc(location, "endpoint_diff_ratio"), "端点画面差异比例"
    )
    _check_int_non_negative(
        issues, ship.candidate_merge_px, _loc(location, "candidate_merge_px"), "候选去重距离"
    )
    _check_positive(issues, ship.selection_timeout_s, _loc(location, "selection_timeout_s"), "选船查找超时")
    if not ship.loadout_slots:
        _warn(issues, "empty", _loc(location, "loadout_slots"), "未配置空装备槽校验，无法确认未带信号旗/加成卡")
    seen_slots: set[str] = set()
    for index, slot in enumerate(ship.loadout_slots):
        slot_loc = f"{_loc(location, 'loadout_slots')}[{index}]"
        if not slot.slot_id:
            _error(issues, "missing", f"{slot_loc}.slot_id", "slot_id 不能为空")
        elif slot.slot_id in seen_slots:
            _error(issues, "duplicate", f"{slot_loc}.slot_id", f"slot_id 重复: {slot.slot_id}")
        else:
            seen_slots.add(slot.slot_id)
        if not slot.name:
            _warn(issues, "missing", f"{slot_loc}.name", "空装备槽缺少名称")
        if not slot.empty_template:
            _error(issues, "missing", f"{slot_loc}.empty_template", "empty_template 不能为空")
        elif ship.ship_dir is not None:
            expected = ship.ship_dir / slot.empty_template
            _check_image_file(expected, issues, f"{slot_loc}.empty_template")
    return issues


# ---------------------------------------------------------------------------
# 地图包（详细设计 4.7）
# ---------------------------------------------------------------------------


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _inside_bounds(point: tuple[float, float], bounds: tuple[float, float, float, float]) -> bool:
    x_min, y_min, x_max, y_max = bounds
    return x_min <= point[0] <= x_max and y_min <= point[1] <= y_max


def _bounds_valid(bounds: tuple[float, float, float, float]) -> bool:
    x_min, y_min, x_max, y_max = bounds
    return _finite(x_min, y_min, x_max, y_max) and x_max > x_min and y_max > y_min


def _image_bounds_valid(bounds: tuple[float, float, float, float]) -> bool:
    u_min, v_min, u_max, v_max = bounds
    return _finite(u_min, v_min, u_max, v_max) and u_max > u_min and v_max > v_min


def _map_label(pack: MapPack) -> str:
    if pack.pack_dir is not None:
        return f"maps/{pack.pack_dir.name}"
    return f"maps/{pack.map_id}"


def validate_map_pack(pack: MapPack, *, location: str | None = None) -> list[ValidationIssue]:
    """单张地图包校验，覆盖详细设计 4.7 的全部规则（错误/警告分级）。"""
    issues: list[ValidationIssue] = []
    label = location or _map_label(pack)
    if pack.pack_dir is not None and pack.pack_dir.name != pack.map_id:
        _error(
            issues,
            "mismatch",
            f"{label}: map_id",
            f"目录名 {pack.pack_dir.name!r} 与 map_id {pack.map_id!r} 不一致",
        )
    if not pack.name:
        _error(issues, "missing", f"{label}: name", "地图缺少显示名称")
    if pack.background_path is None:
        _error(
            issues,
            "file_missing",
            f"{label}: background.png",
            f"底图缺失: {pack.pack_dir / 'background.png' if pack.pack_dir else 'background.png'}",
        )
    elif pack.background_size is None:
        _error(issues, "file_unreadable", f"{label}: background.png", "底图不是有效 PNG，无法读取尺寸")
    if not _bounds_valid(pack.world_bounds):
        _error(
            issues,
            "range",
            f"{label}: world_bounds",
            f"world_bounds 必须满足 x_max>x_min 且 y_max>y_min 且为有限值，当前 {pack.world_bounds!r}",
        )
    if not _image_bounds_valid(pack.image_bounds):
        _error(
            issues,
            "range",
            f"{label}: image_bounds",
            f"image_bounds 必须满足 u_max>u_min 且 v_max>v_min 且为有限值，当前 {pack.image_bounds!r}",
        )
    if not _finite(*pack.size_meters) or pack.size_meters[0] <= 0 or pack.size_meters[1] <= 0:
        _error(issues, "range", f"{label}: size_meters", f"地图尺寸必须为正数，当前 {pack.size_meters!r}")

    if _bounds_valid(pack.world_bounds):
        x_min, y_min, x_max, y_max = pack.world_bounds
        world_width = x_max - x_min
        world_height = y_max - y_min
        size_width, size_height = pack.size_meters
        if size_width > 0 and abs(world_width - size_width) / size_width > 0.01:
            _warn(
                issues,
                "size_mismatch",
                f"{label}: size_meters",
                f"world_bounds 宽度 {world_width} 与 size_meters 宽度 {size_width} 相差超过 1%",
            )
        if size_height > 0 and abs(world_height - size_height) / size_height > 0.01:
            _warn(
                issues,
                "size_mismatch",
                f"{label}: size_meters",
                f"world_bounds 高度 {world_height} 与 size_meters 高度 {size_height} 相差超过 1%",
            )

    background_size = pack.background_size
    if background_size is not None and _image_bounds_valid(pack.image_bounds):
        u_min, v_min, u_max, v_max = pack.image_bounds
        width, height = background_size
        if u_min < 0 or v_min < 0 or u_max > width or v_max > height:
            _error(
                issues,
                "range",
                f"{label}: image_bounds",
                f"image_bounds {pack.image_bounds!r} 超出底图范围 {width}×{height}",
            )

    _validate_world_to_image(issues, pack, label)
    if pack.image_orientation not in set(ImageOrientation):
        _error(issues, "enum", f"{label}: image_orientation", f"非法底图方向: {pack.image_orientation!r}")

    # --- 出生点规则 ---
    if not pack.spawns:
        _warn(issues, "empty", f"{label}: spawns", "地图没有配置出生点，运行期无法开局")
    seen_spawns: set[str] = set()
    world_bounds = pack.world_bounds
    for spawn in pack.spawns:
        spawn_loc = f"{label}: spawns.{spawn.spawn_id}"
        if spawn.spawn_id in seen_spawns:
            _error(issues, "duplicate", f"{spawn_loc}.spawn_id", f"出生点编号重复: {spawn.spawn_id}")
        else:
            seen_spawns.add(spawn.spawn_id)
        if not spawn.spawn_id:
            _error(issues, "missing", f"{label}: spawns", "出生点缺少编号")
        if _bounds_valid(world_bounds) and not _inside_bounds(spawn.position, world_bounds):
            _error(
                issues,
                "range",
                f"{spawn_loc}.position",
                f"出生点 {spawn.position!r} 超出 world_bounds {world_bounds!r}",
            )
        if not 0.0 <= spawn.facing_deg < 360.0:
            _warn(issues, "range", f"{spawn_loc}.facing_deg", f"朝向应在 0～360 度，当前 {spawn.facing_deg!r}")
    spawn_list = list(pack.spawns)
    for index, first in enumerate(spawn_list):
        for second in spawn_list[index + 1 :]:
            distance = _distance(first.position, second.position)
            if distance < 500.0:
                _warn(
                    issues,
                    "spawn_too_close",
                    f"{label}: spawns",
                    f"出生点 {first.spawn_id} 与 {second.spawn_id} 间距 {distance:.1f} 米小于 500 米，可能识别混淆",
                )

    # --- 航线规则 ---
    if not pack.routes:
        _warn(issues, "empty", f"{label}: routes", "地图没有配置航线，运行期无法导航")
    spawn_positions = {spawn.spawn_id: spawn.position for spawn in pack.spawns}
    seen_routes: set[str] = set()
    for route in pack.routes:
        route_loc = f"{label}: routes.{route.route_id}"
        if route.route_id in seen_routes:
            _error(issues, "duplicate", f"{route_loc}.route_id", f"航线编号重复: {route.route_id}")
        else:
            seen_routes.add(route.route_id)
        if not route.route_id:
            _error(issues, "missing", f"{label}: routes", "航线缺少编号")
        if route.spawn_id not in spawn_positions:
            _error(
                issues,
                "unknown_spawn",
                f"{route_loc}.spawn_id",
                f"航线引用的出生点不存在: {route.spawn_id!r}",
            )
        if not math.isfinite(route.weight) or route.weight < 0:
            _error(issues, "range", f"{route_loc}.weight", f"航线权重必须是非负数字，当前 {route.weight!r}")
        elif route.weight == 0:
            _warn(issues, "zero_weight", f"{route_loc}.weight", "航线权重为 0，运行时不会被选择")
        if len(route.waypoints) < 2:
            _error(
                issues,
                "range",
                f"{route_loc}.waypoints",
                f"航线至少需要 2 个航点，当前 {len(route.waypoints)} 个",
            )
            continue
        waypoints = list(route.waypoints)
        for index, waypoint in enumerate(waypoints):
            if not _finite(*waypoint):
                _error(issues, "range", f"{route_loc}.waypoints[{index}]", f"航点含非有限值: {waypoint!r}")
            elif _bounds_valid(world_bounds) and not _inside_bounds(waypoint, world_bounds):
                _error(
                    issues,
                    "range",
                    f"{route_loc}.waypoints[{index}]",
                    f"航点 {waypoint!r} 超出 world_bounds {world_bounds!r}",
                )
        spawn_position = spawn_positions.get(route.spawn_id)
        if spawn_position is not None and _finite(*waypoints[0]):
            first_distance = _distance(spawn_position, waypoints[0])
            if first_distance > 1500.0:
                _warn(
                    issues,
                    "waypoint_far",
                    f"{route_loc}.waypoints[0]",
                    f"首航点距出生点 {first_distance:.0f} 米，超过 1500 米",
                )
        for index in range(len(waypoints) - 1):
            pair_distance = _distance(waypoints[index], waypoints[index + 1])
            if pair_distance < 200.0:
                _warn(
                    issues,
                    "waypoint_close",
                    f"{route_loc}.waypoints[{index}]",
                    f"航点间距 {pair_distance:.0f} 米小于 200 米",
                )
            if pair_distance > 10000.0:
                _warn(
                    issues,
                    "waypoint_far",
                    f"{route_loc}.waypoints[{index}]",
                    f"航点间距 {pair_distance:.0f} 米超过 10000 米，点击精度可能不足",
                )
    return issues


def _validate_world_to_image(issues: list[ValidationIssue], pack: MapPack, label: str) -> None:
    matrix = pack.world_to_image
    if matrix is None:
        return
    if len(matrix) != 6:
        _error(issues, "length", f"{label}: world_to_image", "world_to_image 需要 6 个数字（2x3 仿射）")
        return
    if abs(matrix[0] * matrix[4] - matrix[1] * matrix[3]) < 1e-12:
        _error(
            issues,
            "world_to_image_invertible",
            f"{label}: world_to_image",
            "world_to_image 不可逆（线性部分行列式为 0）",
        )
        return
    if not (_bounds_valid(pack.world_bounds) and _image_bounds_valid(pack.image_bounds)):
        return
    expected = WorldImageTransform(
        world_bounds=pack.world_bounds,
        image_bounds=pack.image_bounds,
        orientation=pack.image_orientation,
    )
    x_min, y_min, x_max, y_max = pack.world_bounds
    corners = ((x_min, y_min), (x_min, y_max), (x_max, y_min), (x_max, y_max))
    max_difference = 0.0
    for x, y in corners:
        expected_u, expected_v = expected.world_to_image(x, y)
        provided_u, provided_v = apply_affine(affine_to_matrix(matrix), x, y)
        max_difference = max(max_difference, math.hypot(expected_u - provided_u, expected_v - provided_v))
    if max_difference > 1.0:
        _warn(
            issues,
            "world_to_image_conflict",
            f"{label}: world_to_image",
            f"world_to_image 与 bounds+orientation 换算结果相差 {max_difference:.1f} 像素；实现以矩阵为准",
        )


def validate_maps(maps: Mapping[str, MapPack], *, root: Path | None = None) -> list[ValidationIssue]:
    """全部地图包校验；没有任何地图包时记 error（运行前必须至少一张地图）。"""
    issues: list[ValidationIssue] = []
    if not maps:
        _error(issues, "empty", "maps", "未找到任何地图包，至少需要一张完成标定的地图")
        return issues
    seen_ids: set[str] = set()
    for map_id, pack in maps.items():
        label = (
            relative_label(root, pack.pack_dir) if root is not None and pack.pack_dir is not None else _map_label(pack)
        )
        if map_id != pack.map_id:
            _error(
                issues,
                "mismatch",
                f"{label}: map_id",
                f"加载键 {map_id!r} 与 map_id {pack.map_id!r} 不一致",
            )
        if pack.map_id in seen_ids:
            _error(issues, "duplicate", f"{label}: map_id", f"地图编号重复: {pack.map_id}")
        seen_ids.add(pack.map_id)
        issues.extend(validate_map_pack(pack, location=label))
    return issues


# ---------------------------------------------------------------------------
# 账号档案（用户扩展需求：多账号选择与会话间轮换）
# ---------------------------------------------------------------------------


def validate_accounts(
    accounts: AccountsConfig,
    *,
    project_root: Path | None = None,
    default_profile_id: str | None = None,
    location: str = "accounts.yaml",
) -> list[ValidationIssue]:
    """账号档案校验：别名唯一、profile/ship 引用存在、轮换策略与安全约定。"""
    issues: list[ValidationIssue] = []
    seen_aliases: set[str] = set()
    for index, account in enumerate(accounts.accounts):
        account_loc = f"{location}: accounts[{index}]"
        if not account.alias:
            _error(issues, "missing", f"{account_loc}.alias", "账号缺少本地别名 alias")
        elif account.alias in seen_aliases:
            _error(issues, "duplicate", f"{account_loc}.alias", f"账号别名重复: {account.alias!r}")
        else:
            seen_aliases.add(account.alias)
        if not account.display_name:
            _warn(issues, "missing", f"{account_loc}.display_name", "账号缺少显示名称（日志只显示别名）")
        if account.daily_battle_cap is not None:
            _check_int_positive(
                issues, account.daily_battle_cap, f"{account_loc}.daily_battle_cap", "账号每日场次上限"
            )
        if account.max_battles_per_session is not None:
            _check_int_positive(
                issues,
                account.max_battles_per_session,
                f"{account_loc}.max_battles_per_session",
                "账号单次会话场次上限",
            )
        _validate_allowed_hours(issues, account.allowed_hours, f"{account_loc}.allowed_hours")
        profile_id = account.profile or default_profile_id
        if project_root is not None and account.profile:
            profile_path = project_root / "profiles" / account.profile / "profile.yaml"
            if not profile_path.is_file():
                _error(
                    issues,
                    "unknown_profile",
                    f"{account_loc}.profile",
                    f"账号引用的客户端配置不存在: {account.profile!r}（缺少 {profile_path}）",
                )
        if project_root is not None and account.ship_id and profile_id:
            ship_path = project_root / "profiles" / profile_id / "ships" / account.ship_id / "ship.yaml"
            if not ship_path.is_file():
                _error(
                    issues,
                    "unknown_ship",
                    f"{account_loc}.ship_id",
                    f"账号引用的船只配置不存在: {account.ship_id!r}（缺少 {ship_path}）",
                )

    rotation_loc = f"{location}: rotation"
    if accounts.rotation_mode not in ROTATION_MODES:
        _error(
            issues,
            "unsupported",
            f"{rotation_loc}.mode",
            f"v1 只支持 round_robin 轮换模式（当前 {accounts.rotation_mode!r}）",
        )
    if accounts.rotation_enabled:
        _check_int_positive(
            issues,
            accounts.max_battles_per_account,
            f"{rotation_loc}.max_battles_per_account",
            "每账号轮换场次",
        )
        if not accounts.enabled_accounts():
            _error(
                issues,
                "no_enabled_accounts",
                f"{location}: accounts",
                "启用账号轮换时至少需要一个 enabled=true 的账号档案",
            )
    return issues


# ---------------------------------------------------------------------------
# 组合校验
# ---------------------------------------------------------------------------


def _profile_label(config: AppConfig) -> str:
    profile = config.profile
    if profile.profile_dir is not None:
        return relative_label(config.project_root, profile.profile_dir / "profile.yaml")
    return "profile.yaml"


def _keys_label(config: AppConfig) -> str:
    profile = config.profile
    if profile.profile_dir is not None:
        return relative_label(config.project_root, profile.profile_dir / profile.keys_file)
    return profile.keys_file


def _ship_label(config: AppConfig) -> str:
    ship = config.ship
    if ship.ship_dir is not None:
        return relative_label(config.project_root, ship.ship_dir / "ship.yaml")
    return "ship.yaml"


def validate_app(config: AppConfig) -> list[ValidationIssue]:
    """完整项目校验：全局配置 + 账号档案 + 按键 + 客户端 + 船只 + 地图包 + 跨文件引用。"""
    issues: list[ValidationIssue] = []
    root = config.project_root
    issues.extend(validate_global_config(config.global_config))
    accounts = config.global_config.accounts
    accounts_label = relative_label(root, root / accounts.accounts_file)
    issues.extend(
        validate_accounts(
            accounts,
            project_root=root,
            default_profile_id=config.profile.client_id,
            location=accounts_label,
        )
    )
    if config.account is not None:
        if accounts.get(config.account.alias) is None:
            _error(
                issues,
                "account_mismatch",
                f"{accounts_label}: accounts.{config.account.alias}",
                f"AppConfig.account={config.account.alias!r} 不在账号档案中",
            )
        elif not config.account.enabled:
            _warn(
                issues,
                "disabled_account",
                f"{accounts_label}: accounts.{config.account.alias}.enabled",
                f"账号 {config.account.alias!r} 已禁用（enabled=false），仍被显式指定运行",
            )
    issues.extend(validate_keymap(config.keymap, location=_keys_label(config)))

    loadout_features = {
        f"{LOADOUT_FEATURE_PREFIX}{slot.slot_id}": slot for slot in config.ship.loadout_slots
    }
    allowed_runtime = RUNTIME_FEATURES | set(loadout_features)
    issues.extend(
        validate_profile(
            config.profile,
            location=_profile_label(config),
            allowed_runtime_features=allowed_runtime,
        )
    )
    ship_issues = validate_ship(config.ship, location=_ship_label(config))
    if config.profile.ship_check_sequence:
        # 信号旗已由 profile.ship_check_sequence（切装备页校验“信号旗 0/8”）负责，
        # 此时 ship.yaml 不再配置 loadout_slots 属预期，去掉这条误导性 warning
        ship_issues = [
            issue
            for issue in ship_issues
            if not (issue.code == "empty" and issue.location.endswith("loadout_slots"))
        ]
    issues.extend(ship_issues)
    issues.extend(validate_maps(config.maps, root=root))

    # --- 跨文件引用 ---
    known_features = set(config.profile.features) | set(allowed_runtime)
    map_config = config.global_config.map
    for feature_id in config.profile.loadout_verify_features:
        if feature_id not in loadout_features and feature_id not in known_features:
            _warn(
                issues,
                "runtime_feature",
                f"{_profile_label(config)}: loadout_verify_features",
                f"{feature_id!r} 未对应任何空装备槽（期望 {sorted(loadout_features)}），需在运行期注册",
            )
    actions = config.keymap.actions
    profile_loc = _profile_label(config)
    if config.profile.exit.menu_key and config.profile.exit.menu_key not in actions:
        _error(
            issues,
            "unknown_key",
            f"{profile_loc}: exit.menu_key",
            f"退出菜单按键 {config.profile.exit.menu_key!r} 未在 keys.yaml.actions 中定义",
        )
    toggle_key = config.global_config.navigation.full_map_toggle_key
    if toggle_key and toggle_key not in actions:
        _error(
            issues,
            "unknown_key",
            "config.yaml: navigation.full_map_toggle_key",
            f"全屏地图动作 {toggle_key!r} 未在 keys.yaml.actions 中定义",
        )
    if map_config.forced_map_id and map_config.forced_map_id not in config.maps:
        _error(
            issues,
            "unknown_map",
            "config.yaml: map.forced_map_id",
            f"强制地图 {map_config.forced_map_id!r} 未加载（已加载 {sorted(config.maps)}）",
        )
    if map_config.default_spawn_id:
        spawn_ids: set[str] = set()
        if map_config.forced_map_id and map_config.forced_map_id in config.maps:
            spawn_ids = {spawn.spawn_id for spawn in config.maps[map_config.forced_map_id].spawns}
        else:
            for pack in config.maps.values():
                spawn_ids.update(spawn.spawn_id for spawn in pack.spawns)
        if spawn_ids and map_config.default_spawn_id not in spawn_ids:
            _error(
                issues,
                "unknown_spawn",
                "config.yaml: map.default_spawn_id",
                f"默认出生点 {map_config.default_spawn_id!r} 不存在（可用 {sorted(spawn_ids)}）",
            )
    configured_ship_id = config.global_config.ship.ship_id
    if configured_ship_id and config.ship.ship_id and configured_ship_id != config.ship.ship_id:
        _warn(
            issues,
            "override",
            "config.yaml: ship.ship_id",
            f"实际加载的船只 {config.ship.ship_id!r} 与全局配置 {configured_ship_id!r} 不同（命令行覆盖）",
        )
    if config.profile.camera.sensitivity != config.keymap.camera_sensitivity:
        _warn(
            issues,
            "mismatch",
            f"{profile_loc}: camera.sensitivity",
            f"profile.camera.sensitivity={config.profile.camera.sensitivity} 与 "
            f"keys.yaml camera.sensitivity={config.keymap.camera_sensitivity} 不一致",
        )
    if config.profile.camera.relative != config.keymap.camera_relative:
        _warn(
            issues,
            "mismatch",
            f"{profile_loc}: camera.relative",
            "profile.camera.relative 与 keys.yaml camera.relative 不一致",
        )
    # autopilot 模式不需要地图识别与航线标定：地图包可为空，自身标记模板可缺失（仅影响
    # marker_lost 死亡信号与对称目标计算，缺失时回退小地图中心）。
    if config.global_config.navigation.mode == "autopilot":
        issues = _downgrade_autopilot_requirements(issues)
    return issues


def _downgrade_autopilot_requirements(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    """autopilot 模式下把“地图包/自身标记”相关 error 降级为 warning。"""
    from ..common.errors import Severity

    downgraded: list[ValidationIssue] = []
    for issue in issues:
        if issue.level is not Severity.ERROR:
            downgraded.append(issue)
            continue
        if issue.code == "empty" and issue.location == "maps":
            downgraded.append(
                ValidationIssue(
                    Severity.WARNING,
                    issue.code,
                    issue.location,
                    "autopilot 模式不需要地图包（仅 waypoints 模式需要标定地图与航线）",
                )
            )
            continue
        if "marker_template" in issue.location:
            downgraded.append(
                ValidationIssue(
                    Severity.WARNING,
                    issue.code,
                    issue.location,
                    "autopilot 模式缺少自身标记模板：无法计算对称敌区目标，将回退小地图中心；"
                    "marker_lost 死亡信号也不可用",
                )
            )
            continue
        downgraded.append(issue)
    return downgraded


def validate_project(
    root: Path | str,
    profile_id: str = "cn360",
    *,
    ship_id: str | None = None,
    project: LoadedProject | None = None,
    account: str | None = None,
) -> ValidationReport:
    """校验整个工程：宽松加载 + 全部校验规则，返回去重后的问题清单。"""
    loaded = (
        project
        if project is not None
        else load_project(root, profile_id, ship_id=ship_id, account=account)
    )
    issues = list(loaded.issues)
    issues.extend(validate_app(loaded.config))
    deduped: list[ValidationIssue] = []
    seen: set[tuple[str, str, str, str]] = set()
    for issue in issues:
        key = (issue.level.value, issue.code, issue.location, issue.message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    return ValidationReport(issues=tuple(deduped))
