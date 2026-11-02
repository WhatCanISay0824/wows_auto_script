"""配置与地图包加载（详细设计第 3、4 章；实现口径见实现补充文档）。

职责边界：
- 只读取 YAML、做类型转换并按 ``src/config/models.py`` 构造数据模型；
- 结构/范围/引用校验放在 ``src.config.validate``，本模块只把“文件读不了、必填字段
  缺失、类型无法转换”这类导致解析无法继续的问题记进 ``ValidationIssue``；
- 引用文件（模板、底图、船只卡片）缺失不在此报错，由校验模块统一给出 error；
  已存在的文件路径填进模型的 ``template_path`` / ``background_path`` / ``card_path``；
- 相对路径按各自配置文件的所属目录解析：全局 ``paths.*`` 按项目根目录，
  客户端配置的模板按 profile 目录，船只模板按船只目录，地图底图按地图包目录。

设计说明（实现假设）：
- ``load_app`` 是严格入口：加载后执行完整校验，有 error 时抛 ``ConfigError``；
  ``load_project`` 是宽松入口，返回模型与解析期问题清单，供校验工具与测试使用；
- ``config_hash`` 取 SHA-256 摘要前 16 位（详细设计 11.2），参与计算的规范化内容
  是“全局配置有效值 + 所有配置 YAML 的内容哈希（按相对路径排序）”。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from ..common.coords import ImageOrientation
from ..common.errors import ConfigError, Severity, ValidationIssue
from ..common.types import NormalizedRect
from .models import (
    AccountConfig,
    AccountsConfig,
    AppConfig,
    BattleHudConfig,
    BehaviorConfig,
    CameraConfig,
    ClientProfile,
    ConsumableSlot,
    DeathConfig,
    ExitConfig,
    FeatureDef,
    GlobalConfig,
    InputConfig,
    KeyMap,
    LoadoutSlotDef,
    LoggingConfig,
    MapConfig,
    MapPack,
    MinimapConfig,
    NavigationConfig,
    OverloadConfig,
    PathsConfig,
    PerceptionConfig,
    Route,
    RuntimeConfig,
    SafetyConfig,
    SessionConfig,
    ShipConfig,
    ShipProfile,
    SpawnPoint,
    StepDef,
    TimeoutsConfig,
    WindowConfig,
)

# 步骤序列允许的动作（详细设计 10.1 + 实现补充 P1-7）
STEP_ACTIONS: frozenset[str] = frozenset(
    {"key", "click_feature", "wait_feature", "wait_feature_absent", "wait_ms", "scroll"}
)
# 船只列表滚动方向
SCROLL_DIRECTIONS: frozenset[str] = frozenset({"up", "down"})
# 死亡信号名（详细设计 5.9）
DEATH_SIGNALS: frozenset[str] = frozenset(
    {"hp_bar_absent", "hp_zero", "banner", "marker_lost", "spectator_ui"}
)
# 支持的输入后端（详细设计 9.6）
INPUT_BACKENDS: frozenset[str] = frozenset({"sendinput", "setcursor"})
# 账号轮换模式（v1 仅 round_robin）
ROTATION_MODES: frozenset[str] = frozenset({"round_robin"})
# 账号档案文件禁止出现的凭据字段（CLAUDE.md：不保存账号、密码、令牌）
# 键名精确匹配清单 + 键名包含的敏感词
FORBIDDEN_ACCOUNT_KEYS: frozenset[str] = frozenset(
    {
        "username",
        "user_name",
        "login",
        "login_name",
        "account_name",
        "real_account",
        "email",
        "phone",
        "credential",
        "credentials",
        "cookie",
        "cookies",
        "auth",
        "apikey",
        "api_key",
        "secret_key",
        "账号",
        "用户名",
        "密码",
        "邮箱",
        "验证码",
    }
)
FORBIDDEN_ACCOUNT_KEY_PARTS: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "验证码",
    "密码",
    "令牌",
)
# 过载保护配置（config.yaml overload 段）：未知键按 error 处理，避免拼写错误被静默忽略
OVERLOAD_KEYS: frozenset[str] = frozenset(item.name for item in fields(OverloadConfig))
OVERLOAD_INT_FIELDS: frozenset[str] = frozenset(
    {"starvation_pause_events", "starvation_stop_events", "auto_resume_max"}
)

IssueSink = list[ValidationIssue]
SourceSink = MutableMapping[str, str]


# ---------------------------------------------------------------------------
# 问题记录与基础类型转换
# ---------------------------------------------------------------------------


def _add(
    issues: IssueSink,
    level: Severity,
    code: str,
    location: str,
    message: str,
) -> None:
    issues.append(ValidationIssue(level=level, code=code, location=location, message=message))


def _error(issues: IssueSink, code: str, location: str, message: str) -> None:
    _add(issues, Severity.ERROR, code, location, message)


def _warn(issues: IssueSink, code: str, location: str, message: str) -> None:
    _add(issues, Severity.WARNING, code, location, message)


def relative_label(root: Path, path: Path) -> str:
    """把路径转成相对项目根目录的 POSIX 风格文本，用于问题定位与 sources 记录。"""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def resolve_path(value: str | Path, base: Path) -> Path:
    """相对路径按 base 解析为绝对路径；绝对路径原样返回。"""
    path = Path(value)
    if path.is_absolute():
        return path
    return (base / path).resolve()


def probe_png_size(path: Path) -> tuple[int, int] | None:
    """只读 PNG 文件头，返回 (宽, 高)；不是有效 PNG 时返回 None。"""
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def _read_yaml(path: Path, issues: IssueSink, location: str) -> Mapping[str, Any] | None:
    """读取 YAML 映射；失败记 error 并返回 None。"""
    if not path.is_file():
        _error(issues, "file_missing", location, f"配置文件不存在: {path}")
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _error(issues, "file_unreadable", location, f"配置文件读取失败: {exc}")
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        _error(issues, "yaml_syntax", location, f"YAML 解析失败: {exc}")
        return None
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        _error(issues, "yaml_type", location, "顶层结构必须是映射（mapping）")
        return None
    return data


def _section(
    raw: Mapping[str, Any],
    key: str,
    issues: IssueSink,
    location: str,
) -> Mapping[str, Any]:
    """取子映射；缺失返回空映射，类型错误记 error。"""
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        _error(issues, "type", f"{location}.{key}", "必须是映射（mapping）")
        return {}
    return value


def _optional_section(
    raw: Mapping[str, Any],
    key: str,
    issues: IssueSink,
    location: str,
) -> Mapping[str, Any] | None:
    """取可选子映射；缺失或 null 返回 None。"""
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        _error(issues, "type", f"{location}.{key}", "必须是映射（mapping）")
        return None
    return value


def _str_value(
    raw: Mapping[str, Any],
    key: str,
    default: str,
    issues: IssueSink,
    location: str,
) -> str:
    value = raw.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        _error(issues, "type", f"{location}.{key}", f"必须是字符串，实际为 {type(value).__name__}")
        return default
    return value


def _opt_str(
    raw: Mapping[str, Any],
    key: str,
    default: str | None,
    issues: IssueSink,
    location: str,
) -> str | None:
    value = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        _error(issues, "type", f"{location}.{key}", f"必须是字符串或 null，实际为 {type(value).__name__}")
        return default
    return value


def _int_value(
    raw: Mapping[str, Any],
    key: str,
    default: int,
    issues: IssueSink,
    location: str,
) -> int:
    value = raw.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        _error(issues, "type", f"{location}.{key}", f"必须是整数，实际为 {value!r}")
        return default
    return value


def _float_value(
    raw: Mapping[str, Any],
    key: str,
    default: float,
    issues: IssueSink,
    location: str,
) -> float:
    value = raw.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _error(issues, "type", f"{location}.{key}", f"必须是数字，实际为 {value!r}")
        return default
    number = float(value)
    if not math.isfinite(number):
        _error(issues, "range", f"{location}.{key}", f"必须是有限数字，实际为 {value!r}")
        return default
    return number


def _bool_value(
    raw: Mapping[str, Any],
    key: str,
    default: bool,
    issues: IssueSink,
    location: str,
) -> bool:
    value = raw.get(key, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        _error(issues, "type", f"{location}.{key}", f"必须是布尔值，实际为 {value!r}")
        return default
    return value


def _numbers(
    raw: Mapping[str, Any],
    key: str,
    default: tuple[float, ...] | None,
    issues: IssueSink,
    location: str,
) -> tuple[float, ...] | None:
    """取数字序列；元素不全是数字时记 error 并返回 default。"""
    value = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, Sequence) or isinstance(value, str):
        _error(issues, "type", f"{location}.{key}", "必须是数字列表")
        return default
    items: list[float] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            _error(issues, "type", f"{location}.{key}[{index}]", "必须是数字")
            return default
        number = float(item)
        if not math.isfinite(number):
            _error(issues, "range", f"{location}.{key}[{index}]", f"必须是有限数字，实际为 {item!r}")
            return default
        items.append(number)
    return tuple(items)


def _float_pair(
    raw: Mapping[str, Any],
    key: str,
    default: tuple[float, float],
    issues: IssueSink,
    location: str,
) -> tuple[float, float]:
    values = _numbers(raw, key, default, issues, location)
    if values is None or len(values) != 2:
        if values is not None:
            _error(issues, "length", f"{location}.{key}", "需要 2 个数字")
        return default
    return (values[0], values[1])


def _int_pair(
    raw: Mapping[str, Any],
    key: str,
    default: tuple[int, int],
    issues: IssueSink,
    location: str,
) -> tuple[int, int]:
    value = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, Sequence) or isinstance(value, str):
        _error(issues, "type", f"{location}.{key}", "必须是整数列表")
        return default
    items: list[int] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int):
            _error(issues, "type", f"{location}.{key}[{index}]", "必须是整数")
            return default
        items.append(item)
    if len(items) != 2:
        _error(issues, "length", f"{location}.{key}", "需要 2 个整数")
        return default
    return (items[0], items[1])


def _float_quad(
    raw: Mapping[str, Any],
    key: str,
    default: tuple[float, float, float, float],
    issues: IssueSink,
    location: str,
) -> tuple[float, float, float, float]:
    values = _numbers(raw, key, default, issues, location)
    if values is None:
        return default
    if len(values) != 4:
        _error(issues, "length", f"{location}.{key}", "需要 4 个数字")
        return default
    return (values[0], values[1], values[2], values[3])


def _str_list(
    raw: Mapping[str, Any],
    key: str,
    default: tuple[str, ...],
    issues: IssueSink,
    location: str,
) -> tuple[str, ...]:
    value = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, Sequence) or isinstance(value, str):
        _error(issues, "type", f"{location}.{key}", "必须是字符串列表")
        return default
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            _error(issues, "type", f"{location}.{key}[{index}]", "必须是字符串")
            return default
        items.append(item)
    return tuple(items)


def _str_float_mapping(
    raw: Mapping[str, Any],
    key: str,
    default: Mapping[str, float],
    issues: IssueSink,
    location: str,
) -> Mapping[str, float]:
    value = raw.get(key)
    if value is None:
        return dict(default)
    if not isinstance(value, Mapping):
        _error(issues, "type", f"{location}.{key}", "必须是映射（mapping）")
        return dict(default)
    result: dict[str, float] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not isinstance(item, (int, float)) or isinstance(item, bool):
            _error(issues, "type", f"{location}.{key}.{name}", "权重必须是数字")
            continue
        number = float(item)
        if not math.isfinite(number):
            _error(issues, "range", f"{location}.{key}.{name}", f"权重必须是有限数字，实际为 {item!r}")
            continue
        result[name] = number
    return result


def _normalized_rect(
    raw: Mapping[str, Any],
    key: str,
    default: NormalizedRect | None,
    issues: IssueSink,
    location: str,
) -> NormalizedRect | None:
    value = raw.get(key)
    if value is None:
        return default
    try:
        return NormalizedRect.from_list(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        _error(issues, "range", f"{location}.{key}", f"归一化矩形非法: {exc}")
        return default


def _int_triple(
    raw: Mapping[str, Any],
    key: str,
    default: tuple[int, int, int] | None,
    issues: IssueSink,
    location: str,
) -> tuple[int, int, int] | None:
    value = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 3:
        _error(issues, "type", f"{location}.{key}", "需要 3 个整数（HSV 分量）")
        return default
    items: list[int] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int):
            _error(issues, "type", f"{location}.{key}[{index}]", "必须是整数")
            return default
        items.append(item)
    return (items[0], items[1], items[2])


def _record_source(sources: SourceSink | None, key: str, source: str) -> None:
    if sources is not None:
        sources[key] = source


def _is_forbidden_account_key(key: str) -> bool:
    lowered = key.strip().lower()
    if lowered in FORBIDDEN_ACCOUNT_KEYS:
        return True
    return any(part in lowered for part in FORBIDDEN_ACCOUNT_KEY_PARTS)


def _scan_forbidden_credentials(value: Any, issues: IssueSink, location: str) -> bool:
    """递归扫描账号档案，发现密码/令牌/真实账号字段时记 error 并返回 True。"""
    found = False
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_location = f"{location}.{key}"
            if isinstance(key, str) and _is_forbidden_account_key(key):
                _error(
                    issues,
                    "credential_field",
                    key_location,
                    "账号档案禁止保存真实账号/密码/令牌等凭据字段（CLAUDE.md 安全约定），请删除该字段",
                )
                found = True
            if _scan_forbidden_credentials(item, issues, key_location):
                found = True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            if _scan_forbidden_credentials(item, issues, f"{location}[{index}]"):
                found = True
    return found


# ---------------------------------------------------------------------------
# 全局配置（详细设计 3.3）
# ---------------------------------------------------------------------------


def _resolved_default_paths(root: Path) -> PathsConfig:
    """配置文件缺失时使用的默认路径（仍按项目根目录解析为绝对路径）。"""
    defaults = PathsConfig()
    return PathsConfig(
        data_dir=resolve_path(defaults.data_dir, root),
        log_dir=resolve_path(defaults.log_dir, root),
        screenshot_dir=resolve_path(defaults.screenshot_dir, root),
        maps_dir=resolve_path(defaults.maps_dir, root),
        profiles_dir=resolve_path(defaults.profiles_dir, root),
    )


def load_global(
    path: Path | str,
    *,
    project_root: Path | str | None = None,
    issues: IssueSink | None = None,
    sources: SourceSink | None = None,
    location: str | None = None,
) -> GlobalConfig:
    """加载全局配置 config.yaml（宽松模式：问题记入 issues，返回可用模型）。"""
    collected: IssueSink = issues if issues is not None else []
    config_path = Path(path)
    root = Path(project_root).resolve() if project_root is not None else config_path.resolve().parent
    label = location or relative_label(root, config_path)
    raw = _read_yaml(config_path, collected, label)
    if raw is None:
        return GlobalConfig(
            paths=_resolved_default_paths(root), project_root=root, config_path=config_path
        )
    defaults = GlobalConfig()

    def section(name: str) -> Mapping[str, Any]:
        data = _section(raw, name, collected, label)
        if data and sources is not None:
            for key in data:
                _record_source(sources, f"{name}.{key}", label)
        return data

    runtime_raw = section("runtime")
    runtime = RuntimeConfig(
        loop_hz=_int_value(runtime_raw, "loop_hz", defaults.runtime.loop_hz, collected, f"{label}: runtime"),
        capture_hz=_float_value(
            runtime_raw, "capture_hz", defaults.runtime.capture_hz, collected, f"{label}: runtime"
        ),
        debug=_bool_value(runtime_raw, "debug", defaults.runtime.debug, collected, f"{label}: runtime"),
        log_level=_str_value(
            runtime_raw, "log_level", defaults.runtime.log_level, collected, f"{label}: runtime"
        ),
    )

    paths_raw = section("paths")
    paths = PathsConfig(
        data_dir=resolve_path(
            _str_value(paths_raw, "data_dir", str(defaults.paths.data_dir), collected, f"{label}: paths"),
            root,
        ),
        log_dir=resolve_path(
            _str_value(paths_raw, "log_dir", str(defaults.paths.log_dir), collected, f"{label}: paths"),
            root,
        ),
        screenshot_dir=resolve_path(
            _str_value(
                paths_raw, "screenshot_dir", str(defaults.paths.screenshot_dir), collected, f"{label}: paths"
            ),
            root,
        ),
        maps_dir=resolve_path(
            _str_value(paths_raw, "maps_dir", str(defaults.paths.maps_dir), collected, f"{label}: paths"),
            root,
        ),
        profiles_dir=resolve_path(
            _str_value(paths_raw, "profiles_dir", str(defaults.paths.profiles_dir), collected, f"{label}: paths"),
            root,
        ),
    )

    map_raw = section("map")
    map_config = MapConfig(
        auto_identify=_bool_value(map_raw, "auto_identify", defaults.map.auto_identify, collected, f"{label}: map"),
        forced_map_id=_opt_str(map_raw, "forced_map_id", defaults.map.forced_map_id, collected, f"{label}: map"),
        spawn_tolerance_m=_float_value(
            map_raw, "spawn_tolerance_m", defaults.map.spawn_tolerance_m, collected, f"{label}: map"
        ),
        default_spawn_id=_opt_str(
            map_raw, "default_spawn_id", defaults.map.default_spawn_id, collected, f"{label}: map"
        ),
        expected_minimap_scale=(
            None
            if map_raw.get("expected_minimap_scale") is None
            else _float_value(
                map_raw,
                "expected_minimap_scale",
                defaults.map.expected_minimap_scale or 1.0,
                collected,
                f"{label}: map",
            )
        ),
    )

    navigation_raw = section("navigation")
    nav_loc = f"{label}: navigation"
    navigation = NavigationConfig(
        surface=_str_value(navigation_raw, "surface", defaults.navigation.surface, collected, nav_loc),
        mode=_str_value(navigation_raw, "mode", defaults.navigation.mode, collected, nav_loc),
        autopilot_target=_str_value(
            navigation_raw, "autopilot_target", defaults.navigation.autopilot_target, collected, nav_loc
        ),
        autopilot_reclick_s=_float_pair(
            navigation_raw,
            "autopilot_reclick_s",
            defaults.navigation.autopilot_reclick_s,
            collected,
            nav_loc,
        ),
        autopilot_overshoot=_float_value(
            navigation_raw,
            "autopilot_overshoot",
            defaults.navigation.autopilot_overshoot,
            collected,
            nav_loc,
        ),
        autopilot_offset=_float_pair(
            navigation_raw,
            "autopilot_offset",
            defaults.navigation.autopilot_offset,
            collected,
            nav_loc,
        ),
        arrival_m=_float_value(navigation_raw, "arrival_m", defaults.navigation.arrival_m, collected, nav_loc),
        overshoot_m=_float_value(
            navigation_raw, "overshoot_m", defaults.navigation.overshoot_m, collected, nav_loc
        ),
        overshoot_cos_threshold=_float_value(
            navigation_raw,
            "overshoot_cos_threshold",
            defaults.navigation.overshoot_cos_threshold,
            collected,
            nav_loc,
        ),
        click_retry=_int_value(
            navigation_raw, "click_retry", defaults.navigation.click_retry, collected, nav_loc
        ),
        click_confirm_first_s=_float_value(
            navigation_raw,
            "click_confirm_first_s",
            defaults.navigation.click_confirm_first_s,
            collected,
            nav_loc,
        ),
        click_confirm_s=_float_value(
            navigation_raw, "click_confirm_s", defaults.navigation.click_confirm_s, collected, nav_loc
        ),
        move_confirm_px=_float_value(
            navigation_raw, "move_confirm_px", defaults.navigation.move_confirm_px, collected, nav_loc
        ),
        click_min_interval_s=_float_value(
            navigation_raw,
            "click_min_interval_s",
            defaults.navigation.click_min_interval_s,
            collected,
            nav_loc,
        ),
        retry_interval_s=_float_value(
            navigation_raw, "retry_interval_s", defaults.navigation.retry_interval_s, collected, nav_loc
        ),
        stuck_window_s=_float_value(
            navigation_raw, "stuck_window_s", defaults.navigation.stuck_window_s, collected, nav_loc
        ),
        stuck_distance_px=_float_value(
            navigation_raw, "stuck_distance_px", defaults.navigation.stuck_distance_px, collected, nav_loc
        ),
        max_skipped=_int_value(
            navigation_raw, "max_skipped", defaults.navigation.max_skipped, collected, nav_loc
        ),
        ship_speed_window_s=_float_value(
            navigation_raw,
            "ship_speed_window_s",
            defaults.navigation.ship_speed_window_s,
            collected,
            nav_loc,
        ),
        ship_speed_samples=_int_value(
            navigation_raw, "ship_speed_samples", defaults.navigation.ship_speed_samples, collected, nav_loc
        ),
        ship_lost_degrade_s=_float_value(
            navigation_raw,
            "ship_lost_degrade_s",
            defaults.navigation.ship_lost_degrade_s,
            collected,
            nav_loc,
        ),
        ship_lost_abandon_s=_float_value(
            navigation_raw,
            "ship_lost_abandon_s",
            defaults.navigation.ship_lost_abandon_s,
            collected,
            nav_loc,
        ),
        route_end_action=_str_value(
            navigation_raw, "route_end_action", defaults.navigation.route_end_action, collected, nav_loc
        ),
        full_map_rect=_normalized_rect(
            navigation_raw, "full_map_rect", defaults.navigation.full_map_rect, collected, nav_loc
        ),
        full_map_toggle_key=_str_value(
            navigation_raw,
            "full_map_toggle_key",
            defaults.navigation.full_map_toggle_key,
            collected,
            nav_loc,
        ),
        full_map_open_wait_s=_float_value(
            navigation_raw,
            "full_map_open_wait_s",
            defaults.navigation.full_map_open_wait_s,
            collected,
            nav_loc,
        ),
        full_map_close_wait_s=_float_value(
            navigation_raw,
            "full_map_close_wait_s",
            defaults.navigation.full_map_close_wait_s,
            collected,
            nav_loc,
        ),
        full_map_verify_s=_float_value(
            navigation_raw,
            "full_map_verify_s",
            defaults.navigation.full_map_verify_s,
            collected,
            nav_loc,
        ),
        full_map_retry=_int_value(
            navigation_raw,
            "full_map_retry",
            defaults.navigation.full_map_retry,
            collected,
            nav_loc,
        ),
        autopilot_verify_min_move_px=_float_value(
            navigation_raw,
            "autopilot_verify_min_move_px",
            defaults.navigation.autopilot_verify_min_move_px,
            collected,
            nav_loc,
        ),
        autopilot_verify_min_speed=_float_value(
            navigation_raw,
            "autopilot_verify_min_speed",
            defaults.navigation.autopilot_verify_min_speed,
            collected,
            nav_loc,
        ),
        autopilot_verify_feature=_opt_str(
            navigation_raw,
            "autopilot_verify_feature",
            defaults.navigation.autopilot_verify_feature,
            collected,
            nav_loc,
        ),
    )

    behavior_raw = section("behavior")
    beh_loc = f"{label}: behavior"
    behavior = BehaviorConfig(
        camera_interval_s=_float_pair(
            behavior_raw, "camera_interval_s", defaults.behavior.camera_interval_s, collected, beh_loc
        ),
        camera_mean_s=_float_value(
            behavior_raw, "camera_mean_s", defaults.behavior.camera_mean_s, collected, beh_loc
        ),
        camera_sd_s=_float_value(behavior_raw, "camera_sd_s", defaults.behavior.camera_sd_s, collected, beh_loc),
        attack_interval_s=_float_pair(
            behavior_raw, "attack_interval_s", defaults.behavior.attack_interval_s, collected, beh_loc
        ),
        attack_mean_s=_float_value(
            behavior_raw, "attack_mean_s", defaults.behavior.attack_mean_s, collected, beh_loc
        ),
        attack_sd_s=_float_value(behavior_raw, "attack_sd_s", defaults.behavior.attack_sd_s, collected, beh_loc),
        consumable_ratio=_float_value(
            behavior_raw, "consumable_ratio", defaults.behavior.consumable_ratio, collected, beh_loc
        ),
        consumable_ratio_range=_float_pair(
            behavior_raw,
            "consumable_ratio_range",
            defaults.behavior.consumable_ratio_range,
            collected,
            beh_loc,
        ),
        camera_activity_range=_float_pair(
            behavior_raw, "camera_activity_range", defaults.behavior.camera_activity_range, collected, beh_loc
        ),
        attack_frequency_range=_float_pair(
            behavior_raw,
            "attack_frequency_range",
            defaults.behavior.attack_frequency_range,
            collected,
            beh_loc,
        ),
        long_stare_bias_range=_float_pair(
            behavior_raw,
            "long_stare_bias_range",
            defaults.behavior.long_stare_bias_range,
            collected,
            beh_loc,
        ),
        long_stare_probability=_float_value(
            behavior_raw,
            "long_stare_probability",
            defaults.behavior.long_stare_probability,
            collected,
            beh_loc,
        ),
        long_stare_s=_float_pair(
            behavior_raw, "long_stare_s", defaults.behavior.long_stare_s, collected, beh_loc
        ),
        move_px=_int_pair(behavior_raw, "move_px", defaults.behavior.move_px, collected, beh_loc),
        move_steps=_int_pair(behavior_raw, "move_steps", defaults.behavior.move_steps, collected, beh_loc),
        move_duration_ms=_int_pair(
            behavior_raw, "move_duration_ms", defaults.behavior.move_duration_ms, collected, beh_loc
        ),
        fire_sector_deg=_float_value(
            behavior_raw, "fire_sector_deg", defaults.behavior.fire_sector_deg, collected, beh_loc
        ),
        fire_turn_probability=_float_value(
            behavior_raw,
            "fire_turn_probability",
            defaults.behavior.fire_turn_probability,
            collected,
            beh_loc,
        ),
        fire_burst=_int_pair(behavior_raw, "fire_burst", defaults.behavior.fire_burst, collected, beh_loc),
        fire_interval_ms=_int_pair(
            behavior_raw, "fire_interval_ms", defaults.behavior.fire_interval_ms, collected, beh_loc
        ),
        fire_click_region=(
            _normalized_rect(
                behavior_raw, "fire_click_region", None, collected, beh_loc
            )
            or defaults.behavior.fire_click_region
        ),
        consumable_max_per_battle=_int_value(
            behavior_raw,
            "consumable_max_per_battle",
            defaults.behavior.consumable_max_per_battle,
            collected,
            beh_loc,
        ),
        activity_watchdog_s=_float_pair(
            behavior_raw, "activity_watchdog_s", defaults.behavior.activity_watchdog_s, collected, beh_loc
        ),
        actions_per_minute_max=_int_value(
            behavior_raw,
            "actions_per_minute_max",
            defaults.behavior.actions_per_minute_max,
            collected,
            beh_loc,
        ),
        battle_start_quiet_s=_float_value(
            behavior_raw, "battle_start_quiet_s", defaults.behavior.battle_start_quiet_s, collected, beh_loc
        ),
        max_input_frame_age_s=_float_value(
            behavior_raw,
            "max_input_frame_age_s",
            defaults.behavior.max_input_frame_age_s,
            collected,
            beh_loc,
        ),
    )

    ship_raw = section("ship")
    ship_config = ShipConfig(
        ship_id=_str_value(ship_raw, "ship_id", defaults.ship.ship_id, collected, f"{label}: ship"),
        verify_each_battle=_bool_value(
            ship_raw, "verify_each_battle", defaults.ship.verify_each_battle, collected, f"{label}: ship"
        ),
    )

    safety_raw = section("safety")
    safe_loc = f"{label}: safety"
    safety = SafetyConfig(
        emergency_stop_key=_str_value(
            safety_raw, "emergency_stop_key", defaults.safety.emergency_stop_key, collected, safe_loc
        ),
        resume_key=_str_value(safety_raw, "resume_key", defaults.safety.resume_key, collected, safe_loc),
        max_session_minutes=_float_value(
            safety_raw, "max_session_minutes", defaults.safety.max_session_minutes, collected, safe_loc
        ),
        max_battles_per_session=_int_value(
            safety_raw, "max_battles_per_session", defaults.safety.max_battles_per_session, collected, safe_loc
        ),
        pause_when_unfocused=_bool_value(
            safety_raw, "pause_when_unfocused", defaults.safety.pause_when_unfocused, collected, safe_loc
        ),
        heartbeat_s=_float_value(safety_raw, "heartbeat_s", defaults.safety.heartbeat_s, collected, safe_loc),
        topmost=_bool_value(safety_raw, "topmost", defaults.safety.topmost, collected, safe_loc),
        auto_restart_game=_bool_value(
            safety_raw, "auto_restart_game", defaults.safety.auto_restart_game, collected, safe_loc
        ),
        restart_wait_s=_float_value(
            safety_raw, "restart_wait_s", defaults.safety.restart_wait_s, collected, safe_loc
        ),
        restart_max_attempts=_int_value(
            safety_raw, "restart_max_attempts", defaults.safety.restart_max_attempts, collected, safe_loc
        ),
    )

    # --- 过载保护（config.yaml overload 段；未知键 error） -------------------
    overload_raw = section("overload")
    ol_loc = f"{label}: overload"
    unknown_overload = sorted(set(overload_raw) - OVERLOAD_KEYS)
    for key in unknown_overload:
        _error(
            collected,
            "unknown_key",
            f"{ol_loc}.{key}",
            f"overload 段存在未知配置键 {key!r}（允许: {', '.join(sorted(OVERLOAD_KEYS))}）",
        )
    overload_values: dict[str, Any] = {}
    for item in fields(OverloadConfig):
        if item.name == "mode":
            overload_values["mode"] = _str_value(
                overload_raw, "mode", defaults.overload.mode, collected, ol_loc
            )
        elif item.name in OVERLOAD_INT_FIELDS:
            overload_values[item.name] = _int_value(
                overload_raw, item.name, int(getattr(defaults.overload, item.name)), collected, ol_loc
            )
        else:
            overload_values[item.name] = _float_value(
                overload_raw,
                item.name,
                float(getattr(defaults.overload, item.name)),
                collected,
                ol_loc,
            )
    overload = OverloadConfig(**overload_values)

    session_raw = section("session")
    sess_loc = f"{label}: session"
    allowed_hours_raw = session_raw.get("allowed_hours")
    allowed_hours: tuple[str, str] | None = None
    if allowed_hours_raw is not None:
        if (
            isinstance(allowed_hours_raw, Sequence)
            and not isinstance(allowed_hours_raw, str)
            and len(allowed_hours_raw) == 2
            and all(isinstance(item, str) for item in allowed_hours_raw)
        ):
            allowed_hours = (str(allowed_hours_raw[0]), str(allowed_hours_raw[1]))
        else:
            _error(collected, "type", f"{sess_loc}.allowed_hours", "需要两个 HH:MM 字符串或 null")
    session = SessionConfig(
        between_battle_s=_float_pair(
            session_raw, "between_battle_s", defaults.session.between_battle_s, collected, sess_loc
        ),
        daily_battle_cap=_int_value(
            session_raw, "daily_battle_cap", defaults.session.daily_battle_cap, collected, sess_loc
        ),
        allowed_hours=allowed_hours,
        max_consecutive_start_failures=_int_value(
            session_raw,
            "max_consecutive_start_failures",
            defaults.session.max_consecutive_start_failures,
            collected,
            sess_loc,
        ),
        recovery_max_per_10min=_int_value(
            session_raw, "recovery_max_per_10min", defaults.session.recovery_max_per_10min, collected, sess_loc
        ),
    )

    timeouts_raw = section("timeouts")
    to_loc = f"{label}: timeouts"
    timeouts = TimeoutsConfig(
        **{
            item.name: _float_value(timeouts_raw, item.name, float(getattr(defaults.timeouts, item.name)), collected, to_loc)
            for item in fields(TimeoutsConfig)
        }
    )

    input_raw = section("input")
    inp_loc = f"{label}: input"
    input_config = InputConfig(
        backend=_str_value(input_raw, "backend", defaults.input.backend, collected, inp_loc),
        min_action_interval_ms=_int_value(
            input_raw, "min_action_interval_ms", defaults.input.min_action_interval_ms, collected, inp_loc
        ),
        click_hold_ms=_int_pair(
            input_raw, "click_hold_ms", defaults.input.click_hold_ms, collected, inp_loc
        ),
        key_hold_ms=_int_pair(input_raw, "key_hold_ms", defaults.input.key_hold_ms, collected, inp_loc),
        interval_jitter=_float_value(
            input_raw, "interval_jitter", defaults.input.interval_jitter, collected, inp_loc
        ),
        queue_max=_int_value(input_raw, "queue_max", defaults.input.queue_max, collected, inp_loc),
        dpi_aware=_bool_value(input_raw, "dpi_aware", defaults.input.dpi_aware, collected, inp_loc),
    )

    logging_raw = section("logging")
    log_loc = f"{label}: logging"
    logging_config = LoggingConfig(
        screenshot_on_error=_bool_value(
            logging_raw, "screenshot_on_error", defaults.logging.screenshot_on_error, collected, log_loc
        ),
        delete_screenshots_after_battle=_bool_value(
            logging_raw,
            "delete_screenshots_after_battle",
            defaults.logging.delete_screenshots_after_battle,
            collected,
            log_loc,
        ),
        log_max_bytes=_int_value(
            logging_raw, "log_max_bytes", defaults.logging.log_max_bytes, collected, log_loc
        ),
        log_backup_count=_int_value(
            logging_raw, "log_backup_count", defaults.logging.log_backup_count, collected, log_loc
        ),
        db_fallback_log=_str_value(
            logging_raw, "db_fallback_log", defaults.logging.db_fallback_log, collected, log_loc
        ),
        capture_stats=_bool_value(
            logging_raw, "capture_stats", defaults.logging.capture_stats, collected, log_loc
        ),
        diagnostics_dir=_str_value(
            logging_raw, "diagnostics_dir", defaults.logging.diagnostics_dir, collected, log_loc
        ),
        diagnostics_max_files=_int_value(
            logging_raw, "diagnostics_max_files", defaults.logging.diagnostics_max_files, collected, log_loc
        ),
        diagnostics_max_mb=_float_value(
            logging_raw, "diagnostics_max_mb", defaults.logging.diagnostics_max_mb, collected, log_loc
        ),
        disk_warn_gb=_float_value(
            logging_raw, "disk_warn_gb", defaults.logging.disk_warn_gb, collected, log_loc
        ),
        disk_pause_gb=_float_value(
            logging_raw, "disk_pause_gb", defaults.logging.disk_pause_gb, collected, log_loc
        ),
    )

    perception_raw = section("perception")
    per_loc = f"{label}: perception"
    death_raw = _optional_section(perception_raw, "death", collected, per_loc) or {}
    death_defaults = defaults.perception.death
    death = DeathConfig(
        weights=_str_float_mapping(
            death_raw, "weights", death_defaults.weights, collected, f"{per_loc}.death"
        ),
        score_threshold=_float_value(
            death_raw, "score_threshold", death_defaults.score_threshold, collected, f"{per_loc}.death"
        ),
        stable_s=_float_value(death_raw, "stable_s", death_defaults.stable_s, collected, f"{per_loc}.death"),
        strong_stable_s=_float_value(
            death_raw, "strong_stable_s", death_defaults.strong_stable_s, collected, f"{per_loc}.death"
        ),
        marker_lost_stable_s=_float_value(
            death_raw, "marker_lost_stable_s", death_defaults.marker_lost_stable_s, collected, f"{per_loc}.death"
        ),
        ignore_start_s=_float_value(
            death_raw, "ignore_start_s", death_defaults.ignore_start_s, collected, f"{per_loc}.death"
        ),
        banner_latch_s=_float_value(
            death_raw, "banner_latch_s", death_defaults.banner_latch_s, collected, f"{per_loc}.death"
        ),
    )
    perception = PerceptionConfig(
        template_scales=_numbers(
            perception_raw, "template_scales", defaults.perception.template_scales, collected, per_loc
        )
        or defaults.perception.template_scales,
        feature_confirm_frames=_int_value(
            perception_raw,
            "feature_confirm_frames",
            defaults.perception.feature_confirm_frames,
            collected,
            per_loc,
        ),
        state_unknown_timeout_s=_float_value(
            perception_raw,
            "state_unknown_timeout_s",
            defaults.perception.state_unknown_timeout_s,
            collected,
            per_loc,
        ),
        capture_method=_str_value(
            perception_raw,
            "capture_method",
            defaults.perception.capture_method,
            collected,
            per_loc,
        ),
        orb_features_background=_int_value(
            perception_raw,
            "orb_features_background",
            defaults.perception.orb_features_background,
            collected,
            per_loc,
        ),
        orb_features_minimap=_int_value(
            perception_raw,
            "orb_features_minimap",
            defaults.perception.orb_features_minimap,
            collected,
            per_loc,
        ),
        orb_ratio_test=_float_value(
            perception_raw, "orb_ratio_test", defaults.perception.orb_ratio_test, collected, per_loc
        ),
        ransac_reproj_px=_float_value(
            perception_raw, "ransac_reproj_px", defaults.perception.ransac_reproj_px, collected, per_loc
        ),
        min_inliers=_int_value(
            perception_raw, "min_inliers", defaults.perception.min_inliers, collected, per_loc
        ),
        max_reprojection_error_px=_float_value(
            perception_raw,
            "max_reprojection_error_px",
            defaults.perception.max_reprojection_error_px,
            collected,
            per_loc,
        ),
        map_scale_tolerance=_float_value(
            perception_raw,
            "map_scale_tolerance",
            defaults.perception.map_scale_tolerance,
            collected,
            per_loc,
        ),
        map_rotation_tolerance_deg=_float_value(
            perception_raw,
            "map_rotation_tolerance_deg",
            defaults.perception.map_rotation_tolerance_deg,
            collected,
            per_loc,
        ),
        map_candidate_margin=_float_value(
            perception_raw,
            "map_candidate_margin",
            defaults.perception.map_candidate_margin,
            collected,
            per_loc,
        ),
        map_refresh_s=_float_value(
            perception_raw, "map_refresh_s", defaults.perception.map_refresh_s, collected, per_loc
        ),
        map_identify_retries=_int_value(
            perception_raw,
            "map_identify_retries",
            defaults.perception.map_identify_retries,
            collected,
            per_loc,
        ),
        map_min_inlier_hull_ratio=_float_value(
            perception_raw,
            "map_min_inlier_hull_ratio",
            defaults.perception.map_min_inlier_hull_ratio,
            collected,
            per_loc,
        ),
        map_verify_ncc=_float_value(
            perception_raw, "map_verify_ncc", defaults.perception.map_verify_ncc, collected, per_loc
        ),
        map_refresh_inlier_gain=_float_value(
            perception_raw,
            "map_refresh_inlier_gain",
            defaults.perception.map_refresh_inlier_gain,
            collected,
            per_loc,
        ),
        map_refresh_translation_px=_float_value(
            perception_raw,
            "map_refresh_translation_px",
            defaults.perception.map_refresh_translation_px,
            collected,
            per_loc,
        ),
        death=death,
    )
    if sources is not None and death_raw:
        for key in death_raw:
            _record_source(sources, f"perception.death.{key}", label)

    accounts_raw = section("accounts")
    accounts_section = AccountsConfig(
        accounts=(),
        rotation_enabled=_bool_value(
            accounts_raw,
            "rotation_enabled",
            defaults.accounts.rotation_enabled,
            collected,
            f"{label}: accounts",
        ),
        rotation_mode=_str_value(
            accounts_raw, "rotation_mode", defaults.accounts.rotation_mode, collected, f"{label}: accounts"
        ),
        max_battles_per_account=_int_value(
            accounts_raw,
            "max_battles_per_account",
            defaults.accounts.max_battles_per_account,
            collected,
            f"{label}: accounts",
        ),
        accounts_file=_str_value(
            accounts_raw, "accounts_file", defaults.accounts.accounts_file, collected, f"{label}: accounts"
        ),
    )

    return GlobalConfig(
        runtime=runtime,
        paths=paths,
        map=map_config,
        navigation=navigation,
        behavior=behavior,
        ship=ship_config,
        safety=safety,
        session=session,
        overload=overload,
        timeouts=timeouts,
        input=input_config,
        logging=logging_config,
        perception=perception,
        accounts=accounts_section,
        project_root=root,
        config_path=config_path,
    )


# ---------------------------------------------------------------------------
# 账号档案（用户扩展需求：多账号选择与会话间轮换）
# ---------------------------------------------------------------------------


def _parse_allowed_hours(
    value: Any,
    issues: IssueSink,
    location: str,
) -> tuple[str, str] | None:
    if value is None:
        return None
    if (
        isinstance(value, Sequence)
        and not isinstance(value, str)
        and len(value) == 2
        and all(isinstance(item, str) for item in value)
    ):
        return (str(value[0]), str(value[1]))
    _error(issues, "type", location, "需要两个 HH:MM 字符串或 null")
    return None


def _parse_accounts(data: Mapping[str, Any], issues: IssueSink, location: str) -> tuple[AccountConfig, ...]:
    raw_accounts = data.get("accounts")
    if raw_accounts is None:
        _error(issues, "missing", f"{location}.accounts", "accounts.yaml 缺少 accounts 列表")
        return ()
    if not isinstance(raw_accounts, Sequence) or isinstance(raw_accounts, str):
        _error(issues, "type", f"{location}.accounts", "accounts 必须是列表")
        return ()
    accounts: list[AccountConfig] = []
    for index, item in enumerate(raw_accounts):
        item_loc = f"{location}.accounts[{index}]"
        if not isinstance(item, Mapping):
            _error(issues, "type", item_loc, "账号档案必须是映射（mapping）")
            continue
        alias = _str_value(item, "alias", "", issues, item_loc)
        if not alias:
            _error(issues, "missing", f"{item_loc}.alias", "账号档案缺少本地别名 alias")
            continue
        daily_cap: int | None = None
        if item.get("daily_battle_cap") is not None:
            daily_cap = _int_value(item, "daily_battle_cap", 0, issues, item_loc)
        max_battles: int | None = None
        if item.get("max_battles_per_session") is not None:
            max_battles = _int_value(item, "max_battles_per_session", 0, issues, item_loc)
        accounts.append(
            AccountConfig(
                alias=alias,
                display_name=_str_value(item, "display_name", "", issues, item_loc),
                profile=_opt_str(item, "profile", None, issues, item_loc) or None,
                ship_id=_opt_str(item, "ship_id", None, issues, item_loc) or None,
                daily_battle_cap=daily_cap,
                allowed_hours=_parse_allowed_hours(
                    item.get("allowed_hours"), issues, f"{item_loc}.allowed_hours"
                ),
                max_battles_per_session=max_battles,
                enabled=_bool_value(item, "enabled", True, issues, item_loc),
                notes=_str_value(item, "notes", "", issues, item_loc),
            )
        )
    return tuple(accounts)


def load_accounts(
    root: Path | str,
    accounts_file: str = "accounts.yaml",
    *,
    issues: IssueSink | None = None,
    sources: SourceSink | None = None,
    location: str | None = None,
    base: AccountsConfig | None = None,
) -> AccountsConfig:
    """加载账号档案文件（只含本地别名与配置覆盖，禁止凭据字段）。

    文件缺失时记 error 并返回 base（load_project 会把它降级为 warning，保持无账号
    场景向后兼容）；发现 password/token/真实账号字段时记 error 并拒绝加载全部档案。
    """
    collected: IssueSink = issues if issues is not None else []
    root_path = Path(root).resolve()
    path = resolve_path(accounts_file, root_path)
    label = location or relative_label(root_path, path)
    defaults = base or AccountsConfig()
    if not path.is_file():
        _error(collected, "file_missing", label, f"账号档案文件不存在: {path}")
        return defaults
    raw = _read_yaml(path, collected, label)
    if raw is None:
        return defaults
    if _scan_forbidden_credentials(raw, collected, label):
        # 安全约定：拒绝加载整个账号档案，避免凭据进入数据库/日志
        return AccountsConfig(accounts_file=str(accounts_file))
    rotation_raw = _section(raw, "rotation", collected, label)
    rotation_loc = f"{label}: rotation"
    accounts_config = AccountsConfig(
        accounts=_parse_accounts(raw, collected, label),
        rotation_enabled=_bool_value(
            rotation_raw, "enabled", defaults.rotation_enabled, collected, rotation_loc
        ),
        rotation_mode=_str_value(
            rotation_raw, "mode", defaults.rotation_mode, collected, rotation_loc
        ),
        max_battles_per_account=_int_value(
            rotation_raw,
            "max_battles_per_account",
            defaults.max_battles_per_account,
            collected,
            rotation_loc,
        ),
        accounts_file=str(accounts_file),
    )
    if sources is not None:
        for key in rotation_raw:
            _record_source(sources, f"accounts.rotation.{key}", label)
        for account in accounts_config.accounts:
            _record_source(sources, f"accounts.{account.alias}.alias", label)
    return accounts_config


def load_account(
    root: Path | str,
    alias: str,
    accounts_file: str = "accounts.yaml",
    *,
    issues: IssueSink | None = None,
) -> AccountConfig | None:
    """按本地别名取单个账号档案；不存在或文件非法时返回 None。"""
    accounts = load_accounts(root, accounts_file, issues=issues)
    return accounts.get(alias)


# ---------------------------------------------------------------------------
# 客户端配置（详细设计 3.2）
# ---------------------------------------------------------------------------


def _parse_steps(
    value: Any,
    issues: IssueSink,
    location: str,
) -> tuple[StepDef, ...]:
    """解析步骤序列；结构错误记 error 并跳过该步骤。"""
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str):
        _error(issues, "type", location, "步骤序列必须是列表")
        return ()
    steps: list[StepDef] = []
    for index, item in enumerate(value):
        step_loc = f"{location}[{index}]"
        if not isinstance(item, Mapping):
            _error(issues, "type", step_loc, "步骤必须是映射（mapping）")
            continue
        action = item.get("action")
        if not isinstance(action, str) or not action:
            _error(issues, "missing", f"{step_loc}.action", "步骤缺少 action")
            continue
        steps.append(
            StepDef(
                action=action,
                key=_opt_str(item, "key", None, issues, step_loc),
                feature=_opt_str(item, "feature", None, issues, step_loc),
                timeout_ms=_int_value(item, "timeout_ms", 3000, issues, step_loc),
                retries=_int_value(item, "retries", 1, issues, step_loc),
                optional=_bool_value(item, "optional", False, issues, step_loc),
                wait_after_ms=_int_value(item, "wait_after_ms", 0, issues, step_loc),
                duration_ms=_int_value(item, "duration_ms", 0, issues, step_loc),
                scroll_clicks=_int_value(item, "scroll_clicks", 0, issues, step_loc),
                region=_normalized_rect(item, "region", None, issues, step_loc),
                offset_x=_int_value(item, "offset_x", 0, issues, step_loc),
                offset_y=_int_value(item, "offset_y", 0, issues, step_loc),
                interval_ms=_int_value(item, "interval_ms", 300, issues, step_loc),
            )
        )
    return tuple(steps)


def _parse_features(
    raw: Mapping[str, Any],
    profile_dir: Path,
    issues: IssueSink,
    location: str,
    sources: SourceSink | None = None,
) -> dict[str, FeatureDef]:
    """解析界面特征表；region 缺失或非法的特征不加载（记 error）。"""
    defaults = FeatureDef(feature_id="", template="", region=NormalizedRect(0.0, 0.0, 1.0, 1.0))
    features: dict[str, FeatureDef] = {}
    for feature_id, item in raw.items():
        feature_loc = f"{location}.{feature_id}"
        if not isinstance(feature_id, str) or not feature_id:
            _error(issues, "type", f"{location}[特征编号]", "特征编号必须是非空字符串")
            continue
        _record_source(sources, f"profile.features.{feature_id}", location.split(": ", 1)[0])
        if not isinstance(item, Mapping):
            _error(issues, "type", feature_loc, "特征定义必须是映射（mapping）")
            continue
        region = _normalized_rect(item, "region", None, issues, feature_loc)
        if region is None:
            _error(issues, "missing", f"{feature_loc}.region", "特征缺少有效的 region")
            continue
        template = _str_value(item, "template", "", issues, feature_loc)
        if not template:
            _error(issues, "missing", f"{feature_loc}.template", "特征缺少 template")
        scales = _numbers(item, "scales", defaults.scales, issues, feature_loc)
        template_path = resolve_path(template, profile_dir) if template else None
        features[feature_id] = FeatureDef(
            feature_id=feature_id,
            template=template,
            region=region,
            threshold=_float_value(item, "threshold", defaults.threshold, issues, feature_loc),
            scales=scales if scales is not None else defaults.scales,  # type: ignore[arg-type]
            template_path=template_path if template_path is not None and template_path.is_file() else None,
        )
    return features


def load_profile(
    path: Path | str,
    *,
    issues: IssueSink | None = None,
    sources: SourceSink | None = None,
    location: str | None = None,
) -> ClientProfile:
    """加载客户端配置 profile.yaml（宽松模式）。"""
    collected: IssueSink = issues if issues is not None else []
    profile_path = Path(path)
    profile_dir = profile_path.parent
    label = location or profile_path.name
    raw = _read_yaml(profile_path, collected, label)
    if raw is None:
        return ClientProfile(
            client_id="",
            display_name="",
            window=WindowConfig(),
            language="",
            reference_resolution=(0, 0),
            features={},
            profile_dir=profile_dir,
        )

    if sources is not None:
        for key in raw:
            _record_source(sources, f"profile.{key}", label)

    window_raw = _section(raw, "window", collected, label)
    window = WindowConfig(
        process_names=_str_list(window_raw, "process_names", (), collected, f"{label}: window"),
        title_patterns=_str_list(window_raw, "title_patterns", (), collected, f"{label}: window"),
        require_foreground=_bool_value(
            window_raw, "require_foreground", True, collected, f"{label}: window"
        ),
    )

    reference_resolution_raw = raw.get("reference_resolution")
    reference_resolution = (0, 0)
    if (
        isinstance(reference_resolution_raw, Sequence)
        and not isinstance(reference_resolution_raw, str)
        and len(reference_resolution_raw) == 2
        and all(isinstance(item, int) and not isinstance(item, bool) for item in reference_resolution_raw)
    ):
        reference_resolution = (int(reference_resolution_raw[0]), int(reference_resolution_raw[1]))
    else:
        _error(collected, "missing", f"{label}.reference_resolution", "需要两个整数 [宽, 高]")

    features_raw = _section(raw, "features", collected, label)
    features = _parse_features(features_raw, profile_dir, collected, f"{label}: features", sources)

    minimap_raw = _optional_section(raw, "minimap", collected, label)
    minimap: MinimapConfig | None = None
    if minimap_raw is not None:
        rect = _normalized_rect(minimap_raw, "rect", None, collected, f"{label}: minimap")
        if rect is None:
            _error(collected, "missing", f"{label}: minimap.rect", "小地图缺少有效的 rect")
        marker_template = _opt_str(minimap_raw, "marker_template", None, collected, f"{label}: minimap")
        minimap = MinimapConfig(
            rect=rect or NormalizedRect(0.0, 0.0, 1.0, 1.0),
            border_px=_int_value(minimap_raw, "border_px", 0, collected, f"{label}: minimap"),
            frame_feature=_opt_str(minimap_raw, "frame_feature", None, collected, f"{label}: minimap"),
            marker_template=marker_template,
            marker_angles=_int_value(minimap_raw, "marker_angles", 24, collected, f"{label}: minimap"),
            marker_hsv_lower=_int_triple(
                minimap_raw, "marker_hsv_lower", None, collected, f"{label}: minimap"
            ),
            marker_hsv_upper=_int_triple(
                minimap_raw, "marker_hsv_upper", None, collected, f"{label}: minimap"
            ),
            marker_jump_px=_float_value(
                minimap_raw, "marker_jump_px", 300.0, collected, f"{label}: minimap"
            ),
            marker_lost_s=_float_value(minimap_raw, "marker_lost_s", 3.0, collected, f"{label}: minimap"),
        )

    battle_hud_raw = _section(raw, "battle_hud", collected, label)
    hud_loc = f"{label}: battle_hud"
    battle_hud = BattleHudConfig(
        hp_bar_region=_normalized_rect(battle_hud_raw, "hp_bar_region", None, collected, hud_loc),
        hp_fill_hsv_lower=_int_triple(battle_hud_raw, "hp_fill_hsv_lower", None, collected, hud_loc),
        hp_fill_hsv_upper=_int_triple(battle_hud_raw, "hp_fill_hsv_upper", None, collected, hud_loc),
        hp_fill_zero_ratio=_float_value(battle_hud_raw, "hp_fill_zero_ratio", 0.03, collected, hud_loc),
        # ISS-002（评审 10.2）：颜色无关的血条填充列比例判定与退出授权参数
        hp_bar_empty_max_ratio=_float_value(
            battle_hud_raw, "hp_bar_empty_max_ratio", 0.02, collected, hud_loc
        ),
        hp_bar_alive_min_ratio=_float_value(
            battle_hud_raw, "hp_bar_alive_min_ratio", 0.10, collected, hud_loc
        ),
        hp_bar_confirm_frames=_int_value(
            battle_hud_raw, "hp_bar_confirm_frames", 2, collected, hud_loc
        ),
        hp_bar_evidence_ttl_s=_float_value(
            battle_hud_raw, "hp_bar_evidence_ttl_s", 1.0, collected, hud_loc
        ),
        hp_zero_feature=_opt_str(battle_hud_raw, "hp_zero_feature", None, collected, hud_loc),
        spectator_features=_str_list(battle_hud_raw, "spectator_features", (), collected, hud_loc),
        death_banner_feature=_str_value(
            battle_hud_raw, "death_banner_feature", "death_banner", collected, hud_loc
        ),
        result_feature=_str_value(battle_hud_raw, "result_feature", "battle_results", collected, hud_loc),
        result_continue_feature=_opt_str(
            battle_hud_raw, "result_continue_feature", None, collected, hud_loc
        ),
        result_return_feature=_opt_str(
            battle_hud_raw, "result_return_feature", "result_return_port_button", collected, hud_loc
        ),
    )

    camera_raw = _section(raw, "camera", collected, label)
    camera = CameraConfig(
        relative=_bool_value(camera_raw, "relative", True, collected, f"{label}: camera"),
        sensitivity=_float_value(camera_raw, "sensitivity", 1.0, collected, f"{label}: camera"),
    )

    exit_raw = _section(raw, "exit", collected, label)
    exit_loc = f"{label}: exit"
    exit_config = ExitConfig(
        menu_key=_str_value(exit_raw, "menu_key", "escape", collected, exit_loc),
        menu_open_feature=_opt_str(exit_raw, "menu_open_feature", None, collected, exit_loc),
        return_port_feature=_opt_str(exit_raw, "return_port_feature", None, collected, exit_loc),
        confirm_feature=_opt_str(exit_raw, "confirm_feature", None, collected, exit_loc),
        port_feature=_str_value(exit_raw, "port_feature", "port_indicator", collected, exit_loc),
        direct_exit_feature=_opt_str(exit_raw, "direct_exit_feature", None, collected, exit_loc),
        post_click_wait_ms=_int_value(exit_raw, "post_click_wait_ms", 500, collected, exit_loc),
    )

    state_features_raw = _section(raw, "state_features", collected, label)
    state_features: dict[str, tuple[str, ...]] = {}
    for state_name in state_features_raw:
        if not isinstance(state_name, str):
            _error(collected, "type", f"{label}: state_features", "状态名必须是字符串")
            continue
        state_features[state_name] = _str_list(
            state_features_raw, state_name, (), collected, f"{label}: state_features"
        )
        if sources is not None:
            _record_source(sources, f"profile.state_features.{state_name}", label)

    if sources is not None:
        for name in (
            "start_sequence",
            "start_sequence_full",
            "popup_sequence",
            "result_sequence",
            "ship_selection_sequence",
            "clear_loadout_sequence",
            "ship_verify_features",
            "loadout_verify_features",
            "popup_features",
            "minimap",
            "battle_hud",
            "camera",
            "exit",
        ):
            if name in raw:
                _record_source(sources, f"profile.{name}", label)

    return ClientProfile(
        client_id=_str_value(raw, "client_id", "", collected, label),
        display_name=_str_value(raw, "display_name", "", collected, label),
        window=window,
        language=_str_value(raw, "language", "", collected, label),
        reference_resolution=reference_resolution,
        features=features,
        start_sequence=_parse_steps(raw.get("start_sequence"), collected, f"{label}: start_sequence"),
        start_sequence_full=_parse_steps(
            raw.get("start_sequence_full"), collected, f"{label}: start_sequence_full"
        ),
        popup_sequence=_parse_steps(raw.get("popup_sequence"), collected, f"{label}: popup_sequence"),
        result_sequence=_parse_steps(raw.get("result_sequence"), collected, f"{label}: result_sequence"),
        ship_selection_sequence=_parse_steps(
            raw.get("ship_selection_sequence"), collected, f"{label}: ship_selection_sequence"
        ),
        ship_check_sequence=_parse_steps(
            raw.get("ship_check_sequence"), collected, f"{label}: ship_check_sequence"
        ),
        ship_verify_features=_str_list(
            raw, "ship_verify_features", (), collected, f"{label}: ship_verify_features"
        ),
        loadout_verify_features=_str_list(
            raw, "loadout_verify_features", (), collected, f"{label}: loadout_verify_features"
        ),
        clear_loadout_sequence=(
            None
            if raw.get("clear_loadout_sequence") is None
            else _parse_steps(raw.get("clear_loadout_sequence"), collected, f"{label}: clear_loadout_sequence")
        ),
        keys_file=_str_value(raw, "keys_file", "keys.yaml", collected, label),
        minimap=minimap,
        battle_hud=battle_hud,
        camera=camera,
        exit=exit_config,
        state_features=state_features,
        popup_features=_str_list(raw, "popup_features", (), collected, f"{label}: popup_features"),
        profile_dir=profile_dir,
        notes=_str_value(raw, "notes", "", collected, label),
    )


# ---------------------------------------------------------------------------
# 按键配置（详细设计 3.4）
# ---------------------------------------------------------------------------


def load_keymap(
    path: Path | str,
    *,
    issues: IssueSink | None = None,
    sources: SourceSink | None = None,
    location: str | None = None,
) -> KeyMap:
    """加载 keys.yaml（兼容 ``camera_relative`` 与 ``camera.relative`` 两种写法）。"""
    collected: IssueSink = issues if issues is not None else []
    keymap_path = Path(path)
    label = location or keymap_path.name
    raw = _read_yaml(keymap_path, collected, label)
    if raw is None:
        return KeyMap()

    actions_raw = _section(raw, "actions", collected, label)
    actions: dict[str, str] = {}
    for name, key in actions_raw.items():
        if not isinstance(name, str) or not isinstance(key, str) or not key:
            _error(collected, "type", f"{label}: actions.{name}", "动作名与按键名都必须是非空字符串")
            continue
        actions[name] = key
        if sources is not None:
            _record_source(sources, f"keys.actions.{name}", label)

    camera_raw = _section(raw, "camera", collected, label)
    camera_relative = _bool_value(camera_raw, "relative", True, collected, f"{label}: camera")
    if "camera_relative" in raw:
        camera_relative = _bool_value(raw, "camera_relative", camera_relative, collected, label)
    camera_sensitivity = _float_value(camera_raw, "sensitivity", 1.0, collected, f"{label}: camera")

    consumables_raw = raw.get("consumables")
    entries: Sequence[Any] = ()
    if isinstance(consumables_raw, Mapping):
        entries = consumables_raw.get("slots") or ()
    elif isinstance(consumables_raw, Sequence) and not isinstance(consumables_raw, str):
        entries = consumables_raw
    elif consumables_raw is not None:
        _error(collected, "type", f"{label}: consumables", "消耗品必须是列表或包含 slots 的映射")
    consumables: list[ConsumableSlot] = []
    for index, item in enumerate(entries):
        item_loc = f"{label}: consumables[{index}]"
        if not isinstance(item, Mapping):
            _error(collected, "type", item_loc, "消耗品条目必须是映射（mapping）")
            continue
        key = _str_value(item, "key", "", collected, item_loc)
        if not key:
            _error(collected, "missing", f"{item_loc}.key", "消耗品缺少按键")
            continue
        consumables.append(ConsumableSlot(key=key, name=_str_value(item, "name", "", collected, item_loc)))
    if sources is not None and entries:
        _record_source(sources, "keys.consumables", label)

    return KeyMap(
        actions=actions,
        camera_relative=camera_relative,
        consumables=tuple(consumables),
        camera_sensitivity=camera_sensitivity,
    )


# ---------------------------------------------------------------------------
# 船只配置（详细设计 3.2 / 6.8）
# ---------------------------------------------------------------------------


def load_ship(
    path: Path | str,
    *,
    issues: IssueSink | None = None,
    sources: SourceSink | None = None,
    location: str | None = None,
) -> ShipProfile:
    """加载 ships/<ship_id>/ship.yaml（宽松模式）。"""
    collected: IssueSink = issues if issues is not None else []
    ship_path = Path(path)
    ship_dir = ship_path.parent
    label = location or ship_path.name
    raw = _read_yaml(ship_path, collected, label)
    if raw is None:
        return ShipProfile(ship_id="", display_name="", ship_dir=ship_dir)

    if sources is not None:
        for key in raw:
            _record_source(sources, f"ship.{key}", label)

    card_template = _str_value(raw, "card_template", "card.png", collected, label)
    card_path = resolve_path(card_template, ship_dir) if card_template else None
    hp_bar_template = _opt_str(raw, "hp_bar_template", None, collected, label)
    hp_bar_path = resolve_path(hp_bar_template, ship_dir) if hp_bar_template else None
    card_search_region = _normalized_rect(raw, "card_search_region", None, collected, label)
    # 已选中证据（ISS-007）：selected_feature 是目标船选中态模板文件名（含选中高亮/边框）；
    # 搜索区域缺省回退 card_search_region。模板缺失时 selected_path=None，运行期保守失败。
    selected_feature = _opt_str(raw, "selected_feature", None, collected, label)
    selected_template_path = resolve_path(selected_feature, ship_dir) if selected_feature else None
    selected_region = _normalized_rect(raw, "selected_region", None, collected, label)
    if selected_region is None:
        selected_region = card_search_region
    # 右侧详情面板船名模板（ISS-007 实机补充 2026-10-31）：与选中态模板互为兜底
    selected_name_feature = _opt_str(raw, "selected_name_feature", None, collected, label)
    selected_name_template_path = (
        resolve_path(selected_name_feature, ship_dir) if selected_name_feature else None
    )
    selected_name_region = _normalized_rect(raw, "selected_name_region", None, collected, label)
    loadout_slots_raw = raw.get("loadout_slots")
    slots: list[LoadoutSlotDef] = []
    if loadout_slots_raw is None:
        pass
    elif not isinstance(loadout_slots_raw, Sequence) or isinstance(loadout_slots_raw, str):
        _error(collected, "type", f"{label}: loadout_slots", "空装备槽必须是列表")
    else:
        for index, item in enumerate(loadout_slots_raw):
            slot_loc = f"{label}: loadout_slots[{index}]"
            if not isinstance(item, Mapping):
                _error(collected, "type", slot_loc, "空装备槽条目必须是映射（mapping）")
                continue
            slot_id = _str_value(item, "slot_id", "", collected, slot_loc)
            if not slot_id:
                _error(collected, "missing", f"{slot_loc}.slot_id", "空装备槽缺少 slot_id")
                continue
            empty_template = _str_value(item, "empty_template", "", collected, slot_loc)
            if not empty_template:
                _error(collected, "missing", f"{slot_loc}.empty_template", "空装备槽缺少 empty_template")
            template_path = resolve_path(empty_template, ship_dir) if empty_template else None
            slots.append(
                LoadoutSlotDef(
                    slot_id=slot_id,
                    name=_str_value(item, "name", "", collected, slot_loc),
                    empty_template=empty_template,
                    region=_normalized_rect(item, "region", None, collected, slot_loc),
                    template_path=(
                        template_path if template_path is not None and template_path.is_file() else None
                    ),
                )
            )
            if sources is not None:
                _record_source(sources, f"ship.loadout_slots.{slot_id}", label)

    return ShipProfile(
        ship_id=_str_value(raw, "ship_id", "", collected, label),
        display_name=_str_value(raw, "display_name", "", collected, label),
        card_template=card_template,
        card_search_region=card_search_region,
        selected_feature=selected_feature,
        selected_region=selected_region,
        selected_threshold=_float_value(raw, "selected_threshold", 0.78, collected, label),
        selected_name_feature=selected_name_feature,
        selected_name_region=selected_name_region,
        selected_name_threshold=_float_value(raw, "selected_name_threshold", 0.78, collected, label),
        hp_bar_template=hp_bar_template,
        loadout_preset_feature=_opt_str(raw, "loadout_preset_feature", None, collected, label),
        loadout_slots=tuple(slots),
        max_scrolls=_int_value(raw, "max_scrolls", 20, collected, label),
        scroll_direction=_str_value(raw, "scroll_direction", "down", collected, label),
        scroll_clicks=_int_value(raw, "scroll_clicks", 1, collected, label),
        cover_list_start=_bool_value(raw, "cover_list_start", True, collected, label),
        max_position_scrolls=_int_value(raw, "max_position_scrolls", 10, collected, label),
        scroll_settle_s=_float_value(raw, "scroll_settle_s", 0.4, collected, label),
        confirm_frames=_int_value(raw, "confirm_frames", 2, collected, label),
        confirm_timeout_s=_float_value(raw, "confirm_timeout_s", 3.0, collected, label),
        max_click_attempts=_int_value(raw, "max_click_attempts", 2, collected, label),
        endpoint_stable_scrolls=_int_value(raw, "endpoint_stable_scrolls", 2, collected, label),
        endpoint_diff_ratio=_float_value(raw, "endpoint_diff_ratio", 0.02, collected, label),
        candidate_merge_px=_int_value(raw, "candidate_merge_px", 8, collected, label),
        selection_timeout_s=_float_value(raw, "selection_timeout_s", 60.0, collected, label),
        notes=_str_value(raw, "notes", "", collected, label),
        ship_dir=ship_dir,
        card_path=card_path if card_path is not None and card_path.is_file() else None,
        hp_bar_path=hp_bar_path if hp_bar_path is not None and hp_bar_path.is_file() else None,
        selected_path=(
            selected_template_path
            if selected_template_path is not None and selected_template_path.is_file()
            else None
        ),
        selected_name_path=(
            selected_name_template_path
            if selected_name_template_path is not None and selected_name_template_path.is_file()
            else None
        ),
    )


# ---------------------------------------------------------------------------
# 地图包（详细设计第 4 章）
# ---------------------------------------------------------------------------


def _parse_spawns(
    data: Mapping[str, Any] | None,
    issues: IssueSink,
    location: str,
) -> tuple[SpawnPoint, ...]:
    if data is None:
        return ()
    raw_spawns = data.get("spawns")
    if raw_spawns is None:
        _error(issues, "missing", f"{location}.spawns", "spawns.yaml 缺少 spawns 列表")
        return ()
    if not isinstance(raw_spawns, Sequence) or isinstance(raw_spawns, str):
        _error(issues, "type", f"{location}.spawns", "spawns 必须是列表")
        return ()
    spawns: list[SpawnPoint] = []
    for index, item in enumerate(raw_spawns):
        item_loc = f"{location}.spawns[{index}]"
        if not isinstance(item, Mapping):
            _error(issues, "type", item_loc, "出生点必须是映射（mapping）")
            continue
        spawn_id = _str_value(item, "spawn_id", "", issues, item_loc)
        if not spawn_id:
            _error(issues, "missing", f"{item_loc}.spawn_id", "出生点缺少 spawn_id")
            continue
        position = _float_pair(item, "position", (0.0, 0.0), issues, item_loc)
        if item.get("position") is None:
            _error(issues, "missing", f"{item_loc}.position", "出生点缺少 position")
            continue
        spawns.append(
            SpawnPoint(
                spawn_id=spawn_id,
                position=position,
                facing_deg=_float_value(item, "facing_deg", 0.0, issues, item_loc),
                label=_str_value(item, "label", "", issues, item_loc),
            )
        )
    return tuple(spawns)


def _parse_routes(
    data: Mapping[str, Any] | None,
    issues: IssueSink,
    location: str,
) -> tuple[Route, ...]:
    if data is None:
        return ()
    raw_routes = data.get("routes")
    if raw_routes is None:
        _error(issues, "missing", f"{location}.routes", "routes.yaml 缺少 routes 列表")
        return ()
    if not isinstance(raw_routes, Sequence) or isinstance(raw_routes, str):
        _error(issues, "type", f"{location}.routes", "routes 必须是列表")
        return ()
    routes: list[Route] = []
    for index, item in enumerate(raw_routes):
        item_loc = f"{location}.routes[{index}]"
        if not isinstance(item, Mapping):
            _error(issues, "type", item_loc, "航线必须是映射（mapping）")
            continue
        route_id = _str_value(item, "route_id", "", issues, item_loc)
        if not route_id:
            _error(issues, "missing", f"{item_loc}.route_id", "航线缺少 route_id")
            continue
        spawn_id = _str_value(item, "spawn_id", "", issues, item_loc)
        if not spawn_id:
            _error(issues, "missing", f"{item_loc}.spawn_id", "航线缺少 spawn_id")
        raw_waypoints = item.get("waypoints")
        waypoints: list[tuple[float, float]] = []
        if raw_waypoints is None:
            _error(issues, "missing", f"{item_loc}.waypoints", "航线缺少 waypoints")
        elif not isinstance(raw_waypoints, Sequence) or isinstance(raw_waypoints, str):
            _error(issues, "type", f"{item_loc}.waypoints", "waypoints 必须是列表")
        else:
            for wp_index, waypoint in enumerate(raw_waypoints):
                wp_loc = f"{item_loc}.waypoints[{wp_index}]"
                try:
                    if not isinstance(waypoint, Sequence) or isinstance(waypoint, str) or len(waypoint) != 2:
                        raise ValueError("需要两个数字")
                    if any(
                        isinstance(value, bool) or not isinstance(value, (int, float)) for value in waypoint
                    ):
                        raise ValueError("需要两个数字")
                    if not all(math.isfinite(float(value)) for value in waypoint):
                        raise ValueError("航点必须是有限数字")
                    waypoints.append((float(waypoint[0]), float(waypoint[1])))
                except (TypeError, ValueError) as exc:
                    _error(issues, "type", wp_loc, f"航点非法: {exc}")
        routes.append(
            Route(
                route_id=route_id,
                spawn_id=spawn_id,
                waypoints=tuple(waypoints),
                weight=_float_value(item, "weight", 1.0, issues, item_loc),
                description=_str_value(item, "description", "", issues, item_loc),
            )
        )
    return tuple(routes)


def _build_map_pack(
    pack_dir: Path,
    raw: Mapping[str, Any],
    issues: IssueSink,
    sources: SourceSink | None,
    location: str,
) -> MapPack | None:
    """按 map.yaml + spawns.yaml + routes.yaml 构造地图包；关键字段非法时跳过。"""
    map_label = f"{location}/map.yaml"
    map_id = _str_value(raw, "map_id", pack_dir.name, issues, map_label)
    if not map_id:
        _error(issues, "missing", f"{map_label}.map_id", "地图缺少 map_id")
        map_id = pack_dir.name
    name = _str_value(raw, "name", "", issues, map_label)
    if not name:
        _error(issues, "missing", f"{map_label}.name", "地图缺少 name")

    size_meters = _float_pair(raw, "size_meters", (0.0, 0.0), issues, map_label)
    if raw.get("size_meters") is None:
        _error(issues, "missing", f"{map_label}.size_meters", "地图缺少 size_meters")
        return None
    world_bounds = _float_quad(raw, "world_bounds", (0.0, 0.0, 0.0, 0.0), issues, map_label)
    if raw.get("world_bounds") is None:
        _error(issues, "missing", f"{map_label}.world_bounds", "地图缺少 world_bounds")
        return None
    image_bounds = _float_quad(raw, "image_bounds", (0.0, 0.0, 0.0, 0.0), issues, map_label)
    if raw.get("image_bounds") is None:
        _error(issues, "missing", f"{map_label}.image_bounds", "地图缺少 image_bounds")
        return None

    orientation_value = _str_value(raw, "image_orientation", "normal", issues, map_label)
    try:
        orientation = ImageOrientation(orientation_value)
    except ValueError:
        _error(
            issues,
            "enum",
            f"{map_label}.image_orientation",
            f"非法方向 {orientation_value!r}，允许 {[item.value for item in ImageOrientation]}",
        )
        return None

    world_to_image: tuple[float, ...] | None = None
    matrix_values = _numbers(raw, "world_to_image", None, issues, map_label)
    if matrix_values is not None:
        if len(matrix_values) != 6:
            _error(issues, "length", f"{map_label}.world_to_image", "需要 6 个数字（2x3 仿射）")
        elif abs(_affine_det(matrix_values)) < 1e-12:
            # 详细设计 4.7：不可逆的矩阵丢弃，改由 bounds + orientation 计算
            _warn(
                issues,
                "world_to_image_invertible",
                f"{map_label}.world_to_image",
                "world_to_image 不可逆，已忽略并改按 image_bounds + image_orientation 计算",
            )
        else:
            world_to_image = matrix_values

    background_path = pack_dir / "background.png"
    background_size = probe_png_size(background_path) if background_path.is_file() else None

    spawns_data = _read_yaml(pack_dir / "spawns.yaml", issues, f"{location}/spawns.yaml")
    routes_data = _read_yaml(pack_dir / "routes.yaml", issues, f"{location}/routes.yaml")
    if sources is not None:
        for field_name in ("map_id", "name", "size_meters", "world_bounds", "image_bounds", "image_orientation"):
            if field_name in raw:
                _record_source(sources, f"maps.{map_id}.{field_name}", map_label)
        if spawns_data is not None:
            _record_source(sources, f"maps.{map_id}.spawns", f"{location}/spawns.yaml")
        if routes_data is not None:
            _record_source(sources, f"maps.{map_id}.routes", f"{location}/routes.yaml")

    return MapPack(
        map_id=map_id,
        name=name,
        size_meters=size_meters,
        world_bounds=world_bounds,
        image_bounds=image_bounds,
        image_orientation=orientation,
        world_to_image=world_to_image,
        default_minimap_rect=_normalized_rect(raw, "default_minimap_rect", None, issues, map_label),
        notes=_str_value(raw, "notes", "", issues, map_label),
        pack_dir=pack_dir,
        background_path=background_path if background_path.is_file() else None,
        background_size=background_size,
        spawns=_parse_spawns(spawns_data, issues, f"{location}/spawns.yaml"),
        routes=_parse_routes(routes_data, issues, f"{location}/routes.yaml"),
    )


def _affine_det(matrix: Sequence[float]) -> float:
    """2x3 仿射矩阵的线性部分行列式。"""
    return matrix[0] * matrix[4] - matrix[1] * matrix[3]


def load_maps(
    directory: Path | str,
    *,
    issues: IssueSink | None = None,
    sources: SourceSink | None = None,
    location: str | None = None,
    root: Path | str | None = None,
) -> dict[str, MapPack]:
    """加载 maps/<map_id>/ 下的全部地图包，返回 {map_id: MapPack}。"""
    collected: IssueSink = issues if issues is not None else []
    maps_dir = Path(directory)
    if not maps_dir.exists():
        # 空地图目录由校验模块报 error（“未找到任何地图包”），这里保持静默
        return {}
    if not maps_dir.is_dir():
        _error(collected, "type", location or maps_dir.name, f"地图目录不是目录: {maps_dir}")
        return {}
    packs: dict[str, MapPack] = {}
    base = Path(root).resolve() if root is not None else maps_dir.resolve().parent
    prefix = location or relative_label(base, maps_dir)
    for pack_dir in sorted(item for item in maps_dir.iterdir() if item.is_dir()):
        label = f"{prefix}/{pack_dir.name}"
        map_yaml = pack_dir / "map.yaml"
        raw = _read_yaml(map_yaml, collected, f"{label}/map.yaml")
        if raw is None:
            continue
        pack = _build_map_pack(pack_dir, raw, collected, sources, label)
        if pack is None:
            continue
        if pack.map_id in packs:
            _error(
                collected,
                "duplicate",
                f"{label}/map.yaml.map_id",
                f"地图编号重复: {pack.map_id}",
            )
            continue
        packs[pack.map_id] = pack
    return packs


# ---------------------------------------------------------------------------
# 项目级加载与配置摘要
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoadedProject:
    """一次宽松加载的结果：数据模型 + 解析期问题 + 参与 config_hash 的文件。"""

    config: AppConfig
    issues: list[ValidationIssue] = field(default_factory=list)
    config_files: tuple[Path, ...] = ()

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.is_error]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if not issue.is_error]


def _canonical_value(value: Any, root: Path) -> Any:
    """把配置值转成可 JSON 序列化、且与机器路径无关的规范形式。"""
    if isinstance(value, Path):
        try:
            return value.resolve().relative_to(root).as_posix()
        except (OSError, ValueError):
            return value.as_posix()
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _canonical_value(getattr(value, item.name), root)
            for item in fields(value)
            if item.name not in {"project_root", "config_path"}
        }
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item, root) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_canonical_value(item, root) for item in value]
    return value


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_config_hash(
    global_config: GlobalConfig,
    file_hashes: Mapping[str, str],
    *,
    account_alias: str | None = None,
) -> str:
    """按规范化内容计算配置摘要（SHA-256 前 16 位，详细设计 11.2）。

    评审补充 P1-11：摘要包含全局配置有效值 + 各配置文件内容哈希（按相对路径排序）；
    多账号扩展后额外包含当前账号别名（不同账号的会话记录可区分）。
    """
    canonical = {
        "global": _canonical_value(global_config, global_config.project_root or Path(".")),
        "files": {str(key): value for key, value in sorted(file_hashes.items())},
        "account": account_alias or "",
    }
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return _sha256_text(payload)[:16]


def _file_hashes(root: Path, files: Iterable[Path]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in files:
        if not path.is_file():
            continue
        relative = relative_label(root, path)
        try:
            hashes[relative] = _sha256_text(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            hashes[relative] = ""
    return hashes


def _apply_account_overrides(
    global_config: GlobalConfig,
    account: AccountConfig | None,
) -> GlobalConfig:
    """把账号档案的每日上限/允许时段/单会话场次覆盖到全局配置。"""
    if account is None:
        return global_config
    session_updates: dict[str, Any] = {}
    if account.daily_battle_cap is not None:
        session_updates["daily_battle_cap"] = account.daily_battle_cap
    if account.allowed_hours is not None:
        session_updates["allowed_hours"] = account.allowed_hours
    safety_updates: dict[str, Any] = {}
    if account.max_battles_per_session is not None:
        safety_updates["max_battles_per_session"] = account.max_battles_per_session
    updated = global_config
    if session_updates:
        updated = replace(updated, session=replace(updated.session, **session_updates))
    if safety_updates:
        updated = replace(updated, safety=replace(updated.safety, **safety_updates))
    return updated


def load_project(
    root: Path | str,
    profile_id: str = "cn360",
    *,
    ship_id: str | None = None,
    account: str | None = None,
) -> LoadedProject:
    """宽松加载整个项目：配置文件 + 账号档案 + 客户端配置 + 按键 + 船只 + 地图包。

    ``account`` 指定本地账号别名时：profile/ship 与 session/safety 上限按账号覆盖；
    不指定时行为与旧版本一致（单账号手动登录模式）。
    """
    project_root = Path(root).resolve()
    collected: IssueSink = []
    sources: dict[str, str] = {}
    config_files: list[Path] = []

    config_path = project_root / "config.yaml"
    config_files.append(config_path)
    global_config = load_global(
        config_path,
        project_root=project_root,
        issues=collected,
        sources=sources,
        location="config.yaml",
    )

    # --- 账号档案（可选；缺失时 warning，保持无账号场景向后兼容） ---
    accounts_path = resolve_path(global_config.accounts.accounts_file, project_root)
    config_files.append(accounts_path)
    if accounts_path.is_file():
        accounts_config = load_accounts(
            project_root,
            global_config.accounts.accounts_file,
            issues=collected,
            sources=sources,
            location=relative_label(project_root, accounts_path),
            base=global_config.accounts,
        )
    else:
        accounts_config = global_config.accounts
        _warn(
            collected,
            "accounts_missing",
            "accounts.yaml",
            f"未找到账号档案文件 {accounts_path}，按单账号手动登录模式运行",
        )
    global_config = replace(global_config, accounts=accounts_config)

    account_config: AccountConfig | None = None
    if account is not None:
        if not account:
            _error(collected, "missing", "accounts.yaml: accounts", "账号别名不能为空")
        account_config = accounts_config.get(account)
        if account_config is None:
            _error(
                collected,
                "unknown_account",
                f"accounts.yaml: accounts.{account}",
                f"账号档案不存在: {account!r}（可用 {sorted(item.alias for item in accounts_config.accounts)}）",
            )
        else:
            _record_source(sources, "app.account", relative_label(project_root, accounts_path))
            global_config = _apply_account_overrides(global_config, account_config)

    effective_profile_id = account_config.profile if account_config and account_config.profile else profile_id
    effective_ship_arg = account_config.ship_id if account_config is not None else ship_id
    if account_config is not None:
        effective_ship_arg = effective_ship_arg or ship_id
    if not effective_profile_id:
        _error(
            collected,
            "missing",
            "profile",
            "缺少客户端配置编号：请通过 --profile 指定，或在账号档案填写 profile",
        )
        effective_profile_id = "cn360"

    profile_dir = global_config.paths.profiles_dir / effective_profile_id
    profile_path = profile_dir / "profile.yaml"
    config_files.append(profile_path)
    profile = load_profile(
        profile_path,
        issues=collected,
        sources=sources,
        location=relative_label(project_root, profile_path),
    )

    keys_path = profile_dir / profile.keys_file
    config_files.append(keys_path)
    keymap = load_keymap(
        keys_path,
        issues=collected,
        sources=sources,
        location=relative_label(project_root, keys_path),
    )

    resolved_ship_id = effective_ship_arg or global_config.ship.ship_id
    if not resolved_ship_id:
        _error(
            collected,
            "missing",
            "config.yaml: ship.ship_id",
            "缺少船只编号：请在 config.yaml 填写 ship.ship_id 或通过 --ship 指定",
        )
    ship_path = profile_dir / "ships" / (resolved_ship_id or "placeholder") / "ship.yaml"
    config_files.append(ship_path)
    ship = load_ship(
        ship_path,
        issues=collected,
        sources=sources,
        location=relative_label(project_root, ship_path),
    )
    if resolved_ship_id and ship.ship_id and ship.ship_id != resolved_ship_id:
        _error(
            collected,
            "mismatch",
            f"{relative_label(project_root, ship_path)}.ship_id",
            f"ship.yaml 的 ship_id={ship.ship_id!r} 与目录/配置 {resolved_ship_id!r} 不一致",
        )

    maps = load_maps(
        global_config.paths.maps_dir,
        issues=collected,
        sources=sources,
        location="maps",
        root=project_root,
    )
    for pack in maps.values():
        if pack.pack_dir is None:
            continue
        for name in ("map.yaml", "spawns.yaml", "routes.yaml"):
            candidate = pack.pack_dir / name
            if candidate.is_file():
                config_files.append(candidate)

    file_hashes = _file_hashes(project_root, config_files)
    account_alias = account_config.alias if account_config is not None else None
    config_hash = compute_config_hash(global_config, file_hashes, account_alias=account_alias)
    config = AppConfig(
        global_config=global_config,
        profile=profile,
        keymap=keymap,
        ship=ship,
        maps=maps,
        project_root=project_root,
        config_hash=config_hash,
        sources=sources,
        account=account_config,
    )
    return LoadedProject(config=config, issues=collected, config_files=tuple(config_files))


def load_app(
    root: Path | str,
    profile_id: str = "cn360",
    *,
    ship_id: str | None = None,
    account: str | None = None,
) -> AppConfig:
    """严格加载入口：加载 + 完整校验，有 error 时抛 ConfigError（退出码 2 口径）。"""
    project = load_project(root, profile_id, ship_id=ship_id, account=account)
    from .validate import validate_app

    issues = list(project.issues) + validate_app(project.config)
    errors = [issue for issue in issues if issue.is_error]
    if errors:
        raise ConfigError(issues)
    return project.config
