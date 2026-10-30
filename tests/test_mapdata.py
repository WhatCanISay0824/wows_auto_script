"""地图包加载与校验单元测试（详细设计 4.1~4.7）。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from src.config.loader import load_project
from src.config.validate import ValidationReport, validate_map_pack, validate_project

from tests.fixtures.configs import builder as fx


@pytest.fixture
def project(tmp_path: Path) -> fx.TestProject:
    return fx.build_valid_project(tmp_path)


def _report(tmp_path: Path, project: fx.TestProject) -> ValidationReport:
    return validate_project(tmp_path, project.client_id)


def _edit_route(project: fx.TestProject, route_index: int, mutate: dict) -> None:
    data = fx.load_yaml(project.routes_path)
    data["routes"][route_index].update(mutate)
    fx.write_yaml(project.routes_path, data)


# ---------------------------------------------------------------------------
# 正常加载
# ---------------------------------------------------------------------------


def test_load_map_pack_fields(project: fx.TestProject, tmp_path: Path) -> None:
    loaded = load_project(tmp_path, project.client_id)
    assert loaded.issues == []
    pack = loaded.config.maps[project.map_id]
    assert pack.map_id == project.map_id
    assert pack.name == "测试地图"
    assert pack.size_meters == (12000.0, 12000.0)
    assert pack.world_bounds == (-6000.0, -6000.0, 6000.0, 6000.0)
    assert pack.image_bounds == (20.0, 20.0, 780.0, 780.0)
    assert pack.background_size == (800, 800)
    assert pack.spawn("east_1") is not None
    assert pack.spawn("east_1").position == (4000.0, -4000.0)
    assert [route.route_id for route in pack.routes_for("east_1")] == ["east_1_south"]
    assert pack.route("east_1_south").waypoints[0] == (3500.0, -3500.0)


def test_world_to_image_transform(project: fx.TestProject, tmp_path: Path) -> None:
    pack = load_project(tmp_path, project.client_id).config.maps[project.map_id]
    transform = pack.transform()
    # 世界原点在地图中心 -> 底图可游玩区域中心
    assert transform.world_to_image(0.0, 0.0) == pytest.approx((400.0, 400.0))
    u, v = transform.world_to_image(-6000.0, 6000.0)
    assert (u, v) == pytest.approx((20.0, 20.0))
    x, y = transform.image_to_world(20.0, 20.0)
    assert (x, y) == pytest.approx((-6000.0, 6000.0))


def test_map_orientation_rot90_accepted(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(project.map_path, "image_orientation", "rot90")
    loaded = load_project(tmp_path, project.client_id)
    assert loaded.config.maps[project.map_id].image_orientation.value == "rot90"
    report = _report(tmp_path, project)
    assert report.ok, report.format_lines()


# ---------------------------------------------------------------------------
# 4.7 校验规则：目录/底图/坐标
# ---------------------------------------------------------------------------


def test_directory_name_must_equal_map_id(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(project.map_path, "map_id", "other_map")
    report = _report(tmp_path, project)
    assert any(issue.code == "mismatch" and "map_id" in issue.location for issue in report.errors)


def test_background_missing(project: fx.TestProject, tmp_path: Path) -> None:
    (project.map_dir / "background.png").unlink()
    report = _report(tmp_path, project)
    assert any(issue.code == "file_missing" and "background.png" in issue.location for issue in report.errors)


def test_background_unreadable(project: fx.TestProject, tmp_path: Path) -> None:
    (project.map_dir / "background.png").write_text("not a png", encoding="utf-8")
    report = _report(tmp_path, project)
    assert any(issue.code == "file_unreadable" and "background.png" in issue.location for issue in report.errors)


def test_image_bounds_outside_background(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(project.map_path, "image_bounds", [20, 20, 900, 780])
    report = _report(tmp_path, project)
    assert any(issue.code == "range" and "image_bounds" in issue.location for issue in report.errors)


def test_invalid_world_bounds(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(project.map_path, "world_bounds", [6000, 6000, -6000, -6000])
    report = _report(tmp_path, project)
    assert any(issue.code == "range" and "world_bounds" in issue.location for issue in report.errors)


def test_size_meters_mismatch_warns(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(project.map_path, "size_meters", [10000, 12000])
    report = _report(tmp_path, project)
    assert report.ok
    assert any(issue.code == "size_mismatch" for issue in report.warnings)


# ---------------------------------------------------------------------------
# 4.7 校验规则：出生点
# ---------------------------------------------------------------------------


def test_spawn_outside_world_bounds(project: fx.TestProject, tmp_path: Path) -> None:
    data = fx.load_yaml(project.spawns_path)
    data["spawns"][0]["position"] = [7000, -4000]
    fx.write_yaml(project.spawns_path, data)
    report = _report(tmp_path, project)
    assert any(issue.code == "range" and "east_1" in issue.location for issue in report.errors)


def test_spawns_too_close_warns(project: fx.TestProject, tmp_path: Path) -> None:
    data = fx.load_yaml(project.spawns_path)
    data["spawns"][1]["position"] = [4100, -4000]
    fx.write_yaml(project.spawns_path, data)
    report = _report(tmp_path, project)
    assert report.ok
    assert any(issue.code == "spawn_too_close" for issue in report.warnings)


def test_duplicate_spawn_id(project: fx.TestProject, tmp_path: Path) -> None:
    data = fx.load_yaml(project.spawns_path)
    data["spawns"].append({"spawn_id": "east_1", "position": [0, 0]})
    fx.write_yaml(project.spawns_path, data)
    report = _report(tmp_path, project)
    assert any(issue.code == "duplicate" and "spawn" in issue.location for issue in report.errors)


def test_invalid_spawn_facing_warns(project: fx.TestProject, tmp_path: Path) -> None:
    data = fx.load_yaml(project.spawns_path)
    data["spawns"][0]["facing_deg"] = 720
    fx.write_yaml(project.spawns_path, data)
    report = _report(tmp_path, project)
    assert any(issue.code == "range" and "facing_deg" in issue.location for issue in report.warnings)


def test_empty_spawns_warns(project: fx.TestProject, tmp_path: Path) -> None:
    fx.write_yaml(project.spawns_path, {"spawns": []})
    report = _report(tmp_path, project)
    assert any(issue.code == "empty" and "spawns" in issue.location for issue in report.warnings)
    # 没有出生点时航线引用会连带报 unknown_spawn（错误级）
    assert any(issue.code == "unknown_spawn" for issue in report.errors)


# ---------------------------------------------------------------------------
# 4.7 校验规则：航线
# ---------------------------------------------------------------------------


def test_route_unknown_spawn(project: fx.TestProject, tmp_path: Path) -> None:
    _edit_route(project, 0, {"spawn_id": "ghost_spawn"})
    report = _report(tmp_path, project)
    assert any(issue.code == "unknown_spawn" for issue in report.errors)


def test_route_needs_two_waypoints(project: fx.TestProject, tmp_path: Path) -> None:
    _edit_route(project, 0, {"waypoints": [[3500, -3500]]})
    report = _report(tmp_path, project)
    assert any(issue.code == "range" and "waypoints" in issue.location for issue in report.errors)


def test_waypoint_outside_world_bounds(project: fx.TestProject, tmp_path: Path) -> None:
    _edit_route(project, 0, {"waypoints": [[3500, -3500], [7000, 3000]]})
    report = _report(tmp_path, project)
    assert any(issue.code == "range" and "waypoints[1]" in issue.location for issue in report.errors)


def test_first_waypoint_far_from_spawn_warns(project: fx.TestProject, tmp_path: Path) -> None:
    _edit_route(project, 0, {"waypoints": [[1000, -1000], [0, 1000]]})
    report = _report(tmp_path, project)
    assert any(issue.code == "waypoint_far" and "waypoints[0]" in issue.location for issue in report.warnings)


def test_adjacent_waypoints_too_close_warns(project: fx.TestProject, tmp_path: Path) -> None:
    _edit_route(project, 0, {"waypoints": [[3500, -3500], [3400, -3400], [0, 1000]]})
    report = _report(tmp_path, project)
    assert any(issue.code == "waypoint_close" for issue in report.warnings)


def test_adjacent_waypoints_too_far_warns(project: fx.TestProject, tmp_path: Path) -> None:
    _edit_route(project, 0, {"waypoints": [[3500, -3500], [-5900, -5900], [5900, 5900]]})
    report = _report(tmp_path, project)
    assert any(issue.code == "waypoint_far" and issue.level.value == "warning" for issue in report.warnings)


def test_duplicate_route_id(project: fx.TestProject, tmp_path: Path) -> None:
    data = fx.load_yaml(project.routes_path)
    data["routes"].append(
        {"route_id": "east_1_south", "spawn_id": "east_1", "waypoints": [[0, 0], [100, 100]]}
    )
    fx.write_yaml(project.routes_path, data)
    report = _report(tmp_path, project)
    assert any(issue.code == "duplicate" and "route" in issue.location for issue in report.errors)


def test_negative_and_zero_route_weight(project: fx.TestProject, tmp_path: Path) -> None:
    _edit_route(project, 0, {"weight": -1.0})
    _edit_route(project, 1, {"weight": 0.0})
    report = _report(tmp_path, project)
    assert any(issue.code == "range" and "weight" in issue.location for issue in report.errors)
    assert any(issue.code == "zero_weight" for issue in report.warnings)


def test_empty_routes_warns(project: fx.TestProject, tmp_path: Path) -> None:
    fx.write_yaml(project.routes_path, {"routes": []})
    report = _report(tmp_path, project)
    assert report.ok
    assert any(issue.code == "empty" and "routes" in issue.location for issue in report.warnings)


# ---------------------------------------------------------------------------
# 4.7 校验规则：方向与 world_to_image
# ---------------------------------------------------------------------------


def test_invalid_orientation_is_error(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(project.map_path, "image_orientation", "diagonal")
    report = _report(tmp_path, project)
    assert any(issue.code == "enum" and "image_orientation" in issue.location for issue in report.errors)
    # 非法方向的地图包被跳过加载
    assert load_project(tmp_path, project.client_id).config.maps == {}


def test_world_to_image_not_invertible_ignored(project: fx.TestProject, tmp_path: Path) -> None:
    fx.edit_yaml_file(project.map_path, "world_to_image", [0, 0, 0, 0, 0, 0])
    pack = load_project(tmp_path, project.client_id).config.maps[project.map_id]
    assert pack.world_to_image is None
    report = _report(tmp_path, project)
    assert any(issue.code == "world_to_image_invertible" for issue in report.issues)


def test_world_to_image_conflict_warns(project: fx.TestProject, tmp_path: Path) -> None:
    # 与 bounds+orientation 一致时无警告；平移 100 像素后应以矩阵为准并警告
    consistent = [760 / 12000, 0.0, 400.0, 0.0, -760 / 12000, 400.0]
    fx.edit_yaml_file(project.map_path, "world_to_image", consistent)
    report = _report(tmp_path, project)
    assert not any(issue.code == "world_to_image_conflict" for issue in report.issues)
    conflicting = [760 / 12000, 0.0, 500.0, 0.0, -760 / 12000, 400.0]
    fx.edit_yaml_file(project.map_path, "world_to_image", conflicting)
    report = _report(tmp_path, project)
    assert any(issue.code == "world_to_image_conflict" for issue in report.warnings)
    pack = load_project(tmp_path, project.client_id).config.maps[project.map_id]
    assert pack.world_to_image is not None  # 矩阵有效：实现以矩阵为准


def test_duplicate_map_id_across_directories(project: fx.TestProject, tmp_path: Path) -> None:
    copy_dir = project.maps_dir / "copy_of_map"
    shutil.copytree(project.map_dir, copy_dir)
    fx.edit_yaml_file(copy_dir / "map.yaml", "map_id", project.map_id)
    report = _report(tmp_path, project)
    assert any(issue.code == "duplicate" and "map" in issue.location for issue in report.errors)


def test_validate_map_pack_directly(project: fx.TestProject, tmp_path: Path) -> None:
    loaded = load_project(tmp_path, project.client_id)
    pack = loaded.config.maps[project.map_id]
    assert validate_map_pack(pack) == []
