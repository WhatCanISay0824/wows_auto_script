"""账号档案测试（T7 扩展需求：账号选择 + 每账号独立配置 + 会话间轮换）。

安全口径：accounts.yaml 只允许本地别名与配置覆盖；出现密码/令牌/真实账号字段必须
报 error 并拒绝加载，且不进入任何数据库表或日志。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from src.common.clock import FakeClock
from src.common.errors import ConfigError, DatabaseError
from src.config.loader import (
    load_account,
    load_accounts,
    load_app,
    load_project,
)
from src.config.validate import validate_project
from src.storage.database import Database
from src.storage.event_log import EventSink
from src.tools import validate as validate_tool

from tests.fixtures.configs import builder as fx
from tests.fixtures.configs.builder import ACCOUNTS_FIXTURE

TZ_CN = timezone(timedelta(hours=8))
WALL_START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=TZ_CN)


@pytest.fixture
def project(tmp_path: Path) -> fx.TestProject:
    return fx.build_valid_project(tmp_path)


def _edit_accounts(project: fx.TestProject, updater: Callable[[dict], None]) -> None:
    data = fx.load_yaml(project.accounts_path)
    updater(data)
    fx.write_yaml(project.accounts_path, data)


def _error_codes(issues) -> list[str]:
    return [issue.code for issue in issues]


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


def test_load_accounts(project: fx.TestProject) -> None:
    accounts = load_accounts(project.root)
    assert [account.alias for account in accounts.accounts] == ["alt1", "alt2"]
    assert accounts.rotation_enabled is False
    assert accounts.rotation_mode == "round_robin"
    assert accounts.max_battles_per_account == 3
    assert accounts.accounts_file == "accounts.yaml"
    alt1 = accounts.get("alt1")
    assert alt1 is not None
    assert alt1.profile == "testclient"
    assert alt1.ship_id == "test_ship"
    assert alt1.daily_battle_cap == 8
    assert alt1.max_battles_per_session == 2
    assert alt1.allowed_hours is None
    assert alt1.enabled is True
    alt2 = accounts.get("alt2")
    assert alt2 is not None
    assert alt2.allowed_hours == ("09:00", "22:00")
    assert alt2.enabled is False


def test_load_account_single(project: fx.TestProject) -> None:
    account = load_account(project.root, "alt1")
    assert account is not None
    assert account.alias == "alt1"
    assert load_account(project.root, "missing") is None


def test_load_accounts_missing_file_records_issue(tmp_path: Path) -> None:
    issues: list = []
    accounts = load_accounts(tmp_path, issues=issues)
    assert accounts.accounts == ()
    assert [issue.code for issue in issues] == ["file_missing"]


def test_static_fixture_is_used(project: fx.TestProject) -> None:
    assert project.accounts_path.is_file()
    assert ACCOUNTS_FIXTURE.is_file()
    assert project.accounts_path.read_bytes() == ACCOUNTS_FIXTURE.read_bytes()


# ---------------------------------------------------------------------------
# 每账号配置覆盖
# ---------------------------------------------------------------------------


def test_load_project_without_account_is_backward_compatible(project: fx.TestProject) -> None:
    loaded = load_project(project.root, project.client_id)
    assert loaded.issues == []
    assert loaded.config.account is None
    assert loaded.config.account_alias is None
    assert loaded.config.global_config.session.daily_battle_cap == 30  # 全局默认


def test_load_project_with_account_overrides(project: fx.TestProject) -> None:
    loaded = load_project(project.root, project.client_id, account="alt1")
    assert loaded.issues == []
    config = loaded.config
    assert config.account is not None
    assert config.account.alias == "alt1"
    assert config.account_alias == "alt1"
    assert config.profile.client_id == "testclient"
    assert config.ship.ship_id == "test_ship"
    # 账号覆盖每日场次上限 / 单次会话场次
    assert config.global_config.session.daily_battle_cap == 8
    assert config.global_config.safety.max_battles_per_session == 2
    assert config.global_config.session.allowed_hours is None
    # 覆盖关系记录来源文件
    assert config.sources["app.account"] == "accounts.yaml"
    assert config.sources["accounts.rotation.enabled"] == "accounts.yaml"


def test_load_project_with_account_allowed_hours(project: fx.TestProject) -> None:
    loaded = load_project(project.root, project.client_id, account="alt2")
    config = loaded.config
    assert config.global_config.session.allowed_hours == ("09:00", "22:00")
    assert config.global_config.session.daily_battle_cap == 6


def test_account_profile_and_ship_override(tmp_path: Path) -> None:
    """账号档案可覆盖 profile / ship_id；需要一个额外的客户端配置目录。"""
    project = fx.build_valid_project(tmp_path)
    # 复制一份客户端配置作为账号专用 profile
    alt_profile = project.root / "profiles" / "altclient"
    alt_profile.mkdir()
    for name in ("profile.yaml", "keys.yaml"):
        (alt_profile / name).write_bytes((project.profile_dir / name).read_bytes())
    profile_data = fx.load_yaml(alt_profile / "profile.yaml")
    profile_data["client_id"] = "altclient"
    fx.write_yaml(alt_profile / "profile.yaml", profile_data)
    # 额外船只
    alt_ship = alt_profile / "ships" / "alt_ship"
    alt_ship.mkdir(parents=True)
    ship_data = fx.load_yaml(project.ship_path)
    ship_data["ship_id"] = "alt_ship"
    fx.write_yaml(alt_ship / "ship.yaml", ship_data)
    fx.write_png(alt_ship / "card.png", fx.template_image(seed=11))
    for name in ("signal_flags_empty.png", "economic_bonus_empty.png"):
        fx.write_png(alt_ship / "loadout" / name, fx.template_image(seed=12))

    _edit_accounts(
        project,
        lambda data: data["accounts"][0].update({"profile": "altclient", "ship_id": "alt_ship"}),
    )
    loaded = load_project(project.root, project.client_id, account="alt1")
    assert loaded.issues == []
    assert loaded.config.profile.client_id == "altclient"
    assert loaded.config.ship.ship_id == "alt_ship"
    assert loaded.config.profile.profile_dir == alt_profile
    assert loaded.config.ship.ship_dir == alt_ship


def test_explicit_ship_argument_still_works(project: fx.TestProject) -> None:
    loaded = load_project(project.root, project.client_id, ship_id="test_ship")
    assert loaded.config.ship.ship_id == "test_ship"


def test_unknown_account_is_error(project: fx.TestProject) -> None:
    loaded = load_project(project.root, project.client_id, account="ghost")
    assert any(issue.code == "unknown_account" for issue in loaded.errors)
    with pytest.raises(ConfigError) as exc_info:
        load_app(project.root, project.client_id, account="ghost")
    assert any(issue.code == "unknown_account" for issue in exc_info.value.errors)


def test_load_app_with_account(project: fx.TestProject) -> None:
    app = load_app(project.root, project.client_id, account="alt1")
    assert app.account is not None
    assert app.account.alias == "alt1"
    assert app.global_config.session.daily_battle_cap == 8


# ---------------------------------------------------------------------------
# config_hash：包含 accounts.yaml 内容 + 当前 alias
# ---------------------------------------------------------------------------


def test_config_hash_distinguishes_account(project: fx.TestProject) -> None:
    hash_none = load_project(project.root, project.client_id).config.config_hash
    hash_alt1 = load_project(project.root, project.client_id, account="alt1").config.config_hash
    hash_alt2 = load_project(project.root, project.client_id, account="alt2").config.config_hash
    assert len(hash_none) == 16
    assert hash_none != hash_alt1
    assert hash_alt1 != hash_alt2


def test_config_hash_includes_accounts_file_content(project: fx.TestProject) -> None:
    hash_before = load_project(project.root, project.client_id).config.config_hash
    _edit_accounts(project, lambda data: data["accounts"][0].update({"notes": "内容变化"}))
    hash_after = load_project(project.root, project.client_id).config.config_hash
    assert hash_after != hash_before


def test_config_hash_stable_for_same_account(project: fx.TestProject) -> None:
    first = load_project(project.root, project.client_id, account="alt1").config.config_hash
    second = load_project(project.root, project.client_id, account="alt1").config.config_hash
    assert first == second


# ---------------------------------------------------------------------------
# 安全：凭据字段拒绝加载
# ---------------------------------------------------------------------------


def test_password_field_rejected(project: fx.TestProject) -> None:
    _edit_accounts(project, lambda data: data["accounts"][0].update({"password": "hunter2"}))
    loaded = load_project(project.root, project.client_id)
    credential_issues = [issue for issue in loaded.errors if issue.code == "credential_field"]
    assert credential_issues
    assert "password" in credential_issues[0].location
    assert loaded.config.global_config.accounts.accounts == ()  # 拒绝加载全部档案
    with pytest.raises(ConfigError) as exc_info:
        load_app(project.root, project.client_id)
    assert any(issue.code == "credential_field" for issue in exc_info.value.errors)


@pytest.mark.parametrize("forbidden_key", ["token", "secret", "access_token", "username", "账号"])
def test_credential_like_fields_rejected(project: fx.TestProject, forbidden_key: str) -> None:
    _edit_accounts(project, lambda data: data["accounts"][0].update({forbidden_key: "x"}))
    report = validate_project(project.root, project.client_id)
    assert any(issue.code == "credential_field" for issue in report.errors)


def test_credential_values_in_notes_are_allowed(project: fx.TestProject) -> None:
    """notes 是自由文本，可以提到“密码”等词，不应被误报（只检查字段名）。"""
    _edit_accounts(
        project,
        lambda data: data["accounts"][0].update({"notes": "真实登录与密码由用户手动完成"}),
    )
    loaded = load_project(project.root, project.client_id)
    assert loaded.issues == []


# ---------------------------------------------------------------------------
# 校验规则
# ---------------------------------------------------------------------------


def test_accounts_validation_duplicate_alias(project: fx.TestProject) -> None:
    _edit_accounts(project, lambda data: data["accounts"][1].update({"alias": "alt1"}))
    report = validate_project(project.root, project.client_id)
    assert any(issue.code == "duplicate" and "alias" in issue.location for issue in report.errors)


def test_accounts_validation_empty_alias(project: fx.TestProject) -> None:
    _edit_accounts(project, lambda data: data["accounts"][1].update({"alias": ""}))
    report = validate_project(project.root, project.client_id)
    assert any(issue.code == "missing" and "alias" in issue.location for issue in report.errors)


def test_accounts_validation_profile_and_ship_reference(project: fx.TestProject) -> None:
    _edit_accounts(
        project,
        lambda data: data["accounts"][0].update({"profile": "ghost_profile", "ship_id": "ghost_ship"}),
    )
    report = validate_project(project.root, project.client_id)
    assert any(issue.code == "unknown_profile" for issue in report.errors)
    assert any(issue.code == "unknown_ship" for issue in report.errors)


def test_accounts_validation_bad_caps_and_hours(project: fx.TestProject) -> None:
    _edit_accounts(
        project,
        lambda data: data["accounts"][0].update(
            {"daily_battle_cap": 0, "max_battles_per_session": -1, "allowed_hours": ["8:00", "22:00"]}
        ),
    )
    report = validate_project(project.root, project.client_id)
    assert any(issue.code == "range" and "daily_battle_cap" in issue.location for issue in report.errors)
    assert any(issue.code == "range" and "max_battles_per_session" in issue.location for issue in report.errors)
    assert any(issue.code == "format" and "allowed_hours" in issue.location for issue in report.errors)


def test_rotation_enabled_requires_enabled_account(project: fx.TestProject) -> None:
    def disable_all(data: dict) -> None:
        data["rotation"]["enabled"] = True
        for account in data["accounts"]:
            account["enabled"] = False

    _edit_accounts(project, disable_all)
    report = validate_project(project.root, project.client_id)
    assert any(issue.code == "no_enabled_accounts" for issue in report.errors)
    with pytest.raises(ConfigError):
        load_app(project.root, project.client_id)


def test_rotation_enabled_with_enabled_account_passes(project: fx.TestProject) -> None:
    _edit_accounts(project, lambda data: data["rotation"].update({"enabled": True}))
    report = validate_project(project.root, project.client_id)
    assert report.ok, report.format_lines()
    loaded = load_project(project.root, project.client_id)
    assert loaded.config.global_config.accounts.rotation_enabled is True


def test_rotation_mode_and_cap_validation(project: fx.TestProject) -> None:
    def mutate(data: dict) -> None:
        data["rotation"]["enabled"] = True
        data["rotation"]["mode"] = "random"
        data["rotation"]["max_battles_per_account"] = 0

    _edit_accounts(project, mutate)
    report = validate_project(project.root, project.client_id)
    assert any(issue.code == "unsupported" and "mode" in issue.location for issue in report.errors)
    assert any(issue.code == "range" and "max_battles_per_account" in issue.location for issue in report.errors)


def test_disabled_account_explicitly_selected_warns(project: fx.TestProject) -> None:
    report = validate_project(project.root, project.client_id, account="alt2")
    assert report.ok
    assert any(issue.code == "disabled_account" for issue in report.warnings)


def test_missing_accounts_file_is_warning_only(tmp_path: Path) -> None:
    project = fx.build_valid_project(tmp_path, with_accounts=False)
    loaded = load_project(project.root, project.client_id)
    assert loaded.config.account is None
    assert [issue.code for issue in loaded.warnings] == ["accounts_missing"]
    assert loaded.errors == []
    report = validate_project(project.root, project.client_id)
    assert report.ok
    # 旧调用方式（不传 account）仍可严格加载
    assert load_app(project.root, project.client_id).account is None


# ---------------------------------------------------------------------------
# 数据库：账号维度隔离
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(wall_start=WALL_START)


@pytest.fixture
def db(tmp_path: Path, clock: FakeClock) -> Database:
    database = Database(tmp_path / "data" / "wows_script.db", clock=clock)
    yield database
    database.close()


def test_database_account_alias_stored_and_inherited(db: Database) -> None:
    session_alt1 = db.begin_session("cn360", "hash-1", account_alias="alt1")
    db.begin_battle(session_alt1, "north_sea")
    session_alt2 = db.begin_session("cn360", "hash-2", account_alias="alt2")
    db.begin_battle(session_alt2, "north_sea")
    rows = db.connection.execute("SELECT account_alias FROM sessions ORDER BY session_id").fetchall()
    assert [row[0] for row in rows] == ["alt1", "alt2"]
    rows = db.connection.execute("SELECT account_alias FROM battles ORDER BY battle_id").fetchall()
    assert [row[0] for row in rows] == ["alt1", "alt2"]
    # 显式传入 account_alias 时覆盖继承值
    battle_override = db.begin_battle(session_alt1, "north_sea", account_alias="alt2")
    row = db.connection.execute(
        "SELECT account_alias FROM battles WHERE battle_id = ?", (battle_override,)
    ).fetchone()
    assert row[0] == "alt2"


def test_count_battles_on_isolated_per_account(db: Database, clock: FakeClock) -> None:
    session_alt1 = db.begin_session("cn360", "hash-1", account_alias="alt1")
    db.begin_battle(session_alt1, None)
    db.begin_battle(session_alt1, None)
    session_alt2 = db.begin_session("cn360", "hash-2", account_alias="alt2")
    db.begin_battle(session_alt2, None)
    clock.advance(24 * 3600)
    db.begin_battle(session_alt2, None)

    today = date(2026, 1, 1)
    tomorrow = date(2026, 1, 2)
    assert db.count_battles_on(today, "alt1") == 2
    assert db.count_battles_on(today, "alt2") == 1
    assert db.count_battles_on(today, "ghost") == 0
    assert db.count_battles_on(today) == 3  # 不传账号 = 全部账号合计（旧行为）
    assert db.count_battles_on(tomorrow, "alt2") == 1
    assert db.count_battles_on(tomorrow, "alt1") == 0


def test_old_database_calls_backward_compatible(db: Database) -> None:
    """旧调用（不传 account_alias）必须保持原行为，写入 NULL。"""
    session_id = db.begin_session("cn360", "hash")
    battle_id = db.begin_battle(session_id, "north_sea")
    assert db.count_battles_on(date(2026, 1, 1)) == 1
    row = db.connection.execute(
        "SELECT account_alias FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    assert row[0] is None
    row = db.connection.execute(
        "SELECT account_alias FROM battles WHERE battle_id = ?", (battle_id,)
    ).fetchone()
    assert row[0] is None


def test_event_sink_account_isolation(tmp_path: Path, clock: FakeClock) -> None:
    db_path = tmp_path / "data" / "wows_script.db"
    sink = EventSink(
        db_path,
        clock=clock,
        fallback_log=tmp_path / "logs" / "db_fallback.log",
        min_commit_interval_s=0.0,
        poll_timeout_s=0.01,
    )
    sink.start()
    try:
        session_alt1 = sink.begin_session("cn360", "hash-1", account_alias="alt1")
        sink.begin_battle(session_alt1, map_id="north_sea")
        session_alt2 = sink.begin_session("cn360", "hash-2", account_alias="alt2")
        sink.begin_battle(session_alt2, map_id="north_sea")
        sink.begin_battle(session_alt2, map_id="north_sea")
        today = clock.wall_now().date()
        assert sink.count_battles_on(today) == 3
        assert sink.count_battles_on(today, "alt1") == 1
        assert sink.count_battles_on(today, "alt2") == 2
        sink.flush()
    finally:
        sink.stop(drain=True)
    connection = sqlite3.connect(str(db_path))
    try:
        rows = connection.execute(
            "SELECT account_alias, COUNT(*) FROM battles GROUP BY account_alias ORDER BY account_alias"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [("alt1", 1), ("alt2", 2)]


def test_event_sink_old_calls_backward_compatible(tmp_path: Path, clock: FakeClock) -> None:
    db_path = tmp_path / "data" / "wows_script.db"
    sink = EventSink(
        db_path,
        clock=clock,
        fallback_log=tmp_path / "logs" / "db_fallback.log",
        min_commit_interval_s=0.0,
        poll_timeout_s=0.01,
    )
    sink.start()
    try:
        session_id = sink.begin_session("cn360", "hash")
        battle_id = sink.begin_battle(session_id, "north_sea")
        assert sink.count_battles_on(clock.wall_now().date()) == 1
        sink.log_action(battle_id, "nav_click", None)
        sink.end_battle(battle_id, "dead", "menu")
        sink.end_session(session_id, "done", 1)
        sink.flush()
    finally:
        sink.stop(drain=True)
    connection = sqlite3.connect(str(db_path))
    try:
        aliases = [row[0] for row in connection.execute("SELECT account_alias FROM battles")]
    finally:
        connection.close()
    assert aliases == [None]


# ---------------------------------------------------------------------------
# validate 工具
# ---------------------------------------------------------------------------


def test_validate_tool_with_account(project: fx.TestProject, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = validate_tool.main(
        ["--root", str(project.root), "--profile", project.client_id, "--account", "alt1"]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "[ERROR]" not in captured.out


def test_validate_tool_credential_rejected(project: fx.TestProject, capsys: pytest.CaptureFixture[str]) -> None:
    _edit_accounts(project, lambda data: data["accounts"][0].update({"token": "abc"}))
    exit_code = validate_tool.main(["--root", str(project.root)])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "credential_field" in captured.out


def test_app_config_account_none_by_default(project: fx.TestProject) -> None:
    app = load_app(project.root, project.client_id)
    assert app.account is None
    assert app.account_alias is None
    assert app.global_config.accounts.rotation_enabled is False


def test_invalid_account_types_record_errors(project: fx.TestProject) -> None:
    def mutate(data: dict) -> None:
        data["accounts"][0]["daily_battle_cap"] = "eight"
        data["accounts"][1]["enabled"] = "yes"
        data["rotation"]["enabled"] = "true"

    _edit_accounts(project, mutate)
    loaded = load_project(project.root, project.client_id)
    codes = _error_codes(loaded.errors)
    assert "type" in codes
    assert any(issue.code == "type" and "daily_battle_cap" in issue.location for issue in loaded.errors)


def test_database_error_is_not_raised_for_unknown_mark(db: Database) -> None:
    session_id = db.begin_session("cn360", "hash")
    with pytest.raises(DatabaseError):
        db.save_random_profile(999, {"camera_activity": 1.0})
    assert session_id == 1
