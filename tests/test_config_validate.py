"""配置校验器与 validate 工具单元测试（T1：src/config/validate.py、src/tools/validate.py）。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import pytest
from src.config.loader import load_keymap, load_project
from src.config.validate import (
    ValidationReport,
    validate_app,
    validate_keymap,
    validate_project,
)
from src.tools import validate as validate_tool

from tests.fixtures.configs import builder as fx


def _codes(issues: Iterable) -> list[str]:
    return [issue.code for issue in issues]


def _errors(report: ValidationReport) -> list:
    return report.errors


def _warnings(report: ValidationReport) -> list:
    return report.warnings


@pytest.fixture
def project(tmp_path: Path) -> fx.TestProject:
    return fx.build_valid_project(tmp_path)


def _report(tmp_path: Path, project: fx.TestProject) -> ValidationReport:
    return validate_project(tmp_path, project.client_id)


# ---------------------------------------------------------------------------
# 通过路径
# ---------------------------------------------------------------------------


def test_valid_project_passes(project: fx.TestProject, tmp_path: Path) -> None:
    report = _report(tmp_path, project)
    assert report.ok
    assert report.errors == []
    assert report.warnings == []
    assert report.format_lines() == []
    assert "0 个错误" in report.summary()


def test_allowed_hours_valid(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(tmp_path / "config.yaml", "session.allowed_hours", ["08:00", "23:00"])
    report = _report(tmp_path, project)
    assert report.ok, report.format_lines()


@pytest.mark.parametrize("field,value", [
    ("combat_fire_interval_s", [.5, 5]), ("combat_fire_interval_s", [5, 3]),
    ("aim_tolerance_deg", 0), ("aim_tolerance_deg", 31),
    ("aim_counts_per_degree", 0), ("aim_step_max_px", 0), ("aim_feedback_s", -1),
])
def test_invalid_combat_parameters_are_rejected(project: fx.TestProject, tmp_path: Path, field, value) -> None:
    fx.edit_yaml_file(tmp_path / "config.yaml", f"behavior.{field}", value)
    report = _report(tmp_path, project)
    assert any(issue.code == "range" and field in issue.location for issue in report.errors)


@pytest.mark.parametrize("field,value", [
    ("idle_camera_interval_s", [1.9, 4.0]),
    ("idle_camera_interval_s", [3.0, 4.6]),
    ("idle_camera_interval_s", [4.5, 3.0]),
    ("idle_camera_interval_s", [2.0, float("inf")]),
    ("idle_camera_move_px", [0, 48]),
    ("idle_camera_move_px", [24, 81]),
    ("idle_camera_move_px", [50, 40]),
    ("idle_camera_duration_s", [0.2, 0.7]),
    ("idle_camera_duration_s", [0.5, 1.1]),
    ("idle_camera_duration_s", [0.8, 0.5]),
    ("idle_camera_actions_per_minute_max", 0),
])
def test_invalid_idle_camera_parameters_are_rejected(
    project: fx.TestProject, tmp_path: Path, field, value
) -> None:
    fx.edit_yaml_file(tmp_path / "config.yaml", f"behavior.{field}", value)
    report = _report(tmp_path, project)
    assert any(issue.code == "range" and field in issue.location for issue in report.errors)


@pytest.mark.parametrize("field,value", [
    ("enemy_hsv_upper", [181, 255, 255]), ("view_cone_hsv_lower", [110, 60, 35]),
])
def test_invalid_combat_hsv_is_rejected(project: fx.TestProject, tmp_path: Path, field, value) -> None:
    fx.edit_yaml_file(project.profile_dir / "profile.yaml", f"minimap.{field}", value)
    report = _report(tmp_path, project)
    assert any(issue.code == "range" and field.split("_hsv")[0] in issue.location for issue in report.errors)


# ---------------------------------------------------------------------------
# 真实仓库：实机截图提取完成后应无 error（warning 可接受）
# ---------------------------------------------------------------------------


def test_real_repo_has_no_errors_after_calibration(project_root: Path) -> None:
    report = validate_project(project_root, "cn360")
    assert not report.has_errors, [issue.format() for issue in report.errors]
    warnings = report.warnings
    # 允许的 warning：空地图包（autopilot 不需要）、LOADING/POST_EXIT 状态特征为空等
    assert warnings
    assert all(issue.location for issue in warnings)
    assert any(issue.code == "empty" and issue.location == "maps" for issue in warnings)


def test_real_repo_tool_exit_code_and_output(project_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = validate_tool.main(["--root", str(project_root)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "配置校验完成" in captured.out


# ---------------------------------------------------------------------------
# 结构/引用/范围错误
# ---------------------------------------------------------------------------


def test_missing_feature_template_file(project: fx.TestProject, tmp_path: Path) -> None:
    (project.profile_dir / "features" / "queue_indicator.png").unlink()
    report = _report(tmp_path, project)
    matching = [
        issue for issue in report.errors if issue.code == "file_missing" and "queue_indicator" in issue.location
    ]
    assert len(matching) == 1


def test_uniform_template_rejected(project: fx.TestProject, tmp_path: Path) -> None:
    fx.write_png(project.profile_dir / "features" / "death_banner.png", fx.uniform_image())
    report = _report(tmp_path, project)
    assert any(issue.code == "image_uniform" and "death_banner" in issue.location for issue in report.errors)


def test_small_template_rejected(project: fx.TestProject, tmp_path: Path) -> None:
    fx.write_png(project.profile_dir / "features" / "minimap_frame.png", fx.template_image(seed=5, size=4))
    report = _report(tmp_path, project)
    assert any(issue.code == "image_too_small" for issue in report.errors)


def test_invalid_global_ranges(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(tmp_path / "config.yaml", "runtime.loop_hz", 0)
    fx.edit_yaml_file(tmp_path / "config.yaml", "runtime.log_level", "LOUD")
    fx.edit_yaml_file(tmp_path / "config.yaml", "behavior.consumable_ratio", 1.5)
    fx.edit_yaml_file(tmp_path / "config.yaml", "input.interval_jitter", -0.5)
    report = _report(tmp_path, project)
    errors = report.errors
    assert any(issue.code == "range" and "runtime.loop_hz" in issue.location for issue in errors)
    assert any(issue.code == "enum" and "runtime.log_level" in issue.location for issue in errors)
    assert any(issue.code == "range" and "behavior.consumable_ratio" in issue.location for issue in errors)
    assert any(issue.code == "range" and "input.interval_jitter" in issue.location for issue in errors)


def test_unsupported_navigation_options(project: fx.TestProject, tmp_path: Path) -> None:
    # ISS-001 后 full_map 已是受支持的点击面；这里验证未知 surface 与不支持的 route_end_action。
    fx.edit_yaml_file(tmp_path / "config.yaml", "navigation.surface", "bad_surface")
    fx.edit_yaml_file(tmp_path / "config.yaml", "navigation.route_end_action", "return")
    report = _report(tmp_path, project)
    assert any(issue.code == "enum" and "navigation.surface" in issue.location for issue in report.errors)
    assert any(issue.code == "unsupported" and "route_end_action" in issue.location for issue in report.errors)


def test_full_map_surface_requires_rect(project: fx.TestProject, tmp_path: Path) -> None:
    """ISS-001：autopilot 的 full_map 点击面必须配置全屏地图内容区矩形。"""
    fx.edit_yaml_file(tmp_path / "config.yaml", "navigation.surface", "full_map")
    report = _report(tmp_path, project)
    assert any(issue.code == "missing" and "navigation.full_map_rect" in issue.location for issue in report.errors)


def test_full_map_surface_with_rect_is_accepted(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(tmp_path / "config.yaml", "navigation.surface", "full_map")
    fx.edit_yaml_file(tmp_path / "config.yaml", "navigation.full_map_rect", [0.2, 0.1, 0.8, 0.9])
    report = _report(tmp_path, project)
    assert not [issue for issue in report.errors if "navigation.surface" in issue.location]
    assert not [issue for issue in report.errors if "navigation.full_map_rect" in issue.location]


def test_invalid_allowed_hours(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(tmp_path / "config.yaml", "session.allowed_hours", ["8:00", "23:00"])
    report = _report(tmp_path, project)
    assert any(issue.code == "format" and "allowed_hours" in issue.location for issue in report.errors)


def test_disk_threshold_ordering(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(tmp_path / "config.yaml", "logging.disk_warn_gb", 0.1)
    report = _report(tmp_path, project)
    assert any(issue.code == "range" and "disk_warn_gb" in issue.location for issue in report.errors)


def test_unknown_state_and_feature_references(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(
        project.profile_path, "state_features.PORT", ["port_indicator", "ghost_button"]
    )
    fx.edit_yaml_file(project.profile_path, "state_features.DEAD", ["death_banner"])
    fx.edit_yaml_file(project.profile_path, "popup_features", ["reward_panel", "ghost_panel"])
    report = _report(tmp_path, project)
    assert any(issue.code == "unknown_feature" and "ghost_button" in issue.message for issue in report.errors)
    assert any(issue.code == "enum" and "DEAD" in issue.message for issue in report.errors)
    assert any(issue.code == "unknown_feature" and "ghost_panel" in issue.message for issue in report.errors)


def test_step_sequence_semantics(project: fx.TestProject, tmp_path: Path) -> None:
    steps = [
        {"action": "key"},
        {"action": "click_feature"},
        {"action": "wait_feature", "feature": "ghost_feature"},
        {"action": "wait_ms", "duration_ms": 0},
        {"action": "scroll", "scroll_clicks": 0},
        {"action": "jump"},
        {"action": "key", "key": "escape", "timeout_ms": 0, "retries": -1},
    ]
    fx.edit_yaml_file(project.profile_path, "start_sequence", steps)
    report = _report(tmp_path, project)
    errors = report.errors
    assert any(issue.code == "missing" and issue.location.endswith(".key") for issue in errors)
    assert any(issue.code == "missing" and issue.location.endswith(".feature") for issue in errors)
    assert any(issue.code == "unknown_feature" for issue in errors)
    assert any(issue.code == "enum" and "jump" in issue.message for issue in errors)
    assert any(issue.code == "range" and "scroll_clicks" in issue.location for issue in errors)
    assert any(issue.code == "range" and "timeout_ms" in issue.location for issue in errors)
    assert any(issue.code == "range" and "duration_ms" in issue.location for issue in errors)
    assert any(issue.code == "range" and "retries" in issue.location for issue in errors)


def test_empty_start_sequence_warns(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(project.profile_path, "start_sequence", [])
    fx.edit_yaml_file(project.profile_path, "start_sequence_full", [])
    report = _report(tmp_path, project)
    assert report.ok
    assert any(issue.code == "empty" for issue in report.warnings)


def test_key_frame_returns_running(project: fx.TestProject, tmp_path: Path) -> None:
    # 步骤 offset_x/offset_y 是合法字段，不应产生错误
    report = _report(tmp_path, project)
    assert not any("offset" in issue.location for issue in report.issues)


def test_exit_menu_key_unknown(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(project.profile_path, "exit.menu_key", "no_such_key")
    report = _report(tmp_path, project)
    assert any(issue.code == "unknown_key" and "exit.menu_key" in issue.location for issue in report.errors)


def test_client_id_mismatch(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(project.profile_path, "client_id", "another_client")
    report = _report(tmp_path, project)
    assert any(issue.code == "mismatch" and "client_id" in issue.location for issue in report.errors)


def test_forced_map_and_default_spawn_missing(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(tmp_path / "config.yaml", "map.forced_map_id", "ghost_map")
    fx.edit_yaml_file(tmp_path / "config.yaml", "map.default_spawn_id", "ghost_spawn")
    report = _report(tmp_path, project)
    assert any(issue.code == "unknown_map" for issue in report.errors)
    assert any(issue.code == "unknown_spawn" and "default_spawn_id" in issue.location for issue in report.errors)


def test_ship_card_and_scroll_validation(project: fx.TestProject, tmp_path: Path) -> None:
    (project.ship_dir / "card.png").unlink()
    fx.edit_yaml_file(project.ship_path, "scroll_direction", "left")
    fx.edit_yaml_file(project.ship_path, "scroll_clicks", 0)
    report = _report(tmp_path, project)
    assert any(issue.code == "file_missing" and "card_template" in issue.location for issue in report.errors)
    assert any(issue.code == "enum" and "scroll_direction" in issue.location for issue in report.errors)
    assert any(issue.code == "range" and "scroll_clicks" in issue.location for issue in report.errors)


def test_keymap_known_keys_hook(project: fx.TestProject) -> None:
    keymap = load_keymap(project.keys_path)
    assert validate_keymap(keymap) == []
    issues = validate_keymap(keymap, known_keys={"escape", "mouse_left"})
    assert [issue.code for issue in issues] == ["unknown_key"]
    assert "full_map" in issues[0].location


def test_keymap_missing_required_actions(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(project.keys_path, "actions", {"escape": "escape"})
    report = _report(tmp_path, project)
    locations = [issue.location for issue in report.errors if issue.code == "missing"]
    assert any(location.endswith("actions.fire") for location in locations)
    assert any(location.endswith("actions.full_map") for location in locations)


def test_death_weights_validation(project: fx.TestProject, tmp_path: Path) -> None:
    weights = {
        "hp_bar_absent": 0.35,
        "hp_zero": 0.25,
        "banner": 0.25,
        "marker_lost": 0.10,
        "spectator_ui": 0.05,
    }
    weights.pop("hp_zero")
    weights["unknown_signal"] = 0.1
    fx.edit_yaml_file(tmp_path / "config.yaml", "perception.death.weights", weights)
    report = _report(tmp_path, project)
    assert any(issue.code == "missing" and "hp_zero" in issue.message for issue in report.errors)
    assert any(issue.code == "unknown" and "unknown_signal" in issue.message for issue in report.warnings)
    assert any(issue.code == "range" and "权重之和" in issue.message for issue in report.warnings)


def test_perception_ranges(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(tmp_path / "config.yaml", "perception.orb_ratio_test", 1.5)
    fx.edit_yaml_file(tmp_path / "config.yaml", "perception.map_rotation_tolerance_deg", 90)
    fx.edit_yaml_file(tmp_path / "config.yaml", "perception.death.score_threshold", 0)
    fx.edit_yaml_file(tmp_path / "config.yaml", "perception.map_verify_ncc", 2)
    report = _report(tmp_path, project)
    errors = report.errors
    assert any("orb_ratio_test" in issue.location for issue in errors)
    assert any("map_rotation_tolerance_deg" in issue.location for issue in errors)
    assert any("score_threshold" in issue.location for issue in errors)
    assert any("map_verify_ncc" in issue.location for issue in errors)


def test_validate_report_shapes(project: fx.TestProject, tmp_path: Path) -> None:
    report = _report(tmp_path, project)
    assert isinstance(report, ValidationReport)
    assert report.ok and not report.has_errors
    assert report.errors == [] and report.warnings == []
    assert report.summary().startswith("配置校验完成")


# ---------------------------------------------------------------------------
# validate 命令行工具
# ---------------------------------------------------------------------------


def test_tool_exit_zero_on_valid_project(project: fx.TestProject, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = validate_tool.main(["--root", str(tmp_path), "--profile", project.client_id])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "[ERROR]" not in captured.out
    assert "校验通过" in captured.out


def test_tool_json_output(project: fx.TestProject, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = validate_tool.main(
        ["--root", str(tmp_path), "--profile", project.client_id, "--json"]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["profile"] == project.client_id
    assert payload["errors"] == []
    assert payload["warnings"] == []


def test_tool_json_errors(project: fx.TestProject, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fx.edit_yaml_file(tmp_path / "config.yaml", "runtime.loop_hz", 0)
    exit_code = validate_tool.main(
        ["--root", str(tmp_path), "--profile", project.client_id, "--json"]
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    payload = json.loads(captured.out)
    assert payload["errors"]
    assert any("runtime.loop_hz" in item["location"] for item in payload["errors"])


def test_tool_missing_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = validate_tool.main(["--root", str(tmp_path / "missing")])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "不存在" in captured.err


def test_tool_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        validate_tool.main(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "--profile" in captured.out


def test_validate_app_without_project(tmp_path: Path) -> None:
    loaded = load_project(tmp_path, "cn360")
    issues = validate_app(loaded.config)
    assert any(issue.code == "empty" and issue.location == "maps" for issue in issues)


# ---------------------------------------------------------------------------
# 过载保护配置校验（overload 段：mode 枚举、阈值范围与逐级收紧）
# ---------------------------------------------------------------------------


def test_overload_valid_overrides(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.mode", "pause_stop")
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.sample_hz", 2)
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.commit_pause_floor_mb", 3000)
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.commit_pause_limit_ratio", 0.06)
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.commit_warn_floor_mb", 9000)
    report = _report(tmp_path, project)
    assert report.ok, report.format_lines()


def test_overload_invalid_mode(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.mode", "yolo")
    report = _report(tmp_path, project)
    assert any(
        issue.code == "enum" and "overload.mode" in issue.location for issue in report.errors
    )


def test_overload_ranges_rejected(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.sample_hz", 0)
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.cpu_warn_pct", 0)
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.min_coverage", 1.5)
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.starvation_pause_events", 0)
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.auto_resume_max", -1)
    report = _report(tmp_path, project)
    errors = report.errors
    assert any("overload.sample_hz" in issue.location for issue in errors)
    assert any("overload.cpu_warn_pct" in issue.location for issue in errors)
    assert any("overload.min_coverage" in issue.location for issue in errors)
    assert any("overload.starvation_pause_events" in issue.location for issue in errors)
    assert any("overload.auto_resume_max" in issue.location for issue in errors)


def test_overload_commit_thresholds_must_tighten(project: fx.TestProject, tmp_path: Path) -> None:
    # 停止阈值不能高于暂停阈值，否则会先暂停后“更容易停止”
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.commit_stop_floor_mb", 9000)
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.commit_fast_stop_limit_ratio", 0.5)
    report = _report(tmp_path, project)
    errors = report.errors
    assert any(
        "overload.commit_stop_floor_mb" in issue.location for issue in errors
    )
    assert any(
        "overload.commit_fast_stop_limit_ratio" in issue.location for issue in errors
    )


def test_overload_window_relationships(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.window_long_s", 10)
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.commit_stop_window_s", 120)
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.commit_fast_stop_sustained_s", 200)
    report = _report(tmp_path, project)
    errors = report.errors
    assert any("overload.window_long_s" in issue.location for issue in errors)
    assert any("overload.commit_stop_window_s" in issue.location for issue in errors)
    assert any("overload.commit_fast_stop_sustained_s" in issue.location for issue in errors)


def test_real_repo_overload_defaults_validate(project_root: Path) -> None:
    report = validate_project(project_root, "cn360")
    assert not report.has_errors, [issue.format() for issue in report.errors]
    assert not any("overload" in issue.location for issue in report.issues)
