"""事件队列、批量写库与文本日志（详细设计 2.4 / 11.1 / 11.5 / 11.8）。

职责：
- ``EventSink``：主线程唯一允许的数据库写入入口。它把事件放进线程安全队列，
  由内部日志线程批量写入 SQLite；开始会话、开始战斗、保存随机档案等需要返回
  ID 的接口通过 Future 同步返回（Lead 评审：数据库连接只在日志线程创建）；
- ``configure_text_logging``：按详细设计 11.5 配置 ``logs/session_<时间>.log``
  大小轮转文本日志，并同时输出控制台；
- 数据库写入失败时把事件降级写入 ``logs/db_fallback.log``（11.8），
  并每 ``retry_interval_s`` 秒尝试重建连接，不中断脚本运行。

线程模型：一个日志线程（``wows-log``）独占 ``Database`` 连接；调用方线程只碰队列。
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..common.clock import Clock, SystemClock, iso_timestamp
from ..common.errors import DatabaseError
from .database import EXIT_METHOD_VALUES, RESULT_VALUES, Database

# 文本日志文件名前缀与 logger 名称
LOG_FILE_PREFIX = "session_"
LOGGER_NAME = "wows_script"
_LOGGER_NAME = "wows_script.storage.events"
_DEFAULT_QUEUE_MAX = 2000
_DEFAULT_MAX_BATCH = 200
_DEFAULT_RETRY_INTERVAL_S = 30.0
_DEFAULT_POLL_TIMEOUT_S = 0.2
_DEFAULT_COMMAND_TIMEOUT_S = 10.0
_LOG_MIRROR_THROTTLED_KINDS = frozenset({"action"})

# 战斗标记事件名 -> Database 方法名
_MARK_METHODS: Mapping[str, str] = {
    "queued": "mark_queued",
    "death": "mark_death",
    "exit_requested": "mark_exit_requested",
    "exit_confirmed": "mark_exit_confirmed",
    "returned_to_port": "mark_returned_to_port",
}


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SinkStats:
    """EventSink 运行统计（诊断/测试用）。"""

    submitted: int = 0
    written: int = 0
    fallback: int = 0
    dropped: int = 0
    failed_batches: int = 0
    db_broken: bool = False


# ---------------------------------------------------------------------------
# 队列条目
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogEvent:
    """一条待写库事件（payload 只放基础类型，便于降级为 JSON 文本）。"""

    kind: str
    payload: Mapping[str, Any]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class _EventItem:
    event: LogEvent


@dataclass(frozen=True, slots=True)
class _CommandItem:
    description: str
    action: Callable[[Database], Any] | None
    future: Any  # concurrent.futures.Future


@dataclass(frozen=True, slots=True)
class _StopItem:
    drain: bool


# ---------------------------------------------------------------------------
# 文本日志
# ---------------------------------------------------------------------------


def configure_text_logging(
    log_dir: Path | str,
    *,
    level: str = "INFO",
    max_bytes: int = 20 * 1024 * 1024,
    backup_count: int = 5,
    session_started: datetime | None = None,
    console: bool = True,
) -> logging.Logger:
    """配置 ``logs/session_<开始时间>.log`` 文本日志并返回项目 logger。"""
    from logging.handlers import RotatingFileHandler

    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    moment = session_started or datetime.now().astimezone()
    log_path = directory / f"{LOG_FILE_PREFIX}{moment:%Y%m%d_%H%M%S}.log"
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_coerce_level(level))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
    )
    file_handler = RotatingFileHandler(
        log_path, maxBytes=int(max_bytes), backupCount=int(backup_count), encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if console:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
    return logger


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    value = getattr(logging, str(level).upper(), None)
    return value if isinstance(value, int) else logging.INFO


# ---------------------------------------------------------------------------
# EventSink
# ---------------------------------------------------------------------------


class EventSink:
    """线程安全的事件提交门面：唯一的数据库写入方是内部日志线程。

    用法::

        sink = EventSink(db_path, fallback_log=fallback_path, logger=logger)
        sink.start()
        session_id = sink.begin_session("cn360", config_hash)
        sink.log_state(session_id, None, "PORT", 0.9)
        sink.stop(drain=True)
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
        fallback_log: Path | str | None = None,
        queue_max: int = _DEFAULT_QUEUE_MAX,
        max_batch: int = _DEFAULT_MAX_BATCH,
        min_commit_interval_s: float = 1.0,
        retry_interval_s: float = _DEFAULT_RETRY_INTERVAL_S,
        poll_timeout_s: float = _DEFAULT_POLL_TIMEOUT_S,
        command_timeout_s: float = _DEFAULT_COMMAND_TIMEOUT_S,
        database_factory: Callable[[], Database] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._clock = clock or SystemClock()
        self._logger = logger or logging.getLogger(_LOGGER_NAME)
        self._fallback_path = (
            Path(fallback_log) if fallback_log is not None else self._db_path.parent / "db_fallback.log"
        )
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max(1, int(queue_max)))
        self._max_batch = max(1, int(max_batch))
        self._min_commit_interval_s = max(0.0, float(min_commit_interval_s))
        self._retry_interval_s = max(0.0, float(retry_interval_s))
        self._poll_timeout_s = max(0.01, float(poll_timeout_s))
        self._command_timeout_s = max(0.1, float(command_timeout_s))
        self._database_factory = database_factory or (lambda: Database(self._db_path, clock=self._clock))
        self._thread: threading.Thread | None = None
        self._started = False
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._pending: Any = None
        self._db: Database | None = None
        self._db_broken = False
        self._next_retry_at = 0.0
        self._last_drop_log_at = 0.0
        # 统计
        self._submitted = 0
        self._written = 0
        self._fallback = 0
        self._dropped = 0
        self._failed_batches = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def fallback_log_path(self) -> Path:
        return self._fallback_path

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive() and not self._stopping.is_set())

    @property
    def stats(self) -> SinkStats:
        with self._lock:
            return SinkStats(
                submitted=self._submitted,
                written=self._written,
                fallback=self._fallback,
                dropped=self._dropped,
                failed_batches=self._failed_batches,
                db_broken=self._db_broken,
            )

    def start(self) -> None:
        """启动日志线程（幂等）。"""
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run, name="wows-log", daemon=True)
        self._thread.start()

    def stop(self, drain: bool = True, timeout_s: float = _DEFAULT_COMMAND_TIMEOUT_S) -> None:
        """停止日志线程；drain=True 时先处理完队列中已有事件。"""
        thread = self._thread
        if thread is None:
            self._stopping.set()
            return
        if thread.is_alive():
            try:
                self._queue.put(_StopItem(drain=drain), timeout=timeout_s)
            except queue.Full:  # pragma: no cover - 队列满且线程卡死
                self._logger.error("停止日志线程时队列已满，强制中断")
                self._stopping.set()
            thread.join(timeout=timeout_s)
            if thread.is_alive():  # pragma: no cover - 防御性
                self._logger.error("日志线程未在 %.1f 秒内退出", timeout_s)
        self._stopping.set()

    def flush(self, timeout_s: float = _DEFAULT_COMMAND_TIMEOUT_S) -> None:
        """等待此前提交的事件全部处理（写库或降级写文本）。"""
        if not self.is_running:
            return
        self._submit_command("flush", None, timeout_s=timeout_s)

    def __enter__(self) -> EventSink:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.stop(drain=True)

    # ------------------------------------------------------------------
    # Database 代理接口（与 src.storage.database.Database 同名）
    # ------------------------------------------------------------------
    def begin_session(
        self,
        profile_id: str,
        config_hash: str,
        account_alias: str | None = None,
    ) -> int:
        return int(
            self._submit_command(
                "begin_session",
                lambda db: db.begin_session(profile_id, config_hash, account_alias),
            )
        )

    def end_session(self, session_id: int, stop_reason: str, total_battles: int) -> bool:
        return self._submit_event(
            "session_end",
            {
                "session_id": int(session_id),
                "stop_reason": stop_reason,
                "total_battles": int(total_battles),
            },
        )

    def begin_battle(
        self,
        session_id: int,
        map_id: str | None = None,
        account_alias: str | None = None,
    ) -> int:
        return int(
            self._submit_command(
                "begin_battle", lambda db: db.begin_battle(session_id, map_id, account_alias)
            )
        )

    def set_battle_context(
        self,
        battle_id: int,
        *,
        map_id: str | None = None,
        spawn_id: str | None = None,
        route_id: str | None = None,
    ) -> bool:
        return self._submit_event(
            "battle_context",
            {
                "battle_id": int(battle_id),
                "map_id": map_id,
                "spawn_id": spawn_id,
                "route_id": route_id,
            },
        )

    def mark_queued(self, battle_id: int) -> bool:
        return self._mark("queued", battle_id)

    def mark_death(self, battle_id: int) -> bool:
        return self._mark("death", battle_id)

    def mark_exit_requested(self, battle_id: int) -> bool:
        return self._mark("exit_requested", battle_id)

    def mark_exit_confirmed(self, battle_id: int) -> bool:
        return self._mark("exit_confirmed", battle_id)

    def mark_returned_to_port(self, battle_id: int) -> bool:
        return self._mark("returned_to_port", battle_id)

    def _mark(self, name: str, battle_id: int) -> bool:
        if name not in _MARK_METHODS:  # pragma: no cover - 内部调用
            raise ValueError(f"未知战斗标记: {name}")
        return self._submit_event("mark", {"battle_id": int(battle_id), "mark": name})

    def save_random_profile(self, battle_id: int, profile: Mapping[str, Any]) -> int:
        snapshot = dict(profile)
        return int(
            self._submit_command(
                "save_random_profile", lambda db: db.save_random_profile(battle_id, snapshot)
            )
        )

    def count_battles_on(self, day: Any, account_alias: str | None = None) -> int:
        """查询某本地日期已开始的战斗数（指定账号时按账号隔离统计）。"""
        return int(
            self._submit_command(
                "count_battles_on", lambda db: db.count_battles_on(day, account_alias)
            )
        )

    def end_battle(
        self,
        battle_id: int,
        result: str,
        exit_method: str,
        exit_error: str | None = None,
    ) -> bool:
        if result not in RESULT_VALUES:  # pragma: no cover - 早失败，避免污染队列
            raise ValueError(f"非法 result: {result!r}")
        if exit_method not in EXIT_METHOD_VALUES:  # pragma: no cover
            raise ValueError(f"非法 exit_method: {exit_method!r}")
        return self._submit_event(
            "battle_end",
            {
                "battle_id": int(battle_id),
                "result": result,
                "exit_method": exit_method,
                "exit_error": exit_error,
            },
        )

    def log_action(
        self,
        battle_id: int | None,
        action_type: str,
        detail: Mapping[str, Any] | None,
        random_profile_id: int | None = None,
    ) -> bool:
        return self._submit_event(
            "action",
            {
                "battle_id": battle_id,
                "action_type": action_type,
                "detail": dict(detail) if detail is not None else None,
                "random_profile_id": random_profile_id,
            },
        )

    def log_state(
        self,
        session_id: int | None,
        battle_id: int | None,
        state: str,
        confidence: float | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> bool:
        return self._submit_event(
            "state",
            {
                "session_id": session_id,
                "battle_id": battle_id,
                "state": state,
                "confidence": confidence,
                "detail": dict(detail) if detail is not None else None,
            },
        )

    def log_session_started(self, session_id: int, profile_id: str, config_hash: str) -> None:
        """会话开始后写一条文本日志（数据库记录已由 begin_session 完成）。"""
        self._logger.info(
            "会话开始 session=%s profile=%s config_hash=%s", session_id, profile_id, config_hash
        )

    # ------------------------------------------------------------------
    # 提交与队列
    # ------------------------------------------------------------------
    def _submit_event(self, kind: str, payload: Mapping[str, Any]) -> bool:
        if self._stopping.is_set():
            self._logger.warning("日志线程已停止，丢弃事件: %s", kind)
            return False
        event = LogEvent(kind=kind, payload=payload, occurred_at=self._clock.wall_now())
        try:
            self._queue.put_nowait(_EventItem(event))
        except queue.Full:
            with self._lock:
                self._dropped += 1
                self._submitted += 1
            self._log_drop_warning(kind)
            return False
        with self._lock:
            self._submitted += 1
        return True

    def _submit_command(
        self,
        description: str,
        action: Callable[[Database], Any] | None,
        *,
        timeout_s: float | None = None,
    ) -> Any:
        if not self.is_running:
            raise RuntimeError(f"EventSink 未启动或已停止，无法执行 {description}")
        import concurrent.futures

        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        item = _CommandItem(description=description, action=action, future=future)
        self._queue.put(item)
        timeout = self._command_timeout_s if timeout_s is None else timeout_s
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            raise DatabaseError(f"等待日志线程完成 {description} 超时（{timeout} 秒）") from exc

    def _log_drop_warning(self, kind: str) -> None:
        now = time.monotonic()
        if now - self._last_drop_log_at >= 5.0:
            self._last_drop_log_at = now
            with self._lock:
                dropped = self._dropped
            self._logger.warning("事件队列已满，已丢弃 %d 条事件（最近一条: %s）", dropped, kind)

    # ------------------------------------------------------------------
    # 日志线程
    # ------------------------------------------------------------------
    def _run(self) -> None:
        try:
            self._loop()
        except Exception:  # pragma: no cover - 防御性：日志线程不能静默死亡
            self._logger.exception("日志线程异常退出")
        finally:
            self._stopping.set()
            self._fail_pending(DatabaseError("日志线程已停止，事件未写入数据库"))
            self._close_database()

    def _close_database(self) -> None:
        """在日志线程内关闭数据库连接（SQLite 连接必须在创建它的线程关闭）。"""
        database, self._db = self._db, None
        if database is None:
            return
        try:
            database.close()
        except Exception:  # pragma: no cover - 关闭失败不影响退出
            self._logger.exception("关闭数据库连接失败")

    def _loop(self) -> None:
        last_commit = 0.0
        while True:
            item = self._pending if self._pending is not None else self._next_item()
            self._pending = None
            if item is None:
                continue
            if isinstance(item, _StopItem):
                self._handle_stop(item)
                return
            if isinstance(item, _CommandItem):
                self._execute_command(item)
                continue
            batch = [item.event]
            self._collect_batch(batch)
            wait = self._min_commit_interval_s - (time.monotonic() - last_commit)
            if wait > 0:
                self._collect_for(batch, wait)
            self._write_batch(batch)
            last_commit = time.monotonic()

    def _next_item(self) -> Any:
        try:
            return self._queue.get(timeout=self._poll_timeout_s)
        except queue.Empty:
            return None

    def _collect_batch(self, batch: list[LogEvent]) -> None:
        """把队列中已就绪的连续事件并入当前批，遇到命令/停止条目则暂存。"""
        while len(batch) < self._max_batch:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, _EventItem):
                batch.append(item.event)
            else:
                self._pending = item
                return

    def _collect_for(self, batch: list[LogEvent], wait_s: float) -> None:
        """在提交间隔内继续收集事件，保证“每秒最多一次批量提交”的节流。"""
        deadline = time.monotonic() + wait_s
        while len(batch) < self._max_batch:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                return
            if isinstance(item, _EventItem):
                batch.append(item.event)
            else:
                self._pending = item
                return

    def _handle_stop(self, item: _StopItem) -> None:
        if not item.drain:
            return
        while True:
            try:
                pending = self._queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(pending, _EventItem):
                self._write_batch([pending.event])
            elif isinstance(pending, _CommandItem):
                self._execute_command(pending)
            # 其他停止条目忽略

    def _fail_pending(self, error: DatabaseError) -> None:
        while True:
            try:
                pending = self._queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(pending, _CommandItem) and not pending.future.done():
                pending.future.set_exception(error)

    # ------------------------------------------------------------------
    # 数据库写入
    # ------------------------------------------------------------------
    def _ensure_database(self) -> Database | None:
        """返回可用数据库；连接失败或处于重试间隔内时返回 None（走降级文本）。"""
        if self._db is not None and not self._db_broken:
            return self._db
        now = self._clock.now()
        if now < self._next_retry_at:
            return None
        if self._db is not None:
            try:
                self._db.reconnect()
            except Exception as exc:
                self._db_broken = True
                self._next_retry_at = now + self._retry_interval_s
                self._logger.error("数据库重连失败（%.0f 秒后重试）: %s", self._retry_interval_s, exc)
                return None
            self._db_broken = False
            self._logger.info("数据库重连成功: %s", self._db_path)
            return self._db
        try:
            self._db = self._database_factory()
        except Exception as exc:
            self._db_broken = True
            self._next_retry_at = now + self._retry_interval_s
            self._logger.error("数据库打开失败（%.0f 秒后重试）: %s", self._retry_interval_s, exc)
            return None
        self._db_broken = False
        self._logger.info("数据库已打开: %s", self._db_path)
        return self._db

    def _write_batch(self, batch: list[LogEvent]) -> None:
        if not batch:
            return
        for event in batch:
            self._mirror_event(event)
        database = self._ensure_database()
        if database is None:
            self._write_fallback(batch, "数据库不可用")
            return
        try:
            with database.transaction():
                for event in batch:
                    self._apply_event(database, event)
        except Exception as exc:
            with self._lock:
                self._failed_batches += 1
            self._db_broken = True
            self._next_retry_at = self._clock.now() + self._retry_interval_s
            self._logger.error("批量写入数据库失败，%d 条事件降级到文本日志: %s", len(batch), exc)
            self._write_fallback(batch, str(exc))
            return
        with self._lock:
            self._written += len(batch)

    def _apply_event(self, database: Database, event: LogEvent) -> None:
        payload = event.payload
        kind = event.kind
        if kind == "action":
            database.log_action(
                payload.get("battle_id"),
                str(payload["action_type"]),
                payload.get("detail"),
                payload.get("random_profile_id"),
                occurred_at=event.occurred_at,
            )
        elif kind == "state":
            database.log_state(
                payload.get("session_id"),
                payload.get("battle_id"),
                str(payload["state"]),
                payload.get("confidence"),
                payload.get("detail"),
                occurred_at=event.occurred_at,
            )
        elif kind == "mark":
            method_name = _MARK_METHODS[str(payload["mark"])]
            method = getattr(database, method_name)
            method(payload["battle_id"], occurred_at=event.occurred_at)
        elif kind == "battle_end":
            database.end_battle(
                payload["battle_id"],
                str(payload["result"]),
                str(payload["exit_method"]),
                payload.get("exit_error"),
            )
        elif kind == "session_end":
            database.end_session(
                payload["session_id"], str(payload["stop_reason"]), payload["total_battles"]
            )
        elif kind == "battle_context":
            database.set_battle_context(
                payload["battle_id"],
                map_id=payload.get("map_id"),
                spawn_id=payload.get("spawn_id"),
                route_id=payload.get("route_id"),
            )
        else:  # pragma: no cover - 内部事件类型固定
            raise DatabaseError(f"未知事件类型: {kind}")

    def _execute_command(self, item: _CommandItem) -> None:
        if item.future.done():
            return
        if item.action is None:  # 屏障：仅等待此前事件处理完
            item.future.set_result(None)
            return
        database = self._ensure_database()
        if database is None:
            item.future.set_exception(DatabaseError(f"数据库不可用，无法执行 {item.description}"))
            return
        try:
            result = item.action(database)
        except Exception as exc:
            if isinstance(exc, DatabaseError):
                self._db_broken = True
                self._next_retry_at = self._clock.now() + self._retry_interval_s
                self._write_fallback(
                    [],
                    f"命令 {item.description} 失败: {exc}",
                )
            self._logger.error("日志命令 %s 失败: %s", item.description, exc)
            item.future.set_exception(exc)
            return
        item.future.set_result(result)
        if item.description != "flush":
            self._logger.info("数据库命令 %s 完成: %s", item.description, result)

    def _write_fallback(self, batch: list[LogEvent], reason: str) -> None:
        records: list[dict[str, Any]] = []
        for event in batch:
            records.append(
                {
                    "occurred_at": iso_timestamp(event.occurred_at),
                    "kind": event.kind,
                    "payload": event.payload,
                    "error": reason,
                }
            )
        if not records:
            records.append({"occurred_at": iso_timestamp(self._clock.wall_now()), "kind": "db_error", "error": reason})
        try:
            self._fallback_path.parent.mkdir(parents=True, exist_ok=True)
            with self._fallback_path.open("a", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            self._logger.exception("降级文本日志写入失败: %s", self._fallback_path)
            return
        with self._lock:
            self._fallback += len(batch)

    # ------------------------------------------------------------------
    # 文本日志镜像
    # ------------------------------------------------------------------
    def _mirror_event(self, event: LogEvent) -> None:
        payload = event.payload
        kind = event.kind
        try:
            if kind == "action":
                self._logger.info(
                    "动作 battle=%s type=%s detail=%s",
                    payload.get("battle_id"),
                    payload.get("action_type"),
                    _brief(payload.get("detail")),
                )
            elif kind == "state":
                self._logger.info(
                    "状态 session=%s battle=%s state=%s confidence=%s",
                    payload.get("session_id"),
                    payload.get("battle_id"),
                    payload.get("state"),
                    payload.get("confidence"),
                )
            elif kind == "mark":
                self._logger.info("战斗 %s 标记 %s", payload.get("battle_id"), payload.get("mark"))
            elif kind == "battle_end":
                self._logger.info(
                    "战斗 %s 结束 result=%s exit_method=%s",
                    payload.get("battle_id"),
                    payload.get("result"),
                    payload.get("exit_method"),
                )
            elif kind == "session_end":
                self._logger.info(
                    "会话 %s 结束 reason=%s total_battles=%s",
                    payload.get("session_id"),
                    payload.get("stop_reason"),
                    payload.get("total_battles"),
                )
            elif kind == "battle_context":
                self._logger.info(
                    "战斗 %s 上下文 map=%s spawn=%s route=%s",
                    payload.get("battle_id"),
                    payload.get("map_id"),
                    payload.get("spawn_id"),
                    payload.get("route_id"),
                )
        except Exception:  # pragma: no cover - 文本日志失败不影响主流程
            self._logger.exception("文本日志镜像失败: %s", event.kind)


def _brief(detail: Any, limit: int = 160) -> str:
    if detail is None:
        return "-"
    text = str(detail)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# 与任务/计划中的命名兼容：EventLog 是 EventSink 的别名
EventLog = EventSink
