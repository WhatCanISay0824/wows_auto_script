"""配置加载器单元测试（T1：src/config/loader.py）。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from src.common.errors import ConfigError
from src.config.loader import (
    load_app,
    load_global,
    load_keymap,
    load_maps,
    load_profile,
    load_project,
    load_ship,
    probe_png_size,
)
from src.config.validate import validate_app, validate_project

from tests.fixtures.configs import builder as fx

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "configs"


def test_targeted_combat_configuration_is_loaded(tmp_path: Path) -> None:
    project = fx.build_valid_project(tmp_path)
    behavior = {
        "targeted_combat": True, "combat_fire_interval_s": [4, 7],
        "aim_tolerance_deg": 9, "aim_counts_per_degree": 4.5,
        "aim_step_max_px": 55, "aim_feedback_s": .8,
        "idle_camera_interval_s": [2.5, 4.0],
        "idle_camera_move_px": [28, 52],
        "idle_camera_duration_s": [.4, .8],
        "idle_camera_actions_per_minute_max": 120,
    }
    for key, value in behavior.items():
        fx.edit_yaml_file(tmp_path / "config.yaml", f"behavior.{key}", value)
    hsv = {
        "enemy_hsv_lower": [1, 160, 180], "enemy_hsv_upper": [16, 250, 250],
        "view_cone_hsv_lower": [96, 65, 40], "view_cone_hsv_upper": [101, 250, 150],
    }
    for key, value in hsv.items():
        fx.edit_yaml_file(project.profile_dir / "profile.yaml", f"minimap.{key}", value)
    loaded = load_project(tmp_path, project.client_id)
    assert not loaded.errors
    for key, value in behavior.items():
        assert getattr(loaded.config.global_config.behavior, key) == (tuple(value) if isinstance(value, list) else value)
    for key, value in hsv.items():
        assert getattr(loaded.config.profile.minimap, key) == tuple(value)


# ---------------------------------------------------------------------------
# 真实仓库配置（模板/地图待实机标定，加载应不崩溃）
# ---------------------------------------------------------------------------


def test_load_global_real_config(project_root: Path) -> None:
    issues: list = []
    config = load_global(project_root / "config.yaml", project_root=project_root, issues=issues)
    assert issues == []
    assert config.project_root == project_root
    assert config.config_path == project_root / "config.yaml"
    assert config.runtime.loop_hz == 10
    assert config.runtime.capture_hz == 2.0
    assert config.paths.data_dir == (project_root / "data").resolve()
    assert config.paths.maps_dir == (project_root / "maps").resolve()
    assert config.ship.ship_id == "leningrad"


def test_load_global_new_fields_from_review(project_root: Path) -> None:
    """评审补充 P0-3 新增字段必须被 loader 解析（防止字段被静默忽略）。"""
    config = load_global(project_root / "config.yaml", project_root=project_root)
    nav = config.navigation
    assert nav.move_confirm_px == 3.0
    assert nav.stuck_distance_px == 3.0
    assert nav.stuck_window_s == 20.0
    assert nav.retry_interval_s == 4.0
    assert nav.ship_speed_window_s == 4.0
    assert nav.ship_speed_samples == 9
    behavior = config.behavior
    assert behavior.camera_sd_s == pytest.approx(2.8)
    assert behavior.attack_sd_s == pytest.approx(21.0)
    assert behavior.actions_per_minute_max == 40
    assert behavior.max_input_frame_age_s == 1.0
    assert behavior.idle_camera_interval_s == (3.0, 4.5)
    assert behavior.idle_camera_move_px == (24, 48)
    assert behavior.idle_camera_duration_s == (0.5, 0.7)
    assert behavior.idle_camera_actions_per_minute_max == 180
    perception = config.perception
    assert perception.map_min_inlier_hull_ratio == 0.25
    assert perception.map_verify_ncc == 0.30
    assert perception.map_refresh_inlier_gain == 0.20
    assert perception.map_refresh_translation_px == 5.0
    assert perception.death.banner_latch_s == 5.0
    assert config.timeouts.start_coop_s == 30.0
    assert config.timeouts.port_state_s == 60.0
    logging_config = config.logging
    assert logging_config.diagnostics_max_files == 200
    assert logging_config.diagnostics_max_mb == 100.0
    assert logging_config.disk_warn_gb == 2.0
    assert logging_config.disk_pause_gb == 0.5
    assert logging_config.db_fallback_log == "logs/db_fallback.log"


def test_load_global_missing_file_uses_defaults(tmp_path: Path) -> None:
    issues: list = []
    config = load_global(tmp_path / "config.yaml", project_root=tmp_path, issues=issues)
    assert [issue.code for issue in issues] == ["file_missing"]
    assert config.runtime.loop_hz == 10
    assert config.paths.data_dir == (tmp_path / "data").resolve()


def test_load_global_invalid_types_record_issues(tmp_path: Path) -> None:
    fx.write_yaml(
        tmp_path / "config.yaml",
        {"runtime": {"loop_hz": "fast"}, "navigation": {"arrival_m": "far"}, "input": {"backend": 3}},
    )
    issues: list = []
    config = load_global(tmp_path / "config.yaml", project_root=tmp_path, issues=issues)
    codes = {issue.code for issue in issues}
    assert "type" in codes
    # 类型错误回退默认值，加载不崩溃
    assert config.runtime.loop_hz == 10
    assert config.navigation.arrival_m == 400.0
    assert isinstance(config.input.backend, str)


def test_load_profile_real_cn360(project_root: Path) -> None:
    issues: list = []
    profile = load_profile(
        project_root / "profiles" / "cn360" / "profile.yaml", issues=issues
    )
    assert issues == []
    assert profile.client_id == "cn360"
    assert profile.display_name
    assert profile.reference_resolution == (2560, 1600)
    assert len(profile.features) == 20
    assert profile.battle_hud.hp_zero_feature == "hp_zero_value"
    # 2026-10-30 实机截图已全部提取（含退出菜单/确认框/装备页/标签页）
    assert all(feature.template_path is not None for feature in profile.features.values())
    assert profile.battle_hud.spectator_features == ()
    assert profile.popup_features == ()
    assert profile.state_features["LOADING"] == ()
    assert profile.state_features["EXIT_MENU_OPEN"] == ("exit_menu_return_port",)
    feature = profile.feature("port_battle_button")
    assert feature is not None
    assert feature.template == "features/port_battle_button.png"
    assert feature.region.to_list() == [0.40, 0.0, 0.56, 0.06]
    assert profile.minimap is not None
    assert profile.minimap.marker_template == "marker/marker.png"
    assert profile.minimap.rect.to_list() == [0.796875, 0.675, 1.0, 1.0]
    assert profile.minimap.marker_hsv_lower == (0, 0, 180)
    assert profile.minimap.marker_hsv_upper == (180, 140, 255)
    assert profile.battle_hud.hp_fill_zero_ratio == 0.05
    assert profile.exit.menu_key == "escape"
    assert profile.exit.confirm_feature == "exit_confirm_yes"
    assert len(profile.start_sequence) == 4
    assert len(profile.start_sequence_full) == 3
    # 2026-10-31：结算页不再固定点“回到港口”，由状态机按“继续战斗优先”选择按钮
    assert len(profile.result_sequence) == 1
    assert profile.result_sequence[0].feature == "battle_results"
    assert profile.battle_hud.result_continue_feature == "result_continue_battle_button"
    assert profile.battle_hud.result_return_feature == "result_return_port_button"
    assert profile.ship_selection_sequence == ()
    # 2026-10-31 用户确认：取消装备页/信号旗/加成卡检测
    assert profile.ship_check_sequence == ()
    assert profile.loadout_verify_features == ()
    assert profile.state_features["PORT"][0] == "port_indicator"
    assert profile.profile_dir == project_root / "profiles" / "cn360"


def test_load_keymap_real(project_root: Path) -> None:
    keymap = load_keymap(project_root / "profiles" / "cn360" / "keys.yaml")
    assert keymap.actions["escape"] == "escape"
    assert keymap.actions["fire"] == "mouse_left"
    assert keymap.actions["full_map"] == "m"
    assert keymap.camera_relative is True
    assert keymap.camera_sensitivity == 1.0
    assert [slot.key for slot in keymap.consumables] == ["1", "2", "3", "4", "5"]
    assert keymap.escape == "escape"
    assert keymap.fire == "mouse_left"


def test_load_keymap_missing_file_recorded(tmp_path: Path) -> None:
    issues: list = []
    keymap = load_keymap(tmp_path / "keys.yaml", issues=issues)
    assert [issue.code for issue in issues] == ["file_missing"]
    assert keymap.actions == {}


def test_load_ship_real(project_root: Path) -> None:
    path = project_root / "profiles" / "cn360" / "ships" / "leningrad" / "ship.yaml"
    ship = load_ship(path)
    assert ship.ship_id == "leningrad"
    assert ship.display_name == "列宁格勒"
    assert ship.card_template == "card.png"
    assert ship.card_path == path.parent / "card.png"  # 已从实机截图提取
    assert ship.hp_bar_template == "hp_bar.png"
    assert ship.hp_bar_path == path.parent / "hp_bar.png"
    assert ship.scroll_direction == "down"
    assert ship.scroll_clicks == 2
    assert ship.max_scrolls == 20
    assert ship.loadout_slots == ()  # 2026-10-31 起不再校验装备/信号旗
    assert ship.ship_dir == path.parent


def test_load_maps_empty_and_missing(tmp_path: Path, project_root: Path) -> None:
    # 仓库 maps/ 只有 README.md，没有地图包目录
    assert load_maps(project_root / "maps") == {}
    assert load_maps(tmp_path / "no_maps_dir") == {}  # 空目录不报错，由校验器给出 error


def test_load_project_repo_lenient(project_root: Path) -> None:
    loaded = load_project(project_root, "cn360")
    assert loaded.issues == []
    config = loaded.config
    assert config.profile.client_id == "cn360"
    assert config.ship.ship_id == "leningrad"
    assert config.maps == {}
    assert config.project_root == project_root
    assert len(config.config_hash) == 16
    assert int(config.config_hash, 16) >= 0
    assert config.current_map is None
    assert config.sources["runtime.loop_hz"] == "config.yaml"
    assert config.sources["profile.features.port_indicator"] == "profiles/cn360/profile.yaml"
    assert config.sources["keys.actions.escape"] == "profiles/cn360/keys.yaml"
    assert config.sources["ship.ship_id"] == "profiles/cn360/ships/leningrad/ship.yaml"


def test_load_app_repo_succeeds_after_calibration(project_root: Path) -> None:
    """12 张实机截图提取完成后，真实仓库配置应无 error（warning 可接受）。"""
    app = load_app(project_root, "cn360")
    assert app.profile.client_id == "cn360"
    assert app.ship.ship_id == "leningrad"
    assert app.ship.card_path is not None
    issues = validate_app(app)
    errors = [issue for issue in issues if issue.is_error]
    assert errors == [], "\n".join(issue.format() for issue in errors)


# ---------------------------------------------------------------------------
# 完整测试工程
# ---------------------------------------------------------------------------


def test_load_project_valid_fixture(tmp_path: Path) -> None:
    project = fx.build_valid_project(tmp_path)
    loaded = load_project(tmp_path, project.client_id)
    assert loaded.issues == []
    config = loaded.config
    assert config.profile.client_id == project.client_id
    assert config.profile.profile_dir == project.profile_dir
    # 模板/卡片路径已解析到实际文件
    feature = config.profile.feature("port_indicator")
    assert feature is not None
    assert feature.template_path == project.profile_dir / "features" / "port_indicator.png"
    assert config.ship.card_path == project.ship_dir / "card.png"
    assert config.ship.loadout_slots[0].template_path == project.ship_dir / "loadout" / "signal_flags_empty.png"
    # 地图包
    assert list(config.maps) == [project.map_id]
    pack = config.maps[project.map_id]
    assert pack.pack_dir == project.map_dir
    assert pack.background_path == project.map_dir / "background.png"
    assert pack.background_size == (800, 800)
    assert pack.image_orientation.value == "normal"
    assert [spawn.spawn_id for spawn in pack.spawns] == ["east_1", "west_1"]
    assert [route.route_id for route in pack.routes] == ["east_1_south", "west_1_center"]
    assert config.current_map is pack
    # sources 记录覆盖关系
    assert config.sources["maps.test_map.map_id"] == "maps/test_map/map.yaml"
    assert config.sources["maps.test_map.spawns"] == "maps/test_map/spawns.yaml"
    assert config.sources["profile.state_features.PORT"] == "profiles/testclient/profile.yaml"
    # 新增 StepDef 字段
    click_step = config.profile.result_sequence[1]
    assert click_step.action == "click_feature"
    assert click_step.offset_x == 4
    assert click_step.offset_y == -4
    assert config.keymap.resolve("full_map") == "m"


def test_load_app_valid_fixture_returns_appconfig(tmp_path: Path) -> None:
    project = fx.build_valid_project(tmp_path)
    app = load_app(tmp_path, project.client_id)
    assert app.profile.client_id == project.client_id
    assert app.config_hash == load_project(tmp_path, project.client_id).config.config_hash
    assert app.map_pack(project.map_id) is app.maps[project.map_id]


def test_relative_paths_resolution(tmp_path: Path) -> None:
    project = fx.build_valid_project(tmp_path)
    loaded = load_project(tmp_path, project.client_id)
    config = loaded.config
    assert config.global_config.paths.log_dir == (tmp_path / "logs").resolve()
    assert config.global_config.paths.profiles_dir == (tmp_path / "profiles").resolve()
    # keys_file 相对 profile 目录解析
    assert loaded.config.keymap.actions
    assert (project.profile_dir / "keys.yaml").is_file()


def test_keys_file_missing_is_error(tmp_path: Path) -> None:
    project = fx.build_valid_project(tmp_path)
    (project.profile_dir / "keys.yaml").unlink()
    loaded = load_project(tmp_path, project.client_id)
    assert any(issue.code == "file_missing" and "keys.yaml" in issue.location for issue in loaded.errors)
    assert loaded.config.keymap.actions == {}


def test_config_hash_stable_and_sensitive(tmp_path: Path) -> None:
    project = fx.build_valid_project(tmp_path)
    hash1 = load_project(tmp_path, project.client_id).config.config_hash
    hash2 = load_project(tmp_path, project.client_id).config.config_hash
    assert hash1 == hash2
    assert len(hash1) == 16
    fx.edit_yaml_file(tmp_path / "config.yaml", "runtime.loop_hz", 12)
    hash3 = load_project(tmp_path, project.client_id).config.config_hash
    assert hash3 != hash1
    # 修改地图 YAML 同样影响摘要
    fx.edit_yaml_file(project.map_path, "name", "改名地图")
    hash4 = load_project(tmp_path, project.client_id).config.config_hash
    assert hash4 != hash3


def test_config_hash_independent_of_project_location(tmp_path: Path) -> None:
    """相同内容的工程放在不同绝对路径下，config_hash 应一致（规范化口径）。"""
    project_a = fx.build_valid_project(tmp_path / "a")
    project_b = fx.build_valid_project(tmp_path / "b")
    hash_a = load_project(project_a.root, project_a.client_id).config.config_hash
    hash_b = load_project(project_b.root, project_b.client_id).config.config_hash
    assert hash_a == hash_b


def test_invalid_yaml_syntax_does_not_crash(tmp_path: Path) -> None:
    shutil.copyfile(FIXTURE_DIR / "invalid_yaml" / "bad_syntax.yaml", tmp_path / "config.yaml")
    loaded = load_project(tmp_path, "cn360")
    assert any(issue.code == "yaml_syntax" for issue in loaded.errors)
    assert loaded.config.global_config.runtime.loop_hz == 10  # 回退默认值


def test_profile_feature_missing_region_is_loader_error(tmp_path: Path) -> None:
    project = fx.build_valid_project(tmp_path)
    fx.delete_yaml_key(project.profile_path, "features.port_indicator.region")
    loaded = load_project(tmp_path, project.client_id)
    assert any("port_indicator" in issue.location and issue.code == "missing" for issue in loaded.errors)
    assert "port_indicator" not in loaded.config.profile.features


def test_missing_ship_id_flagged(tmp_path: Path) -> None:
    project = fx.build_valid_project(tmp_path)
    fx.edit_yaml_file(tmp_path / "config.yaml", "ship.ship_id", "")
    loaded = load_project(tmp_path, project.client_id)
    assert any(issue.code == "missing" and "ship.ship_id" in issue.location for issue in loaded.errors)
    with pytest.raises(ConfigError):
        load_app(tmp_path, project.client_id)


def test_load_app_profile_missing(tmp_path: Path) -> None:
    fx.build_valid_project(tmp_path)
    with pytest.raises(ConfigError) as exc_info:
        load_app(tmp_path, "no_such_profile")
    assert any("profile.yaml" in issue.location for issue in exc_info.value.errors)


def test_probe_png_size(tmp_path: Path) -> None:
    image_path = fx.write_png(tmp_path / "a.png", fx.template_image(seed=1, size=12))
    assert probe_png_size(image_path) == (12, 12)
    bad_path = tmp_path / "bad.png"
    bad_path.write_text("not a png", encoding="utf-8")
    assert probe_png_size(bad_path) is None
    assert probe_png_size(tmp_path / "missing.png") is None


def test_map_pack_orientation_and_background(tmp_path: Path) -> None:
    project = fx.build_valid_project(tmp_path)
    fx.edit_yaml_file(project.map_path, "image_orientation", "rot90")
    loaded = load_project(tmp_path, project.client_id)
    pack = loaded.config.maps[project.map_id]
    assert pack.image_orientation.value == "rot90"
    transform = pack.transform()
    image_x, image_y = transform.world_to_image(0.0, 0.0)
    assert 20 <= image_x <= 780
    assert 20 <= image_y <= 780


# ---------------------------------------------------------------------------
# 过载保护（overload 段；未知键 error、缺省用模型默认值）
# ---------------------------------------------------------------------------


def test_load_global_overload_from_real_config(project_root: Path) -> None:
    issues: list = []
    config = load_global(project_root / "config.yaml", project_root=project_root, issues=issues)
    assert issues == []
    overload = config.overload
    assert overload.mode == "monitor"
    assert overload.sample_hz == 1.0
    assert overload.cpu_warn_pct == 90.0
    assert overload.physical_warn_floor_mb == 2500.0
    assert overload.physical_warn_limit_ratio == 0.08
    assert overload.commit_warn_floor_mb == 6000.0
    assert overload.commit_warn_limit_ratio == 0.12
    assert overload.commit_pause_floor_mb == 2000.0
    assert overload.commit_pause_limit_ratio == 0.05
    assert overload.commit_pause_occupancy == 0.8
    assert overload.commit_stop_floor_mb == 1200.0
    assert overload.commit_stop_limit_ratio == 0.025
    assert overload.commit_stop_window_s == 30.0
    assert overload.commit_stop_occupancy == 0.9
    assert overload.commit_fast_stop_floor_mb == 400.0
    assert overload.commit_fast_stop_limit_ratio == 0.01
    assert overload.commit_fast_stop_sustained_s == 10.0
    assert overload.starvation_lateness_s == 2.5
    assert overload.starvation_pause_events == 3
    assert overload.starvation_stop_events == 3
    assert overload.script_private_warn_floor_mb == 1024.0
    assert overload.script_private_baseline_ratio == 2.0
    assert overload.sleep_detect_s == 1.0
    assert overload.sleep_cover_ratio == 0.5
    assert overload.sleep_grace_s == 120.0
    assert overload.clock_jump_s == 2.0
    assert overload.auto_resume_max == 2
    assert overload.auto_resume_repeat_window_s == 600.0


def test_load_global_overload_defaults_when_section_missing(tmp_path: Path) -> None:
    fx.write_yaml(tmp_path / "config.yaml", {"runtime": {"loop_hz": 10}})
    issues: list = []
    config = load_global(tmp_path / "config.yaml", project_root=tmp_path, issues=issues)
    assert issues == []
    assert config.overload.mode == "monitor"
    assert config.overload.commit_pause_floor_mb == 2000.0


def test_load_global_overload_overrides(tmp_path: Path) -> None:
    fx.write_yaml(
        tmp_path / "config.yaml",
        {
            "overload": {
                "mode": "pause_stop",
                "sample_hz": 2,
                "commit_pause_floor_mb": 3000,
                "commit_pause_limit_ratio": 0.06,
                "auto_resume_max": 1,
                "starvation_pause_events": 5,
            }
        },
    )
    issues: list = []
    config = load_global(tmp_path / "config.yaml", project_root=tmp_path, issues=issues)
    assert issues == []
    overload = config.overload
    assert overload.mode == "pause_stop"
    assert overload.sample_hz == 2.0
    assert overload.commit_pause_floor_mb == 3000.0
    assert overload.commit_pause_limit_ratio == 0.06
    assert overload.auto_resume_max == 1
    assert overload.starvation_pause_events == 5


def test_load_global_overload_unknown_key_is_error(tmp_path: Path) -> None:
    fx.write_yaml(
        tmp_path / "config.yaml",
        {"overload": {"mode": "monitor", "commit_pause_floor": 1234}},
    )
    issues: list = []
    config = load_global(tmp_path / "config.yaml", project_root=tmp_path, issues=issues)
    unknown = [issue for issue in issues if issue.code == "unknown_key"]
    assert len(unknown) == 1
    assert "overload.commit_pause_floor" in unknown[0].location
    assert config.overload.commit_pause_floor_mb == 2000.0  # 未知键不影响默认值


def test_load_global_overload_type_errors_fall_back(tmp_path: Path) -> None:
    fx.write_yaml(
        tmp_path / "config.yaml",
        {"overload": {"mode": 3, "sample_hz": "fast", "auto_resume_max": 1.5}},
    )
    issues: list = []
    config = load_global(tmp_path / "config.yaml", project_root=tmp_path, issues=issues)
    codes = {issue.code for issue in issues}
    assert "type" in codes
    assert config.overload.mode == "monitor"
    assert config.overload.sample_hz == 1.0
    assert config.overload.auto_resume_max == 2


def test_overload_unknown_key_fails_validate_project(tmp_path: Path) -> None:
    """validate_project 会收集 loader 的 unknown_key，作为 error 阻止启动。"""
    project = fx.build_valid_project(tmp_path)
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.mode", "monitor")
    fx.edit_yaml_file(tmp_path / "config.yaml", "overload.commit_pause_floor", 1234)
    report = validate_project(tmp_path, project.client_id)
    assert any(issue.code == "unknown_key" for issue in report.errors)
