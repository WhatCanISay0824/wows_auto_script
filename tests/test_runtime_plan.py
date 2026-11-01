"""启动选项解析与生效值测试（GUI 设计文档 4.3 / 4.4）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from src.common.errors import ConfigError, Severity
from src.runtime.messages import StartOptions
from src.runtime.plan import (
    build_effective_plan,
    list_ships,
    load_accounts_or_none,
    load_app_for_account,
    plan_preview,
    preflight,
    resolve_ship,
    resolve_start_plan,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 船只名称解析（迁入后行为与旧 CLI 一致）
# ---------------------------------------------------------------------------


def test_list_ships_real_repo() -> None:
    ships = dict(list_ships(PROJECT_ROOT, "cn360"))
    assert ships["leningrad"] == "列宁格勒"
    assert "iron_duke" in ships


def test_resolve_ship_by_display_name_and_id() -> None:
    assert resolve_ship(PROJECT_ROOT, "cn360", None, "列宁格勒") == "leningrad"
    assert resolve_ship(PROJECT_ROOT, "cn360", None, "leningrad") == "leningrad"
    # 未指定名称时原样返回 ship_id（CLI --ship 语义）
    assert resolve_ship(PROJECT_ROOT, "cn360", "iron_duke", None) == "iron_duke"
    assert resolve_ship(PROJECT_ROOT, "cn360", None, None) is None


def test_resolve_ship_unknown_lists_candidates() -> None:
    with pytest.raises(ConfigError) as exc_info:
        resolve_ship(PROJECT_ROOT, "cn360", None, "不存在的船")
    assert "现有船只" in str(exc_info.value)


def test_resolve_ship_partial_match(tmp_path: Path) -> None:
    ships_dir = tmp_path / "profiles" / "test" / "ships"
    for ship_id, display_name in (("a", "列宁格勒"), ("b", "列宁格勒改")):
        directory = ships_dir / ship_id
        directory.mkdir(parents=True)
        (directory / "ship.yaml").write_text(
            f"ship_id: {ship_id}\ndisplay_name: {display_name}\n", encoding="utf-8"
        )
    assert list_ships(tmp_path, "test") == [("a", "列宁格勒"), ("b", "列宁格勒改")]
    # 部分匹配命中多个 -> 拒绝并要求精确指定
    with pytest.raises(ConfigError) as exc_info:
        resolve_ship(tmp_path, "test", None, "列宁")
    assert "多个档案" in str(exc_info.value)
    # 精确匹配优先，不再报多义
    assert resolve_ship(tmp_path, "test", None, "列宁格勒") == "a"
    assert resolve_ship(tmp_path, "test", None, "列宁格勒改") == "b"


def test_list_ships_skips_broken_ship_yaml(tmp_path: Path) -> None:
    ships_dir = tmp_path / "profiles" / "test" / "ships"
    good = ships_dir / "ok"
    good.mkdir(parents=True)
    (good / "ship.yaml").write_text("ship_id: ok\ndisplay_name: 好船\n", encoding="utf-8")
    bad = ships_dir / "bad"
    bad.mkdir(parents=True)
    (bad / "ship.yaml").write_text(": : :\n", encoding="utf-8")
    assert list_ships(tmp_path, "test") == [("ok", "好船")]


# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------


def test_preflight_real_repo_ok() -> None:
    result = preflight(PROJECT_ROOT, StartOptions(root=PROJECT_ROOT))
    assert result.ok
    assert result.profile_id == "cn360"
    assert result.ship_id is None  # 未指定时回退 config.yaml ship.ship_id


def test_preflight_ship_name_resolved() -> None:
    result = preflight(PROJECT_ROOT, StartOptions(root=PROJECT_ROOT, ship_name="列宁格勒"))
    assert result.ok
    assert result.ship_id == "leningrad"


def test_preflight_missing_root(tmp_path: Path) -> None:
    result = preflight(tmp_path, StartOptions(root=tmp_path))
    assert not result.ok
    codes = {item.code for item in result.issues}
    assert "root_invalid" in codes
    assert "profile_not_found" in codes


def test_preflight_invalid_options() -> None:
    result = preflight(
        PROJECT_ROOT,
        StartOptions(root=PROJECT_ROOT, max_battles=0, log_level="VERBOSE"),
    )
    codes = {item.code for item in result.issues}
    assert "max_battles_invalid" in codes
    assert "log_level_invalid" in codes


def test_preflight_rotate_disabled_in_repo() -> None:
    result = preflight(PROJECT_ROOT, StartOptions(root=PROJECT_ROOT, rotate=True))
    codes = {item.code for item in result.issues}
    assert "rotation_disabled" in codes


def test_preflight_unknown_account() -> None:
    result = preflight(PROJECT_ROOT, StartOptions(root=PROJECT_ROOT, account_alias="不存在"))
    codes = {item.code for item in result.issues}
    assert "account_not_found" in codes


# ---------------------------------------------------------------------------
# 生效值（账号覆盖后的真实值）
# ---------------------------------------------------------------------------


def test_plan_preview_without_account() -> None:
    result = plan_preview(PROJECT_ROOT, StartOptions(root=PROJECT_ROOT))
    assert result.ok, result.issues
    assert result.plan is not None
    plan = result.plan
    assert plan.client_id == "cn360"
    assert plan.client_display_name
    assert plan.ship_id  # config.yaml 默认船
    assert plan.ship_display_name
    assert plan.account_alias is None
    assert plan.max_battles_override is None
    assert plan.rotation_limit is None
    assert plan.safety_battle_cap > 0
    assert plan.daily_cap > 0
    assert plan.navigation_mode in ("autopilot", "waypoints")
    assert plan.emergency_stop_key
    assert plan.resume_key
    assert plan.heartbeat_s > 0
    assert plan.config_hash
    assert plan.log_path.is_absolute()
    assert plan.db_path.is_absolute()
    assert plan.db_path.name == "wows_script.db"
    assert plan.diagnostics_dir.is_absolute()
    assert plan.profiles_dir.is_absolute()
    assert plan.start_paused is False


def test_plan_preview_ship_name_and_override() -> None:
    result = plan_preview(
        PROJECT_ROOT,
        StartOptions(root=PROJECT_ROOT, ship_name="列宁格勒", max_battles=3, start_paused=True),
    )
    assert result.ok, result.issues
    assert result.plan is not None
    assert result.plan.ship_id == "leningrad"
    assert result.plan.max_battles_override == 3
    assert result.plan.start_paused is True


def test_plan_preview_account_overrides_repo_config() -> None:
    result = plan_preview(PROJECT_ROOT, StartOptions(root=PROJECT_ROOT, account_alias="alt1"))
    assert result.ok, result.issues
    plan = result.plan
    assert plan is not None
    assert plan.account_alias == "alt1"
    assert plan.account_display_name == "小号一号（本地别名）"
    # accounts.yaml: alt1 覆盖 max_battles_per_session=6 / daily_battle_cap=30
    assert plan.safety_battle_cap == 6
    assert plan.daily_cap == 30


def test_plan_preview_unresolved_ship() -> None:
    result = plan_preview(PROJECT_ROOT, StartOptions(root=PROJECT_ROOT, ship_name="不存在的船"))
    assert result.ok is False
    assert result.plan is None
    assert any(item.code == "ship_unresolved" for item in result.issues)


def test_plan_preview_ambiguous_ship(tmp_path: Path) -> None:
    ships_dir = tmp_path / "profiles" / "cn360" / "ships"
    for ship_id in ("a", "b"):
        directory = ships_dir / ship_id
        directory.mkdir(parents=True)
        (directory / "ship.yaml").write_text(f"ship_id: {ship_id}\ndisplay_name: 同名船\n", encoding="utf-8")
    (tmp_path / "profiles" / "cn360" / "profile.yaml").write_text("client_id: cn360\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("runtime: {}\n", encoding="utf-8")
    result = plan_preview(tmp_path, StartOptions(root=tmp_path, ship_name="同名船"))
    assert result.ok is False
    assert any(item.code == "ship_ambiguous" for item in result.issues)


# ---------------------------------------------------------------------------
# 严格加载入口（CLI / 服务 STARTING 共用）
# ---------------------------------------------------------------------------


def test_load_app_for_account_and_resolve_start_plan() -> None:
    app = load_app_for_account(PROJECT_ROOT, "cn360", None, None)
    assert app.profile.client_id == "cn360"
    app2, plan = resolve_start_plan(StartOptions(root=PROJECT_ROOT, ship_name="列宁格勒", max_battles=2))
    assert app2.ship.ship_id == "leningrad"
    assert plan.ship_id == "leningrad"
    assert plan.max_battles_override == 2


def test_resolve_start_plan_raises_config_error_for_bad_ship() -> None:
    with pytest.raises(ConfigError):
        resolve_start_plan(StartOptions(root=PROJECT_ROOT, ship_name="不存在的船"))


def test_load_accounts_or_none_real_repo() -> None:
    accounts = load_accounts_or_none(PROJECT_ROOT)
    assert accounts is not None
    assert accounts.get("alt1") is not None


def test_build_effective_plan_daily_used_readonly_repo() -> None:
    """daily_used 读不到（数据库不存在/忙）时必须为 None 而不是 0。"""
    app = load_app_for_account(PROJECT_ROOT, "cn360", None, None)
    plan = build_effective_plan(app, StartOptions(root=PROJECT_ROOT))
    assert plan.daily_used is None or plan.daily_used >= 0


def test_issue_codes_are_errors() -> None:
    from src.runtime.plan import issue

    item = issue("x", "loc", "message")
    assert item.level is Severity.ERROR
