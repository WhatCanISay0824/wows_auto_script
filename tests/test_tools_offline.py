"""T6 工具链离线测试（详细设计 12.1 / 第 16 章 V27、V28）。

覆盖范围（全部离线：不截游戏窗口、不发送键鼠、不进入 tkinter mainloop）：

- ``calibrate_common``：画布↔图像坐标、拖框换算、归一化矩形、8 方向预览、
  带注释头的 YAML 读写、模板质量检查；
- ``check_env``：offline 模式在完整测试工程上运行、退出码语义（0/3/2）、报告文件；
- ``record`` / ``replay``：用假帧源录制合成画面，再用同一套感知代码回放，
  校验帧数、特征命中、状态分布与人工标签准确率；
- ``stats``：临时 SQLite 的会话/场次/周转耗时/攻击比例统计；
- ``calibrate_map`` / ``calibrate_profile`` / ``calibrate_ship`` / ``capture_marker``：
  加载→框选→校验→保存的纯函数流程（不创建窗口）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from src.common.clock import FakeClock, FakeSleeper
from src.common.coords import ImageOrientation
from src.common.types import NormalizedRect, Rect
from src.config.loader import load_project, load_ship
from src.config.validate import validate_ship
from src.perception.capture import Frame
from src.storage.database import Database
from src.tools import calibrate_common as cc
from src.tools import calibrate_map, calibrate_profile, calibrate_ship, capture_marker
from src.tools.check_env import CheckResult, CheckStatus, EnvironmentReport, run_checks
from src.tools.record import RecordOptions, load_metadata, record_frames
from src.tools.replay import build_recognizers, replay_frames
from src.tools.stats import collect_stats, format_report, open_database_for_stats

from tests.fixtures.configs.builder import build_valid_project, write_yaml
from tests.helpers import sample_background

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL_NAMES = (
    "check_env",
    "calibrate_common",
    "calibrate_map",
    "calibrate_profile",
    "calibrate_ship",
    "capture_marker",
    "record",
    "replay",
    "stats",
)


# ---------------------------------------------------------------------------
# calibrate_common：坐标换算与方向预览
# ---------------------------------------------------------------------------


def test_viewport_image_canvas_roundtrip() -> None:
    viewport = cc.Viewport(image_width=200, image_height=100, canvas_width=400, canvas_height=300).fit()
    assert viewport.zoom > 0
    for point in ((0.0, 0.0), (199.0, 99.0), (37.5, 12.25)):
        canvas = viewport.image_to_canvas(*point)
        back = viewport.canvas_to_image(*canvas)
        assert back[0] == pytest.approx(point[0], abs=1e-9)
        assert back[1] == pytest.approx(point[1], abs=1e-9)


def test_viewport_zoom_at_keeps_anchor_and_clamps() -> None:
    viewport = cc.Viewport(image_width=200, image_height=100, canvas_width=400, canvas_height=300)
    zoomed = viewport.zoom_at(120.0, 80.0, 2.0)
    assert zoomed.zoom == pytest.approx(2.0)
    anchor = zoomed.canvas_to_image(120.0, 80.0)
    assert anchor[0] == pytest.approx(120.0)
    assert anchor[1] == pytest.approx(80.0)
    clamped = viewport.zoom_at(0.0, 0.0, 1e9)
    assert clamped.zoom == pytest.approx(cc.MAX_ZOOM)
    with pytest.raises(ValueError):
        viewport.zoom_at(0.0, 0.0, 0.0)


def test_selection_from_canvas_drag_and_normalized() -> None:
    viewport = cc.Viewport(image_width=100, image_height=50, canvas_width=100, canvas_height=50)
    # 反向拖拽也应得到左上角在前的矩形
    selection = cc.selection_from_canvas_drag(viewport, (60.0, 40.0), (10.0, 10.0))
    assert selection is not None
    assert selection.to_tuple() == (10, 10, 50, 30)
    rect = selection.normalized(100, 50)
    assert rect.to_list() == pytest.approx([0.1, 0.2, 0.6, 0.8])
    # 点击级拖拽返回 None
    assert cc.selection_from_canvas_drag(viewport, (10.0, 10.0), (11.0, 11.0)) is None
    # 超出图像范围时被裁剪
    clipped = cc.selection_from_canvas_drag(viewport, (-20.0, -20.0), (200.0, 100.0))
    assert clipped is not None
    assert clipped.to_tuple() == (0, 0, 100, 50)


@pytest.mark.parametrize(
    ("orientation", "expected_transform"),
    [
        (ImageOrientation.NORMAL, lambda image: image),
        (ImageOrientation.ROT180, lambda image: image[::-1, ::-1]),
        (ImageOrientation.MIRROR_H, lambda image: image[:, ::-1]),
        (ImageOrientation.MIRROR_V, lambda image: image[::-1, :]),
    ],
)
def test_canonicalize_image_exact_for_axis_flips(orientation: ImageOrientation, expected_transform) -> None:
    image = np.arange(12 * 16 * 3, dtype=np.uint8).reshape(12, 16, 3)
    preview = cc.canonicalize_image(image, (0.0, 0.0, 16.0, 12.0), orientation)
    assert preview.shape == image.shape
    assert np.array_equal(preview, expected_transform(image))


def test_canonicalize_image_swaps_axes_for_quarter_turns() -> None:
    image = np.zeros((12, 16, 3), dtype=np.uint8)
    image[0, 0] = (255, 0, 0)
    for orientation in (ImageOrientation.ROT90, ImageOrientation.ROT270, ImageOrientation.MIRROR_ROT90):
        preview = cc.canonicalize_image(image, (0.0, 0.0, 16.0, 12.0), orientation)
        assert preview.shape == (16, 12, 3)


def test_world_to_image_points_reuse_common_coords() -> None:
    world_bounds = (-6000.0, -6000.0, 6000.0, 6000.0)
    image_bounds = (100.0, 80.0, 1300.0, 1280.0)
    points = cc.world_to_image_points(
        [(0.0, 0.0), (-6000.0, -6000.0)], world_bounds, image_bounds, ImageOrientation.NORMAL
    )
    assert points[0] == pytest.approx((700.0, 680.0))
    assert points[1] == pytest.approx((100.0, 1280.0))


def test_parse_and_format_numbers() -> None:
    assert cc.parse_number_pair("12000,12000") == (12000.0, 12000.0)
    assert cc.parse_number_quad("1 2 3 4") == (1.0, 2.0, 3.0, 4.0)
    assert cc.format_numbers((1.0, 2.5, 0.0)) == "1, 2.5, 0"
    with pytest.raises(ValueError):
        cc.parse_number_quad("1,2,3")
    with pytest.raises(ValueError):
        cc.parse_number("nan")


def test_yaml_roundtrip_preserves_leading_comments(tmp_path: Path) -> None:
    path = tmp_path / "map.yaml"
    path.write_text("# 头部注释\n# 第二行\n\nname: 旧名字\n", encoding="utf-8")
    cc.write_yaml_mapping(path, {"name": "新名字", "nested": {"values": [1, 2]}})
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# 头部注释\n# 第二行")
    data = cc.read_yaml_mapping(path)
    assert data["name"] == "新名字"
    assert data["nested"]["values"] == [1, 2]
    # 新文件按给定 header 写入
    fresh = tmp_path / "fresh.yaml"
    cc.write_yaml_mapping(fresh, {"a": 1}, header="# 模板注释")
    assert fresh.read_text(encoding="utf-8").startswith("# 模板注释")


def test_template_quality_rejects_small_and_uniform() -> None:
    uniform = np.full((20, 20, 3), 30, dtype=np.uint8)
    quality = cc.inspect_template(uniform)
    assert not quality.ok
    assert any("纯色" in message for message in quality.errors)
    tiny = sample_background(6, 6, seed=1)
    assert not cc.inspect_template(tiny).ok
    good = sample_background(40, 30, seed=2)
    assert cc.inspect_template(good).ok
    with pytest.raises(ValueError):
        cc.crop_region(np.zeros((10, 10, 3), np.uint8), cc.BoxSelection(5, 5, 10, 10))


def test_save_and_read_image_unicode_path(tmp_path: Path) -> None:
    image = sample_background(64, 48, seed=3)
    path = tmp_path / "中文目录" / "截图.png"
    cc.write_image(path, image)
    loaded = cc.read_image(path)
    assert np.array_equal(loaded, image)


# ---------------------------------------------------------------------------
# check_env：offline 模式与退出码语义
# ---------------------------------------------------------------------------


def test_environment_report_exit_code_semantics() -> None:
    def report(*statuses: CheckStatus) -> EnvironmentReport:
        return EnvironmentReport(
            mode="test",
            results=tuple(CheckResult(name=f"check_{index}", status=status) for index, status in enumerate(statuses)),
        )

    assert report(CheckStatus.PASS, CheckStatus.PASS).exit_code == 0
    assert report(CheckStatus.PASS, CheckStatus.SKIP).exit_code == 3
    assert report(CheckStatus.FAIL, CheckStatus.SKIP).exit_code == 2
    lines = report(CheckStatus.PASS).format_lines(PROJECT_ROOT)
    assert any("汇总" in line for line in lines)


def test_check_env_offline_on_complete_project(tmp_path: Path) -> None:
    """完整测试工程在 offline 模式下：无 FAIL，installed 检查记 SKIP，退出码 3。"""
    project = build_valid_project(tmp_path)
    report = run_checks(project.root, project.client_id, offline=True)
    names = {item.name: item for item in report.results}
    assert "config_validate" in names
    assert names["config_validate"].status is CheckStatus.PASS
    assert names["geometry_roundtrip"].status is CheckStatus.PASS
    assert names["map_orb_selfcheck"].status is CheckStatus.PASS
    # installed 专属检查在 offline 下全部 SKIP
    assert all(names[name].status is CheckStatus.SKIP for name in ("game_window", "capture_not_black", "ship_card"))
    assert report.failed == [], [item.message for item in report.failed]
    assert report.exit_code == 3
    assert report.report_path is not None and report.report_path.is_file()
    text = report.report_path.read_text(encoding="utf-8")
    assert "环境检查报告" in text
    assert "退出码" in text


def test_check_env_offline_marks_pending_calibration_as_skip(tmp_path: Path) -> None:
    """缺少模板/地图（M0 预期状态）应记为 SKIP 而不是 FAIL。"""
    project = build_valid_project(tmp_path)
    # 删除全部特征模板与地图包，模拟“尚未实机标定”的仓库状态
    for template_path in project.profile_dir.glob("features/*.png"):
        template_path.unlink()
    for marker_path in project.profile_dir.glob("marker/*.png"):
        marker_path.unlink()
    (project.ship_dir / "card.png").unlink()
    for loadout_path in (project.ship_dir / "loadout").glob("*.png"):
        loadout_path.unlink()
    for map_dir in project.maps_dir.glob("*"):
        for file_path in map_dir.glob("*"):
            file_path.unlink()
        map_dir.rmdir()
    report = run_checks(project.root, project.client_id, offline=True, report_path=tmp_path / "env.txt")
    results = {item.name: item for item in report.results}
    assert results["config_validate"].status is CheckStatus.SKIP
    assert results["map_orb_selfcheck"].status is CheckStatus.SKIP
    assert report.failed == []
    assert report.exit_code == 3
    assert (tmp_path / "env.txt").is_file()


# ---------------------------------------------------------------------------
# record / replay：合成画面往返
# ---------------------------------------------------------------------------


class _FakeCapture:
    """假帧源：依次返回预先构造的帧，不访问窗口/屏幕。"""

    def __init__(self, frames: list[Frame]) -> None:
        self._frames = frames
        self._index = 0

    def capture(self) -> Frame:
        if self._index >= len(self._frames):
            raise AssertionError("录制的帧数超过预期")
        frame = self._frames[self._index]
        self._index += 1
        return frame


class _FakeLabels:
    """假状态标签源：前 N 次 poll 返回固定标签。"""

    def __init__(self, label: str, times: int = 1) -> None:
        self._label = label
        self._remaining = times

    def poll(self) -> str | None:
        if self._remaining > 0:
            self._remaining -= 1
            return self._label
        return None


def _make_feature_frame(template: np.ndarray, *, width: int = 640, height: int = 400) -> np.ndarray:
    """构造“界面特征命中”的合成帧：纯色背景 + 在 port_indicator 搜索区域内贴模板。"""
    image = np.full((height, width, 3), 60, dtype=np.uint8)
    # port_indicator 的归一化 region 是 [0,0,0.35,0.12]，贴到该区域内
    x, y = 50, 30
    image[y : y + template.shape[0], x : x + template.shape[1]] = template
    return image


def test_record_and_replay_roundtrip(tmp_path: Path) -> None:
    """假帧录制 2 帧 -> 回放应识别出 PORT、命中 port_indicator、标签准确率 100%。"""
    project = build_valid_project(tmp_path)
    template = cc.read_image(project.profile_dir / "features" / "port_indicator.png")
    frame_image = _make_feature_frame(template)
    frames = [
        Frame(
            image=frame_image.copy(),
            timestamp=float(index),
            window_rect=Rect(0, 0, frame_image.shape[1], frame_image.shape[0]),
            frame_id=index + 1,
        )
        for index in range(2)
    ]
    clock = FakeClock()
    recording_dir = tmp_path / "recording"
    options = RecordOptions(
        output_dir=recording_dir,
        capture_hz=10.0,
        duration_s=None,
        max_frames=2,
        downscale=1.0,
    )
    metadata = record_frames(
        _FakeCapture(frames),
        options,
        clock=clock,
        sleeper=FakeSleeper(clock),
        label_source=_FakeLabels("PORT", times=1),
        profile_id=project.client_id,
        client_id=project.client_id,
        reference_resolution=(640, 400),
    )
    assert metadata.frame_count == 2
    assert metadata.frame_size == (640, 400)
    assert (recording_dir / "metadata.json").is_file()
    assert len(list((recording_dir / "frames").glob("*.png"))) == 2
    assert metadata.state_at(1) == "PORT"

    config = load_project(project.root, project.client_id).config
    recognizers = build_recognizers(config, metadata, root=project.root)
    report = replay_frames(recording_dir, metadata, recognizers)
    assert report.frames_processed == 2
    assert report.frames_failed == 0
    assert report.state_counts.get("PORT") == 2
    assert report.features["port_indicator"].hits == 2
    assert report.accuracy == pytest.approx(1.0)
    assert report.errors == []
    lines = "\n".join(report.format_lines())
    assert "回放识别报告" in lines
    # 回放不产生任何输入动作：这里只验证报告结构中没有动作字段
    assert "action" not in report.to_dict()


def test_record_metadata_roundtrip_and_roi_crop(tmp_path: Path) -> None:
    """metadata 序列化往返 + ROI 裁剪 + 降采样尺寸。"""
    image = sample_background(160, 120, seed=4)
    frame = Frame(image=image, timestamp=2.0, window_rect=Rect(10, 20, 160, 120), frame_id=1)
    clock = FakeClock()
    recording_dir = tmp_path / "recording"
    options = RecordOptions(
        output_dir=recording_dir,
        capture_hz=2.0,
        duration_s=None,
        max_frames=1,
        downscale=2.0,
        roi=Rect(40, 20, 80, 60),
    )
    metadata = record_frames(
        _FakeCapture([frame]),
        options,
        clock=clock,
        sleeper=FakeSleeper(clock),
        profile_id="testclient",
    )
    assert metadata.frame_size == (40, 30)  # ROI 80x60 再降采样 2 倍
    assert metadata.roi == [40.0, 20.0, 120.0, 80.0]
    loaded = load_metadata(recording_dir)
    assert loaded.frame_count == 1
    assert loaded.frame_size == (40, 30)
    assert loaded.window_rect == (10, 20, 160, 120)


# ---------------------------------------------------------------------------
# stats：临时数据库统计
# ---------------------------------------------------------------------------


def _populate_battle(
    database: Database,
    clock: FakeClock,
    *,
    result: str,
    exit_method: str,
    death_to_exit_s: float,
    exit_to_port_s: float,
) -> int:
    battle_id = database.begin_battle(1, "test_map")
    clock.advance(10.0)
    database.mark_queued(battle_id)
    clock.advance(100.0)
    database.mark_death(battle_id)
    clock.advance(death_to_exit_s)
    database.mark_exit_requested(battle_id)
    clock.advance(exit_to_port_s)
    database.mark_returned_to_port(battle_id)
    database.end_battle(battle_id, result, exit_method)
    return battle_id


def test_stats_collect_from_temp_database(tmp_path: Path) -> None:
    """统计工具应输出场次数、完成率、周转耗时中位数与开火比例。"""
    database_path = tmp_path / "data" / "wows_script.db"
    clock = FakeClock()
    database = Database(database_path, clock=clock)
    database.reconnect()
    session_id = database.begin_session("testclient", "hash16")
    first = _populate_battle(
        database, clock, result="dead", exit_method="menu", death_to_exit_s=2.0, exit_to_port_s=30.0
    )
    second = _populate_battle(
        database, clock, result="finished", exit_method="confirm", death_to_exit_s=4.0, exit_to_port_s=10.0
    )
    third = database.begin_battle(session_id, "test_map")
    database.end_battle(third, "aborted", "none")
    for _ in range(3):
        database.log_action(first, "fire_burst", {"count": 2})
    database.log_action(first, "consumable", {"slot": "1"})
    database.log_action(second, "nav_stuck", {"distance": 2})
    database.log_action(second, "nav_abandon", {"reason": "lost"})
    database.log_state(session_id, first, "ERROR_RECOVERY", None, {"attempt": 1})
    database.end_session(session_id, "done", 3)
    database.close()

    opened = open_database_for_stats(database_path)
    try:
        report = collect_stats(opened)
    finally:
        opened.close()
    assert report.sessions == 1
    assert report.battles == 3
    assert report.completed == 2
    assert report.aborted == 1
    assert report.completion_rate == pytest.approx(2 / 3)
    assert report.median_death_to_exit_s == pytest.approx(3.0)
    assert report.median_exit_to_port_s == pytest.approx(20.0)
    assert report.median_death_to_port_s == pytest.approx(23.0)
    assert report.fire_actions == 3
    assert report.consumable_actions == 1
    assert report.fire_ratio == pytest.approx(0.75)
    assert report.nav_stuck == 1
    assert report.nav_abandon == 1
    assert report.recovery_events == 1
    assert report.exit_method_counts.get("menu") == 1
    lines = "\n".join(format_report(report))
    assert "完成率" in lines
    assert "开火比例" in lines
    payload = report.to_dict()
    assert payload["battles"] == 3
    assert payload["completion_rate"] == pytest.approx(2 / 3, rel=1e-3)


def test_stats_missing_database_reports_error(tmp_path: Path) -> None:
    from src.common.errors import DatabaseError

    with pytest.raises(DatabaseError):
        open_database_for_stats(tmp_path / "missing.db")


# ---------------------------------------------------------------------------
# calibrate_map：加载、校验与保存
# ---------------------------------------------------------------------------


def test_calibrate_map_load_validate_and_save(tmp_path: Path) -> None:
    map_dir = tmp_path / "maps" / "north_sea"
    map_dir.mkdir(parents=True)
    background = sample_background(800, 800, seed=5)
    cc.write_image(map_dir / "background.png", background)

    state = calibrate_map.load_editor_state(map_dir)
    assert state.map_id == "north_sea"
    assert state.image_size == (800, 800)
    errors = calibrate_map.apply_field_texts(
        state,
        name="北方海域",
        size_meters_text="12000, 12000",
        world_bounds_text="-6000, -6000, 6000, 6000",
        image_bounds_text="0, 0, 800, 800",
        orientation=ImageOrientation.NORMAL,
        notes="测试地图",
    )
    assert errors == []
    calibrate_map.apply_image_bounds_selection(state, cc.BoxSelection(50, 40, 700, 720))
    assert state.image_bounds == (50.0, 40.0, 750.0, 760.0)
    # 世界坐标中心应落在可游玩区域中心
    center = state.world_to_image_xy(0.0, 0.0)
    assert center == pytest.approx((400.0, 400.0))
    assert calibrate_map.add_spawn(state, calibrate_map.SpawnEdit("east_1", 4000, -4000, 270, "东侧")) is None
    assert calibrate_map.add_spawn(state, calibrate_map.SpawnEdit("east_1", 0, 0)) is not None
    route = calibrate_map.RouteEdit("east_south", "east_1", 1.0, "向南", [(3500, -3500), (1500, -1500)])
    assert calibrate_map.add_route(state, route) is None
    assert calibrate_map.append_waypoint(state, "east_south", 500, 500) is None

    result = calibrate_map.save_editor_state(state)
    assert result.saved is True
    assert result.errors == []
    for name in ("map.yaml", "spawns.yaml", "routes.yaml"):
        assert (map_dir / name).is_file()
    text = (map_dir / "map.yaml").read_text(encoding="utf-8")
    assert "地图元数据" in text  # 新建文件的注释模板
    assert "east_1" in text or "east_1" in (map_dir / "spawns.yaml").read_text(encoding="utf-8")

    reloaded = calibrate_map.load_editor_state(map_dir)
    assert reloaded.name == "北方海域"
    assert reloaded.orientation is ImageOrientation.NORMAL
    assert reloaded.spawns[0].spawn_id == "east_1"
    assert reloaded.routes[0].waypoints[-1] == pytest.approx((500.0, 500.0))

    # 再次保存应保留已有注释头，且越界航点阻止保存
    calibrate_map.append_waypoint(reloaded, "east_south", 99999, 0)
    blocked = calibrate_map.save_editor_state(reloaded)
    assert blocked.saved is False
    assert any("超出" in issue.message for issue in blocked.errors)
    assert (map_dir / "map.yaml").read_text(encoding="utf-8").startswith("# 地图元数据")


def test_calibrate_map_requires_background_for_save(tmp_path: Path) -> None:
    map_dir = tmp_path / "maps" / "empty_map"
    map_dir.mkdir(parents=True)
    state = calibrate_map.load_editor_state(map_dir)
    issues = calibrate_map.validate_editor_state(state)
    assert any("底图" in issue.message for issue in issues if issue.is_error)
    assert calibrate_map.save_editor_state(state).saved is False


# ---------------------------------------------------------------------------
# calibrate_profile / calibrate_ship / capture_marker：纯函数保存流程
# ---------------------------------------------------------------------------


def test_calibrate_profile_save_flow(tmp_path: Path) -> None:
    profile_dir = tmp_path / "profiles" / "testclient"
    (profile_dir / "features").mkdir(parents=True)
    profile_path = profile_dir / "profile.yaml"
    write_yaml(
        profile_path,
        {
            "client_id": "testclient",
            "display_name": "测试客户端",
            "language": "zh_cn",
            "reference_resolution": [640, 400],
            "window": {"process_names": ["test.exe"], "title_patterns": ["测试"]},
        },
    )
    screenshot = sample_background(640, 400, seed=9)
    screenshot_path = tmp_path / "shot.png"
    cc.write_image(screenshot_path, screenshot)
    state = calibrate_profile.load_profile_editor(profile_path, screenshot_path)
    assert state.frame_size == (640, 400)

    state.minimap_rect = NormalizedRect.from_list([0.80, 0.02, 0.98, 0.30])
    state.border_px = 2
    state.hp_region = NormalizedRect.from_list([0.02, 0.86, 0.20, 0.93])
    template_box = cc.BoxSelection(100, 100, 120, 60)
    template = cc.crop_region(screenshot, template_box)
    region = calibrate_profile.search_region_for_box(template_box, 640, 400)
    calibrate_profile.add_feature(
        state.data,
        "port_indicator",
        template=calibrate_profile.feature_template_rel("port_indicator"),
        region=region,
        threshold=0.85,
    )
    state.pending_templates["port_indicator"] = template

    result = calibrate_profile.save_profile_editor(state)
    assert result.saved is True
    assert (profile_dir / "features" / "port_indicator.png").is_file()
    data = cc.read_yaml_mapping(profile_path)
    assert data["minimap"]["rect"] == pytest.approx([0.8, 0.02, 0.98, 0.3])
    assert data["minimap"]["border_px"] == 2
    assert data["battle_hud"]["hp_bar_region"] == pytest.approx([0.02, 0.86, 0.2, 0.93])
    assert data["features"]["port_indicator"]["template"] == "features/port_indicator.png"
    assert data["features"]["port_indicator"]["threshold"] == pytest.approx(0.85)


def test_calibrate_ship_save_flow(tmp_path: Path) -> None:
    ship_dir = tmp_path / "profiles" / "testclient" / "ships" / "test_ship"
    ship_dir.mkdir(parents=True)
    write_yaml(
        ship_dir / "ship.yaml",
        {
            "ship_id": "test_ship",
            "display_name": "测试船",
            "card_template": "card.png",
            "loadout_slots": [
                {
                    "slot_id": "signal_flags",
                    "name": "信号旗槽位",
                    "empty_template": "loadout/signal_flags_empty.png",
                }
            ],
        },
    )
    screenshot = sample_background(640, 400, seed=11)
    screenshot_path = tmp_path / "port.png"
    cc.write_image(screenshot_path, screenshot)
    state = calibrate_ship.load_ship_editor(ship_dir, screenshot_path)
    assert state.ship_id == "test_ship"
    state.card_image = cc.crop_region(screenshot, cc.BoxSelection(200, 150, 160, 80))
    state.loadout_images["economic_bonus"] = cc.crop_region(screenshot, cc.BoxSelection(60, 60, 100, 50))
    calibrate_ship.upsert_loadout_slot(
        state.data,
        "economic_bonus",
        name="经济加成槽位",
        empty_template=calibrate_ship.empty_template_rel("economic_bonus"),
    )
    result = calibrate_ship.save_ship_editor(state)
    assert result.saved is True
    assert (ship_dir / "card.png").is_file()
    assert (ship_dir / "loadout" / "economic_bonus_empty.png").is_file()
    data = cc.read_yaml_mapping(ship_dir / "ship.yaml")
    slot_ids = [slot["slot_id"] for slot in data["loadout_slots"]]
    assert slot_ids == ["signal_flags", "economic_bonus"]
    assert data["ship_id"] == "test_ship"

    # 编号与目录不一致时拒绝保存
    state.data["ship_id"] = "other_ship"
    state.ship_id = "other_ship"
    blocked = calibrate_ship.save_ship_editor(state)
    assert blocked.saved is False
    assert any("不一致" in message for message in blocked.errors)


def test_calibrate_ship_selected_template_and_empty_loadout(tmp_path: Path) -> None:
    """ISS-007 收尾：保存已选中态模板；空 loadout_slots 允许保存（改 warning）。"""
    ship_dir = tmp_path / "profiles" / "testclient" / "ships" / "new_ship"
    ship_dir.mkdir(parents=True)
    write_yaml(
        ship_dir / "ship.yaml",
        {
            "ship_id": "new_ship",
            "display_name": "新船",
            "card_template": "card.png",
            "card_search_region": [0.0, 0.8, 1.0, 1.0],
            "loadout_slots": [],
            "hp_bar_template": "hp_bar.png",  # 未知字段应保留
        },
    )
    screenshot = sample_background(640, 400, seed=21)
    screenshot_path = tmp_path / "port.png"
    cc.write_image(screenshot_path, screenshot)
    state = calibrate_ship.load_ship_editor(ship_dir, screenshot_path)
    state.card_image = cc.crop_region(screenshot, cc.BoxSelection(200, 320, 160, 50))
    state.selected_image = cc.crop_region(screenshot, cc.BoxSelection(200, 300, 160, 70))

    # 空 loadout_slots 不再是 error；仅提示（信号旗由 profile.ship_check_sequence 校验）
    assert calibrate_ship.validate_ship_editor(state) == []
    warnings = calibrate_ship.ship_editor_warnings(state)
    assert any("空装备槽" in message for message in warnings)
    assert not any("selected.png" in message for message in warnings)  # 本次已框选

    result = calibrate_ship.save_ship_editor(state)
    assert result.saved is True
    assert (ship_dir / "card.png").is_file()
    assert (ship_dir / "selected.png").is_file()
    data = cc.read_yaml_mapping(ship_dir / "ship.yaml")
    assert data["loadout_slots"] == []
    assert data["selected_feature"] == "selected.png"
    assert data["selected_region"] == pytest.approx([0.0, 0.8, 1.0, 1.0])
    assert data["selected_threshold"] == pytest.approx(0.78)
    assert data["hp_bar_template"] == "hp_bar.png"
    # 工具生成的 ship.yaml 必须能被正式 loader 读取（selected.png 已存在 → selected_path 可解析）
    ship = load_ship(ship_dir / "ship.yaml")
    assert ship.selected_feature == "selected.png"
    assert ship.selected_path == (ship_dir / "selected.png").resolve()
    assert ship.selected_region == NormalizedRect(0.0, 0.8, 1.0, 1.0)
    assert ship.loadout_slots == ()  # 空 loadout_slots 允许保存


def test_calibrate_ship_card_only_keeps_existing_selected_asset(tmp_path: Path) -> None:
    """只框 card 仍可保存；不得覆盖/删除已标定的 selected.png，也不报 selected 错误。"""
    ship_dir = tmp_path / "profiles" / "testclient" / "ships" / "card_only"
    ship_dir.mkdir(parents=True)
    write_yaml(
        ship_dir / "ship.yaml",
        {
            "ship_id": "card_only",
            "display_name": "只标卡片",
            "card_template": "card.png",
            "selected_feature": "selected.png",
            "loadout_slots": [
                {
                    "slot_id": "signal_flags",
                    "name": "信号旗槽位",
                    "empty_template": "loadout/signal_flags_empty.png",
                }
            ],
        },
    )
    existing = b"existing-selected-asset-not-to-overwrite"
    (ship_dir / "selected.png").write_bytes(existing)
    screenshot = sample_background(640, 400, seed=22)
    screenshot_path = tmp_path / "port.png"
    cc.write_image(screenshot_path, screenshot)
    state = calibrate_ship.load_ship_editor(ship_dir, screenshot_path)
    state.card_image = cc.crop_region(screenshot, cc.BoxSelection(100, 300, 160, 60))
    assert state.selected_image is None
    assert calibrate_ship.validate_ship_editor(state) == []

    result = calibrate_ship.save_ship_editor(state)
    assert result.saved is True
    assert (ship_dir / "card.png").is_file()
    # 未重新框选时不得改动既有 selected.png 资产
    assert (ship_dir / "selected.png").read_bytes() == existing
    data = cc.read_yaml_mapping(ship_dir / "ship.yaml")
    assert data["selected_feature"] == "selected.png"  # 原有字段保留
    assert data["selected_region"] is None  # 原 yaml 未配置且 card_search_region 为空
    assert data["selected_threshold"] == pytest.approx(0.78)


def test_calibrate_ship_card_only_new_ship_without_selected(tmp_path: Path) -> None:
    """向后兼容：只框 card 的新船仍可保存、不写 selected.png、空 selected 不报错。"""
    ship_dir = tmp_path / "profiles" / "testclient" / "ships" / "old_flow"
    ship_dir.mkdir(parents=True)
    write_yaml(
        ship_dir / "ship.yaml",
        {
            "ship_id": "old_flow",
            "display_name": "旧流程船",
            "card_template": "card.png",
            "loadout_slots": [
                {
                    "slot_id": "signal_flags",
                    "name": "信号旗槽位",
                    "empty_template": "loadout/signal_flags_empty.png",
                }
            ],
        },
    )
    screenshot = sample_background(640, 400, seed=23)
    screenshot_path = tmp_path / "port.png"
    cc.write_image(screenshot_path, screenshot)
    state = calibrate_ship.load_ship_editor(ship_dir, screenshot_path)
    state.card_image = cc.crop_region(screenshot, cc.BoxSelection(80, 280, 160, 60))
    assert calibrate_ship.validate_ship_editor(state) == []
    warnings = calibrate_ship.ship_editor_warnings(state)
    assert any("selected.png" in message for message in warnings)

    result = calibrate_ship.save_ship_editor(state)
    assert result.saved is True
    assert not (ship_dir / "selected.png").exists()
    data = cc.read_yaml_mapping(ship_dir / "ship.yaml")
    assert data["selected_feature"] is None


def test_calibrate_ship_rejects_bad_selected_template(tmp_path: Path) -> None:
    """已选中态模板质量过差（纯色）必须报错并阻止保存。"""
    ship_dir = tmp_path / "profiles" / "testclient" / "ships" / "bad_selected"
    ship_dir.mkdir(parents=True)
    write_yaml(
        ship_dir / "ship.yaml",
        {
            "ship_id": "bad_selected",
            "display_name": "坏选中态",
            "card_template": "card.png",
            "loadout_slots": [
                {
                    "slot_id": "signal_flags",
                    "name": "信号旗槽位",
                    "empty_template": "loadout/signal_flags_empty.png",
                }
            ],
        },
    )
    screenshot = sample_background(640, 400, seed=24)
    screenshot_path = tmp_path / "port.png"
    cc.write_image(screenshot_path, screenshot)
    state = calibrate_ship.load_ship_editor(ship_dir, screenshot_path)
    state.card_image = cc.crop_region(screenshot, cc.BoxSelection(100, 300, 160, 60))
    state.selected_image = np.full((40, 40, 3), 128, dtype=np.uint8)  # 纯色
    errors = calibrate_ship.validate_ship_editor(state)
    assert any("选中态卡片" in message for message in errors)
    blocked = calibrate_ship.save_ship_editor(state)
    assert blocked.saved is False
    assert not (ship_dir / "selected.png").exists()


def test_calibrate_ship_name_template_save_flow(tmp_path: Path) -> None:
    """ISS-007 补充：保存右侧详情面板船名模板 selected_name.png 与对应 ship.yaml 字段。"""
    ship_dir = tmp_path / "profiles" / "testclient" / "ships" / "name_ship"
    ship_dir.mkdir(parents=True)
    write_yaml(
        ship_dir / "ship.yaml",
        {
            "ship_id": "name_ship",
            "display_name": "船名船",
            "card_template": "card.png",
            "card_search_region": [0.0, 0.8, 1.0, 1.0],
            "loadout_slots": [],
        },
    )
    screenshot = sample_background(640, 400, seed=31)
    screenshot_path = tmp_path / "port.png"
    cc.write_image(screenshot_path, screenshot)
    state = calibrate_ship.load_ship_editor(ship_dir, screenshot_path)
    state.card_image = cc.crop_region(screenshot, cc.BoxSelection(200, 300, 160, 60))
    state.name_image = cc.crop_region(screenshot, cc.BoxSelection(510, 30, 60, 20))

    assert calibrate_ship.validate_ship_editor(state) == []
    result = calibrate_ship.save_ship_editor(state)
    assert result.saved is True
    assert (ship_dir / "card.png").is_file()
    assert (ship_dir / "selected_name.png").is_file()
    assert not (ship_dir / "selected.png").exists()  # 本次未框选卡片选中态

    data = cc.read_yaml_mapping(ship_dir / "ship.yaml")
    assert data["selected_name_feature"] == "selected_name.png"
    assert data["selected_name_region"] == pytest.approx([0.79, 0.07, 0.91, 0.13])
    assert data["selected_name_threshold"] == pytest.approx(0.78)
    assert data["loadout_slots"] == []  # 空 loadout_slots 仍允许保存

    # 工具生成的 ship.yaml 必须能被正式 loader 解析（ShipProfile/loader 已支持 selected_name_*）
    ship = load_ship(ship_dir / "ship.yaml")
    assert ship.ship_id == "name_ship"
    assert ship.selected_name_feature == "selected_name.png"
    assert ship.selected_name_region == NormalizedRect(0.79, 0.07, 0.91, 0.13)
    assert ship.selected_name_threshold == pytest.approx(0.78)
    assert ship.selected_name_path == (ship_dir / "selected_name.png").resolve()
    issues = validate_ship(ship)
    assert not [issue for issue in issues if issue.is_error]
    assert not [issue for issue in issues if "selected_name" in issue.location]


def test_calibrate_ship_card_and_selected_without_name_fields(tmp_path: Path) -> None:
    """只框 card+selected 仍可保存，且不得凭空写入 selected_name_* 字段。"""
    ship_dir = tmp_path / "profiles" / "testclient" / "ships" / "no_name"
    ship_dir.mkdir(parents=True)
    write_yaml(
        ship_dir / "ship.yaml",
        {
            "ship_id": "no_name",
            "display_name": "无船名模板",
            "card_template": "card.png",
            "loadout_slots": [
                {
                    "slot_id": "signal_flags",
                    "name": "信号旗槽位",
                    "empty_template": "loadout/signal_flags_empty.png",
                }
            ],
        },
    )
    screenshot = sample_background(640, 400, seed=32)
    screenshot_path = tmp_path / "port.png"
    cc.write_image(screenshot_path, screenshot)
    state = calibrate_ship.load_ship_editor(ship_dir, screenshot_path)
    state.card_image = cc.crop_region(screenshot, cc.BoxSelection(100, 300, 160, 60))
    state.selected_image = cc.crop_region(screenshot, cc.BoxSelection(100, 280, 160, 70))
    assert state.name_image is None
    assert calibrate_ship.validate_ship_editor(state) == []

    result = calibrate_ship.save_ship_editor(state)
    assert result.saved is True
    assert (ship_dir / "selected.png").is_file()
    assert not (ship_dir / "selected_name.png").exists()
    data = cc.read_yaml_mapping(ship_dir / "ship.yaml")
    for key in ("selected_name_feature", "selected_name_region", "selected_name_threshold"):
        assert key not in data


def test_calibrate_ship_card_only_preserves_existing_name_fields(tmp_path: Path) -> None:
    """未重新框选船名时保留已有 selected_name_* 与既有 selected_name.png 资产。"""
    ship_dir = tmp_path / "profiles" / "testclient" / "ships" / "keep_name"
    ship_dir.mkdir(parents=True)
    write_yaml(
        ship_dir / "ship.yaml",
        {
            "ship_id": "keep_name",
            "display_name": "保留船名",
            "card_template": "card.png",
            "selected_name_feature": "selected_name.png",
            "selected_name_region": [0.8, 0.08, 0.9, 0.12],
            "selected_name_threshold": 0.9,
            "loadout_slots": [
                {
                    "slot_id": "signal_flags",
                    "name": "信号旗槽位",
                    "empty_template": "loadout/signal_flags_empty.png",
                }
            ],
        },
    )
    existing = b"existing-selected-name-asset-not-to-overwrite"
    (ship_dir / "selected_name.png").write_bytes(existing)
    screenshot = sample_background(640, 400, seed=33)
    screenshot_path = tmp_path / "port.png"
    cc.write_image(screenshot_path, screenshot)
    state = calibrate_ship.load_ship_editor(ship_dir, screenshot_path)
    state.card_image = cc.crop_region(screenshot, cc.BoxSelection(100, 300, 160, 60))
    assert state.name_image is None
    assert calibrate_ship.validate_ship_editor(state) == []

    result = calibrate_ship.save_ship_editor(state)
    assert result.saved is True
    assert (ship_dir / "selected_name.png").read_bytes() == existing
    data = cc.read_yaml_mapping(ship_dir / "ship.yaml")
    assert data["selected_name_feature"] == "selected_name.png"
    assert data["selected_name_region"] == pytest.approx([0.8, 0.08, 0.9, 0.12])
    assert data["selected_name_threshold"] == pytest.approx(0.9)


def test_calibrate_ship_rejects_bad_name_template(tmp_path: Path) -> None:
    """右侧船名模板质量过差（纯色）必须报错并阻止保存。"""
    ship_dir = tmp_path / "profiles" / "testclient" / "ships" / "bad_name"
    ship_dir.mkdir(parents=True)
    write_yaml(
        ship_dir / "ship.yaml",
        {
            "ship_id": "bad_name",
            "display_name": "坏船名",
            "card_template": "card.png",
            "loadout_slots": [
                {
                    "slot_id": "signal_flags",
                    "name": "信号旗槽位",
                    "empty_template": "loadout/signal_flags_empty.png",
                }
            ],
        },
    )
    screenshot = sample_background(640, 400, seed=34)
    screenshot_path = tmp_path / "port.png"
    cc.write_image(screenshot_path, screenshot)
    state = calibrate_ship.load_ship_editor(ship_dir, screenshot_path)
    state.card_image = cc.crop_region(screenshot, cc.BoxSelection(100, 300, 160, 60))
    state.name_image = np.full((40, 40, 3), 128, dtype=np.uint8)  # 纯色
    errors = calibrate_ship.validate_ship_editor(state)
    assert any("右侧船名" in message for message in errors)
    blocked = calibrate_ship.save_ship_editor(state)
    assert blocked.saved is False
    assert not (ship_dir / "selected_name.png").exists()


def test_capture_marker_save_and_profile_update(tmp_path: Path) -> None:
    marker = np.zeros((20, 20, 3), dtype=np.uint8)
    marker[2:18, 8:12] = (240, 240, 240)
    marker[14:18, 4:16] = (240, 240, 240)
    assert capture_marker.validate_marker_image(marker) == []
    marker_dir = tmp_path / "profiles" / "testclient" / "marker"
    written = capture_marker.save_marker_templates(marker_dir, marker, angles=24)
    assert len(written) == 25
    assert (marker_dir / "marker.png").is_file()
    assert (marker_dir / "marker_000.png").is_file()
    assert (marker_dir / "marker_345.png").is_file()
    base = cc.read_image(marker_dir / "marker.png")
    assert np.array_equal(base, marker)

    profile_path = tmp_path / "profiles" / "testclient" / "profile.yaml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text("# 客户端配置\n\nclient_id: testclient\nminimap:\n  rect: [0.8, 0.02, 0.98, 0.3]\n",
                            encoding="utf-8")
    capture_marker.update_profile_marker(profile_path, marker_template="marker/marker.png", angles=24)
    data = cc.read_yaml_mapping(profile_path)
    assert data["minimap"]["marker_template"] == "marker/marker.png"
    assert data["minimap"]["marker_angles"] == 24
    assert profile_path.read_text(encoding="utf-8").startswith("# 客户端配置")
    # 纯色/过小标记被拒绝
    with pytest.raises(ValueError):
        capture_marker.save_marker_templates(tmp_path / "bad", np.full((20, 20, 3), 5, np.uint8))


# ---------------------------------------------------------------------------
# 命令行 --help 可用性（任务硬性约束）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_tool_help_exits_zero(tool: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", f"src.tools.{tool}", "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")[-2000:]
