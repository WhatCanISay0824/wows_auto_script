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


# ---------------------------------------------------------------------------
# 真实仓库：待实机标定的模板/船只与空地图应给出错误（不是崩溃）
# ---------------------------------------------------------------------------


def test_real_repo_reports_errors_instead_of_crashing(project_root: Path) -> None:
    report = validate_project(project_root, "cn360")
    assert report.has_errors
    errors = report.errors
    # 当前仓库：13 个界面模板已从实机截图提取，剩 4 个退出相关模板 + 船卡 + 空信号旗槽待标定。
    # autopilot 模式下空地图包已降级为 warning，不再是 error。
    assert len(errors) >= 4
    assert _codes(errors).count("file_missing") >= 4
    assert all("待实机标定" in issue.message for issue in errors if issue.code == "file_missing")
    # 问题定位包含文件与字段路径
    assert all(issue.location for issue in errors)
    file_issues = [issue for issue in errors if issue.code == "file_missing"]
    assert all(": " in issue.location for issue in file_issues)
    assert any("features.exit_menu_panel.template" in issue.location for issue in errors)


def test_real_repo_tool_exit_code_and_output(project_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = validate_tool.main(["--root", str(project_root)])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "[ERROR]" in captured.out
    assert "个错误" in captured.out
    assert "退出码 2" in captured.out


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
    fx.edit_yaml_file(tmp_path / "config.yaml", "navigation.surface", "full_map")
    fx.edit_yaml_file(tmp_path / "config.yaml", "navigation.route_end_action", "return")
    report = _report(tmp_path, project)
    assert _codes(report.errors).count("unsupported") == 2


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
