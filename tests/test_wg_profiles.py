"""直营服配置通过正式加载、校验和启动预检，资源互相隔离。"""

from pathlib import Path

import pytest
from src.config.loader import load_project
from src.config.validate import validate_project
from src.gui.app import load_gui_catalog
from src.perception.features import FeatureMatcher
from src.runtime.messages import StartOptions
from src.runtime.plan import list_ships, plan_preview, preflight

ROOT = Path(__file__).resolve().parents[1]
WG_PROFILES = ("wg_asia", "wg_eu", "wg_na")


def test_catalog_contains_national_and_direct_servers() -> None:
    catalog = load_gui_catalog(ROOT)
    assert {"cn360", *WG_PROFILES} <= {item.profile_id for item in catalog.profiles}
    assert catalog.default_profile_id == "cn360"


@pytest.mark.parametrize("profile_id", WG_PROFILES)
def test_direct_profile_loads_with_local_assets_and_calibration_warning(profile_id: str) -> None:
    loaded = load_project(ROOT, profile_id, ship_id="iron_duke")
    assert not loaded.errors
    app = loaded.config
    assert app.profile.client_id == profile_id
    assert app.profile.calibration_required
    assert app.profile.language == "zh_cn"
    report = validate_project(ROOT, project=loaded)
    assert not report.errors
    assert any(issue.code == "calibration_required" for issue in report.warnings)
    matcher = FeatureMatcher(app.profile, app.global_config.perception)
    assert matcher.has_feature("exit_confirm_yes")
    profile_dir = ROOT / "profiles" / profile_id
    for feature in app.profile.features.values():
        assert feature.template_path.is_relative_to(profile_dir)
        assert feature.template_path.is_file()
    assert app.ship.ship_dir.is_relative_to(profile_dir)
    assert dict(list_ships(ROOT, profile_id))["iron_duke"] == "铁公爵"
    options = StartOptions(root=ROOT, profile_id=profile_id, ship_id="iron_duke")
    assert preflight(ROOT, options).ok
    preview = plan_preview(ROOT, options)
    assert preview.ok, preview.issues
    assert any(issue.code == "calibration_required" for issue in preview.warnings)
    assert preview.plan.client_id == profile_id
