"""船只档案与船只相关模板的测试（卡片 / 每船独立血条模板 / 按船名解析 / 选中态证据）。"""

from __future__ import annotations

from pathlib import Path

from src.common.types import NormalizedRect, State
from src.config.loader import load_project, load_ship
from src.config.models import ShipProfile
from src.config.validate import validate_ship
from src.perception.engine import PerceptionEngine


def test_leningrad_ship_profile_has_card_and_hp_bar(project_root: Path) -> None:
    app = load_project(project_root, "cn360").config
    assert app.ship.ship_id == "leningrad"
    assert app.ship.display_name == "列宁格勒"
    assert app.ship.card_path is not None and app.ship.card_path.is_file()
    assert app.ship.hp_bar_path is not None and app.ship.hp_bar_path.is_file()


def test_iron_duke_ship_profile_has_card_and_hp_bar(project_root: Path) -> None:
    from src.config.loader import load_ship

    ship = load_ship(project_root / "profiles" / "cn360" / "ships" / "iron_duke" / "ship.yaml")
    assert ship.display_name == "铁公爵"
    assert ship.card_path is not None and ship.card_path.is_file()
    assert ship.hp_bar_path is not None and ship.hp_bar_path.is_file()


def test_engine_applies_ship_specific_hp_bar_template(project_root: Path) -> None:
    app = load_project(project_root, "cn360").config
    engine = PerceptionEngine(app, capture=None)
    template_matcher = engine.feature_matcher.template_matcher
    assert template_matcher.has_feature("hp_bar_present")
    engine.configure_ship_hp_bar(app.ship)
    assert template_matcher.has_feature("hp_bar_present")
    assert engine._hp_bar_check_enabled is True  # noqa: SLF001 - 测试断言内部状态


def test_engine_disables_hp_bar_signal_without_ship_template(project_root: Path) -> None:
    app = load_project(project_root, "cn360").config
    engine = PerceptionEngine(app, capture=None)
    ship_without_template = ShipProfile(ship_id="other", display_name="无血条模板的船")
    engine.configure_ship_hp_bar(ship_without_template)
    assert not engine.feature_matcher.template_matcher.has_feature("hp_bar_present")
    assert engine._hp_bar_check_enabled is False  # noqa: SLF001


def test_observation_feature_ids_include_sequence_features(project_root: Path) -> None:
    """wait_feature 等待的特征必须在 Observation 里，否则永远不会命中。"""
    app = load_project(project_root, "cn360").config
    engine = PerceptionEngine(app, capture=None)
    port_ids = engine._feature_ids_for(State.PORT)  # noqa: SLF001
    assert "loadout_signal_flags" in port_ids  # ship_check_sequence 里的装备页信号旗
    assert "port_equip_tab" in port_ids
    assert "queue_indicator" in port_ids  # start_sequence 里的排队特征
    assert "exit_menu_return_port" in port_ids  # 退出流程特征
    battle_ids = engine._feature_ids_for(State.BATTLE_ACTIVE)  # noqa: SLF001
    assert "death_banner" in battle_ids


# ---------------------------------------------------------------------------
# 已选中证据与选船查找参数（ISS-007 / 实现补充 10.7）
# ---------------------------------------------------------------------------


def test_leningrad_ship_profile_has_selected_evidence_fields(project_root: Path) -> None:
    """选中态证据字段就绪：模板可待标定，但区域/阈值/查找参数必须可配置。"""
    app = load_project(project_root, "cn360").config
    ship = app.ship
    assert ship.selected_feature == "selected.png"
    # 选中态搜索区域缺省回退 card_search_region（轮播条）
    assert ship.selected_region == ship.card_search_region
    assert ship.selected_threshold == 0.85
    # 模板可能尚未标定；已标定时必须解析为存在的文件
    assert ship.selected_path is None or ship.selected_path.is_file()
    assert ship.cover_list_start is True
    assert ship.max_position_scrolls == 10
    assert ship.confirm_frames == 2
    assert ship.confirm_timeout_s == 3.0
    assert ship.max_click_attempts == 2
    assert ship.endpoint_stable_scrolls == 2
    assert ship.selection_timeout_s == 60.0


def test_iron_duke_ship_profile_has_selected_evidence_fields(project_root: Path) -> None:
    ship = load_ship(project_root / "profiles" / "cn360" / "ships" / "iron_duke" / "ship.yaml")
    assert ship.selected_feature == "selected.png"
    assert ship.selected_region == NormalizedRect(0.0, 0.8, 1.0, 1.0)
    assert ship.selected_path is None or ship.selected_path.is_file()
    assert ship.max_scrolls == 20
    assert ship.scroll_clicks == 2


def test_selected_evidence_missing_is_warning_not_error(project_root: Path) -> None:
    """未标定选中态模板：validate 给 warning，不能是阻断运行的 error。"""
    ship = load_ship(project_root / "profiles" / "cn360" / "ships" / "leningrad" / "ship.yaml")
    issues = validate_ship(ship)
    selected_warnings = [
        issue
        for issue in issues
        if not issue.is_error and issue.location.endswith("selected_feature")
    ]
    if ship.selected_path is None:
        assert selected_warnings, [issue.format() for issue in issues]
        assert any(issue.code == "file_missing" for issue in selected_warnings)
    assert not [issue for issue in issues if issue.is_error and "selected_feature" in issue.location]
    assert not [issue for issue in issues if issue.is_error]


def test_ship_profile_selection_defaults() -> None:
    """未写选船参数时使用模型默认值（配置驱动，不写死在 ship_check 里）。"""
    ship = ShipProfile(ship_id="demo", display_name="演示船")
    assert ship.selected_feature is None
    assert ship.selected_path is None
    assert ship.selected_region is None
    assert ship.selected_threshold == 0.85
    assert ship.cover_list_start is True
    assert ship.max_scrolls == 20
    assert ship.max_position_scrolls == 10
    assert ship.scroll_settle_s == 0.4
    assert ship.confirm_frames == 2
    assert ship.confirm_timeout_s == 3.0
    assert ship.max_click_attempts == 2
    assert ship.endpoint_stable_scrolls == 2
    assert ship.endpoint_diff_ratio == 0.02
    assert ship.candidate_merge_px == 8
    assert ship.selection_timeout_s == 60.0


def test_load_ship_selected_region_falls_back_to_card_region(tmp_path: Path) -> None:
    (tmp_path / "ship.yaml").write_text(
        "ship_id: demo\n"
        "display_name: 演示船\n"
        "card_template: card.png\n"
        "card_search_region: [0.0, 0.8, 1.0, 1.0]\n"
        "selected_feature: selected.png\n",
        encoding="utf-8",
    )
    ship = load_ship(tmp_path / "ship.yaml")
    assert ship.selected_region == NormalizedRect(0.0, 0.8, 1.0, 1.0)
    assert ship.selected_path is None  # 模板文件尚未生成
    (tmp_path / "selected.png").write_bytes(b"not a real png")
    ship = load_ship(tmp_path / "ship.yaml")
    assert ship.selected_path == (tmp_path / "selected.png").resolve()


def test_load_ship_explicit_selected_region_and_threshold(tmp_path: Path) -> None:
    (tmp_path / "ship.yaml").write_text(
        "ship_id: demo\n"
        "display_name: 演示船\n"
        "card_template: card.png\n"
        "card_search_region: [0.0, 0.8, 1.0, 1.0]\n"
        "selected_feature: selected.png\n"
        "selected_region: [0.1, 0.82, 0.9, 0.98]\n"
        "selected_threshold: 0.9\n"
        "max_scrolls: 7\n"
        "scroll_clicks: 3\n"
        "cover_list_start: false\n"
        "max_position_scrolls: 4\n"
        "scroll_settle_s: 0.6\n"
        "confirm_frames: 3\n"
        "confirm_timeout_s: 5.0\n"
        "max_click_attempts: 1\n"
        "endpoint_stable_scrolls: 3\n"
        "endpoint_diff_ratio: 0.05\n"
        "candidate_merge_px: 12\n"
        "selection_timeout_s: 30.0\n",
        encoding="utf-8",
    )
    ship = load_ship(tmp_path / "ship.yaml")
    assert ship.selected_region == NormalizedRect(0.1, 0.82, 0.9, 0.98)
    assert ship.selected_threshold == 0.9
    assert ship.max_scrolls == 7
    assert ship.scroll_clicks == 3
    assert ship.cover_list_start is False
    assert ship.max_position_scrolls == 4
    assert ship.scroll_settle_s == 0.6
    assert ship.confirm_frames == 3
    assert ship.confirm_timeout_s == 5.0
    assert ship.max_click_attempts == 1
    assert ship.endpoint_stable_scrolls == 3
    assert ship.endpoint_diff_ratio == 0.05
    assert ship.candidate_merge_px == 12
    assert ship.selection_timeout_s == 30.0


def test_selected_template_present_produces_no_warning(tmp_path: Path) -> None:
    (tmp_path / "ship.yaml").write_text(
        "ship_id: demo\n"
        "display_name: 演示船\n"
        "card_template: card.png\n"
        "selected_feature: selected.png\n",
        encoding="utf-8",
    )
    (tmp_path / "selected.png").write_bytes(b"not a real png")
    ship = load_ship(tmp_path / "ship.yaml")
    issues = validate_ship(ship)
    assert not [issue for issue in issues if "selected_feature" in issue.location]
