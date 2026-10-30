"""SQLite 存储层单元测试（T1：src/storage/database.py，详细设计 11 + 实现补充 v2）。"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from src.common.clock import FakeClock
from src.common.errors import DatabaseError
from src.storage.database import (
    EXIT_METHOD_VALUES,
    RESULT_VALUES,
    SCHEMA_VERSION,
    Database,
)

TZ_CN = timezone(timedelta(hours=8))
WALL_START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=TZ_CN)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(wall_start=WALL_START)


@pytest.fixture
def db(tmp_path: Path, clock: FakeClock) -> Database:
    database = Database(tmp_path / "data" / "wows_script.db", clock=clock)
    yield database
    database.close()


def _columns(database: Database, table: str) -> dict[str, sqlite3.Row]:
    rows = database.connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"]: row for row in rows}


def _row(database: Database, sql: str, parameters: tuple = ()) -> sqlite3.Row | None:
    return database.connection.execute(sql, parameters).fetchone()


def _count(database: Database, table: str) -> int:
    return int(database.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# ---------------------------------------------------------------------------
# 表结构 v2
# ---------------------------------------------------------------------------


def test_schema_version(db: Database) -> None:
    assert db.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2
    assert db.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_sessions_table_v2(db: Database) -> None:
    columns = _columns(db, "sessions")
    assert set(columns) == {
        "session_id",
        "started_at",
        "ended_at",
        "local_date",
        "profile_id",
        "config_hash",
        "account_alias",
        "stop_reason",
        "total_battles",
    }
    assert columns["total_battles"]["dflt_value"] == "0"


def test_battles_table_v2(db: Database) -> None:
    columns = _columns(db, "battles")
    assert "queued_at" in columns
    assert "local_date" in columns
    assert "account_alias" in columns
    assert "random_profile_id" not in columns  # 评审 P1-9：删除该列
    assert "death_detected_at" in columns
    assert "exit_requested_at" in columns
    assert "exit_confirmed_at" in columns
    assert "returned_to_port_at" in columns


def test_account_date_index_exists(db: Database) -> None:
    indexes = db.connection.execute("PRAGMA index_list(battles)").fetchall()
    names = {row["name"] for row in indexes}
    assert "idx_battles_account_date" in names
    info = db.connection.execute("PRAGMA index_info(idx_battles_account_date)").fetchall()
    assert [row["name"] for row in info] == ["local_date", "account_alias"]


def test_random_profiles_one_to_one(db: Database) -> None:
    columns = _columns(db, "battle_random_profiles")
    assert set(columns) == {
        "profile_id",
        "battle_id",
        "camera_activity",
        "attack_frequency",
        "consumable_ratio",
        "long_stare_bias",
        "seed",
    }
    # battle_id 上存在唯一索引（一对一，评审 P1-9）
    indexes = db.connection.execute("PRAGMA index_list(battle_random_profiles)").fetchall()
    unique_columns: list[list[str]] = []
    for index in indexes:
        if index["unique"]:
            info = db.connection.execute(f"PRAGMA index_info({index['name']})").fetchall()
            unique_columns.append([item["name"] for item in info])
    assert ["battle_id"] in unique_columns


def test_actions_table_keeps_random_profile_id(db: Database) -> None:
    assert "random_profile_id" in _columns(db, "battle_actions")
    state_columns = _columns(db, "state_events")
    assert {"event_id", "session_id", "battle_id", "occurred_at", "state", "confidence", "detail"} == set(
        state_columns
    )


# ---------------------------------------------------------------------------
# 会话 / 战斗生命周期
# ---------------------------------------------------------------------------


def test_session_lifecycle(db: Database, clock: FakeClock) -> None:
    session_id = db.begin_session("cn360", "0123456789abcdef")
    assert session_id == 1
    row = _row(db, "SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    assert row is not None
    assert row["started_at"] == "2026-01-01T12:00:00+08:00"
    assert row["local_date"] == "2026-01-01"
    assert row["profile_id"] == "cn360"
    assert row["config_hash"] == "0123456789abcdef"
    assert row["ended_at"] is None
    assert row["total_battles"] == 0

    clock.advance(600)
    db.end_session(session_id, "safety_stop", total_battles=3)
    row = _row(db, "SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    assert row is not None
    assert row["ended_at"] == "2026-01-01T12:10:00+08:00"
    assert row["stop_reason"] == "safety_stop"
    assert row["total_battles"] == 3


def test_end_session_missing_raises(db: Database) -> None:
    with pytest.raises(DatabaseError):
        db.end_session(999, "x", 0)


def test_battle_full_lifecycle(db: Database, clock: FakeClock) -> None:
    session_id = db.begin_session("cn360", "hash")
    battle_id = db.begin_battle(session_id, "north_sea")
    assert battle_id == 1
    db.mark_queued(battle_id)
    clock.advance(5)
    db.mark_death(battle_id)
    clock.advance(1)
    db.mark_exit_requested(battle_id)
    clock.advance(2)
    db.mark_exit_confirmed(battle_id)
    clock.advance(3)
    db.mark_returned_to_port(battle_id)
    db.end_battle(battle_id, "dead", "menu")
    row = _row(db, "SELECT * FROM battles WHERE battle_id = ?", (battle_id,))
    assert row is not None
    assert row["session_id"] == session_id
    assert row["map_id"] == "north_sea"
    assert row["local_date"] == "2026-01-01"
    assert row["queued_at"] == "2026-01-01T12:00:00+08:00"
    assert row["death_detected_at"] == "2026-01-01T12:00:05+08:00"
    assert row["exit_requested_at"] == "2026-01-01T12:00:06+08:00"
    assert row["exit_confirmed_at"] == "2026-01-01T12:00:08+08:00"
    assert row["returned_to_port_at"] == "2026-01-01T12:00:11+08:00"
    assert row["result"] == "dead"
    assert row["exit_method"] == "menu"
    assert row["exit_error"] is None


def test_end_battle_aborted_does_not_confirm_exit(db: Database) -> None:
    session_id = db.begin_session("cn360", "hash")
    battle_id = db.begin_battle(session_id, None)
    db.end_battle(battle_id, "aborted", "none", exit_error="进程崩溃")
    row = _row(db, "SELECT * FROM battles WHERE battle_id = ?", (battle_id,))
    assert row is not None
    assert row["result"] == "aborted"
    assert row["exit_method"] == "none"
    assert row["exit_error"] == "进程崩溃"
    assert row["exit_confirmed_at"] is None


def test_end_battle_validates_enums(db: Database) -> None:
    session_id = db.begin_session("cn360", "hash")
    battle_id = db.begin_battle(session_id, None)
    with pytest.raises(DatabaseError):
        db.end_battle(battle_id, "unknown_result", "menu")
    with pytest.raises(DatabaseError):
        db.end_battle(battle_id, "dead", "unknown_method")
    assert {"dead", "finished", "aborted"} == RESULT_VALUES
    assert {"menu", "confirm", "direct", "recovery", "none"} == EXIT_METHOD_VALUES


def test_set_battle_context(db: Database) -> None:
    session_id = db.begin_session("cn360", "hash")
    battle_id = db.begin_battle(session_id, None)
    db.set_battle_context(battle_id, map_id="north_sea", spawn_id="east_1", route_id="east_1_south")
    row = _row(db, "SELECT map_id, spawn_id, route_id FROM battles WHERE battle_id = ?", (battle_id,))
    assert (row["map_id"], row["spawn_id"], row["route_id"]) == ("north_sea", "east_1", "east_1_south")
    db.set_battle_context(battle_id, spawn_id="west_1")  # 只更新非 None 字段
    row = _row(db, "SELECT map_id, spawn_id FROM battles WHERE battle_id = ?", (battle_id,))
    assert row["map_id"] == "north_sea"
    assert row["spawn_id"] == "west_1"
    with pytest.raises(DatabaseError):
        db.set_battle_context(999, map_id="x")


def test_marks_missing_battle_raise(db: Database) -> None:
    for method in (
        db.mark_queued,
        db.mark_death,
        db.mark_exit_requested,
        db.mark_exit_confirmed,
        db.mark_returned_to_port,
    ):
        with pytest.raises(DatabaseError):
            method(12345)


# ---------------------------------------------------------------------------
# 随机档案
# ---------------------------------------------------------------------------


def _random_profile() -> dict[str, object]:
    return {
        "camera_activity": 1.2,
        "attack_frequency": 0.8,
        "consumable_ratio": 0.15,
        "long_stare_bias": 0.4,
        "seed": 20260101,
    }


def test_save_random_profile(db: Database) -> None:
    session_id = db.begin_session("cn360", "hash")
    battle_id = db.begin_battle(session_id, "north_sea")
    profile_id = db.save_random_profile(battle_id, _random_profile())
    assert profile_id == 1
    row = _row(db, "SELECT * FROM battle_random_profiles WHERE battle_id = ?", (battle_id,))
    assert row is not None
    assert row["camera_activity"] == pytest.approx(1.2)
    assert row["attack_frequency"] == pytest.approx(0.8)
    assert row["consumable_ratio"] == pytest.approx(0.15)
    assert row["long_stare_bias"] == pytest.approx(0.4)
    assert row["seed"] == 20260101


def test_save_random_profile_duplicate_and_missing(db: Database) -> None:
    session_id = db.begin_session("cn360", "hash")
    battle_id = db.begin_battle(session_id, None)
    db.save_random_profile(battle_id, _random_profile())
    with pytest.raises(DatabaseError):
        db.save_random_profile(battle_id, _random_profile())  # 一对一
    with pytest.raises(DatabaseError):
        db.save_random_profile(999, _random_profile())  # 外键
    with pytest.raises(DatabaseError):
        db.save_random_profile(battle_id + 1, {"camera_activity": 1.0})


def test_random_profile_type_validation(db: Database) -> None:
    session_id = db.begin_session("cn360", "hash")
    battle_id = db.begin_battle(session_id, None)
    bad = _random_profile()
    bad["seed"] = 1.5
    with pytest.raises(DatabaseError):
        db.save_random_profile(battle_id, bad)
    bad = _random_profile()
    bad["camera_activity"] = "fast"
    with pytest.raises(DatabaseError):
        db.save_random_profile(battle_id, bad)


# ---------------------------------------------------------------------------
# 动作 / 状态事件
# ---------------------------------------------------------------------------


def test_log_action_and_state(db: Database, clock: FakeClock) -> None:
    session_id = db.begin_session("cn360", "hash")
    battle_id = db.begin_battle(session_id, "north_sea")
    action_id = db.log_action(battle_id, "nav_click_waypoint", {"index": 1, "world": [10.5, -20.0]}, 7)
    assert action_id == 1
    row = _row(db, "SELECT * FROM battle_actions WHERE action_id = ?", (action_id,))
    assert row is not None
    assert row["battle_id"] == battle_id
    assert row["action_type"] == "nav_click_waypoint"
    assert row["random_profile_id"] == 7
    assert '"index": 1' in row["detail"]
    assert row["occurred_at"] == "2026-01-01T12:00:00+08:00"

    clock.advance(3)
    event_id = db.log_state(session_id, battle_id, "BATTLE_ACTIVE", 0.92, {"score": 0.9})
    row = _row(db, "SELECT * FROM state_events WHERE event_id = ?", (event_id,))
    assert row is not None
    assert row["state"] == "BATTLE_ACTIVE"
    assert row["confidence"] == pytest.approx(0.92)
    assert row["occurred_at"] == "2026-01-01T12:00:03+08:00"


def test_log_action_without_battle_and_none_detail(db: Database) -> None:
    action_id = db.log_action(None, "session_stop", None)
    row = _row(db, "SELECT * FROM battle_actions WHERE action_id = ?", (action_id,))
    assert row is not None
    assert row["battle_id"] is None
    assert row["detail"] is None


def test_detail_json_keeps_chinese(db: Database) -> None:
    session_id = db.begin_session("cn360", "hash")
    battle_id = db.begin_battle(session_id, None)
    db.log_action(battle_id, "state_change", {"备注": "测试中文"})
    row = _row(db, "SELECT detail FROM battle_actions WHERE battle_id = ?", (battle_id,))
    assert "测试中文" in row["detail"]


def test_foreign_key_violation(db: Database) -> None:
    with pytest.raises(DatabaseError):
        db.log_action(9999, "nav_click", None)
    with pytest.raises(DatabaseError):
        db.log_state(9999, None, "PORT", 0.9, None)


# ---------------------------------------------------------------------------
# 每日场次统计（评审 P1-11：按 local_date，不用 SQLite date()）
# ---------------------------------------------------------------------------


def test_count_battles_on_uses_local_date_not_utc(tmp_path: Path) -> None:
    # 本地 2026-01-01 00:30 +08:00，此时 UTC 仍是 2025-12-31：
    # 用 SQLite date(started_at) 会算到前一天，必须按 local_date 统计。
    clock = FakeClock(wall_start=datetime(2026, 1, 1, 0, 30, 0, tzinfo=TZ_CN))
    database = Database(tmp_path / "db.sqlite", clock=clock)
    try:
        session_id = database.begin_session("cn360", "hash")
        database.begin_battle(session_id, None)
        assert database.count_battles_on(date(2026, 1, 1)) == 1
        assert database.count_battles_on(date(2025, 12, 31)) == 0
    finally:
        database.close()


def test_count_battles_on_across_days(db: Database, clock: FakeClock) -> None:
    session_id = db.begin_session("cn360", "hash")
    db.begin_battle(session_id, None)
    db.begin_battle(session_id, None)
    clock.advance(24 * 3600)
    db.begin_battle(session_id, None)
    clock.advance(24 * 3600)
    db.begin_battle(session_id, None)
    assert db.count_battles_on(date(2026, 1, 1)) == 2
    assert db.count_battles_on(date(2026, 1, 2)) == 1
    assert db.count_battles_on(date(2026, 1, 3)) == 1
    assert db.count_battles_on(date(2026, 1, 4)) == 0


# ---------------------------------------------------------------------------
# 事务 / 持久化 / 版本
# ---------------------------------------------------------------------------


def test_transaction_rollback(db: Database) -> None:
    session_id = db.begin_session("cn360", "hash")
    battle_id = db.begin_battle(session_id, None)
    with pytest.raises(RuntimeError), db.transaction():
        db.log_action(battle_id, "nav_click", {"index": 1})
        raise RuntimeError("模拟写入中途失败")
    assert _count(db, "battle_actions") == 0


def test_transaction_batch_commit(db: Database) -> None:
    session_id = db.begin_session("cn360", "hash")
    battle_id = db.begin_battle(session_id, None)
    with db.transaction():
        for index in range(5):
            db.log_action(battle_id, "nav_click", {"index": index})
        db.log_state(session_id, battle_id, "BATTLE_ACTIVE", 0.9, None)
    assert _count(db, "battle_actions") == 5
    assert _count(db, "state_events") == 1


def test_persistence_across_reopen(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "db.sqlite"
    first = Database(path, clock=clock)
    session_id = first.begin_session("cn360", "hash")
    battle_id = first.begin_battle(session_id, "north_sea")
    first.mark_queued(battle_id)
    first.close()

    second = Database(path, clock=clock)
    try:
        assert second.count_battles_on(date(2026, 1, 1)) == 1
        row = _row(second, "SELECT queued_at FROM battles WHERE battle_id = ?", (battle_id,))
        assert row["queued_at"] is not None
    finally:
        second.close()


def test_schema_version_too_new(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()
    with pytest.raises(DatabaseError):
        Database(path)


def test_database_error_on_invalid_path(tmp_path: Path) -> None:
    directory = tmp_path / "as_directory"
    directory.mkdir()
    with pytest.raises(DatabaseError):
        Database(directory)


def test_context_manager_and_reconnect(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    with Database(path) as database:
        session_id = database.begin_session("cn360", "hash")
        assert session_id == 1
        database.reconnect()
        assert database.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # 关闭后 WAL 已合并，文件仍可被重新打开
    with Database(path) as reopened:
        assert _count(reopened, "sessions") == 1
