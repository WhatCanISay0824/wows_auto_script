"""船只档案与船只相关模板的测试（卡片 / 每船独立血条模板 / 按船名解析）。"""

from __future__ import annotations

from pathlib import Path

from src.common.types import State
from src.config.loader import load_project
from src.config.models import ShipProfile
from src.perception.engine import PerceptionEngine


def test_leningrad_ship_profile_has_card_and_hp_bar(project_root: Path) -> None:
    app = load_project(project_root, "cn360").config
    assert app.ship.ship_id == "leningrad"
    assert app.ship.display_name == "列宁格勒"
    assert app.ship.card_path is not None and app.ship.card_path.is_file()
    assert app.ship.hp_bar_path is not None and app.ship.hp_bar_path.is_file()


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
