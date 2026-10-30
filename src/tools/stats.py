"""运行统计工具（详细设计 11.3 / 14.5）。

查询 SQLite 运行日志（``src.storage.database.Database`` 的 v2 表结构），输出：

- 会话数、场次数、完成率（result=dead/finished 视为完成，aborted 未完成）；
- 死亡 → 点击退出、退出 → 回港、死亡 → 回港的耗时中位数（详细设计 14.5 指标）；
- 退出方式分布（menu / confirm / direct / recovery / none）与退出失败次数；
- 异常恢复次数（state_events 中 ERROR_RECOVERY 事件 + 退出方式为 recovery 的场次）；
- 开火与消耗品动作数量及开火比例（详细设计 14.5：battle_actions 中
  fire_burst 与 consumable 的数量比）；
- 导航放弃 / 卡住事件次数、按地图与按日期的场次分布。

统计是只读的：不写数据库、不触发日志线程。时间字段是带时区的 ISO-8601 文本，
差值用 ``common.clock.parse_iso`` 解析后计算，避免 SQLite 日期函数对时区的处理问题。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common.clock import parse_iso
from ..common.errors import DatabaseError
from ..storage.database import Database

#: 视为“完成”的 result 取值（详细设计 11.2 / 14.5）
COMPLETED_RESULTS: frozenset[str] = frozenset({"dead", "finished"})
#: 开火动作类型（详细设计 11.4）
FIRE_ACTION_TYPE = "fire_burst"
#: 消耗品动作类型
CONSUMABLE_ACTION_TYPE = "consumable"
#: 异常恢复事件的状态名
RECOVERY_STATE = "ERROR_RECOVERY"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StatsReport:
    """一次统计查询的结果。"""

    database_path: Path
    sessions: int = 0
    battles: int = 0
    completed: int = 0
    aborted: int = 0
    unresolved: int = 0
    result_counts: dict[str, int] = field(default_factory=dict)
    exit_method_counts: dict[str, int] = field(default_factory=dict)
    exit_errors: int = 0
    median_death_to_exit_s: float | None = None
    median_exit_to_port_s: float | None = None
    median_death_to_port_s: float | None = None
    median_battle_duration_s: float | None = None
    median_session_duration_s: float | None = None
    recovery_events: int = 0
    fire_actions: int = 0
    consumable_actions: int = 0
    other_actions: int = 0
    nav_abandon: int = 0
    nav_stuck: int = 0
    per_map: dict[str, int] = field(default_factory=dict)
    per_day: dict[str, int] = field(default_factory=dict)

    @property
    def completion_rate(self) -> float | None:
        if not self.battles:
            return None
        return self.completed / self.battles

    @property
    def fire_ratio(self) -> float | None:
        total = self.fire_actions + self.consumable_actions
        if total <= 0:
            return None
        return self.fire_actions / total

    def to_dict(self) -> dict[str, Any]:
        return {
            "database_path": str(self.database_path),
            "sessions": self.sessions,
            "battles": self.battles,
            "completed": self.completed,
            "aborted": self.aborted,
            "unresolved": self.unresolved,
            "completion_rate": None if self.completion_rate is None else round(self.completion_rate, 4),
            "result_counts": dict(self.result_counts),
            "exit_method_counts": dict(self.exit_method_counts),
            "exit_errors": self.exit_errors,
            "median_death_to_exit_s": _round_optional(self.median_death_to_exit_s),
            "median_exit_to_port_s": _round_optional(self.median_exit_to_port_s),
            "median_death_to_port_s": _round_optional(self.median_death_to_port_s),
            "median_battle_duration_s": _round_optional(self.median_battle_duration_s),
            "median_session_duration_s": _round_optional(self.median_session_duration_s),
            "recovery_events": self.recovery_events,
            "fire_actions": self.fire_actions,
            "consumable_actions": self.consumable_actions,
            "other_actions": self.other_actions,
            "fire_ratio": None if self.fire_ratio is None else round(self.fire_ratio, 4),
            "nav_abandon": self.nav_abandon,
            "nav_stuck": self.nav_stuck,
            "per_map": dict(self.per_map),
            "per_day": dict(self.per_day),
        }


def _round_optional(value: float | None) -> float | None:
    return None if value is None else round(float(value), 3)


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _seconds_between(start: str | None, end: str | None) -> float | None:
    """两个带时区 ISO-8601 文本的秒差；缺失/非法/负值时返回 None。"""
    if not start or not end:
        return None
    try:
        delta = (parse_iso(end) - parse_iso(start)).total_seconds()
    except (ValueError, TypeError):
        return None
    return delta if delta >= 0 else None


# ---------------------------------------------------------------------------
# 数据库打开（只读语义）
# ---------------------------------------------------------------------------


def open_database_for_stats(path: Path | str) -> Database:
    """打开统计用数据库：文件必须存在；空库会初始化表结构后返回（不写业务数据）。"""
    database_path = Path(path)
    if not database_path.is_file():
        raise DatabaseError(f"数据库不存在: {database_path}（先运行脚本产生运行日志）")
    try:
        database = Database(database_path)
    except DatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成 DatabaseError
        raise DatabaseError(f"打开数据库失败: {database_path}: {exc}") from exc
    tables = {
        str(row[0])
        for row in database.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "battles" not in tables or "sessions" not in tables:
        # 当前 Database 在 reconnect() 中初始化表结构；统计工具打开空库时补一次。
        try:
            database.reconnect()
        except DatabaseError as exc:
            database.close()
            raise DatabaseError(f"初始化数据库表结构失败: {exc}") from exc
    return database


# ---------------------------------------------------------------------------
# 统计查询
# ---------------------------------------------------------------------------


def _scalar(connection: sqlite3.Connection, sql: str, parameters: tuple[Any, ...] = ()) -> Any:
    row = connection.execute(sql, parameters).fetchone()
    return None if row is None else row[0]


def collect_stats(database: Database) -> StatsReport:
    """执行全部统计查询（只读）；不抛业务异常，单表缺失时对应指标为 0。"""
    connection = database.connection
    report = StatsReport(database_path=Path(database.path))
    report.sessions = int(_scalar(connection, "SELECT COUNT(*) FROM sessions") or 0)
    report.battles = int(_scalar(connection, "SELECT COUNT(*) FROM battles") or 0)

    for result_value, count in connection.execute(
        "SELECT COALESCE(result, 'unresolved'), COUNT(*) FROM battles GROUP BY COALESCE(result, 'unresolved')"
    ):
        report.result_counts[str(result_value)] = int(count)
    report.completed = sum(
        count for result_value, count in report.result_counts.items() if result_value in COMPLETED_RESULTS
    )
    report.aborted = int(report.result_counts.get("aborted", 0))
    report.unresolved = int(report.result_counts.get("unresolved", 0))

    for method, count in connection.execute(
        "SELECT COALESCE(exit_method, 'unknown'), COUNT(*) FROM battles GROUP BY COALESCE(exit_method, 'unknown')"
    ):
        report.exit_method_counts[str(method)] = int(count)
    report.exit_errors = int(
        _scalar(connection, "SELECT COUNT(*) FROM battles WHERE exit_error IS NOT NULL AND exit_error != ''") or 0
    )

    death_to_exit: list[float] = []
    exit_to_port: list[float] = []
    death_to_port: list[float] = []
    battle_durations: list[float] = []
    for row in connection.execute(
        "SELECT started_at, death_detected_at, exit_requested_at, returned_to_port_at FROM battles"
    ):
        started_at, death_at, exit_at, port_at = row
        battle_durations.append(_seconds_between(started_at, port_at) or 0.0)
        value = _seconds_between(death_at, exit_at)
        if value is not None:
            death_to_exit.append(value)
        value = _seconds_between(exit_at, port_at)
        if value is not None:
            exit_to_port.append(value)
        value = _seconds_between(death_at, port_at)
        if value is not None:
            death_to_port.append(value)
    report.median_death_to_exit_s = _median(death_to_exit)
    report.median_exit_to_port_s = _median(exit_to_port)
    report.median_death_to_port_s = _median(death_to_port)
    durations = [value for value in battle_durations if value > 0]
    report.median_battle_duration_s = _median(durations)

    session_durations: list[float] = []
    for started_at, ended_at in connection.execute("SELECT started_at, ended_at FROM sessions"):
        value = _seconds_between(started_at, ended_at)
        if value is not None:
            session_durations.append(value)
    report.median_session_duration_s = _median(session_durations)

    report.recovery_events = int(
        _scalar(connection, "SELECT COUNT(*) FROM state_events WHERE state = ?", (RECOVERY_STATE,)) or 0
    )

    action_counts: dict[str, int] = {}
    for action_type, count in connection.execute("SELECT action_type, COUNT(*) FROM battle_actions GROUP BY action_type"):
        action_counts[str(action_type)] = int(count)
    report.fire_actions = int(action_counts.get(FIRE_ACTION_TYPE, 0))
    report.consumable_actions = int(action_counts.get(CONSUMABLE_ACTION_TYPE, 0))
    report.other_actions = sum(action_counts.values()) - report.fire_actions - report.consumable_actions
    report.nav_abandon = int(action_counts.get("nav_abandon", 0))
    report.nav_stuck = int(action_counts.get("nav_stuck", 0))

    for map_id, count in connection.execute(
        "SELECT COALESCE(map_id, 'unknown'), COUNT(*) FROM battles GROUP BY COALESCE(map_id, 'unknown')"
    ):
        report.per_map[str(map_id)] = int(count)
    for local_date, count in connection.execute(
        "SELECT COALESCE(local_date, 'unknown'), COUNT(*) FROM battles GROUP BY COALESCE(local_date, 'unknown')"
    ):
        report.per_day[str(local_date)] = int(count)
    return report


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------


def _seconds_text(value: float | None) -> str:
    if value is None:
        return "无数据"
    return f"{value:.1f} s"


def _percent_text(value: float | None) -> str:
    return "无数据" if value is None else f"{value:.1%}"


def format_report(report: StatsReport) -> list[str]:
    """控制台报告（中文字段、对齐；JSON 输出见 ``StatsReport.to_dict``）。"""
    lines = [
        "wows_script 运行统计",
        f"数据库: {report.database_path}",
        "",
        f"会话数: {report.sessions}",
        f"场次数: {report.battles}（完成 {report.completed}，放弃 {report.aborted}，未结束 {report.unresolved}）",
        f"完成率: {_percent_text(report.completion_rate)}（目标 >= 90%，详细设计 14.5）",
        f"结果分布: {_format_counter(report.result_counts)}",
        f"退出方式: {_format_counter(report.exit_method_counts)}",
        f"退出失败(exit_error 非空): {report.exit_errors}",
        "",
        "周转耗时（中位数）:",
        f"  死亡 -> 点击退出: {_seconds_text(report.median_death_to_exit_s)}（目标 <= 3 s）",
        f"  点击退出 -> 回港: {_seconds_text(report.median_exit_to_port_s)}",
        f"  死亡 -> 回到港口: {_seconds_text(report.median_death_to_port_s)}（目标 <= 45 s）",
        f"  单场时长: {_seconds_text(report.median_battle_duration_s)}",
        f"  单次会话时长: {_seconds_text(report.median_session_duration_s)}",
        "",
        f"异常恢复事件: {report.recovery_events}（state_events 中 {RECOVERY_STATE}）",
        f"导航放弃: {report.nav_abandon}，导航卡住: {report.nav_stuck}",
        "",
        f"攻击动作: 开火 {report.fire_actions} 次，消耗品 {report.consumable_actions} 次，"
        f"开火比例 {_percent_text(report.fire_ratio)}（目标 75%~95%）",
        f"其他动作: {report.other_actions} 条",
        "",
        f"按地图场次: {_format_counter(report.per_map)}",
        f"按日期场次: {_format_counter(report.per_day)}",
    ]
    return lines


def _format_counter(counts: dict[str, int]) -> str:
    if not counts:
        return "（无）"
    return "，".join(f"{name}={count}" for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.tools.stats",
        description="查询 SQLite 运行日志，输出会话/场次/周转耗时/攻击比例等统计",
    )
    parser.add_argument("--root", default=".", help="项目根目录（包含 config.yaml），默认当前目录")
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite 数据库路径（默认 <root>/data/wows_script.db）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（便于工具链集成）")
    parser.add_argument("--output", default=None, help="把报告写入文件（默认只打印到控制台）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """统计入口：打开数据库 -> 查询 -> 打印/保存报告。"""
    args = _build_parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"[ERROR] 项目根目录不存在: {root}", file=sys.stderr)
        return 2
    database_path = Path(args.db).expanduser() if args.db else root / "data" / "wows_script.db"
    if not database_path.is_absolute():
        database_path = root / database_path
    try:
        database = open_database_for_stats(database_path)
    except DatabaseError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    try:
        report = collect_stats(database)
    except sqlite3.Error as exc:
        print(f"[ERROR] 查询统计失败: {exc}", file=sys.stderr)
        return 2
    finally:
        database.close()
    text = (
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
        if args.json
        else "\n".join(format_report(report))
    )
    print(text)
    if args.output:
        output_path = Path(args.output).expanduser()
        if not output_path.is_absolute():
            output_path = root / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
        print(f"\n报告已写入: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
