"""SQLite 运行日志（详细设计第 11 章 + 实现补充 v2 表结构）。

设计口径（评审补充 P0/P1 与 Lead 纠偏）：
- 表结构 v2：``sessions.total_battles`` / ``sessions.local_date``、
  ``battles.queued_at`` / ``battles.local_date``，删除 ``battles.random_profile_id``；
  ``battle_random_profiles.battle_id`` 为 UNIQUE 外键（与战斗一对一），
  ``battle_actions.random_profile_id`` 保留为普通列；
- 时间统一为带时区 ISO-8601 文本（详细设计 11.1）；
- 每日场次统计使用 ``local_date``（Python 按本地时区从墙上时间计算），
  禁止用 SQLite ``date()`` 对带时区文本取日期（会把 00:00～08:00 算到前一天，评审 P1-11）；
- 每个连接执行 ``PRAGMA foreign_keys = ON`` 与 ``PRAGMA journal_mode = WAL``；
- **数据库连接只在日志线程创建和使用**：运行期请通过 ``src.storage.event_log.EventSink``
  提交事件；本类只被日志线程独占，或由单元测试直接在单线程里使用。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..common.clock import Clock, SystemClock, iso_timestamp
from ..common.errors import DatabaseError

# 数据库结构版本（实现补充 v2）
SCHEMA_VERSION = 2
# battles.result 合法取值（详细设计 11.2）
RESULT_VALUES: frozenset[str] = frozenset({"dead", "finished", "aborted"})
# battles.exit_method 合法取值（实现补充第 4 节）
EXIT_METHOD_VALUES: frozenset[str] = frozenset({"menu", "confirm", "direct", "recovery", "none"})
# 随机档案字段（实现补充 v2）
RANDOM_PROFILE_FIELDS: tuple[str, ...] = (
    "camera_activity",
    "attack_frequency",
    "consumable_ratio",
    "long_stare_bias",
    "seed",
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    local_date      TEXT NOT NULL,
    profile_id      TEXT NOT NULL,
    config_hash     TEXT NOT NULL,
    account_alias   TEXT,
    stop_reason     TEXT,
    total_battles   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS battles (
    battle_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            INTEGER NOT NULL REFERENCES sessions(session_id),
    started_at            TEXT NOT NULL,
    queued_at             TEXT,
    local_date            TEXT NOT NULL,
    account_alias         TEXT,
    map_id                TEXT,
    spawn_id              TEXT,
    route_id              TEXT,
    death_detected_at     TEXT,
    exit_requested_at     TEXT,
    exit_confirmed_at     TEXT,
    returned_to_port_at   TEXT,
    exit_method           TEXT,
    exit_error            TEXT,
    result                TEXT
);

CREATE TABLE IF NOT EXISTS battle_random_profiles (
    profile_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    battle_id          INTEGER NOT NULL UNIQUE REFERENCES battles(battle_id),
    camera_activity    REAL NOT NULL,
    attack_frequency   REAL NOT NULL,
    consumable_ratio   REAL NOT NULL,
    long_stare_bias    REAL NOT NULL,
    seed               INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS battle_actions (
    action_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    battle_id          INTEGER REFERENCES battles(battle_id),
    occurred_at        TEXT NOT NULL,
    action_type        TEXT NOT NULL,
    detail             TEXT,
    random_profile_id  INTEGER
);

CREATE TABLE IF NOT EXISTS state_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER REFERENCES sessions(session_id),
    battle_id    INTEGER REFERENCES battles(battle_id),
    occurred_at  TEXT NOT NULL,
    state        TEXT NOT NULL,
    confidence   REAL,
    detail       TEXT
);

CREATE INDEX IF NOT EXISTS idx_battles_started ON battles(started_at);
CREATE INDEX IF NOT EXISTS idx_battles_local_date ON battles(local_date);
CREATE INDEX IF NOT EXISTS idx_battles_account_date ON battles(local_date, account_alias);
CREATE INDEX IF NOT EXISTS idx_actions_battle_time ON battle_actions(battle_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_states_battle_time ON state_events(battle_id, occurred_at);
"""

# 战斗时间戳字段 -> 写入方法后缀（内部使用，列名不允许来自外部输入）
_BATTLE_TIME_COLUMNS: Mapping[str, str] = {
    "queued": "queued_at",
    "death": "death_detected_at",
    "exit_requested": "exit_requested_at",
    "exit_confirmed": "exit_confirmed_at",
    "returned_to_port": "returned_to_port_at",
}


class Database:
    """SQLite 日志库：只由日志线程（EventSink 工作线程）创建和写入。"""

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Clock | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._path = Path(path)
        self._clock = clock or SystemClock()
        self._busy_timeout_ms = busy_timeout_ms
        self._transaction_depth = 0
        self._connection = self._connect()
        self._initialize()

    # ------------------------------------------------------------------
    # 连接与结构
    # ------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接（仅限创建它的线程使用；测试用于查询表结构）。"""
        return self._connection

    def _connect(self) -> sqlite3.Connection:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._path)
        except (OSError, sqlite3.Error) as exc:
            raise DatabaseError(f"无法打开 SQLite 数据库 {self._path}: {exc}") from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_ms)}")
        except sqlite3.Error as exc:  # pragma: no cover - 取决于文件系统
            connection.close()
            raise DatabaseError(f"初始化 SQLite PRAGMA 失败: {exc}") from exc
        return connection

    def _initialize(self) -> None:
        raw_version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if raw_version > SCHEMA_VERSION:
            raise DatabaseError(
                f"数据库版本 {raw_version} 高于程序支持的 {SCHEMA_VERSION}，请升级脚本或删除数据库重新生成"
            )
        try:
            self._connection.executescript(_SCHEMA_SQL)
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._connection.commit()
        except sqlite3.Error as exc:
            raise DatabaseError(f"创建数据库表失败: {exc}") from exc

    def reconnect(self) -> None:
        """关闭并重建连接（EventSink 在写入失败后按间隔调用）。"""
        with contextlib.suppress(sqlite3.Error):
            self._connection.close()
        self._connection = self._connect()
        self._initialize()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务：把多次写入合并为一次提交（EventSink 批量写入口）。

        事务内调用本类的写方法不会各自提交；最外层退出时统一 commit/rollback。
        """
        self._transaction_depth += 1
        try:
            yield self._connection
        except BaseException:
            if self._transaction_depth == 1:
                self._connection.rollback()
            raise
        else:
            if self._transaction_depth == 1:
                self._connection.commit()
        finally:
            self._transaction_depth -= 1

    def _write(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """执行写语句：事务内不提交，事务外自动提交（失败由调用方包装异常）。"""
        if self._transaction_depth > 0:
            return self._connection.execute(sql, parameters)
        with self._connection:
            return self._connection.execute(sql, parameters)

    def close(self) -> None:
        try:
            self._connection.commit()
            self._connection.execute("PRAGMA journal_mode = DELETE")  # 合并 WAL，便于复制数据库文件
        except sqlite3.Error:
            pass
        with contextlib.suppress(sqlite3.Error):
            self._connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 时间辅助
    # ------------------------------------------------------------------
    def _now(self, moment: datetime | None = None) -> str:
        return iso_timestamp(moment or self._clock.wall_now())

    def _local_date(self, moment: datetime | None = None) -> str:
        """本地时区日期（YYYY-MM-DD），用于每日场次统计（评审 P1-11）。"""
        return (moment or self._clock.wall_now()).date().isoformat()

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    def begin_session(
        self,
        profile_id: str,
        config_hash: str,
        account_alias: str | None = None,
    ) -> int:
        """开始会话，返回 session_id；account_alias 为本地账号别名（可空）。"""
        moment = self._clock.wall_now()
        try:
            cursor = self._write(
                "INSERT INTO sessions "
                "(started_at, local_date, profile_id, config_hash, account_alias, total_battles) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (
                    self._now(moment),
                    self._local_date(moment),
                    profile_id,
                    config_hash,
                    account_alias,
                ),
            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"创建会话失败: {exc}") from exc
        return int(cursor.lastrowid or 0)

    def end_session(self, session_id: int, stop_reason: str, total_battles: int) -> None:
        """结束会话：写入结束时间、停止原因与总场次。"""
        try:
            cursor = self._write(
                "UPDATE sessions SET ended_at = ?, stop_reason = ?, total_battles = ? WHERE session_id = ?",
                (self._now(), stop_reason, int(total_battles), session_id),
            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"结束会话失败: {exc}") from exc
        if cursor.rowcount == 0:
            raise DatabaseError(f"会话不存在: {session_id}")

    # ------------------------------------------------------------------
    # 战斗
    # ------------------------------------------------------------------
    def begin_battle(
        self,
        session_id: int,
        map_id: str | None = None,
        account_alias: str | None = None,
    ) -> int:
        """开始一场战斗，返回 battle_id；account_alias 未传时从所属会话继承。"""
        moment = self._clock.wall_now()
        effective_alias = account_alias
        if effective_alias is None:
            try:
                row = self._connection.execute(
                    "SELECT account_alias FROM sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
            except sqlite3.Error as exc:
                raise DatabaseError(f"读取会话账号信息失败: {exc}") from exc
            if row is not None:
                effective_alias = row[0]
        try:
            cursor = self._write(
                "INSERT INTO battles (session_id, started_at, local_date, account_alias, map_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    self._now(moment),
                    self._local_date(moment),
                    effective_alias,
                    map_id,
                ),
            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"创建战斗记录失败: {exc}") from exc
        return int(cursor.lastrowid or 0)

    def count_battles_on(self, day: date, account_alias: str | None = None) -> int:
        """统计本地日期 day 当天开始的战斗数（每日场次上限，跨进程累计）。

        ``account_alias`` 为 None 时统计全部账号合计（向后兼容/单账号模式），
        指定时按账号隔离（多账号扩展：每个账号独立每日上限）。
        """
        local_date = day.isoformat() if isinstance(day, date) else str(day)
        try:
            if account_alias is None:
                cursor = self._connection.execute(
                    "SELECT COUNT(*) FROM battles WHERE local_date = ?", (local_date,)
                )
            else:
                cursor = self._connection.execute(
                    "SELECT COUNT(*) FROM battles WHERE local_date = ? AND account_alias = ?",
                    (local_date, account_alias),
                )
            return int(cursor.fetchone()[0])
        except sqlite3.Error as exc:
            raise DatabaseError(f"统计每日场次失败: {exc}") from exc

    def set_battle_context(
        self,
        battle_id: int,
        *,
        map_id: str | None = None,
        spawn_id: str | None = None,
        route_id: str | None = None,
    ) -> None:
        """补充战斗上下文（识别地图/出生点/航线后调用；只更新非 None 字段）。"""
        updates: list[str] = []
        params: list[Any] = []
        for column, value in (("map_id", map_id), ("spawn_id", spawn_id), ("route_id", route_id)):
            if value is not None:
                updates.append(f"{column} = ?")
                params.append(value)
        if not updates:
            return
        params.append(battle_id)
        try:
            cursor = self._write(
                f"UPDATE battles SET {', '.join(updates)} WHERE battle_id = ?", tuple(params)
            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"更新战斗上下文失败: {exc}") from exc
        if cursor.rowcount == 0:
            raise DatabaseError(f"战斗不存在: {battle_id}")

    def _mark_battle_time(self, kind: str, battle_id: int, occurred_at: datetime | None) -> None:
        column = _BATTLE_TIME_COLUMNS[kind]
        try:
            cursor = self._write(
                f"UPDATE battles SET {column} = ? WHERE battle_id = ?",
                (self._now(occurred_at), battle_id),
            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"更新战斗时间戳 {column} 失败: {exc}") from exc
        if cursor.rowcount == 0:
            raise DatabaseError(f"战斗不存在: {battle_id}")

    def mark_queued(self, battle_id: int, occurred_at: datetime | None = None) -> None:
        """记录进入排队的时间（周转指标起点，实现补充 P1-8）。"""
        self._mark_battle_time("queued", battle_id, occurred_at)

    def mark_death(self, battle_id: int, occurred_at: datetime | None = None) -> None:
        self._mark_battle_time("death", battle_id, occurred_at)

    def mark_exit_requested(self, battle_id: int, occurred_at: datetime | None = None) -> None:
        self._mark_battle_time("exit_requested", battle_id, occurred_at)

    def mark_exit_confirmed(self, battle_id: int, occurred_at: datetime | None = None) -> None:
        self._mark_battle_time("exit_confirmed", battle_id, occurred_at)

    def mark_returned_to_port(self, battle_id: int, occurred_at: datetime | None = None) -> None:
        self._mark_battle_time("returned_to_port", battle_id, occurred_at)
        # 回到港口即视为退出动作已完成
        try:
            self._write(
                "UPDATE battles SET exit_confirmed_at = COALESCE(exit_confirmed_at, ?) WHERE battle_id = ?",
                (self._now(occurred_at), battle_id),
            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"更新退出确认时间失败: {exc}") from exc

    def end_battle(
        self,
        battle_id: int,
        result: str,
        exit_method: str,
        exit_error: str | None = None,
    ) -> None:
        """结束一场战斗：result（dead/finished/aborted）与 exit_method（menu/confirm/...）。"""
        if result not in RESULT_VALUES:
            raise DatabaseError(f"非法 result: {result!r}（允许 {sorted(RESULT_VALUES)}）")
        if exit_method not in EXIT_METHOD_VALUES:
            raise DatabaseError(f"非法 exit_method: {exit_method!r}（允许 {sorted(EXIT_METHOD_VALUES)}）")
        try:
            cursor = self._write(
                "UPDATE battles SET result = ?, exit_method = ?, exit_error = ?, "
                "exit_confirmed_at = CASE WHEN exit_confirmed_at IS NULL AND ? != 'none' "
                "THEN ? ELSE exit_confirmed_at END WHERE battle_id = ?",
                (result, exit_method, exit_error, exit_method, self._now(), battle_id),
            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"结束战斗失败: {exc}") from exc
        if cursor.rowcount == 0:
            raise DatabaseError(f"战斗不存在: {battle_id}")

    def save_random_profile(self, battle_id: int, profile: Mapping[str, Any]) -> int:
        """保存本场随机档案（与战斗一对一），返回 profile_id。"""
        missing = [name for name in RANDOM_PROFILE_FIELDS if name not in profile]
        if missing:
            raise DatabaseError(f"随机档案缺少字段: {missing}")
        values = [profile[name] for name in RANDOM_PROFILE_FIELDS]
        for name, value in zip(RANDOM_PROFILE_FIELDS, values, strict=True):
            if name == "seed":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise DatabaseError(f"随机档案 seed 必须是整数，实际为 {value!r}")
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                raise DatabaseError(f"随机档案 {name} 必须是数字，实际为 {value!r}")
        try:
            cursor = self._write(
                "INSERT INTO battle_random_profiles "
                "(battle_id, camera_activity, attack_frequency, consumable_ratio, long_stare_bias, seed) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (battle_id, *values),
            )
        except sqlite3.IntegrityError as exc:
            raise DatabaseError(f"战斗 {battle_id} 已保存随机档案或战斗不存在: {exc}") from exc
        except sqlite3.Error as exc:
            raise DatabaseError(f"保存随机档案失败: {exc}") from exc
        return int(cursor.lastrowid or 0)

    # ------------------------------------------------------------------
    # 动作 / 状态事件
    # ------------------------------------------------------------------
    def log_action(
        self,
        battle_id: int | None,
        action_type: str,
        detail: Mapping[str, Any] | None,
        random_profile_id: int | None = None,
        *,
        occurred_at: datetime | None = None,
    ) -> int:
        """写一条动作日志，detail 以 JSON 文本保存。"""
        try:
            cursor = self._write(
                "INSERT INTO battle_actions (battle_id, occurred_at, action_type, detail, random_profile_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (battle_id, self._now(occurred_at), action_type, _json_text(detail), random_profile_id),
            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"写动作日志失败: {exc}") from exc
        return int(cursor.lastrowid or 0)

    def log_state(
        self,
        session_id: int | None,
        battle_id: int | None,
        state: str,
        confidence: float | None,
        detail: Mapping[str, Any] | None,
        *,
        occurred_at: datetime | None = None,
    ) -> int:
        """写一条状态事件，detail 以 JSON 文本保存。"""
        try:
            cursor = self._write(
                "INSERT INTO state_events (session_id, battle_id, occurred_at, state, confidence, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, battle_id, self._now(occurred_at), state, confidence, _json_text(detail)),
            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"写状态事件失败: {exc}") from exc
        return int(cursor.lastrowid or 0)


def _json_text(payload: Mapping[str, Any] | None) -> str | None:
    if payload is None:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 调用方应保证可序列化
        return json.dumps({"_error": f"detail 序列化失败: {exc}"}, ensure_ascii=False)


@contextmanager
def open_database(path: Path | str, *, clock: Clock | None = None) -> Iterator[Database]:
    """上下文管理器形式的数据库打开（主要给单线程脚本/测试使用）。"""
    database = Database(path, clock=clock)
    try:
        yield database
    finally:
        database.close()
