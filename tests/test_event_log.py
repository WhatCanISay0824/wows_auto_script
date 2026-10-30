"""事件队列与文本日志单元测试（T1：src/storage/event_log.py，详细设计 2.4 / 11.1 / 11.5 / 11.8）。"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from src.common.clock import FakeClock
from src.common.errors import DatabaseError
from src.storage.database import Database
from src.storage.event_log import EventSink, configure_text_logging

TZ_CN = timezone(timedelta(hours=8))
WALL_START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=TZ_CN)


def _logger() -> logging.Logger:
    logger = logging.getLogger("tests.event_log")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.DEBUG)
    return logger


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(wall_start=WALL_START)


def _make_sink(
    tmp_path: Path,
    clock: FakeClock,
    *,
    database_factory=None,
    **kwargs,
) -> EventSink:
    options = {
        "fallback_log": tmp_path / "logs" / "db_fallback.log",
        "clock": clock,
        "logger": _logger(),
        "min_commit_interval_s": 0.0,
        "poll_timeout_s": 0.01,
        "command_timeout_s": 5.0,
    }
    options.update(kwargs)
    return EventSink(tmp_path / "data" / "wows_script.db", database_factory=database_factory, **options)


def _rows(db_path: Path, sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
    connection = sqlite3.connect(str(db_path))
    try:
        connection.row_factory = sqlite3.Row
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def _fallback_records(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 正常路径：同步 ID + 批量写库
# ---------------------------------------------------------------------------


def test_begin_ids_and_event_roundtrip(tmp_path: Path, clock: FakeClock) -> None:
    sink = _make_sink(tmp_path, clock)
    sink.start()
    try:
        session_id = sink.begin_session("cn360", "0123456789abcdef")
        assert session_id == 1
        battle_id = sink.begin_battle(session_id, "north_sea")
        assert battle_id == 1
        assert sink.set_battle_context(battle_id, map_id="north_sea", spawn_id="east_1", route_id="r1")
        assert sink.mark_queued(battle_id)
        profile_id = sink.save_random_profile(
            battle_id,
            {
                "camera_activity": 1.1,
                "attack_frequency": 0.9,
                "consumable_ratio": 0.2,
                "long_stare_bias": 0.3,
                "seed": 42,
            },
        )
        assert profile_id == 1
        assert sink.log_action(battle_id, "nav_click_waypoint", {"index": 0}, profile_id)
        clock.advance(2)
        assert sink.mark_death(battle_id)
        assert sink.mark_exit_requested(battle_id)
        assert sink.mark_returned_to_port(battle_id)
        assert sink.log_state(session_id, battle_id, "BATTLE_ACTIVE", 0.95, {"frame": 3})
        assert sink.end_battle(battle_id, "dead", "menu")
        assert sink.end_session(session_id, "battle_cap", total_battles=1)
        sink.flush()
        stats = sink.stats
        assert stats.written == 9  # 9 个异步事件（3 个同步命令不计入 written）
        assert stats.fallback == 0
        assert stats.dropped == 0
        assert stats.db_broken is False
    finally:
        sink.stop(drain=True)

    db_path = tmp_path / "data" / "wows_script.db"
    sessions = _rows(db_path, "SELECT * FROM sessions")
    assert len(sessions) == 1
    assert sessions[0]["profile_id"] == "cn360"
    assert sessions[0]["config_hash"] == "0123456789abcdef"
    assert sessions[0]["ended_at"] is not None
    assert sessions[0]["total_battles"] == 1
    battles = _rows(db_path, "SELECT * FROM battles")
    assert len(battles) == 1
    assert battles[0]["map_id"] == "north_sea"
    assert battles[0]["spawn_id"] == "east_1"
    assert battles[0]["route_id"] == "r1"
    assert battles[0]["queued_at"] is not None
    assert battles[0]["death_detected_at"] is not None
    assert battles[0]["exit_requested_at"] is not None
    assert battles[0]["returned_to_port_at"] is not None
    assert battles[0]["result"] == "dead"
    assert battles[0]["exit_method"] == "menu"
    profiles = _rows(db_path, "SELECT * FROM battle_random_profiles")
    assert len(profiles) == 1
    assert profiles[0]["seed"] == 42
    actions = _rows(db_path, "SELECT * FROM battle_actions")
    assert len(actions) == 1
    assert actions[0]["action_type"] == "nav_click_waypoint"
    assert actions[0]["random_profile_id"] == 1
    states = _rows(db_path, "SELECT * FROM state_events")
    assert len(states) == 1
    assert states[0]["state"] == "BATTLE_ACTIVE"
    assert states[0]["confidence"] == pytest.approx(0.95)


def test_database_writes_only_in_log_thread(tmp_path: Path, clock: FakeClock) -> None:
    db_path = tmp_path / "data" / "wows_script.db"
    write_threads: set[str] = set()

    def factory() -> Database:
        database = Database(db_path, clock=clock)
        for name in (
            "begin_session",
            "begin_battle",
            "log_action",
            "log_state",
            "end_battle",
            "end_session",
            "count_battles_on",
        ):
            original = getattr(database, name)

            def wrapper(*args, _original=original, **kwargs):
                write_threads.add(threading.current_thread().name)
                return _original(*args, **kwargs)

            setattr(database, name, wrapper)
        return database

    sink = _make_sink(tmp_path, clock, database_factory=factory)
    sink.start()
    try:
        session_id = sink.begin_session("cn360", "hash")
        battle_id = sink.begin_battle(session_id)
        sink.log_action(battle_id, "nav_click", None)
        sink.log_state(session_id, battle_id, "PORT", 0.9)
        sink.end_battle(battle_id, "finished", "menu")
        sink.end_session(session_id, "done", 1)
        sink.flush()
    finally:
        sink.stop(drain=True)
    assert write_threads == {"wows-log"}


def test_batches_events_and_counts_written(tmp_path: Path, clock: FakeClock) -> None:
    sink = _make_sink(tmp_path, clock, max_batch=4)
    sink.start()
    try:
        sink.begin_session("cn360", "hash")
        for index in range(10):
            assert sink.log_action(None, "nav_click", {"index": index})
        sink.flush()
        stats = sink.stats
        assert stats.written == 10
        assert stats.submitted == 10
    finally:
        sink.stop(drain=True)
    assert len(_rows(tmp_path / "data" / "wows_script.db", "SELECT * FROM battle_actions")) == 10


def test_count_battles_on_via_sink(tmp_path: Path, clock: FakeClock) -> None:
    sink = _make_sink(tmp_path, clock)
    sink.start()
    try:
        session_id = sink.begin_session("cn360", "hash")
        sink.begin_battle(session_id)
        sink.begin_battle(session_id)
        assert sink.count_battles_on(date(2026, 1, 1)) == 2
        clock.advance(24 * 3600)
        sink.begin_battle(session_id)
        assert sink.count_battles_on(date(2026, 1, 2)) == 1
        assert sink.count_battles_on(date(2026, 1, 1)) == 2
    finally:
        sink.stop(drain=True)


# ---------------------------------------------------------------------------
# 失败降级与恢复
# ---------------------------------------------------------------------------


class FlakyDatabase:
    """包装真实 Database，可在事务/重连处注入故障。"""

    def __init__(self, database: Database) -> None:
        self.real = database
        self.fail_write = False
        self.fail_reconnect = False
        self.threads: set[str] = set()

    def transaction(self):
        self.threads.add(threading.current_thread().name)
        if self.fail_write:
            raise DatabaseError("注入的批量写入故障")
        return self.real.transaction()

    def reconnect(self) -> None:
        if self.fail_reconnect:
            raise DatabaseError("注入的重连故障")
        self.real.reconnect()

    def __getattr__(self, name: str):
        target = getattr(self.real, name)

        def wrapper(*args, **kwargs):
            self.threads.add(threading.current_thread().name)
            return target(*args, **kwargs)

        setattr(self, name, wrapper)
        return wrapper


def test_fallback_on_write_failure_then_recover(tmp_path: Path, clock: FakeClock) -> None:
    db_path = tmp_path / "data" / "wows_script.db"
    holder: dict[str, FlakyDatabase] = {}

    def factory() -> Database:
        # 必须在日志线程内创建 SQLite 连接（数据库连接只在日志线程创建）
        flaky = FlakyDatabase(Database(db_path, clock=clock))
        holder["db"] = flaky
        return flaky  # type: ignore[return-value]

    sink = _make_sink(tmp_path, clock, database_factory=factory, retry_interval_s=30.0)
    sink.start()
    try:
        session_id = sink.begin_session("cn360", "hash")
        battle_id = sink.begin_battle(session_id)
        flaky = holder["db"]
        flaky.fail_write = True
        assert sink.log_action(battle_id, "nav_click", {"index": 1})
        sink.flush()
        stats = sink.stats
        assert stats.fallback == 1
        assert stats.failed_batches >= 1
        assert stats.db_broken is True
        fallback_path = tmp_path / "logs" / "db_fallback.log"
        records = _fallback_records(fallback_path)
        assert any(record["kind"] == "action" for record in records)
        assert "注入的批量写入故障" in records[0]["error"]

        # 未到重试间隔：继续降级
        assert sink.log_action(battle_id, "nav_click", {"index": 2})
        sink.flush()
        assert len(_fallback_records(fallback_path)) == 2
        assert len(_rows(db_path, "SELECT * FROM battle_actions")) == 0

        # 重连失败：仍降级
        flaky.fail_write = False
        flaky.fail_reconnect = True
        clock.advance(31)
        assert sink.log_action(battle_id, "nav_click", {"index": 3})
        sink.flush()
        assert sink.stats.db_broken is True
        assert len(_fallback_records(fallback_path)) == 3

        # 重连成功：恢复写库
        flaky.fail_reconnect = False
        clock.advance(31)
        assert sink.log_action(battle_id, "nav_click", {"index": 4})
        sink.flush()
        assert sink.stats.db_broken is False
        fallback_count = len(_fallback_records(fallback_path))
        actions = _rows(db_path, "SELECT * FROM battle_actions")
        assert len(actions) == 1
        assert actions[0]["detail"] == '{"index": 4}'
        assert len(_fallback_records(fallback_path)) == fallback_count  # 恢复后不再追加降级记录
    finally:
        sink.stop(drain=True)
    assert flaky.threads == {"wows-log"}


def test_fallback_when_database_cannot_open(tmp_path: Path, clock: FakeClock) -> None:
    def broken_factory() -> Database:
        raise DatabaseError("磁盘不可用")

    sink = _make_sink(tmp_path, clock, database_factory=broken_factory)
    sink.start()
    try:
        assert sink.log_action(None, "capture_error", {"reason": "black_screen"})
        sink.flush()
        stats = sink.stats
        assert stats.fallback == 1
        assert stats.db_broken is True
        # 需要返回 ID 的命令无法降级：抛 DatabaseError
        with pytest.raises(DatabaseError):
            sink.begin_session("cn360", "hash")
    finally:
        sink.stop(drain=True)
    records = _fallback_records(tmp_path / "logs" / "db_fallback.log")
    assert len(records) == 1
    assert records[0]["kind"] == "action"
    assert records[0]["payload"]["action_type"] == "capture_error"


# ---------------------------------------------------------------------------
# 队列上限 / 生命周期
# ---------------------------------------------------------------------------


def test_queue_overflow_drops_events(tmp_path: Path, clock: FakeClock) -> None:
    sink = _make_sink(tmp_path, clock, queue_max=2)
    results = [sink.log_action(None, "nav_click", {"index": index}) for index in range(5)]
    assert results == [True, True, False, False, False]
    stats = sink.stats
    assert stats.submitted == 5
    assert stats.dropped == 3
    sink.stop(drain=True)  # 未启动过线程：直接标记停止，不抛异常
    assert sink.is_running is False
    assert sink.log_action(None, "nav_click", None) is False


def test_commands_require_running_sink(tmp_path: Path, clock: FakeClock) -> None:
    sink = _make_sink(tmp_path, clock)
    with pytest.raises(RuntimeError):
        sink.begin_session("cn360", "hash")
    sink.stop()


def test_stop_is_idempotent(tmp_path: Path, clock: FakeClock) -> None:
    sink = _make_sink(tmp_path, clock)
    sink.start()
    sink.stop(drain=True)
    sink.stop(drain=True)
    assert sink.is_running is False


def test_stop_without_drain_does_not_raise(tmp_path: Path, clock: FakeClock) -> None:
    sink = _make_sink(tmp_path, clock)
    sink.start()
    sink.log_action(None, "nav_click", None)
    sink.stop(drain=False, timeout_s=5.0)
    assert sink.is_running is False


def test_end_battle_enum_validated_synchronously(tmp_path: Path, clock: FakeClock) -> None:
    sink = _make_sink(tmp_path, clock)
    sink.start()
    try:
        session_id = sink.begin_session("cn360", "hash")
        battle_id = sink.begin_battle(session_id)
        with pytest.raises(ValueError):
            sink.end_battle(battle_id, "nope", "menu")
        with pytest.raises(ValueError):
            sink.end_battle(battle_id, "dead", "nope")
    finally:
        sink.stop(drain=True)


# ---------------------------------------------------------------------------
# 文本日志
# ---------------------------------------------------------------------------


def test_configure_text_logging_and_mirror(tmp_path: Path, clock: FakeClock) -> None:
    logger = configure_text_logging(
        tmp_path / "logs", level="INFO", console=False, session_started=WALL_START
    )
    sink = _make_sink(tmp_path, clock, logger=logger)
    sink.start()
    try:
        session_id = sink.begin_session("cn360", "hash")
        battle_id = sink.begin_battle(session_id)
        sink.log_state(session_id, battle_id, "PORT", 0.92)
        sink.log_action(battle_id, "nav_select_route", {"route_id": "r1"})
        sink.flush()
    finally:
        sink.stop(drain=True)
    log_files = list((tmp_path / "logs").glob("session_*.log"))
    assert len(log_files) == 1
    assert log_files[0].name == "session_20260101_120000.log"
    content = log_files[0].read_text(encoding="utf-8")
    assert "state=PORT" in content
    assert "nav_select_route" in content
    assert "begin_session" in content  # 同步命令也记录文本日志


def test_configure_text_logging_replaces_handlers(tmp_path: Path) -> None:
    first = configure_text_logging(tmp_path / "logs", console=False, session_started=WALL_START)
    assert len(first.handlers) == 1
    second = configure_text_logging(
        tmp_path / "logs", level="DEBUG", console=False, session_started=WALL_START + timedelta(seconds=1)
    )
    assert second is first
    assert len(second.handlers) == 1
    assert len(list((tmp_path / "logs").glob("session_*.log"))) == 2
